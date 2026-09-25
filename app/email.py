"""Minimal SMTP sender for auth emails (Phase 1a.1, D-117).

Optional by design: if TX_SMTP_HOST is unset, ``send_email`` is a no-op that
returns False, and the reset flow falls back to admin-generated links. Only
transactional auth mail (a password-reset LINK — never a password) goes here.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

log = logging.getLogger("yonder.email")


def email_configured() -> bool:
    return bool(settings.smtp_host and (settings.smtp_from or settings.smtp_user))


def send_email(to: str, subject: str, body: str) -> bool:
    """Best-effort send. Returns True only if the message was handed to the SMTP
    server; False (logged) on any misconfiguration or failure — callers must not
    reveal the difference to end users."""
    if not email_configured() or not to:
        return False
    msg = EmailMessage()
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
            if settings.smtp_starttls:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        return True
    except Exception:                                   # pragma: no cover - env dependent
        log.exception("password-reset email to %s failed", to)
        return False
