"""Minimal SMTP sender for auth emails (Phase 1a.1, D-117).

Optional by design: if TX_SMTP_HOST is unset, ``send_email`` is a no-op that
returns False, and the reset flow falls back to admin-generated links. Only
transactional auth mail (a password-reset LINK — never a password) goes here.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.config import settings

log = logging.getLogger("yonder.email")


def email_configured() -> bool:
    return bool(settings.smtp_host and (settings.smtp_from or settings.smtp_user))


def smtp_summary() -> str:
    """One line describing the transport, for the admin Users page (no secrets)."""
    if not email_configured():
        return "not configured"
    mode = "SSL" if settings.smtp_ssl else ("STARTTLS" if settings.smtp_starttls else "plain")
    return (f"{settings.smtp_host}:{settings.smtp_port} ({mode}), "
            f"from {settings.smtp_from or settings.smtp_user}")


# What the admin's Server mail page shows about outgoing mail; one per process.
status: dict = {"last_at": None, "last_ok": None, "last_error": None, "last_kind": None}


def _open():
    """An authenticated SMTP connection. The server's certificate is checked —
    smtplib's own default checks neither the certificate nor the hostname
    (YONDER-229), which would hand the SMTP password to anyone in the middle."""
    ctx = ssl.create_default_context()
    if settings.smtp_ssl:
        conn = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15,
                                context=ctx)
    else:
        conn = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
        if settings.smtp_starttls:
            conn.starttls(context=ctx)
    if settings.smtp_user:
        conn.login(settings.smtp_user, settings.smtp_password)
    return conn


def explain(ex: Exception) -> str:
    """The error in plain words, with the usual fix when it's a known mix-up —
    never the password (redacted in case a server echoes it)."""
    text = f"{type(ex).__name__}: {ex}"
    if settings.smtp_password:
        text = text.replace(settings.smtp_password, "***")
    hint = ""
    low = text.lower()
    if "wrong_version_number" in low or "wrong version number" in low:
        hint = (" — the port and TLS mode don't match: port 587 uses STARTTLS "
                "(TX_SMTP_SSL=false), port 465 uses SSL (TX_SMTP_SSL=true).")
    elif "certificate verify failed" in low:
        hint = " — the server's TLS certificate couldn't be verified."
    elif isinstance(ex, smtplib.SMTPAuthenticationError):
        hint = (" — the username or password was refused (Gmail needs an app "
                "password; Proton needs an SMTP token).")
    elif isinstance(ex, (ConnectionRefusedError, TimeoutError, OSError)) and not hint:
        hint = " — the server couldn't be reached on that host and port."
    return (text[:240] + hint)


def _record(ok: bool, kind: str, ex: Exception | None = None) -> None:
    from datetime import datetime, timezone
    status.update(last_at=datetime.now(timezone.utc), last_ok=ok, last_kind=kind,
                  last_error=None if ok else explain(ex))


def check_connection() -> tuple[bool, str]:
    """Connect, secure the connection and sign in — then hang up without
    sending anything. For the admin's "Check connection" button."""
    if not email_configured():
        return False, "Outgoing email isn't configured."
    try:
        with _open() as s:
            s.noop()
        _record(True, "check")
        return True, f"Connected and signed in to {settings.smtp_host}:{settings.smtp_port}."
    except Exception as ex:
        _record(False, "check", ex)
        return False, status["last_error"]


def deliver(to: str, subject: str, body: str) -> None:
    """Hand one message to the SMTP server; raises on any failure. Callers that
    face end users go through ``send_email``; the admin test send calls this
    directly so it can show the real error."""
    msg = EmailMessage()
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with _open() as s:
            s.send_message(msg)
    except Exception as ex:
        _record(False, "send", ex)
        raise
    _record(True, "send")


def send_email(to: str, subject: str, body: str) -> bool:
    """Best-effort send. Returns True only if the message was handed to the SMTP
    server; False (logged) on any misconfiguration or failure — callers must not
    reveal the difference to end users."""
    if not email_configured() or not to:
        return False
    try:
        deliver(to, subject, body)
        return True
    except Exception:                                   # pragma: no cover - env dependent
        log.warning("email to %s failed: %s", to, status["last_error"])
        return False
