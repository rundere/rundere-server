"""YONDER-101 — sharing + email over the API (SPEC-4.2 §3.3).

The user payload carries email / share_with_admins / sharing_prompt /
opt_out_blocker / reset_note; PUT /api/v1/auth/me/sharing and /me/email follow the web's
rules, are audited, and accept only first-party credentials (the app's login
token or the web session) — never a connected app or an API token.
"""
import sys
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
    from app import authn, oauth
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import AccountEvent, Base, OAuthGrant, PersonalApiToken, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    @pytest.fixture(autouse=True)
    def _clean(monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", "")
        monkeypatch.setattr(settings, "smtp_from", "")
        with SessionLocal() as s:
            uids = [u.id for u in s.query(User).filter(User.username.like("sh-%")).all()]
            for m in (AccountEvent, OAuthGrant, PersonalApiToken):
                s.query(m).filter(m.user_id.in_(uids)).delete(synchronize_session=False)
            s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
            s.commit()
        yield

    def _smtp(monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_from", "yonder@example.com")

    def _user(name="sh-alice", **kw):
        with SessionLocal() as s:
            u = User(username=name, password_hash=authn.hash_password("pw"),
                     auth_source=kw.pop("auth_source", "local"), **kw)
            s.add(u)
            s.commit()
            return u.id

    def _login(name="sh-alice"):
        r = client.post("/api/v1/auth/login", json={"username": name, "password": "pw"})
        assert r.status_code == 200
        return r.json()

    def _h(tok):
        return {"Authorization": f"Bearer {tok}"}

    def test_user_payload_has_sharing_fields():
        _user(email="a@example.com", sharing_prompt=True)
        body = _login()
        u = body["user"]
        assert u["email"] == "a@example.com" and u["share_with_admins"] is False   # D-220
        assert u["sharing_prompt"] is True
        assert u["opt_out_blocker"] == ""                            # never blocked now
        assert "Email isn't set up" in u["reset_note"]               # no SMTP here
        me = client.get("/api/v1/auth/me", headers=_h(body["access"])).json()
        assert me == u
        r = client.post("/api/v1/auth/refresh", json={"refresh": body["refresh"]}).json()
        assert r["user"]["sharing_prompt"] is True

    def test_going_private_needs_no_smtp_or_email_and_is_audited():
        """D-220: an opted-in user can always go private again; the payload's
        reset_note then says what a forgotten password means."""
        _user(share_with_admins=True)
        tok = _login()["access"]
        r = client.put("/api/v1/auth/me/sharing", headers=_h(tok), json={"share": False})
        assert r.status_code == 200 and "Email isn't set up" in r.json()["reset_note"]
        assert r.status_code == 200
        assert r.json()["share_with_admins"] is False and r.json()["sharing_prompt"] is False
        with SessionLocal() as s:
            uid = s.query(User).filter_by(username="sh-alice").one().id
            assert s.query(AccountEvent).filter_by(user_id=uid, action="sharing_off").count() == 1
        r = client.put("/api/v1/auth/me/sharing", headers=_h(tok), json={"share": True})
        assert r.status_code == 200 and r.json()["share_with_admins"] is True

    def test_answering_the_prompt_clears_it():
        _user(sharing_prompt=True)
        tok = _login()["access"]
        r = client.put("/api/v1/auth/me/sharing", headers=_h(tok), json={"share": True})
        assert r.status_code == 200 and r.json()["sharing_prompt"] is False

    def test_email_rules(monkeypatch):
        _user()
        _user("sh-bob", email="taken@example.com")
        tok = _login()["access"]
        url = "/api/v1/auth/me/email"
        assert client.put(url, headers=_h(tok),
                          json={"email": "x@example.com", "current_password": "nope"}).status_code == 403
        assert client.put(url, headers=_h(tok), json={"email": "x@example.com"}).status_code == 403
        r = client.put(url, headers=_h(tok), json={"email": "not-an-email", "current_password": "pw"})
        assert r.status_code == 400
        r = client.put(url, headers=_h(tok), json={"email": "TAKEN@example.com", "current_password": "pw"})
        assert r.status_code == 409
        r = client.put(url, headers=_h(tok), json={"email": "new@example.com", "current_password": "pw"})
        assert r.status_code == 200 and r.json()["email"] == "new@example.com"
        _smtp(monkeypatch)
        assert client.put(url, headers=_h(tok), json={
            "email": "new@example.com", "current_password": "pw"}).json()["reset_note"] == ""
        # a local account can change its email but never remove it (D-220)
        r = client.put(url, headers=_h(tok), json={"email": "", "current_password": "pw"})
        assert r.status_code == 400 and "don't remove it" in r.json()["detail"]

    def test_sso_account_needs_no_password_or_smtp():
        _user(auth_source="oidc")
        with SessionLocal() as s:
            tok = authn.issue_access_token(s.query(User).filter_by(username="sh-alice").one())
        r = client.put("/api/v1/auth/me/email", headers=_h(tok), json={"email": "sso@example.com"})
        assert r.status_code == 200
        r = client.put("/api/v1/auth/me/sharing", headers=_h(tok), json={"share": False})
        assert r.status_code == 200 and r.json()["share_with_admins"] is False

    def test_connected_apps_and_api_tokens_cannot_change_settings():
        uid = _user(email="a@example.com", share_with_admins=True)
        with SessionLocal() as s:
            u = s.get(User, uid)
            pat = oauth.create_pat(s, u, "laptop")[1]
            cl, _ = oauth.register_client(s, name="Perplexica",
                                          redirect_uris=["https://p.example.com/cb"])
            g = OAuthGrant(client_id=cl.id, user_id=uid, token_version=u.token_version,
                           expires_at=oauth._now() + oauth.timedelta(days=1))
            s.add(g); s.commit()
            agent = oauth.issue_access(u, g, "http://testserver/mcp")
        for tok in (pat, agent):
            assert client.get("/api/v1/auth/me", headers=_h(tok)).status_code == 200   # can read
            r = client.put("/api/v1/auth/me/sharing", headers=_h(tok), json={"share": False})
            assert r.status_code == 403 and "connected apps" in r.json()["detail"]
            r = client.put("/api/v1/auth/me/email", headers=_h(tok),
                           json={"email": "evil@example.com", "current_password": "pw"})
            assert r.status_code == 403
        with SessionLocal() as s:
            u = s.get(User, uid)
            assert u.email == "a@example.com" and u.share_with_admins

    def test_web_session_counts_as_first_party():
        _user(email="a@example.com")
        web = TestClient(app)
        assert web.post("/login", data={"username": "sh-alice", "password": "pw", "next": "/"},
                        follow_redirects=False).status_code == 303
        r = web.put("/api/v1/auth/me/email",
                    json={"email": "web@example.com", "current_password": "pw"})
        assert r.status_code == 200 and r.json()["email"] == "web@example.com"
