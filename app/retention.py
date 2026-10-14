"""Run·dere — the daily retention job (YONDER-179 part 2, SPEC-8 D-196).

The owner's rule (2026-10-13): stored files and captured emails are kept for
the life of their trip and removed 12 months after it ends — "it needs to be
archived and deleted after a while". `TX_RETENTION_MONTHS` sets the 12.

1. **Stored files** (app/documents.py): bytes deleted on the removal day; the
   row stays, marked `purged_at`. What was extracted from the file stays.
2. **Raw captures** (amends D-15): an email's raw source (attachments
   included) and a web capture's HTML are deleted from the batch payload. A
   tombstone stays: the sha256 content address, when it arrived, its owner,
   subject, sender and Message-ID. Captures still waiting in review are never
   purged — only ones that were logged or ignored.
3. **The mailbox** (when the email inbox is configured): Rundere touches only
   its own two folders, and moves mail to Trash, never expunging it.
   - `Rundere/Captured`: moved to Trash on the day Rundere drops its own copy,
     found again by Message-ID.
   - `Rundere/Dropped`: moved to Trash after `TX_INBOX_DROPPED_DAYS` (30).

Every step is idempotent and logs counts only — never a subject, sender or
address. Run daily by a background thread (start()), or by hand:
`python -m app.cli retention [--dry-run]`.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import settings

log = logging.getLogger("rundere.retention")

# What a raw payload keeps once purged: enough to say what it was, nothing more.
_TOMBSTONE_KEYS = ("capture_channel", "message_id", "subject", "internal_date",
                   "source_url", "document_id", "filename", "content_type",
                   "capture_context")
_RAW_KEYS = ("raw_message_base64", "html")


@dataclass
class Report:
    files_purged: int = 0
    captures_purged: int = 0
    captures_held: int = 0               # due, but still waiting in review
    mail_trashed: int = 0
    dropped_trashed: int = 0
    mail_skipped: str | None = None      # why the mailbox step didn't run
    due_mail: list[str] = field(default_factory=list, repr=False)

    def summary(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "due_mail"}


def _today() -> date:
    return datetime.now(timezone.utc).date()


# --------------------------------------------------------------------------- 1. files
def purge_files(session: Session, today: date, report: Report, *, dry_run: bool) -> None:
    from app import documents
    from app.models import Document
    for doc in session.query(Document).filter(Document.purged_at.is_(None)).all():
        if documents.removal_date(session, doc) <= today:
            report.files_purged += 1
            if not dry_run:
                documents.remove_bytes(doc)
                doc.purged_at = datetime.now(timezone.utc)
    if not dry_run:
        session.commit()


# --------------------------------------------------------------------------- 2. raw captures
def _settled(item) -> bool:
    """Logged, or ignored: nothing left for anyone to decide."""
    from app.review.routes import _is_logged
    return _is_logged(item) or (item.review or {}).get("action_taken") == "ignored"


def _batch_keep_until(session: Session, batch, items) -> date:
    """The latest removal day across the trips its items are filed to; no trip
    → 12 months after it arrived."""
    from app import documents
    from app.models import Trip
    days = [documents.keep_until(session.get(Trip, i.trip_id), batch.received_at)
            for i in items if i.trip_id]
    return max(days) if days else documents.keep_until(None, batch.received_at)


def tombstone(payload: dict, *, mailbox_pending: bool = False) -> dict:
    """What a purged payload keeps: what it was, never its content. The
    subject, sender and Message-ID are read from the raw email once, here."""
    out = {k: payload[k] for k in _TOMBSTONE_KEYS if k in payload}
    raw = payload.get("raw_message_base64")
    if raw:
        import base64
        from email import message_from_bytes, policy
        try:
            msg = message_from_bytes(base64.b64decode(raw), policy=policy.default)
            out["sender"] = str(msg.get("From") or "")[:200] or None
            out.setdefault("subject", str(msg.get("Subject") or "")[:300] or None)
            if not out.get("message_id"):
                out["message_id"] = str(msg.get("Message-ID") or "").strip() or None
        except Exception:
            pass
    out["purged_at"] = datetime.now(timezone.utc).isoformat()
    if mailbox_pending and out.get("message_id"):
        out["mailbox_pending"] = True       # its Captured copy still goes to Trash
    return out


def purged(payload: dict | None) -> bool:
    """A capture whose raw source the retention job removed (D-196)."""
    return bool((payload or {}).get("purged_at"))


def source_gone(session: Session, payload: dict | None) -> bool:
    """Its original is gone: a purged raw capture, or a file capture whose
    stored file was removed — so it can't be extracted again."""
    if purged(payload):
        return True
    p = payload or {}
    if p.get("capture_channel") == "file":
        from app.models import Document
        doc = session.get(Document, p.get("document_id") or "")
        return doc is None or doc.purged_at is not None
    return False


def purge_captures(session: Session, today: date, report: Report, *, dry_run: bool) -> None:
    from app.models import IngestBatch, TravelItem
    for batch in session.query(IngestBatch).filter(IngestBatch.status != "received").all():
        payload = batch.payload or {}
        if payload.get("purged_at") or not any(payload.get(k) for k in _RAW_KEYS):
            continue
        items = session.query(TravelItem).filter(TravelItem.batch_id == batch.id).all()
        if _batch_keep_until(session, batch, items) > today:
            continue
        if not items or not all(_settled(i) for i in items):
            report.captures_held += 1          # never purge what's still in review
            continue
        report.captures_purged += 1
        from_inbox = payload.get("capture_channel") == "email"
        if not dry_run:
            batch.payload = tombstone(payload, mailbox_pending=from_inbox)  # a new dict saves
        elif from_inbox and payload.get("message_id"):
            report.due_mail.append(payload["message_id"])
    if not dry_run:
        session.commit()
        # Every purged inbox email whose Captured copy hasn't gone yet — today's
        # and any a failed mailbox step left behind.
        for batch in session.query(IngestBatch).filter(IngestBatch.status != "received").all():
            p = batch.payload or {}
            if p.get("mailbox_pending") and p.get("message_id"):
                report.due_mail.append(p["message_id"])


def mail_done(session: Session, message_ids: list[str]) -> None:
    """The mailbox step handled these (moved, or no longer there): stop retrying."""
    from app.models import IngestBatch
    done = set(message_ids)
    for batch in session.query(IngestBatch).filter(IngestBatch.status != "received").all():
        p = batch.payload or {}
        if p.get("mailbox_pending") and p.get("message_id") in done:
            batch.payload = {k: v for k, v in p.items() if k != "mailbox_pending"}
    session.commit()


# --------------------------------------------------------------------------- 3. mailbox
def _trash_folder(conn) -> str | None:
    """TX_INBOX_TRASH_FOLDER, else the mailbox's own \\Trash (RFC 6154)."""
    if (settings.inbox_trash_folder or "").strip():
        return settings.inbox_trash_folder.strip()
    try:
        typ, data = conn.list()
    except Exception:
        return None
    import re
    for row in data or []:
        line = row.decode(errors="replace") if isinstance(row, bytes) else str(row)
        if "\\Trash" in line:
            m = re.search(r'"([^"]*)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            if m:
                return m.group(1)
    return None


def _uids(conn, *criteria) -> list[bytes]:
    typ, data = conn.uid("SEARCH", None, *criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def trash_mail(report: Report, today: date, *, dry_run: bool, connect=None) -> list[str]:
    """Move due mail from Rundere's own two folders to Trash. Nothing else in
    the mailbox is ever touched. Returns the Message-IDs it dealt with."""
    from app import inbox
    handled: list[str] = []
    if not inbox.configured():
        report.mail_skipped = "the email inbox isn't configured"
        return handled
    conn = (connect or inbox._connect)()
    try:
        trash = _trash_folder(conn)
        if not trash:
            report.mail_skipped = "no Trash folder found (set TX_INBOX_TRASH_FOLDER)"
            log.warning("retention: %s", report.mail_skipped)
            return handled
        # Captured mail whose capture Rundere just dropped.
        typ, _ = conn.select(inbox._quote(settings.inbox_done_folder))
        if typ == "OK":
            for mid in dict.fromkeys(report.due_mail):
                moved = True
                for uid in _uids(conn, "HEADER", "Message-ID", _imap_quote(mid)):
                    report.mail_trashed += 1
                    if not dry_run:
                        moved = inbox._move(conn, uid, trash) and moved
                if moved:
                    handled.append(mid)             # moved, or already gone from the folder
        # Dropped mail older than the grace period (its arrival date).
        cutoff = (today - timedelta(days=settings.inbox_dropped_days)).strftime("%d-%b-%Y")
        typ, _ = conn.select(inbox._quote(settings.inbox_dropped_folder))
        if typ == "OK":
            for uid in _uids(conn, "BEFORE", cutoff):
                report.dropped_trashed += 1
                if not dry_run:
                    inbox._move(conn, uid, trash)
    finally:
        inbox._logout(conn)
    return handled


def _imap_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------------- the run
def run(session_factory, *, today: date | None = None, dry_run: bool = False,
        mailbox: bool = True, connect=None) -> Report:
    today = today or _today()
    report = Report()
    with session_factory() as session:
        purge_files(session, today, report, dry_run=dry_run)
        purge_captures(session, today, report, dry_run=dry_run)
    if mailbox:
        try:
            handled = trash_mail(report, today, dry_run=dry_run, connect=connect)
            if handled and not dry_run:
                with session_factory() as session:
                    mail_done(session, handled)
        except Exception as ex:                # the mailbox is best-effort; files are done
            report.mail_skipped = f"mailbox step failed ({type(ex).__name__})"
            log.warning("retention: %s", report.mail_skipped)
    log.info("retention%s: %s", " (dry run)" if dry_run else "", report.summary())
    return report


_thread: threading.Thread | None = None
_stop = threading.Event()
_FIRST_DELAY = 10 * 60                   # let startup settle before the first sweep
_EVERY = 24 * 60 * 60


def _loop(session_factory) -> None:
    if _stop.wait(_FIRST_DELAY):
        return
    while True:
        try:
            run(session_factory)
        except Exception:                      # pragma: no cover - never kill the thread
            log.exception("retention run failed; trying again tomorrow")
        if _stop.wait(_EVERY):
            return


def start(session_factory) -> bool:
    """Start the daily sweep (once per process)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(session_factory,),
                               name="retention", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()
