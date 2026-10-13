"""The Run·dere iOS app's built-in OAuth client (YONDER-235, SPEC-8 D-194).

The app signs in through the server's own OAuth (PKCE) on any host — the web
sign-in sheet shows a password form or SSO, whatever that host offers — and its
tokens are first-party: no consent screen, and they may change account settings
like the app's password login can. No other client can be first-party.
"""
import base64
import hashlib
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import authn, oauth, oauth_routes
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import AccountEvent, Base, OAuthClient, OAuthCode, OAuthGrant, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False

    APP_CB = "https://rundere.app/auth/callback"
    BASE = "http://testserver"

    @pytest.fixture(autouse=True)
    def _clean(monkeypatch):
        monkeypatch.setattr(settings, "oidc_enabled", False)
        monkeypatch.setattr(settings, "auth_proxy_header_trust", False)
        monkeypatch.setattr(settings, "oauth_app_redirect_uris", APP_CB)
        oauth_routes.token_limiter._hits.clear()
        with SessionLocal() as s:
            uids = [u.id for u in s.query(User).filter(User.username.like("oapp-%")).all()]
            for model in (OAuthGrant, OAuthCode, AccountEvent):
                s.query(model).filter(model.user_id.in_(uids)).delete(synchronize_session=False)
            s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
            s.commit()
            oauth.ensure_app_client(s)
        yield

    def _user(name, email=None):
        with SessionLocal() as s:
            u = User(username=name, password_hash=authn.hash_password("pw"),
                     auth_source="local", email=email, share_with_admins=True)
            s.add(u)
            s.commit()
            return u.id

    def _pkce():
        v = secrets.token_urlsafe(48)
        return v, base64.urlsafe_b64encode(
            hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()

    def _signed_in(name):
        c = TestClient(app)
        r = c.post("/login", data={"username": name, "password": "pw", "next": "/"},
                   follow_redirects=False)
        assert r.status_code == 303
        return c

    def _authorize(c, cid=oauth.APP_CLIENT_ID, redirect=APP_CB, challenge="x" * 43):
        return c.get(f"/oauth/authorize?response_type=code&client_id={cid}"
                     f"&redirect_uri={quote(redirect, safe='')}&code_challenge={challenge}"
                     f"&code_challenge_method=S256&state=s1"
                     f"&resource={quote(BASE, safe='')}", follow_redirects=False)

    def _app_tokens(name):
        c = _signed_in(name)
        v, ch = _pkce()
        r = _authorize(c, challenge=ch)
        # no consent page: straight back to the app's universal link with a code
        assert r.status_code == 303, r.text[:300]
        loc = urlparse(r.headers["location"])
        assert f"{loc.scheme}://{loc.netloc}{loc.path}" == APP_CB
        q = parse_qs(loc.query)
        assert q["state"] == ["s1"]
        t = TestClient(app).post("/oauth/token", data={
            "grant_type": "authorization_code", "code": q["code"][0],
            "redirect_uri": APP_CB, "code_verifier": v,
            "client_id": oauth.APP_CLIENT_ID, "resource": BASE})
        assert t.status_code == 200, t.text
        return t.json()

    def _h(tok):
        return {"Authorization": f"Bearer {tok}"}

    # ------------------------------------------------------------------ the client
    def test_app_client_is_built_in_public_and_first_party():
        with SessionLocal() as s:
            c = s.get(OAuthClient, oauth.APP_CLIENT_ID)
            assert c.first_party and c.secret_hash is None and c.source == "builtin"
            assert c.redirect_uris == [APP_CB]
            # idempotent, and the setting stays authoritative
            settings.oauth_app_redirect_uris = f"{APP_CB}, https://fork.example/auth/cb"
            try:
                oauth.ensure_app_client(s)
                s.refresh(c)
                assert c.redirect_uris == [APP_CB, "https://fork.example/auth/cb"]
            finally:
                settings.oauth_app_redirect_uris = APP_CB
                oauth.ensure_app_client(s)

    def test_a_non_https_app_redirect_is_ignored(monkeypatch):
        monkeypatch.setattr(settings, "oauth_app_redirect_uris", "rundere://auth, " + APP_CB)
        assert oauth.app_redirect_uris() == [APP_CB]

    def test_registration_can_never_make_a_first_party_client():
        r = TestClient(app).post("/oauth/register", json={
            "client_name": "Run·dere for iOS", "redirect_uris": [APP_CB],
            "first_party": True, "client_id": oauth.APP_CLIENT_ID})
        assert r.status_code == 201
        body = r.json()
        assert body["client_id"] != oauth.APP_CLIENT_ID
        with SessionLocal() as s:
            assert s.get(OAuthClient, body["client_id"]).first_party is False
            s.query(OAuthClient).filter_by(id=body["client_id"]).delete()
            s.commit()

    # ------------------------------------------------------------------ the flow
    def test_app_sign_in_skips_consent_and_its_token_reads_the_api():
        _user("oapp-alice")
        tok = _app_tokens("oapp-alice")
        assert tok["refresh_token"].startswith("yrt_")
        me = TestClient(app).get("/api/v1/auth/me", headers=_h(tok["access_token"]))
        assert me.status_code == 200 and me.json()["username"] == "oapp-alice"
        with SessionLocal() as s:
            uid = s.query(User).filter_by(username="oapp-alice").one().id
            assert s.query(AccountEvent).filter_by(user_id=uid, action="oauth_sign_in").count() == 1

    def test_other_clients_still_get_the_consent_page():
        _user("oapp-carol")
        with SessionLocal() as s:
            other, _ = oauth.register_client(s, name="Lookalike",
                                             redirect_uris=["https://evil.example/cb"])
            cid = other.id
        try:
            r = _authorize(_signed_in("oapp-carol"), cid=cid, redirect="https://evil.example/cb")
            assert r.status_code == 200 and 'name="req"' in r.text
        finally:
            with SessionLocal() as s:
                s.query(OAuthClient).filter_by(id=cid).delete()
                s.commit()

    def test_app_client_only_redirects_to_its_registered_link():
        _user("oapp-dave")
        r = _authorize(_signed_in("oapp-dave"), redirect="https://evil.example/auth/callback")
        assert r.status_code == 400 and "redirect address" in r.text

    def test_signed_out_app_sign_in_goes_through_the_hosts_login():
        r = _authorize(TestClient(app))
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")

    def test_app_oauth_token_is_first_party_other_oauth_is_not():
        uid = _user("oapp-erin", email="erin@example.com")
        tok = _app_tokens("oapp-erin")["access_token"]
        c = TestClient(app)
        r = c.put("/api/v1/auth/me/email", headers=_h(tok),
                  json={"email": "erin2@example.com", "current_password": "pw"})
        assert r.status_code == 200 and r.json()["email"] == "erin2@example.com"
        # an agent's OAuth token for the same user stays third-party
        with SessionLocal() as s:
            u = s.get(User, uid)
            cl, _ = oauth.register_client(s, name="Agent", redirect_uris=["https://a.example/cb"])
            g = OAuthGrant(client_id=cl.id, user_id=uid, token_version=u.token_version,
                           expires_at=oauth._now() + oauth.timedelta(days=1))
            s.add(g)
            s.commit()
            agent = oauth.issue_access(u, g, f"{BASE}/mcp")
            cid = cl.id
        try:
            r = c.put("/api/v1/auth/me/sharing", headers=_h(agent), json={"share": False})
            assert r.status_code == 403
        finally:
            with SessionLocal() as s:
                s.query(OAuthGrant).filter_by(client_id=cid).delete()
                s.query(OAuthClient).filter_by(id=cid).delete()
                s.commit()

    def test_revoked_app_grant_is_not_first_party_or_anything():
        _user("oapp-fay")
        tok = _app_tokens("oapp-fay")
        c = TestClient(app)
        c.post("/oauth/revoke", data={"token": tok["refresh_token"],
                                      "client_id": oauth.APP_CLIENT_ID})
        r = c.put("/api/v1/auth/me/sharing", headers=_h(tok["access_token"]),
                  json={"share": True})
        assert r.status_code == 401

    def test_app_grant_listed_as_a_signed_in_device_and_disconnectable():
        _user("oapp-gus")
        _app_tokens("oapp-gus")
        web = _signed_in("oapp-gus")
        page = web.get("/account").text
        assert "Run·dere for iOS" in page and "signed-in device" in page

    def test_one_users_app_grant_is_invisible_to_another():
        _user("oapp-hal")
        _user("oapp-ivy")
        _app_tokens("oapp-hal")
        page = _signed_in("oapp-ivy").get("/account").text
        assert "signed-in device" not in page

    # ------------------------------------------------------------------ discovery
    def test_auth_methods_names_the_app_client(monkeypatch):
        m = TestClient(app).get("/api/v1/auth/methods").json()
        assert m["oauth"] is True and m["oauth_client_id"] == oauth.APP_CLIENT_ID
        assert m["password"] is True          # both ways in; the app picks
        monkeypatch.setattr(settings, "oauth_app_redirect_uris", "")
        with SessionLocal() as s:
            oauth.ensure_app_client(s)
        m = TestClient(app).get("/api/v1/auth/methods").json()
        assert m["oauth_client_id"] is None
