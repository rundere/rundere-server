"""SPEC-11 phase 3 (D-218): the Places Map lens. The lens tab only with a tile source,
the clusters/places JSON, own-only scoping, and the tile route (the tile server is
never reached: `maps._tile` is stubbed). Every place here is invented."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

try:
    from fastapi.testclient import TestClient
    from app import authn, places as places_svc, scope
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, SavedPlace, User
    from app.trips import maps
    Base.metadata.create_all(engine)
    client = TestClient(app)
except ImportError:                                     # bare host: the container/CI runs these
    pytest.skip("fastapi not installed", allow_module_level=True)

TILE = b"\x89PNG\r\n\x1a\nfake-tile"


@pytest.fixture(autouse=True)
def _clean():
    yield
    with SessionLocal() as s:
        s.query(SavedPlace).delete()
        s.commit()


@pytest.fixture
def no_network(monkeypatch):
    """Any tile fetch is a test failure unless the test stubs `_tile` itself."""
    def boom(*a, **k):
        raise AssertionError("the tile server must not be reached")
    monkeypatch.setattr(maps, "_tile", boom)


def _user(name):
    with SessionLocal() as s:
        u = s.query(User).filter_by(username=name).one_or_none()
        if u is None:
            u = User(username=name, password_hash=authn.hash_password("pw"),
                     auth_source="local")
            s.add(u)
            s.commit()
        s.refresh(u)
        s.expunge(u)
        return u


def _save(user, name, lat, lon):
    with SessionLocal() as s:
        places_svc.save(s, user, name=name, lat=lat, lon=lon, source="web")


WORLD = "s=-85&w=-180&n=85&e=180"


def test_lens_hidden_without_tiles_and_shown_with_them(monkeypatch):
    monkeypatch.setattr(settings, "map_tile_url", "")
    for url in ("/places", "/places?lens=map"):
        html = client.get(url).text
        assert ">Map</a>" not in html and 'id="pl-map"' not in html
        assert "leaflet" not in html
    monkeypatch.setattr(settings, "map_tile_url", "https://tiles.example.test/{z}/{x}/{y}.png")
    page = client.get("/places?lens=map").text
    assert 'href="/places?lens=map"' in page and 'id="pl-map"' in page
    assert "/review/static/vendor/leaflet/leaflet.js" in page
    assert "/review/static/places_map.js" in page
    assert 'href="/places?lens=map"' in client.get("/places?lens=atlas").text
    # The htmx partial (a lens switch) carries the map container too.
    part = client.get("/places?lens=map", headers={"hx-request": "true", "hx-target": "pl-body"})
    assert 'id="pl-map"' in part.text and "<html" not in part.text


def test_map_data_clusters_at_low_zoom_and_places_when_zoomed_in():
    u = _user("map_a")
    _save(u, "Larkspur Cafe", 38.7100, -9.1400)
    _save(u, "Example Bakery", 38.7110, -9.1410)
    _save(u, "Sample Tower", 35.6586, 139.7454)
    low = client.get(f"/places/map/data?{WORLD}&zoom=2")
    assert low.status_code == 200
    body = low.json()
    assert "places" not in body and sum(c["count"] for c in body["clusters"]) == 3
    assert {"geohash", "lat", "lon", "count"} <= set(body["clusters"][0])
    high = client.get("/places/map/data?s=38.70&w=-9.15&n=38.72&e=-9.13&zoom=17").json()
    assert "clusters" not in high
    assert {p["name"] for p in high["places"]} == {"Larkspur Cafe", "Example Bakery"}
    assert set(high["places"][0]) == {"id", "name", "lat", "lon", "activity_type"}


def test_map_data_is_own_only_and_needs_a_login(monkeypatch):
    a, b = _user("map_a"), _user("map_b")
    _save(a, "Larkspur Cafe", 38.7100, -9.1400)
    _save(b, "Quince Garden", 38.7105, -9.1405)
    acting = {"u": a}
    monkeypatch.setattr(scope, "user_of", lambda request: acting["u"])
    url = "/places/map/data?s=38.70&w=-9.15&n=38.72&e=-9.13&zoom=17"
    assert [p["name"] for p in client.get(url).json()["places"]] == ["Larkspur Cafe"]
    acting["u"] = b
    assert [p["name"] for p in client.get(url).json()["places"]] == ["Quince Garden"]
    world = client.get(f"/places/map/data?{WORLD}&zoom=2").json()
    assert sum(c["count"] for c in world["clusters"]) == 1       # clusters are scoped too
    # Nobody signed in (sign-in required): refused before any data is read.
    monkeypatch.undo()
    monkeypatch.setattr(settings, "auth_required", True)
    anon = TestClient(app)
    r = anon.get(url, follow_redirects=False)
    assert r.status_code in (302, 303, 401) and "Larkspur" not in r.text
    assert anon.get("/places/map/tiles/1/0/0.png", follow_redirects=False).status_code in (
        302, 303, 401)


@pytest.mark.parametrize("q", [
    "s=a&w=0&n=1&e=1&zoom=3", "s=0&w=0&n=1&e=1", "s=0&w=0&n=nan&e=1&zoom=3",
    "s=0&w=0&n=1&e=inf&zoom=3", "zoom=3"])
def test_map_data_bad_numbers_are_400(q):
    assert client.get(f"/places/map/data?{q}").status_code == 400


def test_map_data_clamps_an_out_of_range_view():
    u = _user("map_a")
    _save(u, "Larkspur Cafe", 38.71, -9.14)
    r = client.get("/places/map/data?s=-500&w=-900&n=500&e=900&zoom=99")
    assert r.status_code == 200 and len(r.json()["places"]) == 1


def test_data_route_is_not_swallowed_by_the_place_sheet():
    """places_web has GET /places/{place_id}; the map routes are included before it."""
    r = client.get(f"/places/map/data?{WORLD}&zoom=2")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert "clusters" in r.json()
    assert client.get("/places/map").status_code == 404        # a place id, "map": not found
    # The sheet route answers /places/<id> with a place's panel; "map" is no place.
    assert client.get("/places/map/tiles/0/0/0.png").headers.get("content-type") != "text/html"


@pytest.mark.parametrize("path", [
    "/places/map/tiles/20/0/0.png", "/places/map/tiles/3/8/0.png",
    "/places/map/tiles/3/0/9.png", "/places/map/tiles/0/1/0.png",
    "/places/map/tiles/-1/0/0.png", "/places/map/tiles/x/0/0.png",
    "/places/map/tiles/3/-1/0.png"])
def test_invalid_tiles_are_refused_without_a_network_call(path, no_network):
    assert client.get(path).status_code in (400, 404, 422)


def test_a_valid_tile_is_served_from_the_cache_layer(monkeypatch):
    monkeypatch.setattr(settings, "map_tile_url", "https://tiles.example.test/{z}/{x}/{y}.png")
    seen = []

    def fake(template, z, x, y):
        seen.append((template, z, x, y))
        return TILE
    monkeypatch.setattr(maps, "_tile", fake)
    r = client.get("/places/map/tiles/5/10/12.png")
    assert r.status_code == 200 and r.content == TILE
    assert r.headers["content-type"] == "image/png"
    assert "max-age" in r.headers["cache-control"]
    assert seen == [("https://tiles.example.test/{z}/{x}/{y}.png", 5, 10, 12)]
    monkeypatch.setattr(maps, "_tile", lambda *a: None)         # the server has none
    assert client.get("/places/map/tiles/5/10/13.png").status_code == 404


def test_no_tile_source_means_no_tiles(monkeypatch, no_network):
    monkeypatch.setattr(settings, "map_tile_url", "")
    assert client.get("/places/map/tiles/5/10/12.png").status_code == 404


def test_the_in_memory_tile_cache_is_capped(monkeypatch):
    """Panning the world can't grow the server's memory: the disk cache keeps
    every tile, the memory only the most recently used (D-218)."""
    from app.trips import maps
    monkeypatch.setattr(maps, "_MEMORY", maps.OrderedDict())
    monkeypatch.setattr(maps, "_MEMORY_MAX", 3)
    for i in range(5):
        maps._remember(f"tile-{i}", b"x")
    assert list(maps._MEMORY) == ["tile-2", "tile-3", "tile-4"]
