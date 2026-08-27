"""Yonder 4.0 Phase 1b — OIDC client (SPEC-4 D-118).

No live IdP: the deterministic helpers are tested directly, and the login →
callback flow runs with discovery/exchange/decode stubbed. SQLite via conftest.
"""
import base64
import hashlib
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import authn, oidc
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False

    def _enable_oidc():
        settings.oidc_enabled = True
        settings.oidc_issuer = "https://idp.test"
        settings.oidc_client_id = "yonder"
        settings.oidc_client_secret = "shh"

    _DISCO = {"issuer": "https://idp.test",
              "authorization_endpoint": "https://idp.test/authorize",
              "token_endpoint": "https://idp.test/token"}

    # ------------------------------------------------------------------ helpers
    def test_pkce_challenge_is_s256_of_verifier():
        v, c = oidc.new_pkce()
        expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        assert c == expected

    def test_validate_claims(monkeypatch):
        _enable_oidc()
        monkeypatch.setattr(oidc, "discovery", lambda: _DISCO)
        good = {"iss": "https://idp.test", "aud": "yonder",
                "exp": time.time() + 60, "nonce": "N"}
        assert oidc.validate_claims(good, "N") is None
        assert oidc.validate_claims({**good, "iss": "https://evil"}, "N")
        assert oidc.validate_claims({**good, "aud": "someoneelse"}, "N")
        assert oidc.validate_claims({**good, "exp": time.time() - 100}, "N")
        assert oidc.validate_claims(good, "different-nonce")

    def test_user_fields_maps_admin_group():
        settings.oidc_admin_group = "yonder-admins"
        try:
            f = oidc.user_fields({"sub": "s1", "preferred_username": "alice",
                                  "email": "a@e.com", "groups": ["yonder-admins"]})
            assert f == {"sub": "s1", "username": "alice", "email": "a@e.com", "is_admin": True}
            assert oidc.user_fields({"sub": "s2", "preferred_username": "bob"})["is_admin"] is False
        finally:
            settings.oidc_admin_group = ""

    def test_provision_oidc_user_create_adopt_upgrade():
        with SessionLocal() as s:
            s.query(User).filter(User.username.in_(["ssonew", "localdude"])).delete()
            s.commit()
            # new provision
            u = authn.provision_oidc_user(s, sub="abc", username="ssonew", email="e@e.com")
            assert u.auth_source == "oidc" and u.external_subject == "oidc:abc"
            # idempotent by subject
            assert authn.provision_oidc_user(s, sub="abc", username="ssonew").id == u.id
            # adopt an existing same-named local account + upgrade to admin
            s.add(User(username="localdude", password_hash=authn.hash_password("x"),
                       auth_source="local"))
            s.commit()
            adopted = authn.provision_oidc_user(s, sub="xyz", username="localdude", is_admin=True)
            assert adopted.external_subject == "oidc:xyz" and adopted.is_admin
            # auto-create off => unknown user is refused
            settings.oidc_auto_create = False
            try:
                assert authn.provision_oidc_user(s, sub="new2", username="stranger") is None
            finally:
                settings.oidc_auto_create = True

    # ------------------------------------------------------------------ flow
    def test_login_redirects_to_idp(monkeypatch):
        _enable_oidc()
        monkeypatch.setattr(oidc, "discovery", lambda: _DISCO)
        c = TestClient(app)
        r = c.get("/oidc/login?next=/trips", follow_redirects=False)
        assert r.status_code == 303
        loc = urlparse(r.headers["location"])
        q = parse_qs(loc.query)
        assert loc.netloc == "idp.test" and loc.path == "/authorize"
        assert q["client_id"] == ["yonder"] and q["code_challenge_method"] == ["S256"]
        assert "yonder_oidc" in c.cookies

    def test_callback_provisions_and_signs_in(monkeypatch):
        _enable_oidc()
        monkeypatch.setattr(oidc, "discovery", lambda: _DISCO)
        monkeypatch.setattr(oidc, "exchange_code",
                            lambda code, uri, verifier: {"id_token": "h.p.s"})
        monkeypatch.setattr(oidc, "decode_id_token",
                            lambda t: {"sub": "flow1", "preferred_username": "flowuser",
                                       "email": "f@e.com"})
        monkeypatch.setattr(oidc, "validate_claims", lambda claims, nonce: None)
        with SessionLocal() as s:
            s.query(User).filter_by(username="flowuser").delete()
            s.commit()
        c = TestClient(app)
        r = c.get("/oidc/login?next=/trips", follow_redirects=False)
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        cb = c.get(f"/oidc/callback?code=thecode&state={state}", follow_redirects=False)
        assert cb.status_code == 303 and cb.headers["location"] == "/trips"
        assert settings.auth_cookie_name in c.cookies
        with SessionLocal() as s:
            assert s.query(User).filter_by(username="flowuser").one().auth_source == "oidc"

    def test_callback_rejects_bad_state(monkeypatch):
        _enable_oidc()
        monkeypatch.setattr(oidc, "discovery", lambda: _DISCO)
        c = TestClient(app)
        c.get("/oidc/login?next=/", follow_redirects=False)
        r = c.get("/oidc/callback?code=x&state=WRONG", follow_redirects=False)
        assert r.status_code == 303 and "/login?error" in r.headers["location"]
