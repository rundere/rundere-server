"""Server mail (owner, 2026-10-12): SMTP and the email-forward inbox in one admin
page with status and checks; SMTP's TLS certificate is verified (YONDER-229)."""
import smtplib
import ssl
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import authn, email as mail, inbox
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, User

    Base.metadata.create_all(engine)
    client = TestClient(app)

    @pytest.fixture(autouse=True)
    def _smtp_on(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.test")
        monkeypatch.setattr(settings, "smtp_port", 587)
        monkeypatch.setattr(settings, "smtp_user", "trips@example.test")
        monkeypatch.setattr(settings, "smtp_password", "s3cret-smtp")
        monkeypatch.setattr(settings, "smtp_from", "trips@example.test")
        monkeypatch.setattr(settings, "smtp_ssl", False)
        monkeypatch.setattr(settings, "smtp_starttls", True)
        mail.status.update(last_at=None, last_ok=None, last_error=None, last_kind=None)
        yield

    def _cookie(admin):
        with SessionLocal() as s:
            u = User(username=f"sm-{uuid.uuid4().hex[:8]}", auth_source="local",
                     is_admin=admin, email="me@example.test")
            s.add(u); s.commit()
            return {settings.auth_cookie_name: authn.issue_web_token(u)}

    class FakeSMTP:
        """Records how it was opened; never talks to a network."""
        opened = []

        def __init__(self, host, port, timeout=None, context=None):
            FakeSMTP.opened.append(("ssl" if context is not None else "plain", context))
            self.sent = []

        def starttls(self, context=None):
            FakeSMTP.opened.append(("starttls", context))

        def login(self, u, p):
            pass

        def noop(self):
            return 250, b"ok"

        def send_message(self, msg):
            self.sent.append(msg)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_smtp_certificate_is_verified_on_starttls_and_ssl(monkeypatch):
        FakeSMTP.opened = []
        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
        assert mail.check_connection()[0] is True
        monkeypatch.setattr(settings, "smtp_ssl", True)
        assert mail.check_connection()[0] is True
        contexts = [c for kind, c in FakeSMTP.opened if kind in ("starttls", "ssl")]
        assert len(contexts) == 2
        assert all(c.verify_mode == ssl.CERT_REQUIRED and c.check_hostname for c in contexts)

    def test_587_with_ssl_explains_the_mix_up_and_hides_the_password():
        ex = ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number s3cret-smtp")
        text = mail.explain(ex)
        assert "port 587 uses STARTTLS (TX_SMTP_SSL=false)" in text
        assert "s3cret-smtp" not in text

    def test_a_failed_send_is_recorded_for_the_admin(monkeypatch):
        def boom(*a, **kw):
            raise ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")
        monkeypatch.setattr(smtplib, "SMTP", boom)
        assert mail.send_email("someone@example.test", "x", "y") is False
        assert mail.status["last_ok"] is False and "STARTTLS" in mail.status["last_error"]

    def test_server_mail_page_is_admin_only_and_shows_status(monkeypatch):
        assert client.get("/settings/mail", cookies=_cookie(False)).status_code in (401, 403)
        mail.status.update(last_ok=False, last_kind="send", last_error="SSLError: boom")
        from datetime import datetime, timezone
        mail.status["last_at"] = datetime.now(timezone.utc)
        html = client.get("/settings/mail", cookies=_cookie(True)).text
        assert "Outgoing — SMTP" in html and "Incoming — email-forward inbox" in html
        assert "SSLError: boom" in html and "failed" in html
        assert "s3cret-smtp" not in html
        assert 'href="/settings/mail"' in html                  # in the Settings menu

    def test_check_connection_button_reports_success(monkeypatch):
        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        r = client.post("/settings/mail/check-smtp", cookies=_cookie(True))
        assert "Connected and signed in to smtp.example.test:587." in r.text

    def test_send_test_email_button(monkeypatch):
        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        r = client.post("/settings/mail/test", cookies=_cookie(True), data={"to": ""})
        assert "Test email sent to me@example.test." in r.text

    def test_check_inbox_opens_the_folder_read_only(monkeypatch):
        monkeypatch.setattr(settings, "inbox_address", "trips@example.test")
        monkeypatch.setattr(settings, "inbox_imap_host", "imap.example.test")
        monkeypatch.setattr(settings, "inbox_imap_user", "trips@example.test")
        seen = {}

        class Conn:
            capabilities = ("IMAP4REV1", "IDLE")
            def select(self, folder, readonly=False):
                seen["readonly"] = readonly
                return "OK", [b"3"]
            def logout(self):
                return "BYE", []
        monkeypatch.setattr(inbox, "_connect", lambda: Conn())
        r = client.post("/settings/mail/check-inbox", cookies=_cookie(True))
        assert seen["readonly"] is True
        assert "holds 3 messages" in r.text and "offers IMAP IDLE" in r.text
