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
