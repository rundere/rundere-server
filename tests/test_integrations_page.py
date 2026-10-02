"""YONDER (UI): the integrations status moved from the home page to Settings.

Run·dere is first-party (SPEC-8 D-146); AirTrail / AdventureLog are optional,
server-wide and admin-only for now (D-156). The page shows booleans only.
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
    from app.models import Base, User

    Base.metadata.create_all(engine)
    client = TestClient(app)

    def test_home_no_longer_shows_targets():
        html = client.get("/").text
        assert "Targets configured" not in html

    def test_integrations_page_auth_off(monkeypatch):
        monkeypatch.setattr(settings, "adventurelog_api_key", "secret-key-xyz")
        monkeypatch.setattr(settings, "airtrail_token", "")
        r = client.get("/settings/integrations")
        assert r.status_code == 200
        assert "Built in" in r.text and "Connected" in r.text and "Not connected" in r.text
        assert "secret-key-xyz" not in r.text                       # booleans only
        assert 'href="/settings/integrations"' in r.text            # in the settings sidebar/menu

    def test_integrations_page_for_a_non_admin(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        monkeypatch.setattr(settings, "adventurelog_api_key", "k")
        with SessionLocal() as s:
            u = User(username="ig-user", auth_source="local")
            s.add(u); s.commit()
            tok = authn.issue_access_token(u)
        try:
            r = client.get("/settings/integrations", cookies={settings.auth_cookie_name: tok})
            assert r.status_code == 200
            assert "Admin-managed" in r.text and "Connected" not in r.text
        finally:
            with SessionLocal() as s:
                s.query(User).filter(User.username == "ig-user").delete()
                s.commit()

    def test_settings_hub_lists_integrations():
        assert "Integrations" in client.get("/settings").text
