"""Run·dere — building a person's archive in the background (SPEC-13 §2.5, D-241).

A build is a DURABLE `ArchiveExport` row, like a Discovery run (D-63,
app/trips/discovery_jobs.py), not an entry in the in-memory job list:

  * `launch()` checks the limits, creates the row (status=building) and submits
    the worker. One build per person at a time; one build on the server at a
    time (a single worker, so a second person's build waits its turn); at most
    three a day per person; and never when the folder lacks the room.
  * the worker writes the zip to `<TX_EXPORT_DIR>/<user id>/<export id>.zip`,
    readable by the server's user only, then marks the row `ready` with its
    size, sha256 and expiry (7 days). A new archive replaces the person's
    previous one.
  * a build orphaned by a restart still says `building` but isn't in this
    process's _LIVE set: the next look (`latest()`) marks it failed.
    This assumes ONE server process, as discovery_jobs.py does and as the
    image runs. A second process would take a build running in the first for
    orphaned, mark it failed and remove its `.part` file. The worker then
    either fails before the rename or finishes a complete file, so no half-written
    archive results, but several processes aren't supported (SPEC-13 §2.5).
  * the daily retention job (app/retention.py) deletes the file after 7 days
    and marks the row `expired`.

Nothing is logged about a build but ids and counts, and a failure stores the
exception's class name only: an error message could quote a person's data.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import AccountEvent, ArchiveExport, User

log = logging.getLogger("rundere.archive")

KEEP_DAYS = 7                       # fixed by D-241; there is no setting
MAX_PER_DAY = 3                     # builds per person in any 24 hours
SPACE_MARGIN = 100 * 1024 * 1024    # free space wanted on top of the person's files

BUILDING, READY, FAILED, REPLACED, EXPIRED = (
    "building", "ready", "failed", "replaced", "expired")

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tx-archive")
_LOCK = threading.Lock()
_LIVE: set[str] = set()             # export ids building in THIS process


class Refused(Exception):
    """A build can't start right now. The message is for the person."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive UTC."""
    return dt if dt is None or dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- the folder
def root() -> Path:
    """TX_EXPORT_DIR, else `<TX_ARTIFACTS_DIR>/exports`. Never under a web
    root: the only way out is the download route."""
    d = (settings.export_dir or "").strip()
    return Path(d) if d else Path(settings.artifacts_dir) / "exports"


def folder_for(user_id: str) -> Path:
    return root() / user_id


def path_for(export: ArchiveExport) -> Path:
    """Both parts are server-made uuids; nothing a person typed is in the path."""
    return folder_for(export.user_id) / f"{export.id}.zip"


def _private_folder(user_id: str) -> Path:
    """The person's export folder, for the server's user only (0700)."""
    base = root()
    base.mkdir(parents=True, exist_ok=True)
    folder = base / user_id
    folder.mkdir(mode=0o700, exist_ok=True)
    for p in (base, folder):
        try:
            os.chmod(p, 0o700)
        except OSError:                          # a mount that ignores modes
            pass
    return folder


def _free_bytes() -> int:
    base = root()
    probe = base if base.exists() else next(p for p in base.parents if p.exists())
    return shutil.disk_usage(probe).free


def space_problem(session: Session, user: User) -> str | None:
    """Why there's no room to build, or None. The archive holds the person's
    stored files, so that plus a margin must be free. Nothing is ever deleted
    to make room (SPEC-13 §2.5)."""
    from app import documents
    need = documents.used_bytes(session, user) + SPACE_MARGIN
    try:
        free = _free_bytes()
    except OSError:
        return "the export folder isn't available right now"
    if free < need:
        return "this server doesn't have enough free space to build your archive right now"
    return None


# --------------------------------------------------------------------------- state
def _expire_if_orphaned(session: Session, row: ArchiveExport | None) -> None:
    if row is None or row.status != BUILDING:
        return
    with _LOCK:
        live = row.id in _LIVE
    if not live:
        row.status, row.error, row.finished_at = FAILED, "Interrupted", _now()
        session.commit()
        _remove(path_for(row).with_suffix(".part"))


def latest(session: Session, user_id: str) -> ArchiveExport | None:
    """The person's newest build, with an orphaned one marked failed first."""
    for row in (session.query(ArchiveExport)
                .filter(ArchiveExport.user_id == user_id, ArchiveExport.status == BUILDING)
                .all()):
        _expire_if_orphaned(session, row)
    return (session.query(ArchiveExport).filter(ArchiveExport.user_id == user_id)
            .order_by(ArchiveExport.requested_at.desc(), ArchiveExport.id).first())


def own(session: Session, user: User, export_id: str) -> ArchiveExport | None:
    """The caller's own export, else None: someone else's id answers exactly
    like a missing one (D-241). There is no admin view of another person's."""
    row = session.get(ArchiveExport, export_id)
    return row if row is not None and row.user_id == user.id else None


def downloadable(row: ArchiveExport | None) -> bool:
    if row is None or row.status != READY:
        return False
    expires = _aware(row.expires_at)
    return (expires is None or expires > _now()) and path_for(row).is_file()


def builds_today(session: Session, user_id: str) -> int:
    since = _now() - timedelta(hours=24)
    return (session.query(ArchiveExport)
            .filter(ArchiveExport.user_id == user_id, ArchiveExport.requested_at >= since)
            .count())


def start_problem(session: Session, user: User) -> str | None:
    """Why a build can't start now, or None."""
    row = latest(session, user.id)
    if row is not None and row.status == BUILDING:
        return "your archive is already being built"
    if builds_today(session, user.id) >= MAX_PER_DAY:
        return (f"you can build your archive {MAX_PER_DAY} times a day — "
                "try again tomorrow")
    return space_problem(session, user)


# --------------------------------------------------------------------------- launch + worker
def launch(session: Session, user: User, *, base_url: str = "") -> ArchiveExport:
    """Start a build for `user`, or raise Refused. `base_url` is this server's
    address, for the "ready" email's link to the Account page."""
    if (why := start_problem(session, user)) is not None:
        raise Refused(why)
    row = ArchiveExport(user_id=user.id, status=BUILDING, requested_at=_now())
    session.add(row)
    # The account log records each build (SPEC-13 §2.5).
    session.add(AccountEvent(user_id=user.id, actor_id=user.id, action="export_built"))
    session.commit()
    export_id = row.id
    with _LOCK:
        _LIVE.add(export_id)
    _EXECUTOR.submit(_execute, export_id, base_url)
    return row


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("archive: a file in the export folder couldn't be removed")


def _execute(export_id: str, base_url: str = "") -> None:
    """The worker body, in its own session (it runs off-request). Directly
    callable in tests."""
    from app import archive
    tmp: Path | None = None
    try:
        with SessionLocal() as s:
            row = s.get(ArchiveExport, export_id)
            user = s.get(User, row.user_id) if row is not None else None
            if row is None or user is None or row.status != BUILDING:
                return
            folder = _private_folder(user.id)
            final = folder / f"{row.id}.zip"
            tmp = folder / f"{row.id}.part"
            # Created 0600 before a byte is written, whatever the umask.
            os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
            result = archive.build(s, user, tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, final)               # atomic: never a half-written archive
            tmp = None
            now = _now()
            row.status, row.finished_at = READY, now
            row.expires_at = now + timedelta(days=KEEP_DAYS)
            row.size_bytes, row.sha256 = result["bytes"], result["sha256"]
            row.format_version = archive.FORMAT_VERSION
            # One archive per person: the new one replaces the previous.
            for old in (s.query(ArchiveExport)
                        .filter(ArchiveExport.user_id == user.id, ArchiveExport.id != row.id,
                                ArchiveExport.status == READY).all()):
                old.status = REPLACED
                _remove(path_for(old))
            s.commit()
            log.info("archive: build %s finished (%d files, %d bytes)",
                     export_id, result["files"], result["bytes"])
            to = user.email
        _notify_ready(to, base_url)
    except Exception as ex:
        # The class name only: a message could quote the person's data.
        log.error("archive: build %s failed (%s)", export_id, type(ex).__name__)
        if tmp is not None:
            _remove(tmp)
        try:
            with SessionLocal() as s2:
                row = s2.get(ArchiveExport, export_id)
                if row is not None and row.status == BUILDING:
                    row.status, row.error = FAILED, type(ex).__name__[:64]
                    row.finished_at = _now()
                    s2.commit()
        except Exception:                        # pragma: no cover - defensive
            pass
    finally:
        with _LOCK:
            _LIVE.discard(export_id)


def _notify_ready(to: str | None, base_url: str) -> None:
    """Tell the person it's ready, when they have an address and SMTP is set.
    The link is to the Account page, never to the file: a forwarded or leaked
    email must not be a way to the archive (SPEC-13 §4.1)."""
    from app.email import email_configured, send_email
    base = (settings.public_base_url or base_url or "").rstrip("/")
    if not to or not email_configured() or not base:
        return
    send_email(to, "Your Run·dere archive is ready",
               "The archive of your Run·dere data is ready to download.\n\n"
               f"Sign in and open {base}/account/export to get it. It is kept for "
               f"{KEEP_DAYS} days and then deleted.\n\n"
               "If you didn't ask for this, change your password: someone signed in "
               "as you.")


# --------------------------------------------------------------------------- retention
def expire_due(session: Session, *, now: datetime | None = None, dry_run: bool = False) -> int:
    """Delete archives whose 7 days are up and mark their rows expired. Returns
    how many. Called by the daily job (app/retention.py); safe to run again."""
    now = now or _now()
    count = 0
    for row in session.query(ArchiveExport).filter(ArchiveExport.status == READY).all():
        expires = _aware(row.expires_at)
        if expires is None or expires > now:
            continue
        count += 1
        if not dry_run:
            _remove(path_for(row))
            row.status = EXPIRED
    if not dry_run:
        session.commit()
    return count
