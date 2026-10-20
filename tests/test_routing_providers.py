"""Road routers: Valhalla / openrouteservice / OSRM, tried in the configured
order with fall-through (resolve/routing.py). HTTP is stubbed — no network."""
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from resolve import routing  # noqa: E402

PHX, SED = (33.4343, -112.0117), (34.8697, -111.7610)


def _settings(monkeypatch, **kw):
    from app.config import settings
    base = {"routing_providers": "valhalla,ors,osrm", "valhalla_url": "",
            "valhalla_api_key": "", "ors_api_key": "", "ors_url":
            "https://api.openrouteservice.org", "osrm_url": ""}
    for k, v in {**base, **kw}.items():
        monkeypatch.setattr(settings, k, v)


def test_polyline_roundtrip_both_precisions():
    pts = [(33.4343, -112.0117), (34.2, -111.9), (34.8697, -111.761)]
    for prec in (5, 6):
        back = routing.decode_polyline(routing.encode_polyline(pts, prec), prec)
        assert all(abs(a - b) < 10 ** -prec for p, q in zip(pts, back) for a, b in zip(p, q))


def test_falls_through_to_the_next_router(monkeypatch):
    _settings(monkeypatch, valhalla_url="http://valhalla:8002/route",
              ors_api_key="k", osrm_url="http://osrm")
    calls = []

    def fake(url, *, body=None, headers=None, timeout=8):
        calls.append(url)
        if "valhalla" in url:          # home-region tiles: nothing near these stops
            raise urllib.error.HTTPError(url, 400, "No suitable edges", {}, None)
        if "openrouteservice" in url:
            assert headers["Authorization"] == "k"
            assert body["coordinates"][0] == [PHX[1], PHX[0]]          # lon, lat
            return {"routes": [{"summary": {"distance": 101400, "duration": 4260},
                                "geometry": routing.encode_polyline([PHX, SED], 5)}]}
        raise AssertionError("OSRM must not be reached")
    monkeypatch.setattr(routing, "_http_json", fake)
    est = routing.route([PHX, SED])
    assert (est.provider, est.distance_km, est.duration_minutes) == ("ors", 101.4, 71)
    # ORS answers in precision 5; stored lines are always polyline6
    assert routing.decode_polyline(est.polyline, 6)[0] == PHX
    assert len(calls) == 2


def test_valhalla_joins_legs_and_sends_stadia_key(monkeypatch):
    _settings(monkeypatch, valhalla_url="https://api.stadiamaps.com/route/v1",
              valhalla_api_key="s3cret")
    mid = (34.2, -111.9)
    seen = {}

    def fake(url, *, body=None, headers=None, timeout=8):
        seen["url"], seen["body"] = url, body
        return {"trip": {"status": 0, "summary": {"length": 101.37, "time": 4260},
                         "legs": [{"shape": routing.encode_polyline([PHX, mid])},
                                  {"shape": routing.encode_polyline([mid, SED])}]}}
    monkeypatch.setattr(routing, "_http_json", fake)
    est = routing.route([PHX, mid, SED])
    assert est.provider == "valhalla" and est.distance_km == 101.4
    assert seen["url"].endswith("?api_key=s3cret")
    assert seen["body"]["costing"] == "auto" and len(seen["body"]["locations"]) == 3
    assert routing.decode_polyline(est.polyline) == [PHX, mid, SED]    # no doubled joint


def test_unconfigured_routers_are_skipped_and_order_respected(monkeypatch):
    _settings(monkeypatch, routing_providers="osrm,valhalla",
              valhalla_url="http://valhalla/route", osrm_url="http://osrm")
    assert [n for n, _ in routing.configured_routers()] == ["osrm", "valhalla"]
    _settings(monkeypatch)                                   # nothing configured
    assert routing.configured_routers() == []
    assert routing.route([PHX, SED]) is None


def test_long_lines_are_thinned_for_display():
    pts = [(35 + i / 10000, -106 + i / 10000) for i in range(5000)]
    thin = routing._simplify(pts)
    assert len(thin) == 400 and thin[0] == pts[0] and thin[-1] == pts[-1]
