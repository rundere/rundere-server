"""YONDER-253 / SPEC-8 D-197 — admin notifications.

The alert state machine (failing after N in a row, a 24-hour reminder,
recovered), dispatch to the targets that want a category, the Pushover
sender (stubbed HTTP), write-only credentials encrypted at rest, the admin
page (admins only), the health monitor's checks, and the hooks in mail and
the inbox. Never a credential in a message or on the page."""
import base64
import io
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
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
    from app import authn, notify, secretbox
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import AlertState, Base, NotificationTarget, User
    from app.notify import monitor, pushover

    Base.metadata.create_all(engine)
    client = TestClient(app)
    KEY = base64.b64encode(os.urandom(32)).decode()
    TOKEN, USER_KEY = "a" * 15 + "B1c2D3e4F5g6H7i", "u" * 26 + "9Zx1"

    @pytest.fixture(autouse=True)
    def env(monkeypatch):
        monkeypatch.setattr(settings, "secrets_key", KEY)
        monkeypatch.setattr(settings, "secrets_keys", "")
        monkeypatch.setattr(settings, "secrets_key_file", "")
        secretbox._ring_from.cache_clear()
        monkeypatch.setattr(notify, "ASYNC", False)
        sent = []

        def fake_send(secrets, config, title, message, *, quiet=False, url=None):
            sent.append({"secrets": secrets, "config": config, "title": title,
                         "message": message, "quiet": quiet})
            return True, "sent"
        monkeypatch.setattr(pushover, "send", fake_send)
        yield sent
        with SessionLocal() as s:
            s.query(AlertState).delete()
            s.query(NotificationTarget).delete()
            s.query(User).filter(User.username.like("nt-%")).delete(synchronize_session=False)
            s.commit()
        secretbox._ring_from.cache_clear()

    def _target(categories=None, enabled=True, name="Phone"):
        with SessionLocal() as s:
            t = NotificationTarget(kind="pushover", name=name, config={"priority": 0},
                                   categories=categories, enabled=enabled, secret_hint="…9Zx1")
            s.add(t)
            s.flush()
            notify.set_secrets(t, {"token": TOKEN, "user": USER_KEY})
            s.commit()
            return t.id

    def _admin(name="nt-admin", admin=True):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                u = User(username=name, auth_source="local", is_admin=admin)
                s.add(u)
                s.commit()
            return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _state(key):
        with SessionLocal() as s:
            return s.get(AlertState, key)

    # ---------------------------------------------------------------- the state machine
    def test_failing_after_threshold_reminder_and_recovered(env):
        _target()
        kw = dict(category="mail", label="Email inbox (IMAP)", threshold=2)
        assert notify.report("inbox", False, detail="EOF", **kw) is None     # one hiccup
        assert notify.report("inbox", False, detail="EOF again", **kw) == notify.FAILING
        assert env[-1]["title"] == "⚠️ Email inbox (IMAP) is failing"
        assert "EOF again" in env[-1]["message"] and env[-1]["quiet"] is False
        assert notify.report("inbox", False, detail="EOF", **kw) is None     # no spam
        with SessionLocal() as s:                                            # a day later
            st = s.get(AlertState, "inbox")
            st.last_alert_at = datetime.now(timezone.utc) - timedelta(hours=25)
            st.since = datetime.now(timezone.utc) - timedelta(hours=26)
            s.commit()
        assert notify.report("inbox", False, detail="EOF", **kw) == notify.REMINDER
        assert "still failing" in env[-1]["title"] and "26 h" in env[-1]["message"]
        assert notify.report("inbox", True, **kw) == notify.RECOVERED
        assert env[-1]["title"] == "✅ Email inbox (IMAP) recovered" and env[-1]["quiet"]
        assert not _state("inbox").failing and len(env) == 3

    def test_a_blip_below_threshold_never_alerts(env):
        _target()
        kw = dict(category="ai", label="LLM (Ollama)", threshold=2)
        notify.report("llm", False, detail="timeout", **kw)
        assert notify.report("llm", True, **kw) is None and env == []

    def test_targets_get_only_their_categories(env):
        mail_only = _target(categories=["mail"], name="Mail person")
        _target(categories=["storage"], name="Storage person")
        _target(enabled=False, name="Off")
        notify.report("smtp", False, category="mail", label="Outgoing email (SMTP)",
                      detail="auth refused")
        assert len(env) == 1
        with SessionLocal() as s:
            assert s.get(NotificationTarget, mail_only).last_sent_at is not None

    def test_a_failing_target_records_why(monkeypatch):
        tid = _target()
        monkeypatch.setattr(pushover, "send", lambda *a, **k: (False, "Pushover: user key is invalid"))
        notify.report("documents", False, category="storage", label="File store folder",
                      detail="missing")
        with SessionLocal() as s:
            t = s.get(NotificationTarget, tid)
            assert t.last_error == "Pushover: user key is invalid" and t.last_sent_at is None

    def test_credentials_are_encrypted_at_rest():
        tid = _target()
        with SessionLocal() as s:
            t = s.get(NotificationTarget, tid)
            assert TOKEN not in (t.secret_ct or "") and USER_KEY not in (t.secret_ct or "")
            assert notify.secrets_of(t) == {"token": TOKEN, "user": USER_KEY}
            other = NotificationTarget(id="x" * 36, kind="pushover", secret_ct=t.secret_ct)
            with pytest.raises(secretbox.SecretError):        # bound to its own row
                notify.secrets_of(other)

    # ---------------------------------------------------------------- Pushover
    def test_pushover_validation():
        secrets, config = pushover.validate(TOKEN, USER_KEY, "iphone", "1")
        assert secrets == {"token": TOKEN, "user": USER_KEY}
        assert config == {"device": "iphone", "priority": 1}
        for bad in (("short", USER_KEY, "", 0), (TOKEN, "nope", "", 0),
                    (TOKEN, USER_KEY, "bad device!", 0), (TOKEN, USER_KEY, "", 2)):
            with pytest.raises(ValueError):
                pushover.validate(*bad)

    def test_pushover_send(monkeypatch):
        import importlib
        real = importlib.reload(pushover)          # undo the fixture's stub for this test
        seen = {}

        class Resp:
            status = 200

            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def ok(req, timeout=0):
            seen.update(dict(x.split("=", 1) for x in req.data.decode().split("&")))
            assert req.full_url == real.API
            return Resp(b'{"status":1,"request":"r"}')
        monkeypatch.setattr(urllib.request, "urlopen", ok)
        assert real.send({"token": TOKEN, "user": USER_KEY}, {"priority": 1, "device": "ipad"},
                         "T" * 300, "hello", quiet=True, url="https://r.example/x") == (True, "sent")
        assert seen["priority"] == "-1" and seen["device"] == "ipad" and len(seen["title"]) == 250

        def refused(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {},
                                         io.BytesIO(b'{"status":0,"errors":["user key is invalid"]}'))
        monkeypatch.setattr(urllib.request, "urlopen", refused)
        ok_, note = real.send({"token": TOKEN, "user": USER_KEY}, {}, "t", "m")
        assert not ok_ and note == "Pushover: user key is invalid" and TOKEN not in note

        def down(req, timeout=0):
            raise urllib.error.URLError("no route")
        monkeypatch.setattr(urllib.request, "urlopen", down)
        assert real.send({"token": TOKEN, "user": USER_KEY}, {}, "t", "m")[1].startswith("can't reach")

    # ---------------------------------------------------------------- the admin page
    def test_admin_page_add_test_toggle_remove(env):
        h = _admin()
        assert client.get("/settings/notifications", headers=_admin("nt-user", admin=False)
                          ).status_code == 403
        r = client.post("/settings/notifications/pushover", headers=h, data={
            "name": "My phone", "token": TOKEN, "user_key": USER_KEY, "device": "",
            "priority": "0", "categories": ["mail", "storage"]})
        assert r.status_code == 200 and "Added “My phone”" in r.text
        assert TOKEN not in r.text and USER_KEY not in r.text and "…9Zx1" in r.text
        with SessionLocal() as s:
            t = s.query(NotificationTarget).one()
            assert t.categories == ["mail", "storage"] and t.secret_hint == "…9Zx1"
            tid = t.id
        r = client.post(f"/settings/notifications/{tid}/test", headers=h)
        assert "Sent to “My phone”" in r.text and env[-1]["title"] == "Run·dere test notification"
        assert env[-1]["secrets"] == {"token": TOKEN, "user": USER_KEY}
        client.post(f"/settings/notifications/{tid}/toggle", headers=h)
        with SessionLocal() as s:
            assert s.get(NotificationTarget, tid).enabled is False
        r = client.post("/settings/notifications/pushover", headers=h,
                        data={"token": "short", "user_key": USER_KEY})
        assert "30 letters and digits" in r.text
        client.post(f"/settings/notifications/{tid}/delete", headers=h)
        with SessionLocal() as s:
            assert s.query(NotificationTarget).count() == 0

    def test_no_secrets_key_no_targets(monkeypatch):
        monkeypatch.setattr(settings, "secrets_key", "")
        secretbox._ring_from.cache_clear()
        r = client.post("/settings/notifications/pushover", headers=_admin(),
                        data={"token": TOKEN, "user_key": USER_KEY})
        assert "TX_SECRETS_KEY" in r.text
        with SessionLocal() as s:
            assert s.query(NotificationTarget).count() == 0

    # ---------------------------------------------------------------- the monitor
    def test_monitor_checks(monkeypatch, tmp_path, env):
        _target()
        from extract import filescan
        monkeypatch.setattr(settings, "documents_dir", str(tmp_path / "missing"))
        monkeypatch.setattr(settings, "clamav_url", "http://av.test")
        monkeypatch.setattr(settings, "ollama_url", "")

        def av_down(url, name, data, timeout=30):
            raise filescan.ScanUnavailable("URLError")
        monkeypatch.setattr(filescan, "clamav_scan", av_down)
        out = monitor.run_once()
        assert out["documents"] is False and out["antivirus"] is False and "llm" not in out
        assert _state("documents").failing and "missing" in _state("documents").detail
        assert any("File store folder is failing" in m["title"] for m in env)
        assert not any("Virus scanner" in m["title"] for m in env)     # threshold 2
        monitor.run_once()
        assert any("Virus scanner is failing" in m["title"] for m in env)
        (tmp_path / "missing").mkdir()
        monkeypatch.setattr(filescan, "clamav_scan", lambda url, name, data, timeout=30: None)
        monitor.run_once()
        assert not _state("documents").failing
        assert any("File store folder recovered" in m["title"] for m in env)

    def test_monitor_watches_connections_with_safe_messages(monkeypatch, env):
        from app import integrations
        from app.models import UserIntegration
        _target()
        with SessionLocal() as s:
            u = User(username="nt-traveller", auth_source="local")
            s.add(u)
            s.flush()
            s.add(UserIntegration(user_id=u.id, kind="airtrail", base_url="https://at.example",
                                  enabled=True))
            s.commit()
            uid = u.id
        monkeypatch.setattr(integrations, "_probe",
                            lambda c: (False, "HTTP 401: the token was refused"))
        try:
            monitor.check_integrations(SessionLocal)
            monitor.check_integrations(SessionLocal)
            st = _state(f"integration:{uid}:airtrail")
            assert st.failing and st.label == "AirTrail for nt-traveller"
            assert env[-1]["title"] == "⚠️ AirTrail for nt-traveller is failing"
            assert "token was refused" in env[-1]["message"]
        finally:
            with SessionLocal() as s:
                s.query(UserIntegration).filter_by(user_id=uid).delete()
                s.commit()

    def test_inbox_reader_that_stops_is_noticed(monkeypatch):
        from app import inbox
        monkeypatch.setattr(inbox, "configured", lambda: True)
        monkeypatch.setitem(inbox.status, "last_poll_at",
                            datetime.now(timezone.utc) - timedelta(hours=2))
        assert monitor.check_inbox()[0] is False
        monkeypatch.setitem(inbox.status, "last_poll_at", datetime.now(timezone.utc))
        assert monitor.check_inbox() is None

    # ---------------------------------------------------------------- hooks
    def test_mail_and_inbox_report_themselves(monkeypatch, env):
        from app import email as mail, inbox
        _target()
        monkeypatch.setattr(settings, "smtp_password", "hunter2-secret")
        mail._record(False, "send", OSError("refused hunter2-secret"))
        st = _state("smtp")
        assert st.failing and "hunter2-secret" not in (st.detail or "")
        assert env[-1]["title"] == "⚠️ Outgoing email (SMTP) is failing"
        assert "hunter2-secret" not in env[-1]["message"]
        mail._record(True, "check")
        assert env[-1]["title"] == "✅ Outgoing email (SMTP) recovered"
        monkeypatch.setattr(settings, "inbox_imap_password", "imap-pass-xyz")
        inbox._failed(OSError("login failed imap-pass-xyz"))
        assert not _state("inbox").alerted                      # one failure: quiet
        inbox._failed(OSError("login failed imap-pass-xyz"))
        assert env[-1]["title"] == "⚠️ Email inbox (IMAP) is failing"
        assert "imap-pass-xyz" not in env[-1]["message"]
        inbox._ok({}, "push")
        assert env[-1]["title"] == "✅ Email inbox (IMAP) recovered"
