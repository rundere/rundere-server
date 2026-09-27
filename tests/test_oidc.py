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
    from app.models import Base, TravelerIdentity, User

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

    # ------------------------------------------------------------------ YONDER-143
    def test_subject_and_username_matching_two_accounts_prefers_subject():
        """Subject on one account, username on another used to raise
        MultipleResultsFound (a 500 on /oidc/callback)."""
        with SessionLocal() as s:
            s.query(User).filter(User.username.in_(["renamed", "taken"])).delete()
            s.commit()
            s.add_all([User(username="renamed", auth_source="oidc", external_subject="oidc:s143"),
                       User(username="taken", password_hash=authn.hash_password("x"),
                            auth_source="local")])
            s.commit()
            u = authn.provision_oidc_user(s, sub="s143", username="taken")
            assert u.username == "renamed"
            assert s.query(User).filter_by(username="taken").one().external_subject is None
            s.query(User).filter(User.username.in_(["renamed", "taken"])).delete()
            s.commit()

    def test_username_bound_to_another_subject_is_refused():
        with SessionLocal() as s:
            s.query(User).filter_by(username="bound").delete(); s.commit()
            s.add(User(username="bound", auth_source="oidc", external_subject="oidc:original"))
            s.commit()
            try:
                authn.provision_oidc_user(s, sub="impostor", username="bound")
                raise AssertionError("expected SSOLinkConflict")
            except authn.SSOLinkConflict:
                pass
            s.query(User).filter_by(username="bound").delete(); s.commit()

    def test_proxy_linked_account_is_taken_over_by_sso():
        with SessionLocal() as s:
            s.query(User).filter_by(username="proxied").delete(); s.commit()
            s.add(User(username="proxied", auth_source="proxy", external_subject="proxy:proxied"))
            s.commit()
            u = authn.provision_oidc_user(s, sub="p1", username="proxied")
            assert u.external_subject == "oidc:p1"
            s.query(User).filter_by(username="proxied").delete(); s.commit()

    def test_callback_conflict_fails_cleanly(monkeypatch):
        _enable_oidc()
        monkeypatch.setattr(oidc, "discovery", lambda: _DISCO)
        monkeypatch.setattr(oidc, "exchange_code",
                            lambda code, uri, verifier: {"id_token": "h.p.s"})
        monkeypatch.setattr(oidc, "decode_id_token",
                            lambda t: {"sub": "other", "preferred_username": "cbbound"})
        monkeypatch.setattr(oidc, "validate_claims", lambda claims, nonce: None)
        with SessionLocal() as s:
            s.query(User).filter_by(username="cbbound").delete()
            s.add(User(username="cbbound", auth_source="oidc", external_subject="oidc:first"))
            s.commit()
        c = TestClient(app)
        r = c.get("/oidc/login", follow_redirects=False)
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        cb = c.get(f"/oidc/callback?code=x&state={state}", follow_redirects=False)
        assert cb.status_code == 303 and "/login?error" in cb.headers["location"]
        with SessionLocal() as s:
            s.query(User).filter_by(username="cbbound").delete(); s.commit()

    # ------------------------------------------------------------------ YONDER-145
    def _sso_client(username):
        """A signed-in, freshly SSO-provisioned user (sharing_prompt on, no traveller)."""
        with SessionLocal() as s:
            s.query(User).filter_by(username=username).delete(); s.commit()
            u = authn.provision_oidc_user(s, sub=f"sub-{username}", username=username)
            c = TestClient(app)
            c.cookies.set(settings.auth_cookie_name, authn.issue_web_token(u))
            return c

    def _cleanup(usernames, travellers):
        with SessionLocal() as s:
            s.query(User).filter(User.username.in_(usernames)).delete()
            s.query(TravelerIdentity).filter(
                TravelerIdentity.display_name.in_(travellers)).delete()
            s.commit()

    def test_first_sso_sign_in_offers_only_unclaimed_travellers():
        settings.auth_required = True
        _cleanup(["t145a", "t145owner"], ["T145 Free", "T145 Taken"])
        try:
            with SessionLocal() as s:
                free = TravelerIdentity(display_name="T145 Free", kind="guest")
                taken = TravelerIdentity(display_name="T145 Taken", kind="guest")
                s.add_all([free, taken]); s.flush()
                s.add(User(username="t145owner", auth_source="local", traveler_id=taken.id))
                # SQLite reuses ids: another test's leftover user may still point
                # at the id "T145 Free" just got — that would make it look claimed.
                s.query(User).filter(User.traveler_id == free.id).update(
                    {User.traveler_id: None})
                s.commit()
                free_id, taken_id = free.id, taken.id
            c = _sso_client("t145a")
            page = c.get("/account/sharing").text
            assert "Which traveller are you?" in page
            assert "T145 Free" in page and "T145 Taken" not in page
            # claiming someone else's traveller is refused server-side
            r = c.post("/account/sharing", follow_redirects=False,
                       data={"choice": "share", "traveler_id": str(taken_id)})
            assert "err=" in r.headers["location"]
            r = c.post("/account/sharing", follow_redirects=False,
                       data={"choice": "share", "traveler_id": str(free_id), "next": "/trips"})
            assert r.headers["location"] == "/trips"
            with SessionLocal() as s:
                u = s.query(User).filter_by(username="t145a").one()
                assert u.traveler_id == free_id and u.sharing_prompt is False
        finally:
            settings.auth_required = False
            _cleanup(["t145a", "t145owner"], ["T145 Free", "T145 Taken"])

    def test_first_sso_sign_in_can_create_or_skip_a_traveller():
        settings.auth_required = True
        _cleanup(["t145b", "t145c"], ["T145 New"])
        try:
            c = _sso_client("t145b")
            c.post("/account/sharing", data={"choice": "share", "new_traveler": " T145 New "})
            with SessionLocal() as s:
                u = s.query(User).filter_by(username="t145b").one()
                t = s.get(TravelerIdentity, u.traveler_id)
                assert t.display_name == "T145 New" and t.kind == "guest"
            # a duplicate name is refused rather than 500ing on the unique index
            c2 = _sso_client("t145c")
            r = c2.post("/account/sharing", follow_redirects=False,
                        data={"choice": "share", "new_traveler": "t145 new"})
            assert "err=" in r.headers["location"]
            # skipping (both empty) still answers the sharing prompt
            c2.post("/account/sharing", data={"choice": "share"})
            with SessionLocal() as s:
                u = s.query(User).filter_by(username="t145c").one()
                assert u.traveler_id is None and u.sharing_prompt is False
            # …and the standalone page links it later
            assert "Which traveller are you?" in c2.get("/account/traveller").text
            c2.post("/account/traveller", data={"new_traveler": "T145 Later"})
            with SessionLocal() as s:
                assert s.query(User).filter_by(username="t145c").one().traveler_id is not None
        finally:
            settings.auth_required = False
            _cleanup(["t145b", "t145c"], ["T145 New", "T145 Later"])

    # ------------------------------------------------------------------ YONDER-164
    _PUBLIC, _LAN = "yonder.example.net", "yonder.home.example"

    def _per_host(monkeypatch, hosts="yonder.example.net", force="yonder.example.net"):
        _enable_oidc()
        monkeypatch.setattr(oidc, "discovery", lambda: _DISCO)
        monkeypatch.setattr(settings, "oidc_hosts", hosts)
        monkeypatch.setattr(settings, "oidc_force_hosts", force)
        monkeypatch.setattr(settings, "oidc_redirect_url", "")

    def test_forced_host_sends_login_straight_to_sso(monkeypatch):
        _per_host(monkeypatch)
        c = TestClient(app, base_url=f"https://{_PUBLIC}")
        r = c.get("/login?next=/trips", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/oidc/login?next=%2Ftrips"
        r = c.get("/oidc/login?next=/trips", follow_redirects=False)
        q = parse_qs(urlparse(r.headers["location"]).query)
        # the callback follows the host the user came in on
        assert q["redirect_uri"] == [f"https://{_PUBLIC}/oidc/callback"]

    def test_forced_host_after_sso_error_shows_retry_without_password_form(monkeypatch):
        _per_host(monkeypatch)
        page = TestClient(app, base_url=f"https://{_PUBLIC}").get(
            "/login?error=SSO+sign-in+failed").text
        assert "Sign in with" in page and 'name="password"' not in page
        assert "/forgot" not in page

    def test_forced_host_refuses_password_paths(monkeypatch):
        _per_host(monkeypatch)
        monkeypatch.setattr(settings, "auth_registration_open", True)
        c = TestClient(app, base_url=f"https://{_PUBLIC}")
        r = c.post("/login", data={"username": "x", "password": "y", "next": "/"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/oidc/login")
        for path in ("/register", "/forgot"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/login", path
        r = c.post("/forgot", data={"identifier": "x"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        # an emailed reset link still opens (it's built from TX_PUBLIC_BASE_URL)
        assert c.get("/reset/not-a-real-token").status_code == 200

    def test_api_password_login_blocked_only_when_opted_in(monkeypatch):
        _per_host(monkeypatch)
        c = TestClient(app, base_url=f"https://{_PUBLIC}")
        body = {"username": "nobody-164", "password": "wrong"}
        assert c.post("/api/v1/auth/login", json=body).status_code == 401   # still tried
        monkeypatch.setattr(settings, "oidc_force_api_login", True)
        assert c.post("/api/v1/auth/login", json=body).status_code == 403
        # the LAN host is unaffected either way
        lan = TestClient(app, base_url=f"http://{_LAN}")
        assert lan.post("/api/v1/auth/login", json=body).status_code == 401

    def test_lan_host_is_password_only_when_not_listed(monkeypatch):
        _per_host(monkeypatch)
        c = TestClient(app, base_url=f"http://{_LAN}")
        page = c.get("/login").text
        assert 'name="password"' in page and "Sign in with" not in page
        r = c.get("/oidc/login", follow_redirects=False)
        assert "/login?error" in r.headers["location"]

    def test_sso_offered_on_both_hosts_but_forced_on_one(monkeypatch):
        _per_host(monkeypatch, hosts="", force=_PUBLIC)
        page = TestClient(app, base_url=f"http://{_LAN}").get("/login").text
        assert 'name="password"' in page and "Sign in with" in page
        r = TestClient(app, base_url=f"http://{_LAN}").get("/oidc/login", follow_redirects=False)
        assert urlparse(r.headers["location"]).netloc == "idp.test"
