"""Yonder 4.0 Phase 1a.1 — onboarding & user management (SPEC-4 D-117).

Covers first-run setup, the registration toggle, the password-reset link flow,
and the admin Users CRUD (create + pick/create traveller, set password, reset
link, toggle admin, non-admin 403, self-admin guard). SQLite via conftest.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, PasswordResetToken, TravelerIdentity, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    def _wipe_users():
        with SessionLocal() as s:
            s.query(PasswordResetToken).delete()
            s.query(User).delete()
            s.commit()

    def _del_travelers(*names):
        # These tests create travellers in the shared SQLite DB; clean them up so
        # a stray kind='self' row doesn't pollute later files (RULE-10 assumes one).
        with SessionLocal() as s:
            for n in names:
                s.query(TravelerIdentity).filter_by(display_name=n).delete()
            s.commit()

    def _bearer(username, admin=False):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=username).one_or_none()
            if u is None:
                u = User(username=username, password_hash=authn.hash_password("pw"),
                         auth_source="local", is_admin=admin)
                s.add(u)
                s.commit()
                s.refresh(u)
            tok = authn.issue_access_token(u)
        return {"Authorization": f"Bearer {tok}"}

    def test_first_run_setup():
        _wipe_users()
        c = TestClient(app)
        assert c.get("/setup").status_code == 200
        r = c.post("/setup", data={"username": "root", "password": "rootpw",
                                   "email": "", "traveler_id": "none",
                                   "new_traveler": "Owner"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert settings.auth_cookie_name in c.cookies         # logged in
        with SessionLocal() as s:
            u = s.query(User).filter_by(username="root").one()
            assert u.is_admin and u.traveler_id is not None    # created 'self' traveller
            assert s.get(TravelerIdentity, u.traveler_id).kind == "self"
        # once a user exists, setup is closed
        assert c.get("/setup", follow_redirects=False).headers["location"] == "/login"
        _del_travelers("Owner")

    def test_register_toggle():
        settings.auth_registration_open = False
        try:
            assert client.get("/register",
                              follow_redirects=False).headers["location"] == "/login"
            settings.auth_registration_open = True
            r = client.post("/register", data={"username": "selfsignup", "password": "pw",
                                               "traveler_id": "none", "new_traveler": ""},
                            follow_redirects=False)
            assert r.status_code == 303
            with SessionLocal() as s:
                assert s.query(User).filter_by(username="selfsignup").one().is_admin is False
        finally:
            settings.auth_registration_open = False

    def test_forgot_and_reset():
        with SessionLocal() as s:
            s.query(User).filter_by(username="reseter").delete()
            s.commit()
            s.add(User(username="reseter", email="r@example.com",
                       password_hash=authn.hash_password("old"), auth_source="local"))
            s.commit()
        # neutral 200 and a token row is created
        assert client.post("/forgot", data={"identifier": "reseter"}).status_code == 200
        with SessionLocal() as s:
            u = s.query(User).filter_by(username="reseter").one()
            assert s.query(PasswordResetToken).filter_by(user_id=u.id).count() >= 1
            token = authn.create_reset_token(s, u)             # mint one we know the plaintext of
        assert client.get(f"/reset/{token}").status_code == 200
        r = client.post(f"/reset/{token}", data={"password": "newpass", "confirm": "newpass"},
                        follow_redirects=False)
        assert r.status_code == 303 and "msg=reset" in r.headers["location"]
        with SessionLocal() as s:
            assert authn.verify_password("newpass",
                                         s.query(User).filter_by(username="reseter").one().password_hash)
        # single-use
        assert client.post(f"/reset/{token}", data={"password": "x", "confirm": "x"},
                           follow_redirects=False).status_code == 400
        # mismatch is rejected
        with SessionLocal() as s:
            t2 = authn.create_reset_token(s, s.query(User).filter_by(username="reseter").one())
        assert client.post(f"/reset/{t2}", data={"password": "a", "confirm": "b"},
                           follow_redirects=False).status_code == 400

    def test_users_admin_crud():
        hdr = _bearer("acct-admin", admin=True)
        r = client.post("/users/create", headers=hdr,
                        data={"username": "created1", "password": "pw", "email": "",
                              "is_admin": "", "traveler_id": "none", "new_traveler": "New Person"},
                        follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as s:
            u = s.query(User).filter_by(username="created1").one()
            assert u.traveler_id is not None and not u.is_admin
            uid = u.id
        # duplicate username rejected (neutral redirect with err)
        assert "err=" in client.post("/users/create", headers=hdr,
                                     data={"username": "created1", "password": "pw"},
                                     follow_redirects=False).headers["location"]
        # set password
        assert client.post(f"/users/{uid}/password", headers=hdr, data={"password": "np"},
                           follow_redirects=False).status_code == 303
        with SessionLocal() as s:
            assert authn.verify_password(
                "np", s.query(User).filter_by(username="created1").one().password_hash)
        # reset-link surfaces a link
        assert "link=" in client.post(f"/users/{uid}/reset-link", headers=hdr,
                                      follow_redirects=False).headers["location"]
        # toggle admin
        client.post(f"/users/{uid}/admin", headers=hdr, follow_redirects=False)
        with SessionLocal() as s:
            assert s.query(User).filter_by(username="created1").one().is_admin
        # non-admin is blocked (403 from require_admin)
        plain = _bearer("acct-plain", admin=False)
        assert client.post("/users/create", headers=plain,
                           data={"username": "z", "password": "z"},
                           follow_redirects=False).status_code == 403
        _del_travelers("New Person")

    def test_self_admin_guard():
        hdr = _bearer("acct-admin2", admin=True)
        with SessionLocal() as s:
            mid = s.query(User).filter_by(username="acct-admin2").one().id
        r = client.post(f"/users/{mid}/admin", headers=hdr, follow_redirects=False)
        assert "err=" in r.headers["location"]                 # can't change your own role

    def test_account_email_self_service():
        """A user sets their own reset email, but only by re-entering their
        password; emails are unique case-insensitively; /forgot finds by email."""
        hdr = _bearer("mailer")                                  # password "pw"
        with SessionLocal() as s:
            s.query(User).filter(User.email.in_(["me@example.com", "ME@example.com"])
                                 ).update({User.email: None}, synchronize_session=False)
            s.commit()
        assert client.get("/account", headers=hdr).status_code == 200
        r = client.post("/account/email", headers=hdr, follow_redirects=False,
                        data={"email": "me@example.com", "current_password": "wrong"})
        assert "err=" in r.headers["location"]
        r = client.post("/account/email", headers=hdr, follow_redirects=False,
                        data={"email": "me@example.com", "current_password": "pw"})
        assert "msg=" in r.headers["location"]
        with SessionLocal() as s:
            assert s.query(User).filter_by(username="mailer").one().email == "me@example.com"
        # another account can't take the same address, whatever the case
        admin = _bearer("mail-admin", admin=True)
        with SessionLocal() as s:
            other = s.query(User).filter_by(username="mail-admin").one().id
        r = client.post(f"/users/{other}/email", headers=admin, follow_redirects=False,
                        data={"email": "ME@example.com"})
        assert "err=" in r.headers["location"]
        # /forgot resolves the email case-insensitively and mints a token
        with SessionLocal() as s:
            uid = s.query(User).filter_by(username="mailer").one().id
            before = s.query(PasswordResetToken).filter_by(user_id=uid).count()
        assert client.post("/forgot", data={"identifier": "Me@Example.com"}).status_code == 200
        with SessionLocal() as s:
            assert s.query(PasswordResetToken).filter_by(user_id=uid).count() == before + 1

    def test_admin_email_and_test_send(monkeypatch):
        from app import accounts_routes, email as email_mod
        admin = _bearer("mail-admin2", admin=True)
        with SessionLocal() as s:
            uid = s.query(User).filter_by(username="mail-admin2").one().id
        r = client.post(f"/users/{uid}/email", headers=admin, follow_redirects=False,
                        data={"email": "not-an-email"})
        assert "err=" in r.headers["location"]
        client.post(f"/users/{uid}/email", headers=admin, data={"email": "a2@example.com"})
        with SessionLocal() as s:
            assert s.get(User, uid).email == "a2@example.com"
        # no SMTP -> test send explains, never crashes
        monkeypatch.setattr(settings, "smtp_host", "")
        r = client.post("/users/test-email", headers=admin, follow_redirects=False)
        assert "not+configured" in r.headers["location"]
        # SMTP set -> delivered to the admin's own address by default
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_from", "yonder@example.com")
        sent = []
        monkeypatch.setattr(accounts_routes, "deliver", lambda to, subj, body: sent.append(to))
        r = client.post("/users/test-email", headers=admin, follow_redirects=False)
        assert sent == ["a2@example.com"] and "msg=" in r.headers["location"]
        # a failing server surfaces its error to the admin
        def boom(*a):
            raise OSError("connection refused")
        monkeypatch.setattr(accounts_routes, "deliver", boom)
        r = client.post("/users/test-email", headers=admin, follow_redirects=False)
        assert "connection+refused" in r.headers["location"]
        monkeypatch.setattr(settings, "smtp_ssl", True)
        assert "(SSL)" in email_mod.smtp_summary()

    def test_smtp_ssl_uses_implicit_tls(monkeypatch):
        from app import email as email_mod
        used = []

        class FakeSMTP:
            def __init__(self, *a, **k): used.append(type(self).__name__)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): used.append("starttls")
            def login(self, *a): pass
            def send_message(self, m): used.append("sent")

        class SMTP_SSL(FakeSMTP):
            pass

        monkeypatch.setattr(email_mod.smtplib, "SMTP", FakeSMTP)
        monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", SMTP_SSL)
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_from", "y@example.com")
        monkeypatch.setattr(settings, "smtp_ssl", True)
        assert email_mod.send_email("x@example.com", "s", "b")
        assert used == ["SMTP_SSL", "sent"]                      # no STARTTLS on SMTPS
        used.clear()
        monkeypatch.setattr(settings, "smtp_ssl", False)
        assert email_mod.send_email("x@example.com", "s", "b")
        assert used == ["FakeSMTP", "starttls", "sent"]

    def test_account_change_own_password():
        hdr = _bearer("pw-changer")                              # password "pw"
        r = client.post("/account/password", headers=hdr, follow_redirects=False,
                        data={"current_password": "nope", "password": "n1", "confirm": "n1"})
        assert "err=" in r.headers["location"]
        r = client.post("/account/password", headers=hdr, follow_redirects=False,
                        data={"current_password": "pw", "password": "n1", "confirm": "n2"})
        assert "err=" in r.headers["location"]
        r = client.post("/account/password", headers=hdr, follow_redirects=False,
                        data={"current_password": "pw", "password": "n1", "confirm": "n1"})
        assert "msg=" in r.headers["location"]
        assert settings.auth_cookie_name in r.cookies            # this session re-issued
        with SessionLocal() as s:
            u = s.query(User).filter_by(username="pw-changer").one()
            assert authn.verify_password("n1", u.password_hash) and u.token_version == 1
        # the old token is now revoked
        client.cookies.clear()
        assert client.get("/account", headers=hdr).status_code == 401
    def test_settings_hub_admin_section_only_for_admins():
        """4.1: profiles + administration live under /settings, not the top bar;
        the Administration group only shows to admins (or with auth off)."""
        settings.auth_required = True
        try:
            plain = client.get("/settings", headers=_bearer("hub-plain")).text
            assert "Travel profile" in plain and "Account" in plain
            assert "Administration" not in plain and "/admin/rule-setting" not in plain
            admin = client.get("/settings", headers=_bearer("hub-admin", admin=True)).text
            assert "Administration" in admin and "/users" in admin
            # the top bar carries the daily links (Journal included), not admin ones
            bar = client.get("/trips", headers=_bearer("hub-plain")).text
            assert 'href="/admin/rule-setting/list"' not in bar and 'href="/journal"' in bar
        finally:
            settings.auth_required = False
        # auth off = single-user world: everything, minus user-only entries
        off = client.get("/settings").text
        assert "Administration" in off and "/account" not in off

    def test_every_settings_page_has_the_settings_sidebar():
        """4.1.x: moving between settings pages never means going back to the top
        bar — each has the same sidebar, current page highlighted; the admin
        tables use it instead of SQLAdmin's own. Daily pages don't get it."""
        import re
        settings.auth_required = True
        try:
            h = _bearer("side-admin", admin=True)
            for path, label in [("/account", "Account"),
                                ("/trips/settings/travel-profile", "Travel profile"),
                                ("/review/home", "Home profiles"),
                                ("/trips/settings/packing-profile", "Packing profile"),
                                ("/trips/settings/discovery-profile", "Discovery profile"),
                                ("/users", "Users"),
                                ("/about", "About")]:
                # (the /admin tables aren't mounted in the test app; their
                # sidebar comes from admin_templates/sqladmin/layout.html)
                html = client.get(path, headers=h).text
                assert 'class="yn-set-nav"' in html, path
                assert re.search(r'class="[^"]*\bon\b[^"]*"[^>]*>\s*' + re.escape(label), html), path
            for path in ("/trips", "/review", "/settings"):
                assert 'class="yn-set-nav"' not in client.get(path, headers=h).text, path
            # a regular user's sidebar has no Administration group
            plain = client.get("/account", headers=_bearer("side-plain")).text
            assert "Administration" not in plain and "/admin/rule-setting" not in plain
        finally:
            settings.auth_required = False
