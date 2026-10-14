"""Run·dere — the documents store: captured files, per user (YONDER-179,
SPEC-8 D-153/D-196).

- **What:** PDF, .ics and .eml only, recognised from the content, never the
  file name. No photos, audio or receipt images (D-153).
- **Where:** `<TX_DOCUMENTS_DIR>/<owner_id>/<sha256>`, deduplicated within one
  person's files only. The folder is the hoster's choice. A marker file
  (`.rundere-documents`) is written into it the first time; if files are on
  record but the marker is gone, the folder isn't the one the files were
  written to (a drive that didn't mount, so Docker made an empty folder on the
  wrong disk), and file capture stops rather than scatter files across disks.
- **Limits:** `TX_DOCUMENT_MAX_MB` per file (25) and a per-user quota
  (`TX_DOCUMENT_QUOTA_MB`, 1 GB; `User.document_quota_mb` overrides it). At
  the limit new files are refused — nothing is deleted to make room.
- **Safety (YONDER-243):** ClamAV must be configured, or file capture stays
  off; a scanner that can't answer refuses the upload (never stored
  unscanned). PDFs get the static checks; an .eml's PDF attachments are
  checked again when it's read.
- **Who sees a file:** its owner only, as a download (D-196).

Retention (the 12-month purge) is the daily job in app/retention.py.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from datetime import datetime, timezone
from email import message_from_bytes, policy
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Document

log = logging.getLogger("rundere.documents")

MARKER = ".rundere-documents"
PDF, ICS, EML = "application/pdf", "text/calendar", "message/rfc822"
EXTENSION = {PDF: ".pdf", ICS: ".ics", EML: ".eml"}


class Unavailable(Exception):
    """File capture can't take files right now (off, misconfigured, scanner down)."""


class Refused(ValueError):
    """This file can't be stored: wrong type, too big, over quota, or unsafe.
    `status` is the HTTP answer: 415 type, 413 size/quota, 422 unsafe, 400 else."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- the folder
def _root() -> Path | None:
    d = (settings.documents_dir or "").strip()
    return Path(d) if d else None


def status(session: Session) -> tuple[bool, str | None]:
    """(enabled, why not). Creates the folder marker on first use."""
    root = _root()
    if root is None:
        return False, "file capture isn't set up on this server (TX_DOCUMENTS_DIR)"
    if not (settings.clamav_url or "").strip():
        return False, "file capture needs a virus scanner (TX_CLAMAV_URL)"
    if not root.is_dir() or not os.access(root, os.W_OK):
        log.error("documents: %s is missing or not writable", root)
        return False, "the file store isn't available right now"
    marker = root / MARKER
    if not marker.exists():
        if session.query(Document.id).filter(Document.purged_at.is_(None)).first():
            log.error("documents: %s has no %s but files are on record — is the right "
                      "drive mounted? File capture is off until it is.", root, MARKER)
            return False, "the file store isn't available right now"
        marker.write_text("Run·dere documents store (SPEC-8 D-196). Don't delete this file.\n")
    return True, None


def _require(session: Session) -> Path:
    ok, why = status(session)
    if not ok:
        raise Unavailable(why)
    return _root()


def path_for(doc: Document) -> Path | None:
    root = _root()
    return None if root is None else root / doc.owner_id / doc.sha256


# --------------------------------------------------------------------------- what it is
def detect_type(data: bytes) -> str:
    """The file's type from its content, or Refused."""
    head = data[:2048].lstrip()
    if head.startswith(b"%PDF-"):
        return PDF
    text = head.decode("utf-8", errors="replace").lstrip("﻿")
    if text.upper().startswith("BEGIN:VCALENDAR"):
        return ICS
    if b"\x00" not in data[:65536]:
        try:
            msg = message_from_bytes(data, policy=policy.default)
        except Exception:
            msg = None
        if msg is not None and msg.get("From") and (msg.get("Date") or msg.get("Subject")
                                                    or msg.get("Message-ID")):
            return EML
    raise Refused("only PDF, calendar (.ics) and email (.eml) files can be captured", 415)


def _scan(data: bytes, ctype: str, filename: str) -> str | None:
    """ClamAV, then the static checks. Returns notes; Refused or Unavailable."""
    from extract import filescan
    try:
        hit = filescan.clamav_scan(settings.clamav_url, filename or "upload", data)
    except filescan.ScanUnavailable:
        raise Unavailable("the virus scanner can't be reached, so nothing was stored — "
                          "try again shortly") from None
    if hit:
        log.warning("documents: refused an upload — antivirus found %s", hit)
        raise Refused(f"the virus scanner found {hit} in this file — it wasn't stored", 422)
    if ctype == PDF:
        v = filescan.check_pdf(data)
        if not v.ok:
            raise Refused(f"this PDF can't be captured: {v.reason}", 422)
        return "; ".join(v.notes) or None
    if ctype == ICS:
        text = data.decode("utf-8", errors="replace")
        if "END:VCALENDAR" not in text.upper():
            raise Refused("this calendar file is incomplete", 422)
    return None


# --------------------------------------------------------------------------- retention (D-196)
def add_months(d, months: int):
    """`d` plus whole months, clamped to the month's last day (Feb 29 → 28)."""
    import calendar
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def keep_until(trip, created_at) -> "date":
    """The day a stored file or raw capture is removed (D-196): 12 months
    (TX_RETENTION_MONTHS) after its trip ends — the end date, else the start —
    or after it arrived when it's filed to no trip (or the trip has no dates)."""
    from datetime import date
    base = None
    if trip is not None:
        d = (trip.end_date or trip.start_date or "")[:10]
        try:
            base = date.fromisoformat(d) if d else None
        except ValueError:
            base = None
    if base is None:
        base = (created_at or datetime.now(timezone.utc)).date()
    return add_months(base, settings.retention_months)


def removal_date(session: Session, doc: Document):
    from app.models import Trip
    trip = session.get(Trip, doc.trip_id) if doc.trip_id else None
    return keep_until(trip, doc.created_at)


# --------------------------------------------------------------------------- limits
def quota_bytes(user) -> int:
    mb = getattr(user, "document_quota_mb", None) or settings.document_quota_mb
    return int(mb) * 1024 * 1024


def used_bytes(session: Session, user) -> int:
    return int(session.query(func.coalesce(func.sum(Document.size_bytes), 0))
               .filter(Document.owner_id == user.id, Document.purged_at.is_(None))
               .scalar() or 0)


def _mb(n: int) -> str:
    """A size for people: 0 MB, 596 KB, 3.2 MB, 1 GB, 1.5 GB."""
    mb = n / (1024 * 1024)
    if mb >= 1024:
        gb = mb / 1024
        return f"{gb:.0f} GB" if gb == int(gb) else f"{gb:.1f} GB"
    if mb < 1:
        return "0 MB" if n == 0 else f"{max(n / 1024, 1):.0f} KB"
    return f"{mb:.1f} MB"


# --------------------------------------------------------------------------- store
def save(session: Session, user, data: bytes, filename: str = "", *, source: str = "api",
         trip_id: str | None = None) -> tuple[Document, bool]:
    """Store a file for `user`. Returns (document, created) — created is False
    when they already have this exact file (same sha256), which is returned
    as is. Refused / Unavailable otherwise. Caller commits."""
    root = _require(session)
    if not data:
        raise Refused("that file is empty")
    max_bytes = settings.document_max_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise Refused(f"files can be at most {settings.document_max_mb} MB", 413)
    ctype = detect_type(data)
    sha = hashlib.sha256(data).hexdigest()
    existing = (session.query(Document)
                .filter(Document.owner_id == user.id, Document.sha256 == sha).one_or_none())
    if existing is not None and existing.purged_at is None:
        return existing, False
    used, quota = used_bytes(session, user), quota_bytes(user)
    if used + len(data) > quota:
        raise Refused(f"your file storage is full ({_mb(used)} of {_mb(quota)} used) — "
                      "delete some files or ask an admin for more room", 413)
    notes = _scan(data, ctype, filename)
    folder = root / user.id
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".upload-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, folder / sha)        # atomic: never a half-written file
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    name = os.path.basename(filename or "").strip()[:200] or f"capture{EXTENSION[ctype]}"
    doc = existing or Document(owner_id=user.id, sha256=sha)
    doc.content_type, doc.size_bytes, doc.filename = ctype, len(data), name
    doc.source, doc.trip_id, doc.scan_notes = source, trip_id, (notes or None)
    doc.created_at, doc.purged_at = datetime.now(timezone.utc), None
    if existing is None:
        session.add(doc)
    session.flush()
    return doc, True


def read(doc: Document) -> bytes | None:
    """The file's bytes, or None when it was purged or is missing on disk."""
    if doc.purged_at is not None:
        return None
    p = path_for(doc)
    try:
        return p.read_bytes() if p is not None else None
    except OSError:
        return None


def remove_bytes(doc: Document) -> None:
    p = path_for(doc)
    if p is not None:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def delete(session: Session, doc: Document) -> None:
    """Delete a file and its record. What was extracted from it stays."""
    remove_bytes(doc)
    session.delete(doc)


def purge_trip(session: Session, trip_id: str) -> None:
    """Deleting a trip deletes its files at once (D-196)."""
    for doc in session.query(Document).filter(Document.trip_id == trip_id).all():
        delete(session, doc)


def own(session: Session, user, doc_id: str) -> Document | None:
    """The caller's own document, else None — nobody else's file is ever
    visible, not an admin's view, not a fellow traveller's (D-196)."""
    doc = session.get(Document, doc_id)
    return doc if doc is not None and doc.owner_id == user.id else None


# --------------------------------------------------------------------------- capture
def queue_capture(session: Session, doc: Document, user, *, notes: str | None = None,
                  via: str = "upload") -> str:
    """Send a stored file down the capture pipeline as a `file`-channel batch.
    The batch names the document; the bytes stay in the store (processing
    reads them). Returns the batch id. Caller commits."""
    import json

    from app.capture import _internal_key_id
    from app.ingest import store_batch
    payload = {
        "capture_channel": "file",
        "document_id": doc.id,
        "content_type": doc.content_type,
        "filename": doc.filename,
        "capture_context": {"submitted_by": f"file-{via}", "via": via,
                            "trip_id": doc.trip_id, "notes": notes or None},
    }
    batch = store_batch(session, json.dumps(payload).encode("utf-8"), payload,
                        _internal_key_id(session), user_id=user.id)
    session.flush()
    doc.batch_id = batch.id
    return batch.id


def pipeline_payload(session: Session, payload: dict) -> dict:
    """What the extractor sees for a `file` batch: the stored bytes put back
    in. An .eml becomes an ordinary email capture; a PDF or .ics travels as
    file_base64. A file that's gone (purged, deleted) carries `missing`."""
    import base64
    doc = session.get(Document, payload.get("document_id") or "")
    data = read(doc) if doc is not None else None
    out = dict(payload)
    if data is None:
        out["missing"] = True
        return out
    b64 = base64.b64encode(data).decode("ascii")
    if payload.get("content_type") == EML:
        out["raw_message_base64"] = b64
    else:
        out["file_base64"] = b64
    return out
