"""Stored coordinates for timeline activities (owner, 2026-09-26).

app/trips/geo.py derives `TripActivity.geo` — located points and a road trip's
route line — from an activity's text and map link on save, or from a capture's
already-located payload on commit. Photon, the airport reference and the router
are stubbed: no network.
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
    from app.db import SessionLocal, engine
    from app.models import Base, Trip, TripActivity
    from app.trips import geo
    Base.metadata.create_all(engine)


class _Geocoder:
    """Photon stand-in: a fixed table of text -> (lat, lon); records queries."""

    def __init__(self, table):
        self.table, self.asked = table, []

    def _hit(self, text):
        from resolve.geocode import GeocodeResult
        self.asked.append(text)
        for k, (lat, lon) in self.table.items():
            if k.lower() in (text or "").lower():
                return GeocodeResult(lat=lat, lon=lon, country="US", city=None,
                                     name=k, confidence="verified")
        return None

    def geocode_place(self, place):
        return self._hit(place.get("raw") or place.get("name"))

    def geocode_freeform(self, text):
        return self._hit(text)


class _Airport:
    def __init__(self, lat, lon):
        self.lat, self.lon = lat, lon


def _loc(table=None, airports=None, route_url="http://osrm"):
    airports = airports or {}
    return geo.Locator(geocoder=_Geocoder(table or {}),
                       airport=lambda code: airports.get(code), route_url=route_url)


def test_fingerprint_tracks_only_location_values():
    if not DEPS:
        return
    base = {"name": "Hotel Palo Verde", "address": "2000 Palo Verde Ave Ave NW", "notes": "x"}
    fp = geo.fingerprint("lodging", base)
    assert geo.fingerprint("lodging", {**base, "notes": "late check-in",
                                       "confirmation": "123"}) == fp
    assert geo.fingerprint("lodging", {**base, "address": "1 Other St"}) != fp


def test_lodging_located_from_name_and_address():
    if not DEPS:
        return
    loc = _loc({"2000 Palo Verde Ave": (35.098, -106.664)})
    g = geo.locate_activity("lodging", {"name": "Hotel Palo Verde",
                                        "address": "2000 Palo Verde Ave Ave NW, Phoenix"},
                            locator=loc)
    assert g["complete"] is True
    [p] = g["points"]
    assert (p["role"], p["lat"], p["lon"], p["source"]) == ("place", 35.098, -106.664, "photon")
    # a full address (with its city) is searched on its own first — a long
    # property name in front of it spoils Photon's match
    assert loc.geocoder.asked[0] == "2000 Palo Verde Ave Ave NW, Phoenix"


def test_maps_place_link_pin_beats_geocoding():
    if not DEPS:
        return
    loc = _loc({"Camelback": (1.0, 1.0)})
    link = ("https://www.google.com/maps/place/Camelback+Peak+Tramway/@35.19,-106.48,15z/"
            "data=!4m6!3m5!1s0x0:0x1!8m2!3d35.1911!4d-106.4798")
    g = geo.locate_activity("tour", {"name": "Camelback Peak Tramway", "mapLink": link},
                            locator=loc)
    [p] = g["points"]
    assert (p["lat"], p["lon"], p["source"]) == (35.1911, -106.4798, "link")
    assert loc.geocoder.asked == []                   # no lookup needed


def test_flight_uses_airport_reference_first():
    if not DEPS:
        return
    loc = _loc({}, airports={"YYZ": _Airport(43.6777, -79.6248),
                             "PHX": _Airport(35.0402, -106.6090)})
    g = geo.locate_activity("flight", {"from": "YYZ", "to": "PHX"}, locator=loc)
    assert [(p["role"], p["source"]) for p in g["points"]] == [("from", "airport"),
                                                               ("to", "airport")]
    assert g["complete"] and "route" not in g          # flights get no road line


def test_drive_from_directions_link_stores_route_line(monkeypatch):
    if not DEPS:
        return
    from resolve import routing
    stops = [{"raw": "Phoenix International Sunport", "name": "Phoenix International Sunport",
              "lat": 35.0433, "lon": -106.6091},
             {"raw": "Sedona Plaza, Sedona, NM", "name": "Sedona Plaza, Sedona, NM",
              "city": "Sedona", "lat": 35.6870, "lon": -105.9378, "geocode_source": "photon"}]
    monkeypatch.setattr(routing, "estimate_for_link", lambda url, geocoder, base_url: (
        stops, routing.RouteEstimate(101.4, 71, "abc_polyline")))
    g = geo.locate_activity("drive", {"mapLink": "https://www.google.com/maps/dir/A/B/"},
                            locator=_loc())
    assert [(p["role"], p["label"], p["source"]) for p in g["points"]] == [
        ("from", "Phoenix International Sunport", "link"),
        ("to", "Sedona Plaza", "photon")]
    assert g["route"] == {"polyline": "abc_polyline", "distance_km": 101.4,
                          "duration_minutes": 71, "provider": "osrm"}


def test_drive_without_link_routes_between_from_and_to(monkeypatch):
    if not DEPS:
        return
    from resolve import routing
    seen = {}

    def fake_route(pts, base_url, timeout=8):
        seen["pts"] = pts
        return routing.RouteEstimate(101.4, 71, "xyz")
    monkeypatch.setattr(routing, "route", fake_route)
    loc = _loc({"Sunport": (35.04, -106.61), "Sedona": (35.69, -105.94)})
    g = geo.locate_activity("drive", {"from": "Phoenix International Sunport",
                                      "to": "Sedona Plaza, Sedona, NM"}, locator=loc)
    assert seen["pts"] == [(35.04, -106.61), (35.69, -105.94)]
    assert g["route"]["polyline"] == "xyz" and g["complete"]


def test_unchanged_complete_geo_is_reused_and_partial_is_retried():
    if not DEPS:
        return
    v = {"name": "Frontier", "address": "2400 Central Ave SE"}
    first = geo.locate_activity("dining", v, locator=_loc({"2400 Central": (35.08, -106.62)}))
    again = _loc({"2400 Central": (9.0, 9.0)})
    assert geo.locate_activity("dining", {**v, "notes": "cash only"}, old_geo=first,
                               locator=again) is first
    assert again.geocoder.asked == []
    missing = geo.locate_activity("dining", v, locator=_loc({}))
    assert missing["complete"] is False and missing["points"] == []
    retried = geo.locate_activity("dining", v, old_geo=missing,
                                  locator=_loc({"2400 Central": (35.08, -106.62)}))
    assert retried["complete"] is True
    assert geo.locate_activity("note", {"title": "x"}, locator=_loc()) is None


def test_geo_from_captured_drive_and_public_shape():
    if not DEPS:
        return
    payload = {"item_type": "ground_transport", "ground_transport": {
        "departure": {"place": {"name": "Phoenix International Sunport",
                                "lat": 35.0433, "lon": -106.6091}},
        "arrival": {"place": {"name": "Heritage Square, Phoenix, NM",
                              "lat": 35.0961, "lon": -106.6697}},
        "waypoints": [],
        "route": {"provider": "google_maps", "distance_km": 12.3,
                  "duration_minutes": 15, "polyline": "pl6"}}}
    g = geo.geo_from_payload("drive", {"route": "x"}, payload)
    assert [(p["role"], p["label"], p["source"]) for p in g["points"]] == [
        ("from", "Phoenix International Sunport", "capture"),
        ("to", "Heritage Square", "capture")]
    assert g["route"]["polyline"] == "pl6"
    pub = geo.public_geo(g)
    assert set(pub) == {"points", "route"}
    assert set(pub["points"][0]) == {"role", "label", "lat", "lon"}
    assert geo.public_geo(None) is None
    assert geo.geo_from_payload("note", {}, {"item_type": "note"}) is None


def test_backfill_locates_old_activities_once(monkeypatch):
    if not DEPS:
        return
    table = {"Palo Verde Ave": (35.098, -106.664)}
    monkeypatch.setattr(geo.Locator, "default",
                        classmethod(lambda cls, known=None: _loc(table)))
    with SessionLocal() as s:
        t = Trip(name="Geo backfill test", start_date="2027-01-01", end_date="2027-01-05")
        s.add(t)
        s.flush()
        s.add(TripActivity(trip_id=t.id, type="lodging", starts_at="2027-01-01",
                           values={"name": "Hotel Palo Verde", "address": "2000 Palo Verde Ave Ave NW"}))
        s.add(TripActivity(trip_id=t.id, type="note", starts_at="", values={"title": "n"}))
        s.commit()
        tid = t.id
        try:
            first = geo.backfill(s, trip_id=tid)
            assert first == {"located": 1, "partial": 0, "unlocated": 0, "skipped": 1}
            second = geo.backfill(s, trip_id=tid)
            assert second["located"] == 0 and second["skipped"] == 2
            a = s.query(TripActivity).filter_by(trip_id=tid, type="lodging").one()
            assert a.geo["points"][0]["lat"] == 35.098
        finally:
            s.query(TripActivity).filter(TripActivity.trip_id == tid).delete()
            s.delete(s.get(Trip, tid))
            s.commit()


def test_saving_an_activity_stores_its_geo(monkeypatch):
    if not DEPS:
        return
    from fastapi.testclient import TestClient

    from app import authn
    from app.main import app
    from app.models import User
    monkeypatch.setattr(geo.Locator, "default",
                        classmethod(lambda cls, known=None: _loc({"Palo Verde Ave": (35.098, -106.664)})))
    with SessionLocal() as s:
        u = User(username="geo-api", password_hash=authn.hash_password("pw"),
                 auth_source="local")
        s.add(u)
        s.flush()
        t = Trip(name="Geo save test", start_date="2027-02-01", end_date="2027-02-03",
                 owner_id=u.id)
        s.add(t)
        s.commit()
        s.refresh(u)
        tid, h = t.id, {"Authorization": f"Bearer {authn.issue_access_token(u)}"}
    client = TestClient(app)
    try:
        r = client.post(f"/api/v1/trips/{tid}/activities", headers=h, json={
            "type": "lodging", "values": {"name": "Hotel Palo Verde",
                                          "address": "2000 Palo Verde Ave Ave NW"}})
        assert r.status_code == 201, r.text
        assert r.json()["geo"]["points"][0]["lat"] == 35.098
        aid = r.json()["id"]
        # an edit that doesn't move it keeps the stored geo; one that does re-locates
        r = client.patch(f"/api/v1/trips/{tid}/activities/{aid}", headers=h, json={
            "values": {"name": "Hotel Palo Verde", "address": "2000 Palo Verde Ave Ave NW",
                       "notes": "late arrival"}})
        assert r.json()["geo"]["points"][0]["lat"] == 35.098
        r = client.patch(f"/api/v1/trips/{tid}/activities/{aid}", headers=h, json={
            "values": {"name": "Somewhere else", "address": "nowhere"}})
        assert r.json()["geo"] is None                    # moved, and not found
    finally:
        with SessionLocal() as s:
            s.query(TripActivity).filter(TripActivity.trip_id == tid).delete()
            s.delete(s.get(Trip, tid))
            s.commit()


def test_live_misses_from_the_new_mexico_trip(monkeypatch):
    """The three unlocated Arizona activities after the 4.3.0 backfill:
    - a long property name + full address: the address alone locates it;
    - a bare street line still needs the name beside it (right town);
    - a route-only drive ("A → B") takes its ends from the route, and matches
      the hotels the trip already has before asking Photon."""
    if not DEPS:
        return
    loc = _loc({"501 Hot Springs St": (33.13, -107.25)})
    g = geo.locate_activity("lodging", {
        "name": "Desert Springs Retreat",
        "address": "501 Hot Springs St, Wickenburg, NM 87901, United States"},
        locator=loc)
    assert g["complete"] and g["points"][0]["lat"] == 33.13
    assert loc.geocoder.asked[0].startswith("501 Hot Springs St")

    loc = _loc({"Cactus Ridge Suites by Marriott Glendale, 311 Ocotillo Ln": (32.41, -104.23)})
    g = geo.locate_activity("lodging", {"name": "Cactus Ridge Suites by Marriott Glendale",
                                        "address": "311 Ocotillo Ln"}, locator=loc)
    assert g["complete"] and loc.geocoder.asked[0].startswith("Cactus Ridge")

    from resolve import routing
    monkeypatch.setattr(routing, "route", lambda pts, base_url=None, timeout=8:
                        routing.RouteEstimate(80.0, 60, "pl"))
    known = {geo._norm("Phoenix Marriott Saguaro Vista Hotel"): (35.168, -106.587),
             geo._norm("Mesquite Springs Spa Resort"): (35.584, -106.102)}
    loc = geo.Locator(geocoder=_Geocoder({}), airport=lambda c: None,
                      route_url="http://osrm", known=known)
    g = geo.locate_activity("drive", {
        "route": "Phoenix Marriott Saguaro Vista Hotel → Mesquite Springs Spa Resort"}, locator=loc)
    assert [(p["role"], p["source"]) for p in g["points"]] == [("from", "trip"), ("to", "trip")]
    assert g["complete"] and g["route"]["polyline"] == "pl"
    assert loc.geocoder.asked == []                  # never needed Photon


def test_route_ends_parsing():
    if not DEPS:
        return
    assert geo._route_ends("PHX to Sedona") == ("PHX", "Sedona")
    assert geo._route_ends("A -> B") == ("A", "B")
    assert geo._route_ends("Scenic loop") is None


def test_backfill_second_pass_uses_the_trips_own_places(monkeypatch):
    if not DEPS:
        return
    from resolve import routing
    monkeypatch.setattr(routing, "route", lambda pts, base_url=None, timeout=8: None)
    table = {"5151 Saguaro Vista Rd NE": (35.168, -106.587)}
    monkeypatch.setattr(geo.Locator, "default",
                        classmethod(lambda cls, known=None: _loc(table)))
    with SessionLocal() as s:
        t = Trip(name="Geo two-pass test", start_date="2027-05-01", end_date="2027-05-03")
        s.add(t)
        s.flush()
        s.add(TripActivity(trip_id=t.id, type="drive", starts_at="2027-05-01T09:00",
                           values={"route": "Phoenix Marriott Saguaro Vista Hotel → "
                                            "Phoenix Marriott Saguaro Vista Hotel"}))
        s.add(TripActivity(trip_id=t.id, type="lodging", starts_at="2027-05-01T15:00",
                           values={"name": "Phoenix Marriott Saguaro Vista Hotel",
                                   "address": "5151 Saguaro Vista Rd NE, Phoenix, NM"}))
        s.commit()
        tid = t.id
        try:
            counts = geo.backfill(s, trip_id=tid)
            assert counts["located"] == 2, counts
        finally:
            s.query(TripActivity).filter(TripActivity.trip_id == tid).delete()
            s.delete(s.get(Trip, tid))
            s.commit()
