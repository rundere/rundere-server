"""D-198 (YONDER-181): the read-only demo server. One shared login, every write
refused except signing in and out, closed surfaces answer 404, every trip shows
in full with can_edit=false, and the server refuses to start where it could
expose or change real data."""
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
    import pytest
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import authn, demo
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, Trip, TripActivity, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    @pytest.fixture()
    def demo_on():
        """Demo mode on, with its login, for one test; everything put back after."""
        saved = {k: getattr(settings, k) for k in
                 ("demo_mode", "demo_username", "demo_password", "auth_required")}
        settings.demo_mode, settings.auth_required = True, True
        settings.demo_username, settings.demo_password = "demo-t", "demo-pass"
        with SessionLocal() as s:
            u = demo.ensure_demo_user(s)
            t = Trip(name="Demo Lisbon", source="manual", owner_id=u.id,
                     start_date="2031-06-01", end_date="2031-06-05")
            s.add(t)
            s.flush()
            s.add(TripActivity(trip_id=t.id, type="dining", starts_at="2031-06-02T20:00",
                               values={"when": "2031-06-02T20:00", "name": "Demo dinner"}))
            s.commit()
            uid, tid = u.id, t.id
        try:
            yield uid, tid
        finally:
            for k, v in saved.items():
                setattr(settings, k, v)
            with SessionLocal() as s:
                s.query(TripActivity).filter_by(trip_id=tid).delete()
                s.query(Trip).filter_by(id=tid).delete()
                s.commit()

    def _bearer(uid):
        with SessionLocal() as s:
            return {"Authorization": f"Bearer {authn.issue_access_token(s.get(User, uid))}"}

    # ------------------------------------------------------------------ writes
    def test_every_write_is_refused_but_sign_in_works(demo_on):
        uid, tid = demo_on
        h = _bearer(uid)
        for method, path, body in (
                ("patch", f"/api/v1/trips/{tid}", {"name": "x"}),
                ("post", f"/api/v1/trips/{tid}/activities", {"type": "note", "values": {}}),
                ("post", "/api/v1/journal", {"body": "hi"}),
                ("post", "/api/v1/capture", {"url": "https://example.com"}),
                ("put", "/api/v1/auth/me/email", {"email": "a@b.c"})):
            r = getattr(client, method)(path, json=body, headers=h)
            assert r.status_code == 403 and r.json()["demo"] is True, path
        # Signing in (and refreshing) still works, and nothing else changed.
        r = client.post("/api/v1/auth/login", json={"username": "demo-t", "password": "demo-pass"})
        assert r.status_code == 200 and r.json()["access"]
        with SessionLocal() as s:
            assert s.get(Trip, tid).name == "Demo Lisbon"

    def test_an_htmx_write_shows_the_message_in_the_banner(demo_on):
        uid, tid = demo_on
        r = client.post(f"/trips/{tid}/delete", headers={**_bearer(uid), "HX-Request": "true"})
        assert r.status_code == 200                     # htmx only swaps 2xx
        assert r.headers["HX-Retarget"] == "#demo-msg" and "read-only" in r.text
        with SessionLocal() as s:
            assert s.get(Trip, tid) is not None
        plain = client.post(f"/trips/{tid}/delete", headers=_bearer(uid))
        assert plain.status_code == 403

    def test_closed_surfaces_are_404(demo_on):
        for path in ("/mcp", "/.well-known/oauth-authorization-server",
                     "/.well-known/oauth-protected-resource", "/oauth/authorize",
                     "/register", "/forgot", "/setup", "/admin/", "/users",
                     "/settings/integrations", "/settings/notifications",
                     "/account/tokens", "/oidc/login"):
            assert client.get(path).status_code == 404, path
        assert client.post("/oauth/token", data={}).status_code == 404
        assert client.post("/mcp", json={}).status_code == 404

    def test_methods_report_demo_and_password_only(demo_on):
        m = client.get("/api/v1/auth/methods").json()
        assert (m["demo"], m["password"], m["oauth"], m["oauth_client_id"], m["sso"],
                m["registration_open"]) == (True, True, False, None, False, False)
        settings.demo_mode = False
        assert client.get("/api/v1/auth/methods").json()["demo"] is False

    # ------------------------------------------------------------------ reads
    def test_trips_show_in_full_but_cant_be_edited(demo_on):
        uid, tid = demo_on
        trip = client.get(f"/api/v1/trips/{tid}", headers=_bearer(uid)).json()["trip"]
        assert (trip["my_role"], trip["can_edit"]) == ("owner", False)
        page = client.get(f"/trips/{tid}", headers=_bearer(uid)).text
        assert "Demo server:" in page and 'id="demo-msg"' in page       # the banner
        assert "Demo dinner" in page
        # Every tab shows (not a D-199 viewer)…
        for tab in ("discovery", "packing", "costs", "journal"):
            assert f'data-tab="{tab}" onclick' in page, tab
        # …but nothing offers an edit.
        for control in ("+ Add activity", "Edit trip", "Run discovery", "Generate packing plan"):
            assert control not in page, control
        assert "View only" not in page                  # that badge is for viewers
        assert 'class="navcap"' not in page              # D-200's Capture button: nothing to capture

    def test_sign_in_page_shows_the_demo_login(demo_on):
        page = client.get("/login").text
        assert "demo server" in page and "demo-t" in page and "demo-pass" in page
        assert "/forgot" not in page

    def test_settings_hide_connections(demo_on):
        uid, _ = demo_on
        page = client.get("/settings", headers=_bearer(uid)).text
        assert "/settings/integrations" not in page

    # ------------------------------------------------------------------ startup
    def _fresh_session():
        eng = create_engine("sqlite://")
        Base.metadata.create_all(eng)
        return sessionmaker(bind=eng)()

    def test_startup_refuses_a_database_with_real_users(demo_on):
        s = _fresh_session()
        s.add(User(username="someone-real", auth_source="local"))
        s.commit()
        with pytest.raises(demo.DemoMisconfigured, match="other user") as e:
            demo.check_startup(s)
        assert "someone-real" not in str(e.value)        # never named in a public log

    def test_startup_refuses_unsafe_settings(demo_on, monkeypatch):
        s = _fresh_session()
        monkeypatch.setattr(settings, "smtp_host", "mail.example.com")
        monkeypatch.setattr(settings, "demo_password", "")
        monkeypatch.setattr(settings, "auth_required", False)
        with pytest.raises(demo.DemoMisconfigured) as e:
            demo.check_startup(s)
        msg = str(e.value)
        assert "TX_AUTH_REQUIRED" in msg and "TX_DEMO_PASSWORD" in msg and "mail" in msg

    def test_startup_on_a_clean_database_makes_the_login_and_cuts_home_services(
            demo_on, monkeypatch):
        s = _fresh_session()
        monkeypatch.setattr(settings, "photon_url", "http://photon:2322")
        monkeypatch.setattr(settings, "ollama_url", "http://ollama:11434")
        demo.check_startup(s)
        u = s.query(User).one()
        assert u.username == "demo-t" and not u.is_admin
        assert authn.verify_password("demo-pass", u.password_hash)
        assert settings.photon_url == "" and settings.ollama_url == ""
        # A changed TX_DEMO_PASSWORD takes over on the next start.
        monkeypatch.setattr(settings, "demo_password", "new-pass")
        demo.check_startup(s)
        assert authn.verify_password("new-pass", s.query(User).one().password_hash)

    def test_startup_turns_off_the_request_log(demo_on, monkeypatch):
        # YONDER-277: uvicorn's access line carries the visitor's IP; a demo
        # server writes none. Warnings and errors still log.
        import logging
        access = logging.getLogger("uvicorn.access")
        monkeypatch.setattr(access, "disabled", False)
        demo.check_startup(_fresh_session())
        assert access.disabled is True
        assert not logging.getLogger("rundere.demo").disabled

    def test_off_by_default_nothing_changes():
        assert settings.demo_mode is False
        assert not demo.refuses("GET", "/trips")
        assert client.get("/api/v1/auth/methods").json()["demo"] is False
