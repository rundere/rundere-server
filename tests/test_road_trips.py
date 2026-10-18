"""Road trips and map links (owner feedback, 2026-09-26).

Covers: a Maps directions link -> stops / distance / time (resolve/routing.py),
the manual Road trip fill-in (app/trips/enrich.py), Map link vs free-text Source
on every activity type, a saved Maps link winning the Map button, the directions
capture reaching the timeline with From/To, distance and its link, the capture
form's Yonder trip pick, and a reviewer's address fix being re-located.
Routing and geocoding are stubbed — no network.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import sqlalchemy  # noqa: F401
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app.db import engine
    from app.models import Base
    Base.metadata.create_all(engine)

GMAPS = ("https://www.google.com/maps/dir/Phoenix+International+Sunport/"
         "Old+Town+Plaza,+Phoenix,+NM/@35.07,-106.64,13z/data=!4m14!4m13"
         "!1m5!1m1!1s0x0:0x1!2m2!1d-106.6091!2d35.0433"
         "!1m5!1m1!1s0x0:0x2!2m2!1d-106.6697!2d35.0961!3e0")


# ------------------------------------------------------------ resolve/routing

def test_directions_stops_reads_google_names_and_embedded_coords():
    from resolve.routing import directions_stops
    stops = directions_stops(GMAPS)
    assert [s["name"] for s in stops] == ["Phoenix International Sunport",
                                         "Heritage Square, Phoenix, NM"]
    assert (stops[0]["lat"], stops[0]["lon"]) == (35.0433, -106.6091)
    assert (stops[1]["lat"], stops[1]["lon"]) == (35.0961, -106.6697)
    assert stops[1]["city"] == "Phoenix"


def test_directions_stops_apple_and_non_directions():
    from resolve.routing import directions_stops
    apple = directions_stops("https://maps.apple.com/?saddr=35.04,-106.61"
                             "&daddr=Sedona,+NM&dirflg=d")
    assert apple[0]["lat"] == 35.04 and apple[1]["city"] == "Sedona"
    assert directions_stops("https://www.google.com/maps/search/?api=1&query=x") == []


def test_route_parses_osrm_and_fails_soft(monkeypatch):
    import io
    import json
    from resolve import routing

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        return _Resp(json.dumps({"code": "Ok", "routes": [
            {"distance": 101400.0, "duration": 4260.0}]}).encode())
    monkeypatch.setattr(routing.urllib.request, "urlopen", fake_urlopen)
    est = routing.route([(35.04, -106.61), (35.68, -105.94)], base_url="http://osrm")
    assert (est.distance_km, est.duration_minutes) == (101.4, 71)
    # OSRM wants lon,lat
    assert "/route/v1/driving/-106.610000,35.040000;-105.940000,35.680000" in seen["url"]
    assert routing.format_distance(est) == "101 km (63 mi) · ~1 h 11 min"
    # switched off -> None, never a call
    assert routing.route([(1.0, 2.0), (3.0, 4.0)], base_url="") is None

    def boom(req, timeout=0):
        raise routing.urllib.error.URLError("down")
    monkeypatch.setattr(routing.urllib.request, "urlopen", boom)
    assert routing.route([(1.0, 2.0), (3.0, 4.0)], base_url="http://osrm") is None


def test_stop_label_and_address():
    from resolve.routing import stop_address, stop_label
    plaza = {"raw": "Heritage Square, Phoenix, NM", "name": "Heritage Square, Phoenix, NM",
             "city": "Phoenix", "region": "NM"}
    assert stop_label(plaza) == "Heritage Square"
    assert stop_address(plaza) == "Heritage Square, Phoenix, NM"
    # a reviewer-typed street address wins, keeping the name and city
    plaza["address"] = "303 Heritage Way NW"
    assert stop_address(plaza) == "Heritage Square, 303 Heritage Way NW, Phoenix"


# ---------------------------------------------------- manual Road trip fill-in

def _stub_link(monkeypatch, estimate=(210.0, 130)):
    from resolve import routing
    stops = [{"raw": "Phoenix International Sunport",
              "name": "Phoenix International Sunport", "lat": 35.04, "lon": -106.61},
             {"raw": "Sedona Plaza, Sedona, NM", "name": "Sedona Plaza, Sedona, NM",
              "city": "Sedona", "region": "NM", "lat": 35.69, "lon": -105.94}]
    est = routing.RouteEstimate(*estimate) if estimate else None
    monkeypatch.setattr(routing, "estimate_for_link",
                        lambda url, geocoder, base_url: (stops, est))


def test_drive_fills_blanks_from_its_map_link(monkeypatch):
    if not DEPS:
        return
    from app.trips.enrich import enrich_activity_values
    _stub_link(monkeypatch)
    out = enrich_activity_values("drive", {"mapLink": GMAPS, "when": "2026-10-03T09:00"})
    assert out["route"] == "Phoenix International Sunport → Sedona Plaza"
    assert out["from"] == "Phoenix International Sunport"
    assert out["to"] == "Sedona Plaza, Sedona, NM"
    assert out["distance"] == "210 km (130 mi) · ~2 h 10 min"
    assert out["endWhen"] == "2026-10-03T11:10"          # departure + drive time


def test_drive_fill_never_overwrites_and_skips_other_types(monkeypatch):
    if not DEPS:
        return
    from app.trips.enrich import enrich_activity_values
    _stub_link(monkeypatch)
    typed = {"mapLink": GMAPS, "route": "Scenic way", "distance": "about 3 hours",
             "when": "2026-10-03"}
    out = enrich_activity_values("drive", typed)
    assert out["route"] == "Scenic way" and out["distance"] == "about 3 hours"
    assert "endWhen" not in out                  # a date-only departure has no clock
    tour = {"mapLink": GMAPS, "name": "Balloon ride"}
    assert enrich_activity_values("tour", tour) is tour
    assert enrich_activity_values("drive", {"route": "no link"}) == {"route": "no link"}


def test_api_add_drive_activity_fills_distance(monkeypatch):
    if not DEPS:
        return
    from fastapi.testclient import TestClient

    from app.db import SessionLocal
    from app.main import app
    from app import authn
    from app.models import Trip, TripActivity, User
    _stub_link(monkeypatch)
    with SessionLocal() as s:
        u = User(username="road-trip-api", password_hash=authn.hash_password("pw"),
                 auth_source="local")
        s.add(u)
        s.flush()
        t = Trip(name="Road trip test", start_date="2026-10-02", end_date="2026-10-11",
                 owner_id=u.id)
        s.add(t)
        s.commit()
        s.refresh(u)
        tid, token = t.id, authn.issue_access_token(u)
    try:
        r = TestClient(app).post(
            f"/api/v1/trips/{tid}/activities",
            headers={"Authorization": f"Bearer {token}"},
            json={"type": "drive", "values": {"mapLink": GMAPS, "when": "2026-10-03T09:00"}})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["values"]["distance"].startswith("210 km")
        assert body["map_url"] == GMAPS
    finally:
        _drop_trip(tid)


def _drop_trip(tid):
    # Keep the shared test DB free of stray trips: other files auto-link by date.
    from app.db import SessionLocal
    from app.models import Trip, TripActivity
    with SessionLocal() as s:
        s.query(TripActivity).filter(TripActivity.trip_id == tid).delete()
        t = s.get(Trip, tid)
        if t is not None:
            s.delete(t)
        s.commit()


# --------------------------------------------- Map link vs Source; Map button

def test_every_located_type_has_map_link_and_text_source():
    if not DEPS:
        return
    from app.trips import types as at
    for key, t in at.TRIP_ACTIVITY_TYPES.items():
        src = t.field("sourceLink")
        assert src is not None and src["type"] == "text", key   # an email is not a URL
        if key != "note":
            assert t.field("mapLink")["type"] == "url", key
    drive = at.get_type("drive")
    assert drive.field("from") and drive.field("to")


def test_saved_maps_link_wins_the_map_button():
    if not DEPS:
        return
    from app.trips.types import map_url
    apple = "https://maps.apple.com/?q=Camelback+Peak+Tramway&ll=35.19,-106.48"
    goo = "https://maps.app.goo.gl/AbCdEf123"
    assert map_url("tour", {"location": "Camelback Peak", "mapLink": apple}) == apple
    assert map_url("event", {"venue": "Balloon Fiesta Park", "mapLink": goo}) == goo
    # entries made before the Map field: a Maps URL pasted as the Source still wins
    assert map_url("dining", {"name": "Frontier", "sourceLink": goo}) == goo
    # a non-maps Source never becomes the map link
    u = map_url("tour", {"location": "Camelback Peak", "sourceLink": "https://viator.com/x"})
    assert u.startswith("https://www.google.com/maps/search/")
    # a drive with only From/To still searches its route
    assert "Sedona" in map_url("drive", {"from": "PHX", "to": "Sedona"})


# ------------------------------------------------ directions capture -> timeline

def _drive_payload():
    return {"item_type": "ground_transport", "booking_status": "planned",
            "ground_transport": {
                "mode": "car",
                "departure": {"place": {"raw": "Phoenix International Sunport",
                                        "name": "Phoenix International Sunport",
                                        "lat": 35.04, "lon": -106.61},
                              "time": {"local": "2026-10-03", "precision": "date"}},
                "arrival": {"place": {"raw": "Heritage Square, Phoenix, NM",
                                      "name": "Heritage Square, Phoenix, NM",
                                      "city": "Phoenix", "region": "NM",
                                      "address": "303 Heritage Way NW",
                                      "lat": 35.09, "lon": -106.67},
                            "time": {"local": "2026-10-03", "precision": "date"}},
                "waypoints": [],
                "route": {"provider": "google_maps", "source_url": GMAPS,
                          "distance_km": 21.3, "duration_minutes": 19,
                          "is_estimate": True}}}


def test_committed_drive_keeps_from_to_distance_and_link():
    if not DEPS:
        return
    from app.committing import build_yonder_activity
    atype, v = build_yonder_activity(_drive_payload())
    assert atype == "drive"
    assert v["route"] == "Phoenix International Sunport → Heritage Square"
    assert v["from"] == "Phoenix International Sunport"
    assert v["to"] == "Heritage Square, 303 Heritage Way NW, Phoenix"   # the edited address
    assert v["distance"] == "21 km (13 mi) · ~19 min"
    assert v["mapLink"] == GMAPS
    _, lodging = build_yonder_activity(
        {"item_type": "lodging", "lodging": {"property_name": "Hotel Palo Verde"}},
        source="Email: Your stay at Hotel Palo Verde")
    assert lodging["sourceLink"] == "Email: Your stay at Hotel Palo Verde"


def test_resolver_estimates_a_located_maps_drive(monkeypatch):
    if not DEPS:
        return
    from app import resolving
    from resolve import routing
    monkeypatch.setattr(routing, "estimate_for_places",
                        lambda places, base_url=None: routing.RouteEstimate(21.3, 19))
    p = _drive_payload()
    p["ground_transport"]["route"].update(distance_km=None, duration_minutes=None)
    out = resolving._estimate_drive(p)["ground_transport"]["route"]
    assert (out["distance_km"], out["duration_minutes"]) == (21.3, 19)
    # a non-maps route is left alone
    p2 = _drive_payload()
    p2["ground_transport"]["route"] = {"provider": "manual"}
    assert "distance_km" not in resolving._estimate_drive(p2)["ground_transport"]["route"]


def test_reviewer_address_fix_drops_guessed_coords_only():
    if not DEPS:
        return
    from app.review.routes import _apply_edit
    p = _drive_payload()
    p["ground_transport"]["departure"]["place"]["geocode_source"] = "photon"
    assert _apply_edit(p, "/ground_transport/departure/place/city", "Phoenix", "text")
    dep = p["ground_transport"]["departure"]["place"]
    assert "lat" not in dep and "geocode_source" not in dep
    assert p["ground_transport"]["route"]["distance_km"] is None     # recomputed next
    # coordinates the link itself stated are never dropped
    assert _apply_edit(p, "/ground_transport/arrival/place/address", "1 Main St", "text")
    assert p["ground_transport"]["arrival"]["place"]["lat"] == 35.09


def test_street_address_stop_fills_the_address_field():
    from extract.directions_common import _parse_place
    p = _parse_place("100 Harbour Ridge Pkwy, Maple Grove, ON")
    assert p["address"] == "100 Harbour Ridge Pkwy, Maple Grove, ON"
    assert "address" not in _parse_place("Heritage Square, Phoenix, NM")


# ------------------------------------------------ capture form: Yonder trip pick

def test_capture_form_offers_yonder_trips_and_pins_the_pick():
    if not DEPS:
        return
    from fastapi.testclient import TestClient

    from app.db import SessionLocal
    from app.main import app
    from app.models import IngestBatch, TravelItem, Trip
    from app.resolving import _link_chosen_trip
    client = TestClient(app)
    with SessionLocal() as s:
        t = Trip(name="Capture pick test", start_date="2026-10-02", end_date="2026-10-11")
        s.add(t)
        s.commit()
        tid = t.id
    page = client.get("/capture")
    assert page.status_code == 200
    assert f'value="{tid}"' in page.text and "Capture pick test" in page.text
    assert 'type="date"' in page.text and 'type="time"' in page.text
    assert "AdventureLog collection" in page.text

    r = client.post("/capture/web", data={
        "path": "google", "content": GMAPS, "travel_date": "2026-10-03", "trip_id": tid})
    assert r.status_code == 200 and "Captured" in r.text
    with SessionLocal() as s:
        b = [b for b in s.query(IngestBatch).all()
             if (b.payload or {}).get("url") == GMAPS][-1]
        assert b.payload["capture_context"]["trip_id"] == tid
        item = TravelItem(item_type="ground_transport", schema_version="1.4.0",
                          source={"capture_context": {"trip_id": tid}}, extraction={},
                          status={})
        s.add(item)
        s.flush()
        _link_chosen_trip(s, item, item.source["capture_context"])
        assert item.trip_id == tid and item.review["trip_pinned"] is True
        s.rollback()
    _drop_trip(tid)
