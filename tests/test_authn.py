"""Yonder 4.0 Phase 1a — auth core (SPEC-4 Workstream B).

Runs against SQLite via the conftest TX_DATABASE_URL override; needs
fastapi/httpx (skips otherwise, same as test_ingest). Covers the primitives
(scrypt, HS256 JWT, refresh rotation), the providers (local + proxy), and the
HTTP surface (web login cookie flow, /api/v1/auth/*, the auth gate + admin gate).
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
    from app.models import Base, TravelerIdentity, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False        # tests talk plain http
    client = TestClient(app)

    def _mk_user(username, password="pw-correct-horse", admin=False, active=True):
        with SessionLocal() as s:
            existing = s.query(User).filter_by(username=username).one_or_none()
            if existing:
                s.delete(existing)
                s.commit()
            u = User(username=username, password_hash=authn.hash_password(password),
                     auth_source="local", is_admin=admin, is_active=active)
            s.add(u)
            s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    # ---------------------------------------------------------------- primitives
    def test_password_roundtrip():
        h = authn.hash_password("s3cret!")
        assert h.startswith("scrypt$")
        assert authn.verify_password("s3cret!", h)
        assert not authn.verify_password("wrong", h)
        assert not authn.verify_password("s3cret!", None)
        assert not authn.verify_password("s3cret!", "notscrypt")

    def test_jwt_roundtrip_tamper_expiry():
        u = _mk_user("jwtuser")
        tok = authn.issue_access_token(u)
        claims = authn.jwt_decode(tok)
        assert claims and claims["sub"] == u.id and claims["typ"] == "access"
        # tamper: flip the last char of the signature
        bad = tok[:-1] + ("A" if tok[-1] != "A" else "B")
        assert authn.jwt_decode(bad) is None
        # expired
        import time
        expired = authn.jwt_encode({"sub": u.id, "typ": "access", "ver": 0,
                                    "exp": int(time.time()) - 5})
        assert authn.jwt_decode(expired) is None

    def test_refresh_rotation_and_revoke():
        u = _mk_user("refuser")
        with SessionLocal() as s:
            r1 = authn.create_refresh_token(s, u)
            # consume rotates: r1 becomes invalid, user returned
            assert authn.consume_refresh_token(s, r1) is not None
            assert authn.consume_refresh_token(s, r1) is None      # already used
            r2 = authn.create_refresh_token(s, u)
            assert authn.revoke_refresh_token(s, r2) is True
            assert authn.consume_refresh_token(s, r2) is None      # revoked

    def test_local_auth_and_token_version():
        _mk_user("alice", "hunter2")
        with SessionLocal() as s:
            assert authn.authenticate_local(s, "alice", "hunter2") is not None
            assert authn.authenticate_local(s, "alice", "bad") is None
            assert authn.authenticate_local(s, "nobody", "x") is None
            u = s.query(User).filter_by(username="alice").one()
            tok = authn.issue_access_token(u)
            assert authn.user_from_token(s, tok) is not None
            u.token_version += 1                                    # "sign out everywhere"
            s.commit()
            assert authn.user_from_token(s, tok) is None            # old token now dead

    def test_proxy_provision_toggle():
        settings.auth_proxy_header_trust = True
        try:
            with SessionLocal() as s:
                s.query(User).filter_by(username="proxied").delete()
                s.commit()

            class _Req:
                headers = {"x-authentik-username": "proxied"}
            with SessionLocal() as s:
                u = authn.resolve_proxy_user(s, _Req())
                assert u is not None and u.auth_source == "proxy"
                assert not u.is_admin                               # never auto-admin
        finally:
            settings.auth_proxy_header_trust = False

    # ---------------------------------------------------------------- HTTP surface
    def test_api_login_refresh_me():
        _mk_user("bob", "bobpass")
        r = client.post("/api/v1/auth/login", json={"username": "bob", "password": "bobpass"})
        assert r.status_code == 200, r.text
        tokens = r.json()
        assert tokens["user"]["username"] == "bob"
        me = client.get("/api/v1/auth/me",
                        headers={"Authorization": f"Bearer {tokens['access']}"})
        assert me.status_code == 200 and me.json()["username"] == "bob"
        # bad creds
        assert client.post("/api/v1/auth/login",
                           json={"username": "bob", "password": "no"}).status_code == 401
        # refresh rotates and still yields a working access token
        r2 = client.post("/api/v1/auth/refresh", json={"refresh": tokens["refresh"]})
        assert r2.status_code == 200
        assert client.post("/api/v1/auth/refresh",
                           json={"refresh": tokens["refresh"]}).status_code == 401

    def test_web_login_cookie_flow():
        _mk_user("web", "webpass")
        settings.auth_required = True
        try:
            c = TestClient(app)
            # unauthenticated protected page -> redirect to /login
            r = c.get("/", follow_redirects=False)
            assert r.status_code == 303 and "/login" in r.headers["location"]
            # unauthenticated API -> 401 JSON
            assert c.get("/api/v1/trips-nope", follow_redirects=False).status_code == 401
            # login sets the cookie, then the page is reachable
            r = c.post("/login", data={"username": "web", "password": "webpass", "next": "/"},
                       follow_redirects=False)
            assert r.status_code == 303
            assert settings.auth_cookie_name in c.cookies
            assert c.get("/health").status_code == 200      # public regardless
            assert c.get("/", follow_redirects=False).status_code == 200
            # logout clears it
            c.post("/logout", follow_redirects=False)
            assert c.get("/", follow_redirects=False).status_code == 303
        finally:
            settings.auth_required = False

    def test_admin_gate():
        _mk_user("plain", "pw12345", admin=False)
        _mk_user("boss", "pw12345", admin=True)
        settings.auth_required = True
        try:
            reg = client.post("/api/v1/auth/login",
                              json={"username": "plain", "password": "pw12345"}).json()
            adm = client.post("/api/v1/auth/login",
                              json={"username": "boss", "password": "pw12345"}).json()
            plain_hdr = {"Authorization": f"Bearer {reg['access']}"}
            boss_hdr = {"Authorization": f"Bearer {adm['access']}"}
            # non-admin blocked at the gate (403); admin passes the gate (any
            # non-403 — SQLAdmin isn't mounted under a bare TestClient, so the
            # gate letting the request through is exactly what "not 403" proves)
            assert client.get("/admin/", headers=plain_hdr,
                              follow_redirects=False).status_code == 403
            assert client.get("/admin/", headers=boss_hdr,
                              follow_redirects=False).status_code != 403
        finally:
            settings.auth_required = False
