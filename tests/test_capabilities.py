"""D-244: GET /api/v1/auth/methods names what the server can do, so an app gates a
feature on a stable name instead of a 404 or the version number."""
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

    from app import capabilities
    from app.config import settings
    from app.main import app

    from app.db import engine
    from app.models import Base

    Base.metadata.create_all(engine)
    client = TestClient(app)

    # Names an app may already be built against: never removed or renamed (D-244).
    SHIPPED = {"trip_pdf_flags", "release_notes", "places_suggest", "places_lists",
               "themes", "theme_scene_png", "trip_create", "trip_roster"}

    def test_methods_lists_capabilities_without_signing_in():
        r = client.get("/api/v1/auth/methods")
        assert r.status_code == 200
        caps = r.json()["capabilities"]
        assert SHIPPED <= set(caps) and len(caps) == len(set(caps))
        assert caps == capabilities.current()
        assert all(c == c.lower() and c.replace("_", "").isalnum() for c in caps)

    def test_export_and_delete_are_listed_where_the_web_has_them(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        monkeypatch.setattr(settings, "demo_mode", False)
        assert {"data_export", "account_delete"} <= set(capabilities.current())

    def test_no_export_or_delete_with_sign_in_off(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", False)
        caps = set(capabilities.current())
        assert not {"data_export", "account_delete"} & caps and SHIPPED <= caps

    def test_every_listed_route_capability_exists(monkeypatch):
        """A name is a promise: the route behind it answers (401 or a sign-in
        redirect when signed out), never 404."""
        monkeypatch.setattr(settings, "auth_required", True)
        monkeypatch.setattr(settings, "demo_mode", False)
        for path in ("/api/v1/themes", "/api/v1/release-notes", "/api/v1/places/suggest",
                     "/api/v1/places/lists", "/api/v1/trips/none/timeline.pdf",
                     "/account/export", "/account/delete"):
            assert client.get(path, follow_redirects=False).status_code != 404, path
        # POST-only and roster routes (trip_create, trip_roster): signed out, the
        # route answers 401, never 404 or 405.
        for method, path in (("post", "/api/v1/trips"),
                             ("get", "/api/v1/trips/none/travelers"),
                             ("post", "/api/v1/trips/none/travelers"),
                             ("delete", "/api/v1/trips/none/travelers/none"),
                             # trip_manage (YONDER-409)
                             ("delete", "/api/v1/trips/none"),
                             ("get", "/api/v1/trips/none/shares"),
                             ("post", "/api/v1/trips/none/shares"),
                             ("delete", "/api/v1/trips/none/shares/none"),
                             ("put", "/api/v1/trips/none/owner")):
            r = getattr(client, method)(path, follow_redirects=False)
            assert r.status_code not in (404, 405), (method, path, r.status_code)
