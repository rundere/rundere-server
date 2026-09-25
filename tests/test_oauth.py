"""YONDER-107 — Yonder as an OAuth 2.1 authorization server + personal API tokens.

Metadata, the authorization-code + PKCE flow (local sign-in and OIDC sign-in
with the IdP stubbed), consent, refresh rotation and reuse detection, grant
revocation, DCR, confidential clients, personal tokens, and the rule that the
tokens obey per-user scoping and sharing. SQLite via conftest.
"""
import base64
import hashlib
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
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
    from app import authn, oauth, oauth_routes, oidc
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (AccountEvent, Base, OAuthClient, OAuthCode, OAuthGrant,
                            PersonalApiToken, Trip, User)

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False

    CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
    BASE = "http://testserver"

    @pytest.fixture(autouse=True)
    def _clean(monkeypatch):
        monkeypatch.setattr(settings, "oidc_enabled", False)
        monkeypatch.setattr(settings, "auth_proxy_header_trust", False)
        oauth_routes.register_limiter._hits.clear()
        oauth_routes.token_limiter._hits.clear()
        with SessionLocal() as s:
            uids = [u.id for u in s.query(User).filter(User.username.like("oa-%")).all()]
            s.query(OAuthGrant).filter(OAuthGrant.user_id.in_(uids)).delete(synchronize_session=False)
            s.query(OAuthCode).filter(OAuthCode.user_id.in_(uids)).delete(synchronize_session=False)
            s.query(PersonalApiToken).filter(PersonalApiToken.user_id.in_(uids)).delete(
                synchronize_session=False)
            s.query(AccountEvent).filter(AccountEvent.user_id.in_(uids)).delete(
                synchronize_session=False)
            s.query(Trip).filter(Trip.name.like("OA %")).delete(synchronize_session=False)
            s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
            s.commit()
        yield

    # ------------------------------------------------------------------ helpers
    def _user(name, pw="pw", **kw):
        with SessionLocal() as s:
            u = User(username=name, password_hash=authn.hash_password(pw),
                     auth_source="local", **kw)
            s.add(u)
            s.commit()
            return u.id

    def _client(redirects=(CLAUDE_CB,), confidential=False, name="Claude"):
        with SessionLocal() as s:
            c, secret = oauth.register_client(s, name=name, redirect_uris=list(redirects),
                                              confidential=confidential)
            return c.id, secret

    def _pkce():
        v = secrets.token_urlsafe(48)
        c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        return v, c

    def _login(c, name, pw="pw"):
        r = c.post("/login", data={"username": name, "password": pw, "next": "/"},
                   follow_redirects=False)
        assert r.status_code == 303, r.text

    def _authz_url(cid, challenge, redirect=CLAUDE_CB, state="st-1",
                   resource=f"{BASE}/mcp", method="S256"):
        q = (f"response_type=code&client_id={cid}&redirect_uri={quote(redirect, safe='')}"
             f"&code_challenge={challenge}&code_challenge_method={method}"
             f"&state={state}&scope=yonder+offline_access")
        if resource:
            q += f"&resource={quote(resource, safe='')}"
        return f"/oauth/authorize?{q}"

    def _req_token(html):
        m = re.search(r'name="req" value="([^"]+)"', html)
        assert m, html[:500]
        return m.group(1)

    def _consent(c, url, decision="allow"):
        page = c.get(url, follow_redirects=False)
        assert page.status_code == 200, (page.status_code, page.headers.get("location"))
        r = c.post("/oauth/authorize", data={"req": _req_token(page.text),
                                             "decision": decision},
                   follow_redirects=False)
        assert r.status_code == 303
        loc = urlparse(r.headers["location"])
        return loc, parse_qs(loc.query)

    def _code(c, cid, challenge, **kw):
        loc, q = _consent(c, _authz_url(cid, challenge, **kw))
        assert q["state"] == [kw.get("state", "st-1")]
        return q["code"][0]

    def _exchange(c, cid, code, verifier, redirect=CLAUDE_CB, **extra):
        return c.post("/oauth/token", data={"grant_type": "authorization_code",
                                            "code": code, "redirect_uri": redirect,
                                            "code_verifier": verifier, "client_id": cid,
                                            **extra})

    def _tokens(name="oa-alice"):
        """Full flow for a fresh user → (client_id, token response)."""
        _user(name)
        cid, _ = _client()
        c = TestClient(app)
        _login(c, name)
        v, ch = _pkce()
        r = _exchange(c, cid, _code(c, cid, ch), v)
        assert r.status_code == 200, r.text
        return cid, r.json()

    def _bearer(tok):
        return {"Authorization": f"Bearer {tok}"}

    def _refresh(cid, rt):
        return TestClient(app).post("/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": rt, "client_id": cid})

    # ------------------------------------------------------------------ metadata
    def test_metadata_documents():
        c = TestClient(app)
        pr = c.get("/.well-known/oauth-protected-resource").json()
        assert pr["resource"] == f"{BASE}/mcp"
        assert pr["authorization_servers"] == [BASE]
        assert c.get("/.well-known/oauth-protected-resource/mcp").json() == pr
        md = c.get("/.well-known/oauth-authorization-server").json()
        assert md["issuer"] == BASE
        assert md["authorization_endpoint"] == f"{BASE}/oauth/authorize"
        assert md["token_endpoint"] == f"{BASE}/oauth/token"
        assert md["registration_endpoint"] == f"{BASE}/oauth/register"
        assert md["code_challenge_methods_supported"] == ["S256"]
        assert "none" in md["token_endpoint_auth_methods_supported"]
        assert "refresh_token" in md["grant_types_supported"]

    def test_metadata_uses_public_base_url(monkeypatch):
        monkeypatch.setattr(settings, "public_base_url", "https://yonder.example.com/")
        md = TestClient(app).get("/.well-known/oauth-authorization-server").json()
        assert md["issuer"] == "https://yonder.example.com"
        pr = TestClient(app).get("/.well-known/oauth-protected-resource").json()
        assert pr["resource"] == "https://yonder.example.com/mcp"

    def test_well_known_public_with_auth_required(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        c = TestClient(app)
        assert c.get("/.well-known/oauth-authorization-server").status_code == 200
        assert c.post("/oauth/token", data={"grant_type": "x"}).status_code in (400, 401)

    # ------------------------------------------------------------------ code flow
    def test_full_code_flow_local_sign_in():
        _user("oa-alice")
        cid, _ = _client()
        c = TestClient(app)
        v, ch = _pkce()
        url = _authz_url(cid, ch)
        # Not signed in → the normal login page, with the WHOLE request as next.
        r = c.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
        nxt = parse_qs(urlparse(r.headers["location"]).query)["next"][0]
        assert nxt == url
        login = c.post("/login", data={"username": "oa-alice", "password": "pw",
                                       "next": nxt}, follow_redirects=False)
        assert login.headers["location"] == url
        page = c.get(url)
        assert "Allow <b>Claude</b>" in page.text and "claude.ai" in page.text
        loc, q = _consent(c, url)
        assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CLAUDE_CB
        assert q["state"] == ["st-1"] and q["iss"] == [BASE]
        tok = _exchange(c, cid, q["code"][0], v)
        assert tok.status_code == 200 and tok.headers["cache-control"] == "no-store"
        body = tok.json()
        assert body["token_type"] == "Bearer" and body["expires_in"] == 3600
        assert body["refresh_token"].startswith("yrt_")
        claims = authn.jwt_decode(body["access_token"])
        assert claims["typ"] == "oauth" and claims["aud"] == f"{BASE}/mcp" and claims["gid"]
        me = TestClient(app).get("/api/v1/trips", headers=_bearer(body["access_token"]))
        assert me.status_code == 200
        with SessionLocal() as s:
            uid = s.query(User).filter_by(username="oa-alice").one().id
            assert s.query(AccountEvent).filter_by(user_id=uid, action="oauth_consent").count() == 1

    def test_wrong_verifier_fails():
        _user("oa-alice")
        cid, _ = _client()
        c = TestClient(app)
        _login(c, "oa-alice")
        v, ch = _pkce()
        r = _exchange(c, cid, _code(c, cid, ch), _pkce()[0])
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"

    def test_reused_code_fails_and_revokes_grant():
        _user("oa-alice")
        cid, _ = _client()
        c = TestClient(app)
        _login(c, "oa-alice")
        v, ch = _pkce()
        code = _code(c, cid, ch)
        first = _exchange(c, cid, code, v)
        assert first.status_code == 200
        again = _exchange(c, cid, code, v)
        assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
        # the replay revoked what the first exchange produced
        at = first.json()["access_token"]
        assert TestClient(app).get("/api/v1/trips", headers=_bearer(at)).status_code == 401
        assert _refresh(cid, first.json()["refresh_token"]).json()["error"] == "invalid_grant"

    def test_expired_code_fails():
        _user("oa-alice")
        cid, _ = _client()
        c = TestClient(app)
        _login(c, "oa-alice")
        v, ch = _pkce()
        code = _code(c, cid, ch)
        with SessionLocal() as s:
            row = s.query(OAuthCode).filter_by(code_hash=oauth._hash(code)).one()
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            s.commit()
        r = _exchange(c, cid, code, v)
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"

    def test_mismatched_redirect_fails():
        _user("oa-alice")
        cid, _ = _client(redirects=(CLAUDE_CB, "https://other.example.com/cb"))
        c = TestClient(app)
        _login(c, "oa-alice")
        v, ch = _pkce()
        r = _exchange(c, cid, _code(c, cid, ch), v, redirect="https://other.example.com/cb")
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"

    def test_code_is_bound_to_client():
        _user("oa-alice")
        cid, _ = _client()
        other, _ = _client(name="Other")
        c = TestClient(app)
        _login(c, "oa-alice")
        v, ch = _pkce()
        r = _exchange(c, other, _code(c, cid, ch), v)
        assert r.json()["error"] == "invalid_grant"

    def test_unregistered_redirect_never_redirects():
        _user("oa-alice")
        cid, _ = _client()
        c = TestClient(app)
        _login(c, "oa-alice")
        _, ch = _pkce()
        r = c.get(_authz_url(cid, ch, redirect="https://evil.example.com/cb"),
                  follow_redirects=False)
        assert r.status_code == 400 and "location" not in r.headers
        r = c.get(_authz_url("yc_nope", ch), follow_redirects=False)
        assert r.status_code == 400 and "location" not in r.headers

    def test_pkce_required():
        _user("oa-alice")
        cid, _ = _client()
        c = TestClient(app)
        _login(c, "oa-alice")
        r = c.get(_authz_url(cid, "abc", method="plain"), follow_redirects=False)
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert r.status_code == 303 and q["error"] == ["invalid_request"]

    def test_foreign_resource_rejected():
        _user("oa-alice")
        cid, _ = _client()
        c = TestClient(app)
        _login(c, "oa-alice")
        _, ch = _pkce()
        r = c.get(_authz_url(cid, ch, resource="https://elsewhere.example.com/mcp"),
                  follow_redirects=False)
        assert parse_qs(urlparse(r.headers["location"]).query)["error"] == ["invalid_target"]

    def test_consent_deny():
        _user("oa-alice")
        cid, _ = _client()
        c = TestClient(app)
        _login(c, "oa-alice")
        _, ch = _pkce()
        loc, q = _consent(c, _authz_url(cid, ch), decision="deny")
        assert q["error"] == ["access_denied"] and q["state"] == ["st-1"] and "code" not in q

    def test_consent_token_bound_to_user():
        _user("oa-alice")
        _user("oa-bob")
        cid, _ = _client()
        a = TestClient(app)
        _login(a, "oa-alice")
        _, ch = _pkce()
        req = _req_token(a.get(_authz_url(cid, ch)).text)
        b = TestClient(app)
        _login(b, "oa-bob")
        r = b.post("/oauth/authorize", data={"req": req, "decision": "allow"},
                   follow_redirects=False)
        assert r.status_code == 400 and "location" not in r.headers

    def test_sharing_prompt_first():
        _user("oa-new", sharing_prompt=True)
        cid, _ = _client()
        c = TestClient(app)
        _login(c, "oa-new")
        _, ch = _pkce()
        r = c.get(_authz_url(cid, ch), follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/account/sharing?next=")

    def test_loopback_redirect_any_port():
        _user("oa-alice")
        cid, _ = _client(redirects=("http://localhost/callback", "http://127.0.0.1/callback"))
        c = TestClient(app)
        _login(c, "oa-alice")
        v, ch = _pkce()
        cb = "http://localhost:3118/callback"
        page = c.get(_authz_url(cid, ch, redirect=cb))
        assert page.status_code == 200 and "only returns to your own computer" in page.text
        loc, q = _consent(c, _authz_url(cid, ch, redirect=cb))
        assert loc.port == 3118
        assert _exchange(c, cid, q["code"][0], v, redirect=cb).status_code == 200
        bad = c.get(_authz_url(cid, ch, redirect="http://localhost:3118/other"),
                    follow_redirects=False)
        assert bad.status_code == 400

    # ------------------------------------------------------------------ OIDC sign-in mode
    def test_code_flow_via_oidc_sign_in(monkeypatch):
        monkeypatch.setattr(settings, "oidc_enabled", True)
        monkeypatch.setattr(settings, "oidc_issuer", "https://idp.test")
        monkeypatch.setattr(settings, "oidc_client_id", "yonder")
        monkeypatch.setattr(settings, "oidc_client_secret", "shh")
        monkeypatch.setattr(oidc, "discovery", lambda: {
            "issuer": "https://idp.test", "authorization_endpoint": "https://idp.test/authorize",
            "token_endpoint": "https://idp.test/token"})
        monkeypatch.setattr(oidc, "exchange_code", lambda code, uri, verifier: {"id_token": "h.p.s"})
        monkeypatch.setattr(oidc, "decode_id_token",
                            lambda t: {"sub": "oa-sso-1", "preferred_username": "oa-sso"})
        monkeypatch.setattr(oidc, "validate_claims", lambda claims, nonce: None)
        with SessionLocal() as s:           # an existing SSO account (no sharing prompt)
            s.add(User(username="oa-sso", auth_source="oidc", external_subject="oidc:oa-sso-1"))
            s.commit()
        cid, _ = _client()
        c = TestClient(app)
        v, ch = _pkce()
        url = _authz_url(cid, ch)
        r = c.get(url, follow_redirects=False)
        login_url = r.headers["location"]
        page = c.get(login_url)
        assert "Sign in with" in page.text
        nxt = parse_qs(urlparse(login_url).query)["next"][0]
        r = c.get(f"/oidc/login?next={quote(nxt, safe='')}", follow_redirects=False)
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        cb = c.get(f"/oidc/callback?code=x&state={state}", follow_redirects=False)
        assert cb.status_code == 303 and cb.headers["location"] == url
        loc, q = _consent(c, url)
        tok = _exchange(c, cid, q["code"][0], v)
        assert tok.status_code == 200
        me = TestClient(app).get("/api/v1/auth/me", headers=_bearer(tok.json()["access_token"]))
        assert me.status_code == 200 and me.json()["username"] == "oa-sso"
        assert TestClient(app).get("/api/v1/trips",
                                   headers=_bearer(tok.json()["access_token"])).status_code == 200

    def test_local_mode_login_page_has_no_sso_button():
        cid, _ = _client()
        _, ch = _pkce()
        c = TestClient(app)
        r = c.get(_authz_url(cid, ch), follow_redirects=False)
        page = c.get(r.headers["location"])
        assert page.status_code == 200 and "Sign in with" not in page.text

    # ------------------------------------------------------------------ refresh
    def test_refresh_rotates_and_reuse_revokes_grant():
        cid, t1 = _tokens()
        r2 = _refresh(cid, t1["refresh_token"])
        assert r2.status_code == 200
        t2 = r2.json()
        assert t2["refresh_token"] != t1["refresh_token"]
        assert TestClient(app).get("/api/v1/trips", headers=_bearer(t2["access_token"])).status_code == 200
        # replaying the retired token = theft → the whole grant dies
        bad = _refresh(cid, t1["refresh_token"])
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
        assert _refresh(cid, t2["refresh_token"]).json()["error"] == "invalid_grant"
        assert TestClient(app).get("/api/v1/trips", headers=_bearer(t2["access_token"])).status_code == 401
        with SessionLocal() as s:
            assert s.query(AccountEvent).filter_by(action="oauth_refresh_reuse").count() >= 1

    def test_refresh_slides_expiry_and_expired_grant_fails():
        cid, t1 = _tokens()
        with SessionLocal() as s:
            g = s.query(OAuthGrant).filter_by(client_id=cid).one()
            g.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            s.commit()
        assert _refresh(cid, t1["refresh_token"]).json()["error"] == "invalid_grant"

    def test_refresh_bound_to_client():
        cid, t1 = _tokens()
        other, _ = _client(name="Other")
        assert _refresh(other, t1["refresh_token"]).json()["error"] == "invalid_grant"

    # ------------------------------------------------------------------ revocation
    def test_revoke_via_account_page():
        cid, t = _tokens()
        c = TestClient(app)
        _login(c, "oa-alice")
        page = c.get("/account")
        assert "Connected apps" in page.text and "Claude" in page.text
        with SessionLocal() as s:
            gid = s.query(OAuthGrant).filter_by(client_id=cid).one().id
        r = c.post(f"/account/connected-apps/{gid}/revoke", follow_redirects=False)
        assert r.status_code == 303 and "Disconnected" in r.headers["location"]
        assert TestClient(app).get("/api/v1/trips", headers=_bearer(t["access_token"])).status_code == 401
        assert _refresh(cid, t["refresh_token"]).json()["error"] == "invalid_grant"
        assert "No connected apps" in c.get("/account").text
        with SessionLocal() as s:
            assert s.query(AccountEvent).filter_by(action="oauth_revoked").count() >= 1

    def test_cannot_revoke_someone_elses_grant():
        cid, t = _tokens()
        _user("oa-bob")
        c = TestClient(app)
        _login(c, "oa-bob")
        with SessionLocal() as s:
            gid = s.query(OAuthGrant).filter_by(client_id=cid).one().id
        c.post(f"/account/connected-apps/{gid}/revoke")
        assert TestClient(app).get("/api/v1/trips", headers=_bearer(t["access_token"])).status_code == 200

    def test_rfc7009_revoke_endpoint():
        cid, t = _tokens()
        r = TestClient(app).post("/oauth/revoke", data={"token": t["refresh_token"],
                                                        "client_id": cid})
        assert r.status_code == 200
        assert _refresh(cid, t["refresh_token"]).json()["error"] == "invalid_grant"

    def test_token_version_bump_kills_oauth_tokens():
        cid, t = _tokens()
        with SessionLocal() as s:
            u = s.query(User).filter_by(username="oa-alice").one()
            u.token_version += 1
            s.commit()
        assert TestClient(app).get("/api/v1/trips", headers=_bearer(t["access_token"])).status_code == 401
        assert _refresh(cid, t["refresh_token"]).json()["error"] == "invalid_grant"

    def test_deactivation_kills_oauth_tokens():
        cid, t = _tokens()
        with SessionLocal() as s:
            s.query(User).filter_by(username="oa-alice").one().is_active = False
            s.commit()
        assert TestClient(app).get("/api/v1/trips", headers=_bearer(t["access_token"])).status_code == 401
        assert _refresh(cid, t["refresh_token"]).json()["error"] == "invalid_grant"

    def test_audience_is_checked(monkeypatch):
        cid, t = _tokens()
        monkeypatch.setattr(settings, "public_base_url", "https://another.example.com")
        assert TestClient(app).get("/api/v1/trips", headers=_bearer(t["access_token"])).status_code == 401

    # ------------------------------------------------------------------ DCR
    def test_dcr_happy_path():
        c = TestClient(app)
        r = c.post("/oauth/register", json={"client_name": "Claude",
                                            "redirect_uris": [CLAUDE_CB],
                                            "token_endpoint_auth_method": "none",
                                            "grant_types": ["authorization_code", "refresh_token"],
                                            "response_types": ["code"]})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["client_id"].startswith("yc_") and "client_secret" not in body
        assert body["redirect_uris"] == [CLAUDE_CB]
        with SessionLocal() as s:
            row = s.get(OAuthClient, body["client_id"])
            assert row.name == "Claude" and row.secret_hash is None and row.source == "dcr"
        # registering alone grants nothing: the flow still needs sign-in + consent
        _, ch = _pkce()
        r = c.get(_authz_url(body["client_id"], ch), follow_redirects=False)
        assert r.headers["location"].startswith("/login")

    def test_dcr_bad_redirect():
        c = TestClient(app)
        for uris in (["http://evil.example.com/cb"], [], "https://x.example.com",
                     ["javascript:alert(1)"], ["https://x.example.com/cb#frag"]):
            r = c.post("/oauth/register", json={"client_name": "x", "redirect_uris": uris})
            assert r.status_code == 400, uris
            assert r.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")
        ok = c.post("/oauth/register", json={"redirect_uris": ["http://127.0.0.1/callback"]})
        assert ok.status_code == 201

    def test_dcr_rate_limited():
        c = TestClient(app)
        codes = [c.post("/oauth/register", json={"redirect_uris": [CLAUDE_CB]}).status_code
                 for _ in range(settings.oauth_register_rate_limit + 1)]
        assert codes[-1] == 429 and codes[0] == 201

    def test_dcr_confidential_client():
        r = TestClient(app).post("/oauth/register", json={
            "redirect_uris": [CLAUDE_CB], "token_endpoint_auth_method": "client_secret_post"})
        assert r.status_code == 201 and r.json()["client_secret"].startswith("ycs_")

    # ------------------------------------------------------------------ confidential client
    def test_confidential_client_secret():
        _user("oa-alice")
        cid, secret = _client(confidential=True)
        c = TestClient(app)
        _login(c, "oa-alice")
        v, ch = _pkce()
        code = _code(c, cid, ch)
        # no secret → invalid_client (and the code is not consumed by that)
        r = _exchange(c, cid, code, v)
        assert r.status_code == 401 and r.json()["error"] == "invalid_client"
        r = _exchange(c, cid, code, v, client_secret="wrong")
        assert r.status_code == 401
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        r = c.post("/oauth/token", headers={"Authorization": f"Basic {basic}"},
                   data={"grant_type": "authorization_code", "code": code,
                         "redirect_uri": CLAUDE_CB, "code_verifier": v})
        assert r.status_code == 200, r.text
        rt = r.json()["refresh_token"]
        r = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": rt,
                                         "client_id": cid, "client_secret": secret})
        assert r.status_code == 200

    def test_cli_oauth_client_create(capsys):
        from app import cli
        assert cli.main(["oauth-client", "create", "Claude (static)",
                         "--redirect", CLAUDE_CB, "--confidential"]) == 0
        out = capsys.readouterr().out
        cid = re.search(r"client_id:\s+(\S+)", out).group(1)
        assert re.search(r"client_secret:\s+ycs_", out)
        with SessionLocal() as s:
            row = s.get(OAuthClient, cid)
            assert row.source == "cli" and row.secret_hash and row.redirect_uris == [CLAUDE_CB]
        assert cli.main(["oauth-client", "create", "bad", "--redirect", "http://x.example.com"]) == 1
        assert cli.main(["oauth-client", "list"]) == 0

    # ------------------------------------------------------------------ personal API tokens
    def _make_pat(c, name="laptop", days="90"):
        r = c.post("/account/tokens", data={"name": name, "days": days})
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        m = re.search(r"(ypat_[A-Za-z0-9_\-]+)", r.text)
        assert m
        return m.group(1)

    def test_personal_token_lifecycle():
        _user("oa-alice")
        c = TestClient(app)
        _login(c, "oa-alice")
        tok = _make_pat(c)
        api = TestClient(app)
        assert api.get("/api/v1/trips", headers=_bearer(tok)).status_code == 200
        page = c.get("/account").text
        assert "laptop" in page and tok not in page        # shown once only
        with SessionLocal() as s:
            row = s.query(PersonalApiToken).filter_by(name="laptop").one()
            assert row.token_hash == hashlib.sha256(tok.encode()).hexdigest()
            assert row.last_used_at is not None and row.expires_at is not None
            assert s.query(AccountEvent).filter_by(user_id=row.user_id, action="pat_created").count() == 1
        r = c.post(f"/account/tokens/{row.id}/revoke", follow_redirects=False)
        assert r.status_code == 303
        assert api.get("/api/v1/trips", headers=_bearer(tok)).status_code == 401
        with SessionLocal() as s:
            assert s.query(AccountEvent).filter_by(action="pat_revoked").count() >= 1

    def test_personal_token_expiry_and_signout():
        _user("oa-alice")
        c = TestClient(app)
        _login(c, "oa-alice")
        t1, t2 = _make_pat(c, "one", "30"), _make_pat(c, "two", "")
        api = TestClient(app)
        with SessionLocal() as s:
            row = s.query(PersonalApiToken).filter_by(name="one").one()
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            assert s.query(PersonalApiToken).filter_by(name="two").one().expires_at is None
            s.commit()
        assert api.get("/api/v1/trips", headers=_bearer(t1)).status_code == 401
        assert api.get("/api/v1/trips", headers=_bearer(t2)).status_code == 200
        with SessionLocal() as s:
            s.query(User).filter_by(username="oa-alice").one().token_version += 1
            s.commit()
        assert api.get("/api/v1/trips", headers=_bearer(t2)).status_code == 401

    def test_cannot_revoke_someone_elses_pat():
        _user("oa-alice")
        _user("oa-bob")
        a = TestClient(app)
        _login(a, "oa-alice")
        tok = _make_pat(a)
        with SessionLocal() as s:
            tid = s.query(PersonalApiToken).filter_by(name="laptop").one().id
        b = TestClient(app)
        _login(b, "oa-bob")
        b.post(f"/account/tokens/{tid}/revoke")
        assert TestClient(app).get("/api/v1/trips", headers=_bearer(tok)).status_code == 200

    def test_cli_mint_api_token_still_works():
        uid = _user("oa-alice")
        with SessionLocal() as s:
            tok = authn.issue_api_token(s.get(User, uid))
        assert TestClient(app).get("/api/v1/trips", headers=_bearer(tok)).status_code == 200

    # ------------------------------------------------------------------ scoping + proxy
    def test_oauth_token_obeys_sharing_rules():
        """An admin's OAuth token never shows a user's opted-out private trip."""
        _user("oa-admin", is_admin=True)
        with SessionLocal() as s:
            bob = User(username="oa-bob", password_hash=authn.hash_password("pw"),
                       auth_source="local", share_with_admins=False)
            carol = User(username="oa-carol", password_hash=authn.hash_password("pw"),
                         auth_source="local")
            s.add_all([bob, carol]); s.flush()
            s.add_all([Trip(name="OA Bob Private", source="manual", owner_id=bob.id),
                       Trip(name="OA Carol Shared", source="manual", owner_id=carol.id)])
            s.commit()
        cid, _ = _client()
        c = TestClient(app)
        _login(c, "oa-admin")
        v, ch = _pkce()
        at = _exchange(c, cid, _code(c, cid, ch), v).json()["access_token"]
        names = {t["name"] for t in TestClient(app).get("/api/v1/trips",
                                                        headers=_bearer(at)).json()}
        assert "OA Carol Shared" in names and "OA Bob Private" not in names

    def test_proxy_header_never_honoured_on_machine_endpoints(monkeypatch):
        monkeypatch.setattr(settings, "auth_proxy_header_trust", True)
        _user("oa-alice")
        hdr = {"x-authentik-username": "oa-alice"}
        api = TestClient(app)
        assert api.get("/api/v1/trips", headers=hdr).status_code == 401
        assert api.post("/oauth/token", headers=hdr,
                        data={"grant_type": "refresh_token"}).status_code == 401
        # …but the browser consent page does use the proxy identity
        cid, _ = _client()
        _, ch = _pkce()
        page = TestClient(app).get(_authz_url(cid, ch), headers=hdr, follow_redirects=False)
        assert page.status_code == 200 and "oa-alice" in page.text

    def test_is_machine_path():
        for p in ("/mcp", "/mcp/", "/api/v1/trips", "/oauth/token", "/oauth/register",
                  "/oauth/revoke", "/.well-known/oauth-authorization-server"):
            assert authn.is_machine_path(p), p
        for p in ("/oauth/authorize", "/login", "/account", "/trips"):
            assert not authn.is_machine_path(p), p
