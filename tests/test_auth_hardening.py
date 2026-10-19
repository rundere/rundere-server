"""Security review, area 1: authentication (YONDER-293).

Each test pins one finding: refresh tokens die with "sign out everywhere", the
password sign-in throttle, the open-redirect rule, the cross-site write guard,
a deactivated account behind a trusted proxy, and the bounded rate limiter.
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
    from app import authn, authn_routes
    from app.auth import RateLimiter
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, RefreshToken, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False        # tests talk plain http
    client = TestClient(app)

    def _mk_user(username, password="pw-correct-horse", active=True):
        with SessionLocal() as s:
            existing = s.query(User).filter_by(username=username).one_or_none()
            if existing:
                s.query(RefreshToken).filter_by(user_id=existing.id).delete()
                s.delete(existing)
                s.commit()
            u = User(username=username, password_hash=authn.hash_password(password),
                     auth_source="local", is_active=active)
            s.add(u)
            s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _reset_throttle():
        authn_routes._login_by_ip._hits.clear()
        authn_routes._login_by_user._hits.clear()

    # ------------------------------------------------- refresh tokens die too
    def test_password_reset_revokes_refresh_tokens():
        """A stolen app refresh token must not outlive a password reset: before
        the fix only the JWT version moved, and refresh rows don't carry it."""
        u = _mk_user("hard-refresh")
        with SessionLocal() as s:
            user = s.get(User, u.id)
            refresh = authn.create_refresh_token(s, user)
            reset = authn.create_reset_token(s, user)
            assert authn.consume_reset_token(s, reset, "a-new-password") is not None
            assert authn.consume_refresh_token(s, refresh) is None

    def test_end_sessions_revokes_every_live_refresh_token():
        u = _mk_user("hard-end")
        with SessionLocal() as s:
            user = s.get(User, u.id)
            a = authn.create_refresh_token(s, user)
            b = authn.create_refresh_token(s, user)
            before = user.token_version
            authn.end_sessions(s, user)
            s.commit()
            assert user.token_version == before + 1
            assert authn.consume_refresh_token(s, a) is None
            assert authn.consume_refresh_token(s, b) is None

    def test_password_change_signs_the_app_out():
        """Changing your password on the web ends the app's refresh token, while
        the web session that made the change stays signed in."""
        _reset_throttle()
        _mk_user("hard-change", password="old-password-1")
        r = client.post("/api/v1/auth/login",
                        json={"username": "hard-change", "password": "old-password-1"})
        refresh = r.json()["refresh"]
        settings.auth_required = True
        try:
            web = TestClient(app)
            web.post("/login", data={"username": "hard-change", "password": "old-password-1"},
                     follow_redirects=False)
            r = web.post("/account/password", data={
                "current_password": "old-password-1", "password": "new-password-2",
                "confirm": "new-password-2"}, follow_redirects=False)
            assert r.status_code == 303 and "Password+changed" in r.headers["location"]
            assert web.get("/account", follow_redirects=False).status_code == 200
        finally:
            settings.auth_required = False
        r = client.post("/api/v1/auth/refresh", json={"refresh": refresh})
        assert r.status_code == 401

    # ------------------------------------------------------- sign-in throttle
    def test_login_throttle_per_username_web_and_api():
        _reset_throttle()
        _mk_user("hard-throttle")
        for _ in range(settings.login_rate_limit):
            r = client.post("/api/v1/auth/login",
                            json={"username": "hard-throttle", "password": "nope"})
            assert r.status_code == 401
        # Even the RIGHT password is refused while throttled, on both faces.
        r = client.post("/api/v1/auth/login",
                        json={"username": "hard-throttle", "password": "pw-correct-horse"})
        assert r.status_code == 429
        r = client.post("/login", data={"username": "hard-throttle",
                                        "password": "pw-correct-horse"},
                        follow_redirects=False)
        assert r.status_code == 429
        _reset_throttle()
        r = client.post("/api/v1/auth/login",
                        json={"username": "hard-throttle", "password": "pw-correct-horse"})
        assert r.status_code == 200

    def test_successful_logins_are_not_counted():
        _reset_throttle()
        _mk_user("hard-ok")
        for _ in range(settings.login_rate_limit + 2):
            r = client.post("/api/v1/auth/login",
                            json={"username": "hard-ok", "password": "pw-correct-horse"})
            assert r.status_code == 200

    def test_forgot_is_capped_per_address(monkeypatch):
        from app import accounts_routes
        accounts_routes._forgot_limiter._hits.clear()
        sent = []
        _mk_user("hard-forgot")
        with SessionLocal() as s:
            s.query(User).filter_by(username="hard-forgot").update({"email": "f@example.test"})
            s.commit()
        monkeypatch.setattr(accounts_routes, "email_configured", lambda: True)
        monkeypatch.setattr(accounts_routes, "send_email", lambda *a, **k: sent.append(a))
        for _ in range(settings.forgot_rate_limit + 3):
            r = client.post("/forgot", data={"identifier": "hard-forgot"})
            assert r.status_code == 200
        assert len(sent) == settings.forgot_rate_limit
        accounts_routes._forgot_limiter._hits.clear()

    # ---------------------------------------------------------- open redirect
    def test_safe_next_refuses_off_site_forms():
        safe = authn_routes._safe_next
        assert safe("/trips/1?tab=x") == "/trips/1?tab=x"
        for bad in ("//evil.example", "/\\evil.example", "/\\/evil.example",
                    "https://evil.example", "/ok\r\nSet-Cookie: x=1", "", None):
            assert safe(bad) == "/", bad

    def test_login_never_redirects_off_site():
        _reset_throttle()
        _mk_user("hard-next")
        r = client.post("/login", data={"username": "hard-next",
                                        "password": "pw-correct-horse",
                                        "next": "/\\evil.example"},
                        follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"

    # ------------------------------------------------- cross-site write guard
    def test_cross_site_write_is_refused():
        r = client.post("/logout", headers={"Origin": "https://evil.example"},
                        follow_redirects=False)
        assert r.status_code == 403
        r = client.post("/logout", headers={"Origin": "null"}, follow_redirects=False)
        assert r.status_code == 403

    def test_same_origin_and_originless_writes_pass():
        r = client.post("/logout", headers={"Origin": "http://testserver"},
                        follow_redirects=False)
        assert r.status_code == 303
        r = client.post("/logout", follow_redirects=False)       # no Origin: scripts, apps
        assert r.status_code == 303
        r = client.get("/login", headers={"Origin": "https://evil.example"})
        assert r.status_code == 200                               # reads aren't writes

    def test_machine_endpoints_and_bearer_calls_skip_the_guard():
        # A browser-based OAuth client calls /oauth/token from its own origin.
        r = client.post("/oauth/token", data={"grant_type": "nope"},
                        headers={"Origin": "https://claude.ai"})
        assert "cross-site" not in r.text
        r = client.post("/api/v1/auth/logout", json={"refresh": "x"},
                        headers={"Origin": "https://evil.example",
                                 "Authorization": "Bearer not-a-token"})
        assert "cross-site" not in r.text

    # ---------------------------------------------- deactivated behind proxy
    def test_deactivated_user_stays_out_behind_a_trusted_proxy():
        _mk_user("hard-proxy", active=False)
        settings.auth_proxy_header_trust = True
        try:
            from starlette.requests import Request
            req = Request({"type": "http", "method": "GET", "path": "/",
                           "headers": [(b"x-authentik-username", b"hard-proxy")]})
            with SessionLocal() as s:
                assert authn.resolve_user(s, req) is None
        finally:
            settings.auth_proxy_header_trust = False

    # ------------------------------------------------------- bounded limiter
    def test_rate_limiter_sweeps_stale_keys():
        rl = RateLimiter(3, 60)
        rl._SWEEP_AT = 5
        for i in range(5):
            rl.record(f"ip-{i}", now=0.0)
        rl.record("fresh", now=1000.0)               # sweeps the stale five
        assert set(rl._hits) == {"fresh"}
        assert not rl.blocked("fresh", now=1000.0)
        rl.record("fresh", now=1000.0)
        rl.record("fresh", now=1000.0)
        assert rl.blocked("fresh", now=1000.0)

    # ------------------------------------------------- sign-in on by default
    def test_sign_in_is_required_by_default():
        """D-203: a server that never sets TX_AUTH_REQUIRED asks for a sign-in.
        (The suite itself runs with it off; see conftest.)"""
        from app.config import Settings
        assert Settings.model_fields["auth_required"].default is True

    # ------------------------------------------------- security headers
    def test_every_page_has_the_baseline_headers():
        for path in ("/login", "/health", "/about"):
            r = client.get(path)
            assert r.headers["x-frame-options"] == "DENY", path
            assert r.headers["x-content-type-options"] == "nosniff", path
            assert r.headers["referrer-policy"] == "same-origin", path
            assert "frame-ancestors 'none'" in r.headers["content-security-policy"], path
