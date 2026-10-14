"""Email-forward capture (SPEC-9 D-162, D-190; YONDER-199).

The admin owns one mailbox, say `trips@example.com`, that accepts plus-addressed
mail and is reachable over IMAP. Each user gets their own address on it,
`trips+<token>@example.com`; forwarding a confirmation there makes it their
capture. The poller reads the mailbox every few minutes, turns each message into
an ordinary `email`-channel batch — the same raw-RFC 2822 shape the Gmail add-on
sends (D-15) — owned by that user, files the message into a done folder, and
starts processing (D-158) so it lands in their approval queue (D-31).

Who a message is for comes from the ADDRESS, never the sender (D-162): forwarding
from any of your own accounts works, and the token is the capability. So:

- the token is random and can be regenerated (the old address stops working);
- mail to an address that matches no one is dropped and counted — never stored,
  never an ownerless capture (YONDER-186); it stays only in the admin's mailbox;
- each address is rate-limited, and a message is never captured twice;
- nothing a forwarded email says is acted on: it becomes a review item, and
  nothing is written anywhere until its owner approves it.

Configured by `TX_INBOX_*` settings like SMTP, so the mailbox password never
enters the database or a log line. Off until host, user and address are set.
"""
from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import imaplib
import json
import logging
import re
import secrets
import select
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import InboxAddress, InboxMessage, User

log = logging.getLogger("yonder.inbox")

CHANNEL = "email"
_CLIENT_NAME = "email-inbox"
# Headers that can carry the address the mail was delivered to, most reliable
# first: the receiving server's own record, then the visible recipients.
_RECIPIENT_HEADERS = ("Delivered-To", "X-Original-To", "Envelope-To", "X-Forwarded-To",
                      "To", "Cc")
_TOKEN_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"     # no 0/o, 1/l/i look-alikes
_TOKEN_LEN = 12                                          # ~59 bits
_PER_POLL = 50

# Outcomes recorded per message (InboxMessage.outcome).
CAPTURED, DUPLICATE, UNMATCHED, TOO_LARGE, RATE_LIMITED, UNREADABLE = (
    "captured", "duplicate", "unmatched", "too_large", "rate_limited", "unreadable")

# What the admin panel shows about the poller; module state, one per process.
status: dict = {"last_poll_at": None, "last_error": None, "last_counts": {}, "mode": None}


# --------------------------------------------------------------------- config + addresses
def configured() -> bool:
    return bool(settings.inbox_imap_host and settings.inbox_imap_user
                and _split(settings.inbox_address))


def _split(address: str) -> tuple[str, str] | None:
    """`trips@example.com` -> ("trips", "example.com"); None if it isn't one."""
    local, _, domain = (address or "").strip().lower().rpartition("@")
    if not local or not domain or "+" in local or " " in local:
        return None
    return local, domain


def _new_token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LEN))


def address_for(token: str) -> str:
    local, domain = _split(settings.inbox_address)
    return f"{local}+{token}@{domain}"


def user_address(session: Session, user, *, create: bool = True) -> str | None:
    """This person's forward-to address, made on first use. None when the inbox
    isn't configured (or there's no person)."""
    if user is None or not configured():
        return None
    row = session.get(InboxAddress, user.id)
    if row is None:
        if not create:
            return None
        row = InboxAddress(user_id=user.id, token=_new_token())
        session.add(row)
        session.commit()
    return address_for(row.token)


def rotate(session: Session, user) -> str | None:
    """Give this person a new address; the old one stops working at once."""
    if user is None or not configured():
        return None
    row = session.get(InboxAddress, user.id)
    if row is None:
        row = InboxAddress(user_id=user.id, token=_new_token())
        session.add(row)
    else:
        row.token = _new_token()
        row.created_at = datetime.now(timezone.utc)
    session.commit()
    return address_for(row.token)


# --------------------------------------------------------------------- one message
def recipient_tokens(msg) -> list[str]:
    """Every token addressed in this message, in header order, deduplicated:
    `local+TOKEN@domain` for the configured mailbox only."""
    base = _split(settings.inbox_address)
    if base is None:
        return []
    local, domain = base
    found: list[str] = []
    for header in _RECIPIENT_HEADERS:
        for _, addr in getaddresses([str(v) for v in (msg.get_all(header) or [])]):
            a_local, _, a_domain = addr.strip().lower().rpartition("@")
            if a_domain != domain or not a_local.startswith(local + "+"):
                continue
            token = a_local[len(local) + 1:]
            if token and token not in found:
                found.append(token)
    return found


def _message_key(msg, raw: bytes, user_id: str | None) -> str:
    ident = (str(msg.get("Message-ID") or "").strip()
             or "sha256:" + hashlib.sha256(raw).hexdigest())
    return hashlib.sha256(f"{ident}|{user_id or ''}".encode()).hexdigest()


def _record(session: Session, key: str, outcome: str, *, user_id=None, batch_id=None,
            detail: str | None = None) -> bool:
    """Write the per-message record; False when it already exists (a duplicate,
    e.g. another worker got there first)."""
    session.add(InboxMessage(message_key=key, user_id=user_id, outcome=outcome,
                             batch_id=batch_id, detail=(detail or None) and detail[:200]))
    try:
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        return False


def _seen(session: Session, key: str) -> bool:
    return session.query(InboxMessage.id).filter_by(message_key=key).first() is not None


def _over_limit(session: Session, user_id: str) -> bool:
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    n = (session.query(InboxMessage)
         .filter(InboxMessage.user_id == user_id, InboxMessage.outcome == CAPTURED,
                 InboxMessage.received_at >= since).count())
    return n >= settings.inbox_rate_limit


def _iso_date(msg) -> str | None:
    try:
        return parsedate_to_datetime(str(msg.get("Date"))).isoformat()
    except Exception:
        return None


def _internal_key_id(session: Session) -> str:
    """The inert api_keys row inbox batches are attributed to (as the web capture
    does): its hash is random, so it can never be presented as an /ingest key."""
    from app.models import ApiKey
    row = session.query(ApiKey).filter_by(client_name=_CLIENT_NAME).one_or_none()
    if row is None:
        row = ApiKey(client_name=_CLIENT_NAME,
                     key_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest())
        session.add(row)
        session.flush()
    return row.id


@dataclass
class Handled:
    outcome: str
    user_id: str | None = None
    batch_id: str | None = None
    subject: str | None = None


def handle_message(session: Session, raw: bytes) -> list[Handled]:
    """Turn one raw email into a capture for each user it was addressed to.
    Never raises on bad mail; returns what happened, one entry per recipient
    (or one UNMATCHED / UNREADABLE entry)."""
    from app.ingest import store_batch
    if len(raw) > settings.inbox_max_bytes:
        key = _message_key(email.message_from_bytes(raw[:65536]), raw, None)
        _record(session, key, TOO_LARGE, detail=f"{len(raw)} bytes")
        return [Handled(TOO_LARGE)]
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    except Exception:
        _record(session, hashlib.sha256(b"unreadable|" + raw).hexdigest(), UNREADABLE)
        return [Handled(UNREADABLE)]

    tokens = recipient_tokens(msg)
    owners = []
    for token in tokens:
        row = session.query(InboxAddress).filter_by(token=token).one_or_none()
        user = session.get(User, row.user_id) if row is not None else None
        if user is not None and user.is_active and user.id not in {u.id for u in owners}:
            owners.append(user)
    if not owners:
        # D-162: dropped and counted, never an ownerless capture. No address,
        # sender or subject is kept — only that it happened.
        _record(session, _message_key(msg, raw, None), UNMATCHED,
                detail="no known address" if tokens else "not sent to a personal address")
        return [Handled(UNMATCHED)]

    out: list[Handled] = []
    for user in owners:
        key = _message_key(msg, raw, user.id)
        if _seen(session, key):
            out.append(Handled(DUPLICATE, user.id))
            continue
        if _over_limit(session, user.id):
            _record(session, key, RATE_LIMITED, user_id=user.id)
            out.append(Handled(RATE_LIMITED, user.id))
            continue
        payload = {
            "capture_channel": CHANNEL,
            "message_id": str(msg.get("Message-ID") or "").strip() or None,
            "subject": str(msg.get("Subject") or "")[:300] or None,
            "internal_date": _iso_date(msg),
            # D-15: the raw RFC 2822 source, as the Gmail add-on sends it. A
            # message forwarded "as attachment" carries the original inside, and
            # the MIME decoder walks into it, so its booking markup survives.
            "raw_message_base64": base64.b64encode(raw).decode("ascii"),
            "capture_context": {"submitted_by": "email-forward", "via": "email"},
        }
        body = json.dumps(payload).encode("utf-8")
        batch = store_batch(session, body, payload, _internal_key_id(session),
                            user_id=user.id)
        session.flush()
        session.add(InboxMessage(message_key=key, user_id=user.id, outcome=CAPTURED,
                                 batch_id=batch.id))
        try:
            session.commit()
        except IntegrityError:            # another worker captured it first
            session.rollback()
            out.append(Handled(DUPLICATE, user.id))
            continue
        out.append(Handled(CAPTURED, user.id, batch.id, payload["subject"]))
    return out


# --------------------------------------------------------------------- IMAP
def tls_mode() -> str:
    """How the mailbox's certificate is checked: "verified" (the system's CAs and
    the hostname), "pinned" (exactly the certificate in TX_INBOX_IMAP_CA_FILE),
    or "unverified" (TX_INBOX_IMAP_VERIFY=false)."""
    if not settings.inbox_imap_verify:
        return "unverified"
    return "pinned" if settings.inbox_imap_ca_file else "verified"


def _tls_context() -> ssl.SSLContext:
    """The TLS context for the mailbox (YONDER-229). imaplib's own default checks
    neither the certificate nor the hostname, which would let anyone in the
    middle collect the mailbox password — so it is never used.

    - verified: the system's trusted CAs, with the hostname checked.
    - pinned: ONLY the certificate in TX_INBOX_IMAP_CA_FILE is trusted — e.g.
      Proton Bridge's self-signed one, exported from Bridge. Its name (127.0.0.1)
      can't match how the container reaches Bridge, so the hostname isn't
      checked; trusting just that one certificate is the stronger guarantee.
    - unverified: an explicit opt-out for a link you trust; logged every connect."""
    mode = tls_mode()
    if mode == "unverified":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        log.warning("inbox: TLS certificate NOT verified (TX_INBOX_IMAP_VERIFY=false)")
        return ctx
    if mode == "pinned":
        ctx = ssl.create_default_context(cafile=settings.inbox_imap_ca_file)
        ctx.check_hostname = False
        return ctx
    return ssl.create_default_context()


def _connect() -> imaplib.IMAP4:
    ctx = _tls_context()
    if settings.inbox_imap_ssl:
        conn = imaplib.IMAP4_SSL(settings.inbox_imap_host, settings.inbox_imap_port,
                                 ssl_context=ctx, timeout=30)
    else:
        conn = imaplib.IMAP4(settings.inbox_imap_host, settings.inbox_imap_port, timeout=30)
        conn.starttls(ssl_context=ctx)
    conn.login(settings.inbox_imap_user, settings.inbox_imap_password)
    # Servers (Proton Bridge among them) list MOVE / UIDPLUS / IDLE only once
    # signed in; imaplib kept the pre-login list.
    try:
        typ, data = conn.capability()
        if typ == "OK" and data and data[0]:
            conn.capabilities = tuple(data[0].decode().upper().split())
    except Exception:
        pass
    return conn


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _size_of(conn, uid: bytes) -> int:
    typ, data = conn.uid("FETCH", uid, "(RFC822.SIZE)")
    for part in data or []:
        text = part.decode() if isinstance(part, bytes) else str(part)
        if "RFC822.SIZE" in text:
            try:
                return int(text.split("RFC822.SIZE", 1)[1].strip(" )").split()[0])
            except (ValueError, IndexError):
                return 0
    return 0


def _fetch(conn, uid: bytes) -> bytes | None:
    typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
    for part in data or []:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
            return part[1]
    return None


def _move(conn, uid: bytes, folder: str) -> bool:
    """File a message into `folder` (made if missing). MOVE where the server has
    it; otherwise copy, flag deleted and expunge ONLY that message (UID EXPUNGE,
    RFC 4315). A plain EXPUNGE would also permanently remove anything else
    flagged deleted in the folder — someone's own mail in a shared mailbox — so
    without UIDPLUS the copy is left flagged instead. Returns whether it moved."""
    conn.create(_quote(folder))                     # "already exists" is fine
    caps = conn.capabilities or ()
    if "MOVE" in caps:
        typ, _ = conn.uid("MOVE", uid, _quote(folder))
        if typ == "OK":
            return True
    typ, _ = conn.uid("COPY", uid, _quote(folder))
    if typ != "OK":
        log.warning("inbox: couldn't file a message into %r (does the folder exist, or "
                    "does this server only allow folders under a parent such as "
                    "Proton's Folders/?)", folder)
        return False
    conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
    if "UIDPLUS" in caps:
        conn.uid("EXPUNGE", uid)
    return True


_HEADER_FIELDS = "(BODY.PEEK[HEADER.FIELDS (TO CC DELIVERED-TO X-ORIGINAL-TO ENVELOPE-TO X-FORWARDED-TO)])"
# Highest UID already looked at, per (folder, UIDVALIDITY): a big shared mailbox
# isn't rescanned on every new email. In memory: a restart rescans once.
_scanned: dict[tuple[str, bytes], int] = {}


def _uidvalidity(conn) -> bytes:
    try:
        typ, data = conn.response("UIDVALIDITY")
        return (data[0] or b"") if data else b""
    except Exception:
        return b""


def _addressed_to_us(conn, start: int) -> tuple[list[bytes], int]:
    """UIDs from `start` on whose recipient headers name one of OUR plus
    addresses (local+token@domain), and the highest UID seen. Only the
    recipient headers are read, with PEEK, so nothing is marked read — and mail
    that isn't for Run·dere is never fetched, moved or recorded (D-190: the
    mailbox may be someone's own, e.g. a Proton account with an alias)."""
    typ, data = conn.uid("FETCH", f"{start}:*", _HEADER_FIELDS)
    ours: list[bytes] = []
    top = start - 1
    for part in data or []:
        if not (isinstance(part, tuple) and len(part) >= 2):
            continue
        meta = part[0].decode(errors="replace") if isinstance(part[0], bytes) else str(part[0])
        m = re.search(r"UID (\d+)", meta)
        if not m:
            continue
        uid = int(m.group(1))
        if uid < start:                  # "n:*" answers the last message even past it
            continue
        top = max(top, uid)
        if recipient_tokens(email.message_from_bytes(part[1] or b"")):
            ours.append(str(uid).encode())
    return ours, top


def _drain(conn, session_factory) -> dict:
    """Capture, file and count every message for Run·dere waiting in the folder
    on an open, logged-in connection; acknowledge each capture and start
    processing for the people who got one. Mail not addressed to one of our plus
    addresses is left exactly where it is. Returns the counts."""
    counts: dict[str, int] = {}
    captured: list[Handled] = []
    typ, _ = conn.select(_quote(settings.inbox_folder))
    if typ != "OK":
        raise RuntimeError(f"can't open folder {settings.inbox_folder!r}")
    key = (settings.inbox_folder, _uidvalidity(conn))
    ours, top = _addressed_to_us(conn, _scanned.get(key, 0) + 1)
    for uid in ours[:_PER_POLL]:
        with session_factory() as session:
            size = _size_of(conn, uid)
            if size > settings.inbox_max_bytes:
                # Never fetched; it moves to the dropped folder, so it isn't seen again.
                _record(session, hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                        TOO_LARGE, detail=f"{size} bytes")
                results = [Handled(TOO_LARGE)]
            else:
                raw = _fetch(conn, uid)
                results = (handle_message(session, raw) if raw is not None
                           else [Handled(UNREADABLE)])
        for r in results:
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
            if r.outcome == CAPTURED and r.user_id:
                captured.append(r)
        keep = any(r.outcome in (CAPTURED, DUPLICATE) for r in results)
        _move(conn, uid, settings.inbox_done_folder if keep
              else settings.inbox_dropped_folder)
    # Only mark as scanned what was fully handled; a capped batch resumes there.
    _scanned[key] = top if len(ours) <= _PER_POLL else int(ours[_PER_POLL - 1])
    _acknowledge(session_factory, captured)
    _kick(session_factory, {r.user_id for r in captured})
    return counts


def poll_once(session_factory) -> dict:
    """Connect, read the inbox once (_drain), disconnect. Returns the counts."""
    conn = _connect()
    try:
        return _drain(conn, session_factory)
    finally:
        _logout(conn)


def _logout(conn) -> None:
    try:
        conn.logout()
    except Exception:
        pass


def _acknowledge(session_factory, captured: list[Handled]) -> None:
    """A "got it" email for each capture, right away (D-190). It goes to the
    capture owner's OWN account address — never to the message's sender, which
    nobody verified (D-162), so a forged sender can't make Run·dere mail a
    stranger. Best-effort: no SMTP, or no address on the account, means none."""
    if not captured or not settings.inbox_acknowledge:
        return
    from app import email as mail
    if not mail.email_configured():
        return
    base = settings.public_base_url.rstrip("/")
    with session_factory() as session:
        for r in captured:
            user = session.get(User, r.user_id)
            if user is None or not user.email:
                continue
            what = " ".join((r.subject or "").split())[:150] or "your forwarded email"
            where = f"{base}/review" if base else "your review queue in Run·dere"
            mail.send_email(
                user.email, f"Got it: {what}",
                f"Run·dere received the email you forwarded — \"{what}\" — and is "
                f"reading it now.\n\nIt will wait for your approval in {where}. Nothing "
                f"is filed until you approve it.\n\nYou get this because it was sent to "
                f"your Run·dere forwarding address. If you didn't forward it, make a new "
                f"address under Capture → Other ways to capture → Email forward.")


def _kick(session_factory, user_ids: set[str]) -> None:
    """Nobody is at the page when mail arrives, so start processing for each
    person who got a capture, as the capture page does (D-158)."""
    if not user_ids:
        return
    from app.review import autoprocess
    with session_factory() as session:
        for uid in user_ids:
            user = session.get(User, uid)
            if user is None:
                continue
            try:
                autoprocess.kick(user)
            except Exception:
                log.exception("auto-process could not start for an inbox capture")


# --------------------------------------------------------------------- IMAP IDLE
_IDLE_SECONDS = 20 * 60        # re-issue before the ~29-minute server cut-off (RFC 2177)
_IDLE_TICK = 5                 # how often a waiting IDLE checks for shutdown


def supports_idle(conn) -> bool:
    return settings.inbox_idle and "IDLE" in (conn.capabilities or ())


def _readable(conn, seconds: float) -> bool:
    """Is a server line waiting? TLS can hold decrypted bytes the OS socket no
    longer shows, so check those first."""
    sock = conn.socket()
    pending = getattr(sock, "pending", None)
    if pending is not None and pending():
        return True
    ready, _, _ = select.select([sock], [], [], seconds)
    return bool(ready)


def idle_wait(conn, seconds: float = _IDLE_SECONDS) -> bool:
    """Hold an IMAP IDLE (RFC 2177) until the server announces new mail, the
    time is up, or the reader is stopping. True when new mail arrived. Python
    3.12's imaplib has no idle(), so this speaks the three lines of protocol
    itself; it reads only once select() says a line is there, so no socket
    timeout is ever tripped (that would leave imaplib's reader unusable)."""
    tag = conn._new_tag()
    conn.send(tag + b" IDLE\r\n")
    first = conn.readline()
    if not first.startswith(b"+"):
        raise RuntimeError(f"server refused IDLE: {first[:80]!r}")
    new_mail = False
    deadline = time.monotonic() + seconds
    while not new_mail and not _stop.is_set():
        left = deadline - time.monotonic()
        if left <= 0:
            break
        if _readable(conn, min(_IDLE_TICK, left)):
            line = conn.readline()
            if not line:
                raise ConnectionError("the mail server closed the connection")
            new_mail = b"EXISTS" in line or b"RECENT" in line
    conn.send(b"DONE\r\n")
    while True:                                   # read to the IDLE's own completion
        line = conn.readline()
        if not line:
            raise ConnectionError("the mail server closed the connection")
        if line.startswith(tag):
            break
    return new_mail


# --------------------------------------------------------------------- the reader
_stop = threading.Event()
_thread: threading.Thread | None = None


def _ok(counts: dict, mode: str) -> None:
    status.update(last_poll_at=datetime.now(timezone.utc), last_error=None,
                  last_counts=counts, mode=mode)
    from app import notify
    notify.report("inbox", True, category="mail", label="Email inbox (IMAP)")
    if counts:
        log.info("inbox: %s", ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))


def _failed(ex: Exception) -> None:
    """Record a failure for the admin panel: the server's reply, never our
    credentials (redacted in case a server ever echoes them)."""
    text = str(ex)
    if settings.inbox_imap_password:
        text = text.replace(settings.inbox_imap_password, "***")
    status.update(last_poll_at=datetime.now(timezone.utc),
                  last_error=f"{type(ex).__name__}: {text[:200]}")
    log.warning("inbox read failed: %s", status["last_error"])
    # Admin alert (D-197) once it has failed twice in a row — one dropped
    # connection that the next read recovers from pages nobody.
    from app import notify
    notify.report("inbox", False, category="mail", label="Email inbox (IMAP)",
                  detail=status["last_error"], threshold=2)


def poll_and_record(session_factory) -> None:
    """One poll, with its outcome kept in `status`. Never raises."""
    try:
        _ok(poll_once(session_factory), "polling")
    except Exception as ex:
        _failed(ex)


def listen(session_factory) -> bool:
    """One connection's worth of near-real-time reading: drain the folder, then
    IDLE until the server says mail arrived and drain again, re-IDLEing every
    20 minutes. Returns False when the server can't IDLE (the caller polls
    instead) and True when the connection ended and should be re-opened.
    Never raises."""
    try:
        conn = _connect()
    except Exception as ex:
        _failed(ex)
        return True
    try:
        if not supports_idle(conn):
            _ok(_drain(conn, session_factory), "polling")
            return False
        while not _stop.is_set():
            _ok(_drain(conn, session_factory), "push")
            idle_wait(conn)                  # new mail, timeout or stop: drain again
        return True
    except Exception as ex:
        _failed(ex)
        return True
    finally:
        _logout(conn)


def _loop(session_factory) -> None:
    pause = max(30, settings.inbox_poll_seconds)
    while not _stop.is_set():
        if settings.inbox_idle and listen(session_factory):
            # The connection ended (an error or the server's cut-off): reconnect,
            # after a pause when it was an error so a down server isn't hammered.
            _stop.wait(pause if status.get("last_error") else 1)
            continue
        if not settings.inbox_idle:
            poll_and_record(session_factory)
        _stop.wait(pause)


def start(session_factory) -> bool:
    """Start the background reader once per process, if the inbox is configured."""
    global _thread
    if not configured() or (_thread is not None and _thread.is_alive()):
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(session_factory,), name="inbox-reader",
                               daemon=True)
    _thread.start()
    log.info("inbox reader started for %s (%s)", settings.inbox_address,
             "IDLE push, polling fallback" if settings.inbox_idle
             else f"polling every {max(30, settings.inbox_poll_seconds)}s")
    return True


def stop() -> None:
    _stop.set()


def recent_counts(session: Session, days: int = 7) -> dict[str, int]:
    """What the inbox did lately, by outcome — for the admin's panel."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (session.query(InboxMessage.outcome)
            .filter(InboxMessage.received_at >= since).all())
    counts: dict[str, int] = {}
    for (outcome,) in rows:
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts
