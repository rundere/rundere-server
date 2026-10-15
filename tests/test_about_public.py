"""YONDER-278 (SPEC-8 D-202): About is public on every server, and every
signed-out page links to it. Someone who lands on a server cold — the demo
included — can find out what Run·dere is, how to say it, what the server is and
how to get the app, without seeing anything about the server's users or data."""
import sys
import uuid
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

    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    @pytest.fixture()
    def signin_on(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        for k in ("ios_testflight_url", "ios_app_store_url", "android_app_url", "server_contact"):
            monkeypatch.setattr(settings, k, "")
        # Some user must exist, or the gate sends everyone to first-run /setup.
        with SessionLocal() as s:
            u = User(username=f"about-{uuid.uuid4().hex[:8]}", auth_source="local")
            s.add(u)
            s.commit()
            uid = u.id
        yield uid
        with SessionLocal() as s:
            s.query(User).filter_by(id=uid).delete()
            s.commit()

    def _bearer(uid):
        with SessionLocal() as s:
            return {"Authorization": f"Bearer {authn.issue_access_token(s.get(User, uid))}"}

    def test_about_is_public_and_never_shows_the_version_signed_out(signin_on):
        r = client.get("/about", follow_redirects=False)
        assert r.status_code == 200                      # not a redirect to /login
        html = r.text
        assert "run-THAIR" in html and "/brand/audio/rundere.m4a" in html
        assert "self-hosted travel planner" in html      # D-146 wording, not the old feeder
        assert "rundere.app/privacy" in html and "rundere-server" in html   # AGPL §13 source
        assert settings.app_version not in html
        assert 'href="/login"' in html

    def test_signed_in_about_keeps_the_app_layout_and_the_version(signin_on):
        html = client.get("/about", headers=_bearer(signin_on)).text
        assert settings.app_version in html and 'class="pagetitle"' in html

    def test_demo_explainer_only_on_a_demo_server(signin_on, monkeypatch):
        assert "demo server" not in client.get("/about").text
        monkeypatch.setattr(settings, "demo_mode", True)
        html = client.get("/about").text
        assert "demo server" in html and "read-only" in html
        assert "name=Run%C2%B7dere%20demo" in html       # the add-server link names it

    def test_contact_line_only_when_set_and_only_safe_links(signin_on, monkeypatch):
        assert "Questions about this server" not in client.get("/about").text
        monkeypatch.setattr(settings, "server_contact", "admin@example.test")
        assert 'href="mailto:admin@example.test"' in client.get("/about").text
        monkeypatch.setattr(settings, "server_contact", "javascript:alert(1)")
        html = client.get("/about").text
        assert "Questions about this server" not in html and "javascript:" not in html

    def test_add_server_link_is_the_host_the_page_was_served_on(signin_on):
        # Per-host sign-in (YONDER-164): the app must get the host the person is on.
        for host in ("rundere.example.test", "rundere.home.example"):
            html = client.get("/login", headers={"host": host}).text
            assert f'href="rundere://add-server?url=http%3A%2F%2F{host}"' in html, host
        html = client.get("/about", headers={"host": "x.example.test"}).text
        assert "rundere://add-server?url=http%3A%2F%2Fx.example.test" in html

    def test_app_install_lines_follow_their_settings(signin_on, monkeypatch):
        login = client.get("/login").text
        assert "TestFlight" not in login and "Google Play" not in login   # compact: only actionable
        about = client.get("/about").text
        assert "Android:</b> coming later" in about and "private testing" in about
        monkeypatch.setattr(settings, "ios_testflight_url", "https://testflight.apple.com/join/ABC")
        monkeypatch.setattr(settings, "android_app_url", "http://play.example.test/not-https")
        login = client.get("/login").text
        assert 'href="https://testflight.apple.com/join/ABC"' in login
        assert "Google Play" not in login                 # plain http is ignored
        # The App Store replaces TestFlight once the app is public.
        monkeypatch.setattr(settings, "ios_app_store_url", "https://apps.apple.com/app/id1")
        about = client.get("/about").text
        assert "App Store" in about and "testflight.apple.com" not in about

    def test_every_signed_out_page_links_to_about(signin_on):
        for path in ("/login", "/forgot"):
            html = client.get(path).text
            assert 'class="authfoot"' in html and 'href="/about"' in html, path
        assert "kept current every night" not in client.get("/login").text   # not demo here
