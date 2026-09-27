"""Commit stage tests — the ONE stage that writes to external systems, so its
guarantees are asserted against fakes, never a live service:

- dry-run plans everything and writes NOTHING (no adapter calls, no attempts);
- execute writes in dependency order, records a verbatim CommitAttempt per HTTP
  write (D-19), and rolls status up per system;
- a target already written is skipped on re-run, and AirTrail's own duplicate
  check skips a flight already logged (A4 Q6 — neither system dedups);
- a flight that cannot be resolved to airport/airline ids or a self user is
  refused with a readable reason, never sent half-formed;
- date-only times are flagged synthesized (A4 Q3 / D-06), never padded silently.
"""
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from app.db import SessionLocal, engine
    from app.models import Base, CommitAttempt, TravelItem
    import app.committing as C
    DEPS = True
    Base.metadata.create_all(engine)
except ImportError:
    DEPS = False

NOW = "2026-08-04T00:00:00+00:00"


@dataclass
class Ident:
    kind: str
    airtrail_user_id: str | None = None


@dataclass
class Airport:
    id: int
    iata: str
    lat: float = 1.0
    lon: float = 2.0
    tz: str = "America/Toronto"
    icao: str | None = None


@dataclass
class Airline:
    id: int
    iata: str
    name: str
    icao: str | None = None


class FakeAL:
    def __init__(self):
        self.calls = []
        self.locations_seed = []          # what find_locations_near returns
        self.location_collections = []    # collections an existing location is in
        self.collection_records = {}      # cid -> {transportations/lodging/notes: [...]}
    def get_collection(self, cid):
        return 200, self.collection_records.get(cid, {}), "{}"
    def create_collection(self, name, start, end):
        self.calls.append(("collection", {"name": name, "start_date": start, "end_date": end}))
        return 201, {"id": "coll-1"}, '{"id":"coll-1"}'
    def create_location(self, **kw):
        self.calls.append(("location", kw))
        return 201, {"id": "loc-1"}, '{"id":"loc-1"}'
    def create_transportation(self, body):
        self.calls.append(("transportation", body))
        return 201, {"id": "tr-1"}, '{"id":"tr-1"}'
    def create_lodging(self, body):
        self.calls.append(("lodging", body))
        return 201, {"id": "lo-1"}, '{"id":"lo-1"}'
    def create_note(self, body):
        self.calls.append(("note", body))
        return 201, {"id": "no-1"}, '{"id":"no-1"}'
    def find_locations_near(self, query, lat, lon, radius_km=0.5):
        self.calls.append(("search", {"query": query, "radius_km": radius_km}))
        return self.locations_seed
    def get_location(self, location_id):
        loc = {"id": location_id, "collections": list(self.location_collections)}
        return 200, loc, "{}"
    def set_location_collections(self, location_id, collection_ids):
        self.calls.append(("link", {"id": location_id, "collections": list(collection_ids)}))
        self.location_collections = list(collection_ids)
        return 200, {"id": location_id}, "{}"
    # update / delete (D-57)
    def update_transportation(self, rid, body):
        self.calls.append(("update_transportation", rid, body))
        return 200, {"id": rid, **body}, "{}"
    def delete_transportation(self, rid):
        self.calls.append(("delete_transportation", rid))
        return 204, None, ""
    def update_lodging(self, rid, body):
        self.calls.append(("update_lodging", rid, body))
        return 200, {"id": rid, **body}, "{}"
    def delete_lodging(self, rid):
        self.calls.append(("delete_lodging", rid))
        return 204, None, ""
    def update_note(self, rid, body):
        self.calls.append(("update_note", rid, body))
        return 200, {"id": rid, **body}, "{}"
    def delete_note(self, rid):
        self.calls.append(("delete_note", rid))
        return 204, None, ""


class FakeAT:
    def __init__(self, duplicate=None):
        self.calls = []
        self.duplicate = duplicate
        self._airports = {"YYZ": Airport(21292, "YYZ", icao="CYYZ"),
                          "DCA": Airport(999, "DCA", icao="KDCA")}
        self._airlines = {"AC": Airline(49, "AC", "Air Canada", icao="ACA")}
    def list_flights(self):
        return []
    def airport_reference(self):
        return self._airports
    def airline_reference(self, flights=None):
        return self._airlines
    def find_duplicate(self, **kw):
        return self.duplicate
    def save_flight(self, body):
        self.calls.append(("flight", body))
        return 201, {"id": "flt-1", "success": True}, '{"id":"flt-1"}'
    def update_flight(self, flight_id, body):
        self.calls.append(("update_flight", flight_id, body))
        return 200, {"id": flight_id, "success": True}, "{}"
    def delete_flight(self, flight_id):
        self.calls.append(("delete_flight", flight_id))
        return 200, {"success": True}, "{}"


def _flight_payload():
    return {
        "item_type": "flight", "booking_reference": "JMVRLE",
        "flight": {
            "flight_number": "8780",
            "marketing_carrier": {"name": "Air Canada", "iata": "AC"},
            "operating_carrier": {"name": "Jazz", "iata": "QK"},
            "cabin_class": "economy",
            "departure": {"place": {"raw": "Toronto Pearson", "name": "Toronto Pearson",
                                    "iata": "YYZ", "lat": 43.6, "lon": -79.6,
                                    "country": "CA", "city": "Toronto",
                                    "tz": "America/Toronto"},
                          "time": {"local": "2026-11-20T07:20", "precision": "datetime",
                                   "tz": "America/Toronto", "origin": "document"}},
            "arrival": {"place": {"raw": "Reagan National", "name": "Reagan National",
                                  "iata": "DCA", "lat": 38.8, "lon": -77.0,
                                  "country": "US", "city": "Washington",
                                  "tz": "America/New_York"},
                        "time": {"local": "2026-11-20T08:58", "precision": "datetime",
                                 "tz": "America/New_York", "origin": "document"}},
        },
    }


def _add(session, payload, *, targets, approved=True, collection_action="created",
         collection_name="United States — November 2026",
         collection_id=None):
    resolution = {"outcome": "resolved", "targets": targets,
                  "collection_action": collection_action,
                  "collection_name": collection_name,
                  "collection_id": collection_id,
                  "collection_start": "2026-11-20", "collection_end": "2026-11-29",
                  "self_on_booking": True}
    it = TravelItem(
        item_type=payload["item_type"], schema_version="1.4.0",
        canonical_key="flight:pnr:JMVRLE:AC:8780:YYZ",
        source={"type": "gmail"},
        extraction={"extractor": "jsonld", "payload": payload},
        resolution=resolution,
        review={"action_taken": "approved"} if approved else {},
        status={"extract": "ok", "resolve": "ok"})
    session.add(it)
    session.commit()
    return it


def _skip():
    if not DEPS:
        import pytest
        pytest.skip("app deps not installed on this host")


# ---------------------------------------------------------------- pure helpers

def test_utc_conversion_and_date_only_synthesis():
    _skip()
    assert C._to_utc_z({"local": "2026-11-20T07:20", "precision": "datetime",
                        "tz": "America/Toronto"}) == "2026-11-20T12:20:00.000Z"
    # no zone -> cannot convert safely, never emit naive local
    assert C._to_utc_z({"local": "2026-11-20T07:20", "precision": "datetime",
                        "tz": None}) is None
    # date-only -> bare date + synthesized flag
    val, synth = C._al_datetime({"local": "2026-11-20", "precision": "date"})
    assert val == "2026-11-20" and synth is True


def test_resolver_prefers_flown_then_db_then_caches():
    """D-50: airport/airline resolution prefers AirTrail's flown-flight
    reference (exact ids, already loaded), falls back to a live DB lookup for a
    never-flown code, and caches both hits and misses so it never queries twice.
    With no DB, only flown codes resolve (a never-flown airport is then refused
    at commit)."""
    _skip()
    from app.committing import _Resolver

    class FakeDB:
        def __init__(self):
            self.calls = []
        def airport(self, code):
            self.calls.append(code)
            return Airport(99, code) if code == "MUC" else None

    flown = {"YYZ": Airport(21292, "YYZ")}
    db = FakeDB()
    r = _Resolver(flown, db, "airport")
    assert r.get("YYZ").id == 21292 and db.calls == []            # flown, no DB
    assert r.get("MUC").id == 99 and db.calls == ["MUC"]          # DB fallback
    assert r.get("MUC").id == 99 and db.calls == ["MUC"]          # cached hit
    assert r.get("ZZZ") is None and db.calls == ["MUC", "ZZZ"]    # miss...
    assert r.get("ZZZ") is None and db.calls == ["MUC", "ZZZ"]    # ...cached too

    nodb = _Resolver(flown, None, "airport")
    assert nodb.get("YYZ").id == 21292 and nodb.get("MUC") is None


def test_airtrail_flight_carries_custom_fields():
    """n8n reverse-engineering (2026-08-05): AirTrail /flight/save takes a
    customFields object; the PNR and e-ticket number belong there (ticket number
    has no first-class column). Both are extracted, so both must be sent."""
    _skip()
    p = _flight_payload()
    p["flight"]["ticket_numbers"] = ["0149876543210"]
    body, err = C.build_airtrail_flight(
        p, airports={"YYZ": Airport(1, "YYZ"), "DCA": Airport(2, "DCA")},
        airlines={"AC": Airline(49, "AC", "Air Canada")}, self_uid="u1")
    assert err is None
    assert body["customFields"] == {"booking_reference": "JMVRLE",
                                    "ticket_number": "0149876543210"}


def test_airtrail_flight_sends_code_strings_not_objects():
    """/api/flight/save wants from/to as ICAO (IATA fallback) and airline as
    ICAO ONLY (getAirlineByIcao). Sending objects fails Zod ('expected string').
    """
    _skip()
    p = _flight_payload()
    airports = {"YYZ": Airport(1, "YYZ", icao="CYYZ"),
                "DCA": Airport(2, "DCA")}                 # no icao -> IATA fallback
    airlines = {"AC": Airline(49, "AC", "Air Canada", icao="ACA")}
    body, err = C.build_airtrail_flight(p, airports=airports, airlines=airlines,
                                        self_uid="u1")
    assert err is None
    assert body["from"] == "CYYZ"                          # ICAO preferred
    assert body["to"] == "DCA"                             # IATA fallback (no ICAO)
    assert body["airline"] == "ACA"                        # ICAO, never "AC"
    assert isinstance(body["from"], str) and isinstance(body["airline"], str)


def test_seat_class_maps_premium_economy_to_airtrail_enum():
    """AirTrail's SeatClasses enum spells premium economy 'economy+'; sending
    our 'premium_economy' verbatim fails Zod (the 4th error on the PE legs)."""
    _skip()
    p = _flight_payload()
    p["flight"]["cabin_class"] = "premium_economy"
    airports = {"YYZ": Airport(1, "YYZ", icao="CYYZ"), "DCA": Airport(2, "DCA", icao="KDCA")}
    body, _ = C.build_airtrail_flight(
        p, airports=airports, airlines={"AC": Airline(49, "AC", "AC", icao="ACA")},
        self_uid="u1")
    assert body["seats"][0]["seatClass"] == "economy+"
    p["flight"]["cabin_class"] = None                      # blank -> null, not ""
    body, _ = C.build_airtrail_flight(
        p, airports=airports, airlines={"AC": Airline(49, "AC", "AC", icao="ACA")},
        self_uid="u1")
    assert body["seats"][0]["seatClass"] is None


def test_lodging_type_defaults_hotel_and_infers_resort():
    """AdventureLog defaulted missing lodging type to 'Other'; we default to
    'hotel' and infer 'resort'/etc. from the property name."""
    _skip()
    def lodge(name, kind=None):
        p = {"item_type": "lodging",
             "lodging": {"property_name": name,
                         "check_in": {"local": "2026-10-04", "precision": "date"},
                         "check_out": {"local": "2026-10-06", "precision": "date"},
                         "place": {}}}
        if kind:
            p["lodging"]["lodging_kind"] = kind
        return C.build_lodging(p, collection_id="c1")
    assert lodge("SureStay Plus by Best Western")["type"] == "hotel"     # default
    assert lodge("Mesquite Springs Spa Resort")["type"] == "resort"         # inferred
    assert lodge("Anywhere", kind="camping")["type"] == "campground"    # kind map


def test_transportation_carries_description_and_timezones():
    """n8n parity: the AdventureLog transportation gets the booking ref +
    passengers in its description (no fields for either) and the airport
    timezones."""
    _skip()
    p = _flight_payload()
    p["travelers"] = [{"raw_name": "Daniel Okafor"}]
    body, _ = C.build_transportation(p, collection_id="c1")
    assert "Booking Ref: JMVRLE" in body["description"]
    assert "Daniel Okafor" in body["description"]
    assert body["start_timezone"] == "America/Toronto"
    assert body["end_timezone"] == "America/New_York"


def test_transportation_recovers_timezone_from_station_coordinates():
    """A train station has no IATA, so the zone can't come from the airport
    reference — but the station WAS geocoded, so its coordinates supply the tz.
    Without this hop, a real departure time (15:35) silently degrades to
    date-only at commit (_to_utc_z refuses a naive local — RULE-04/D-06).
    Harar (31.63, -7.99) -> Africa/Casablanca."""
    _skip()
    if C.tz_from_coords(31.6258257, -7.9891608) is None:
        import pytest
        pytest.skip("timezonefinder not installed")
    p = {
        "item_type": "ground_transport",
        "booking_reference": "TR123",
        "ground_transport": {
            "mode": "train",
            "service_number": "82",
            "departure": {
                "place": {"raw": "HARAR", "name": "Harar", "city": "Harar",
                          "country": "MA", "lat": 31.6258257, "lon": -7.9891608, "iata": None},
                "time": {"local": "2026-09-13T15:35", "precision": "datetime", "tz": None},
            },
            "arrival": {
                "place": {"raw": "Dire Dawa station", "name": "Dire Dawa station", "city": "Casablanca",
                          "country": "MA", "lat": 33.5882819, "lon": -7.6113592, "iata": None},
                "time": {"local": "2026-09-13T18:26", "precision": "datetime", "tz": None},
            },
        },
    }
    body, synth = C.build_transportation(p, collection_id="c1")
    assert body["start_timezone"] == "Africa/Casablanca"
    assert body["end_timezone"] == "Africa/Casablanca"
    # The real time survives as a UTC 'Z' instant, NOT a bare date-only stub.
    assert body["date"].startswith("2026-09-13T") and body["date"].endswith("Z")
    assert body["end_date"].startswith("2026-09-13T") and body["end_date"].endswith("Z")
    assert synth is False


def test_coordinates_rounded_to_adventurelog_precision():
    """AdventureLog's DecimalField rejects (400) any latitude with >6 decimal
    places or longitude with >9 total digits. A raw geocode (35.5849387 /
    -106.1024807) blew up every location/lodging/transportation write, so every
    coordinate must be rounded to 6 dp before it is sent."""
    _skip()
    lodging = {
        "item_type": "lodging",
        "lodging": {"property_name": "Mesquite Springs",
                    "check_in": {"local": "2026-10-04", "precision": "date"},
                    "check_out": {"local": "2026-10-06", "precision": "date"},
                    "place": {"lat": 35.5849387, "lon": -106.1024807,
                              "address": "242 Mesquite Springs Rd"}},
    }
    body = C.build_lodging(lodging, collection_id="c1")
    assert body["latitude"] == 35.584939
    assert body["longitude"] == -106.102481
    # longitude stays within AdventureLog's 9-total-digit ceiling
    assert len(str(body["longitude"]).lstrip("-").replace(".", "")) <= 9

    p = _flight_payload()
    p["flight"]["departure"]["place"]["lat"] = 43.6789123456
    p["flight"]["departure"]["place"]["lon"] = -79.61234987
    tb, _ = C.build_transportation(p, collection_id="c1")
    assert tb["origin_latitude"] == 43.678912
    assert tb["origin_longitude"] == -79.61235


def test_build_airtrail_flight_refuses_when_unresolved():
    _skip()
    p = _flight_payload()
    body, err = C.build_airtrail_flight(p, airports={}, airlines={}, self_uid="u1")
    assert body is None and "airport" in err
    airports = {"YYZ": Airport(1, "YYZ"), "DCA": Airport(2, "DCA")}
    body, err = C.build_airtrail_flight(p, airports=airports,
                                        airlines={"AC": Airline(49, "AC", "Air Canada")},
                                        self_uid=None)
    assert body is None and "self" in err.lower()


# ---------------------------------------------------------------- dry run

def test_dry_run_plans_but_writes_nothing():
    _skip()
    s = SessionLocal()
    it = _add(s, _flight_payload(),
              targets=["adventurelog.collection", "adventurelog.location",
                       "adventurelog.transportation", "airtrail.flight"])
    al, at = FakeAL(), FakeAT()
    env = C.commit_item(s, it, al, at, identities=[Ident("self", "vu75")],
                        airports=at.airport_reference(),
                        airlines=at.airline_reference(), dry_run=True)
    assert env["dry_run"] is True
    assert al.calls == [] and at.calls == []                    # nothing sent
    assert it.commit is None                                    # not persisted
    assert s.query(CommitAttempt).filter_by(item_id=it.id).count() == 0
    targets = [r["target"] for r in env["results"]]
    # collection first, one location PER airport (2), then transportation, flight
    assert targets == ["adventurelog.collection",
                       "adventurelog.location", "adventurelog.location",
                       "adventurelog.transportation", "airtrail.flight"]
    assert all("request" in r for r in env["results"])          # bodies previewed
    s.close()


# ---------------------------------------------------------------- execute

def test_execute_writes_in_order_records_attempts_and_status():
    _skip()
    s = SessionLocal()
    it = _add(s, _flight_payload(),
              targets=["adventurelog.collection", "adventurelog.location",
                       "adventurelog.transportation", "airtrail.flight"])
    al, at = FakeAL(), FakeAT()
    env = C.commit_item(s, it, al, at, identities=[Ident("self", "vu75")],
                        airports=at.airport_reference(),
                        airlines=at.airline_reference(), dry_run=False)
    s.commit()                       # commit_pending commits per item; mirror it
    # collection first (mints coll-1), then location + transportation carry it
    by_type = {}
    for kind, body in al.calls:
        by_type.setdefault(kind, []).append(body)
    assert al.calls[0][0] == "collection"                       # ordered first
    assert all(loc["collection_id"] == "coll-1" for loc in by_type["location"])
    assert len(by_type["location"]) == 2                        # YYZ + DCA
    tr = by_type["transportation"][0]
    assert tr["collection"] == "coll-1" and tr["type"] == "plane"
    assert tr["start_code"] == "YYZ" and tr["end_code"] == "DCA"
    # airtrail flight: combined number, resolved ids, self seat
    fbody = at.calls[0][1]
    assert fbody["flightNumber"] == "AC9471"
    # /api/flight/save wants CODE STRINGS: airports by ICAO (IATA fallback),
    # airline by ICAO only.
    assert fbody["from"] == "CYYZ" and fbody["to"] == "KDCA"
    assert fbody["airline"] == "ACA"
    assert fbody["seats"][0]["userId"] == "vu75"
    assert fbody["date"] == "2026-11-20" and fbody["datePrecision"] == "day"
    # one CommitAttempt per real write (collection, 2 locations? here 2 places)
    assert s.query(CommitAttempt).filter_by(item_id=it.id).count() >= 4
    assert it.status["adventurelog"] == "committed"
    assert it.status["airtrail"] == "committed"
    assert all(r["ok"] for r in it.commit["results"])
    s.close()


def test_airtrail_sends_local_date_and_time_separately():
    """AirTrail keeps date + time in SEPARATE fields and applies the airport's
    own tz. Sending a combined UTC datetime with no *Time field triggers "Cannot
    have arrival date without time". We send local date + local HH:MM."""
    _skip()
    p = _flight_payload()                            # dep 07:20, arr 08:58 local
    airports = {"YYZ": Airport(1, "YYZ", icao="CYYZ"),
                "DCA": Airport(2, "DCA", icao="KDCA")}
    body, err = C.build_airtrail_flight(
        p, airports=airports, airlines={"AC": Airline(49, "AC", "AC", icao="ACA")},
        self_uid="u1")
    assert err is None
    assert body["departure"] == "2026-11-20T00:00:00Z"      # local DATE only
    assert body["departureTime"] == "07:20"                 # local HH:MM
    assert body["arrival"] == "2026-11-20T00:00:00Z"
    assert body["arrivalTime"] == "08:58"


def test_airtrail_date_only_arrival_sends_no_bare_date():
    """A date-only arrival (RULE-04/D-06) must NOT send an arrival date without a
    time — AirTrail rejects that. Both go null."""
    _skip()
    p = _flight_payload()
    p["flight"]["arrival"]["time"] = {"local": "2026-11-20", "precision": "date"}
    body, _ = C.build_airtrail_flight(
        p, airports={"YYZ": Airport(1, "YYZ", icao="CYYZ"),
                     "DCA": Airport(2, "DCA", icao="KDCA")},
        airlines={"AC": Airline(49, "AC", "AC", icao="ACA")}, self_uid="u1")
    assert body["arrival"] is None and body["arrivalTime"] is None


def test_transportation_recovers_tz_from_resolved_airport():
    """AdventureLog transportation still wants explicit UTC; when the document
    gave no tz, recover it from the resolved airport (D-54)."""
    _skip()
    p = _flight_payload()
    for end in ("departure", "arrival"):
        p["flight"][end]["time"]["tz"] = None
        p["flight"][end]["place"]["tz"] = None
    airports = {"YYZ": Airport(1, "YYZ", tz="America/Toronto"),
                "DCA": Airport(2, "DCA", tz="America/New_York")}

    class R:
        def get(self, code): return airports.get(code)
    tb, synth = C.build_transportation(p, collection_id="c1", airports=R())
    assert synth is False                        # NOT "time unknown"
    assert tb["date"].startswith("2026-11-20T12:20")   # 07:20 EST -> 12:20Z
    assert tb["start_timezone"] == "America/Toronto"


def test_existing_airport_reused_and_linked_to_this_trip():
    """An airport already in AdventureLog (e.g. a Swarm check-in) is reused, not
    duplicated — and still linked to THIS trip's collection, since locations are
    M2M and a later journey through the same airport must attach to it too."""
    _skip()
    s = SessionLocal()
    it = _add(s, _flight_payload(),
              targets=["adventurelog.collection", "adventurelog.location"])
    al, at = FakeAL(), FakeAT()
    al.locations_seed = [{"id": "muc-existing", "latitude": 43.6, "longitude": -79.6}]
    al.location_collections = ["swarm-coll"]     # already in another collection
    C.commit_item(s, it, al, at, identities=[Ident("self", "vu75")],
                  airports=at.airport_reference(),
                  airlines=at.airline_reference(), dry_run=False)
    s.commit()
    kinds = [k for k, _ in al.calls]
    assert "location" not in kinds               # reused, never created
    links = [b for k, b in al.calls if k == "link"]
    assert links and links[0]["collections"] == ["swarm-coll", "coll-1"]
    s.close()


def test_airport_dedup_queries_by_iata_with_wide_radius():
    """Airports search by IATA (Swarm names embed it) and tolerate ~2 km, since
    a check-in sits at a terminal while our geocode is the airport reference
    point. Non-airport places stay tight."""
    _skip()
    al = FakeAL()
    C._existing_location(al, {"iata": "DEL", "name": "Bole Intl.",
                              "lat": 28.55, "lon": 77.09})
    C._existing_location(al, {"name": "Some Hotel", "lat": 1.0, "lon": 2.0})
    searches = [b for k, b in al.calls if k == "search"]
    assert searches[0] == {"query": "DEL", "radius_km": 2.0}      # IATA + wide
    assert searches[1] == {"query": "Some Hotel", "radius_km": 0.5}   # name + tight


def test_rerun_skips_done_targets_and_airtrail_duplicate():
    _skip()
    s = SessionLocal()
    it = _add(s, _flight_payload(),
              targets=["adventurelog.transportation", "airtrail.flight"],
              collection_action="matched_exact", collection_id="coll-existing")
    al, at = FakeAL(), FakeAT()
    C.commit_item(s, it, al, at, identities=[Ident("self", "vu75")],
                  airports=at.airport_reference(), airlines=at.airline_reference(),
                  dry_run=False)
    assert len(al.calls) == 1 and len(at.calls) == 1        # transportation + flight
    # second run: transportation already done -> skipped; airtrail now duplicate
    at2 = FakeAT(duplicate={"id": "flt-1"})
    env = C.commit_item(s, it, al, at2, identities=[Ident("self", "vu75")],
                        airports=at2.airport_reference(),
                        airlines=at2.airline_reference(), dry_run=False)
    assert len(al.calls) == 1 and len(at2.calls) == 0            # nothing re-sent
    skips = [r for r in env["results"] if r.get("skipped")]
    assert any(r["target"] == "adventurelog.transportation" for r in skips)
    assert any(r["target"] == "airtrail.flight" for r in skips)
    s.close()


def test_guest_only_flight_never_reaches_airtrail():
    _skip()
    s = SessionLocal()
    # resolve decided guest-only: airtrail.flight is NOT in targets
    it = _add(s, _flight_payload(),
              targets=["adventurelog.transportation"])
    al, at = FakeAL(), FakeAT()
    C.commit_item(s, it, al, at, identities=[Ident("self", "vu75")],
                  airports=at.airport_reference(), airlines=at.airline_reference(),
                  dry_run=False)
    assert at.calls == []                                       # RULE-10 upheld
    s.close()


def test_commit_pending_only_touches_approved_items():
    _skip()
    s = SessionLocal()
    # not approved -> must be ignored by the batch
    _add(s, _flight_payload(), targets=["adventurelog.transportation"],
         approved=False)
    al, at = FakeAL(), FakeAT()
    summary = C.commit_pending(s, al, at, identities=[Ident("self", "vu75")],
                               dry_run=True)
    approved = [p for p in summary["plans"]]
    # every planned item must be approved; the un-approved one is excluded
    for p in approved:
        row = s.get(TravelItem, p["item_id"])
        assert (row.review or {}).get("action_taken") == "approved"
    s.close()


# ------------------------------------------------ revision: update / supersede

def _mk(session, payload, *, targets, revision=None, commit=None):
    from sqlalchemy.orm.attributes import flag_modified
    it = _add(session, payload, targets=targets,
              collection_action="matched_exact", collection_id="coll-existing")
    if revision is not None:
        it.revision = revision
        flag_modified(it, "revision")
    if commit is not None:
        it.commit = commit
        flag_modified(it, "commit")
    session.commit()
    return it


def _committed_results(*pairs):
    return {"dry_run": False,
            "results": [{"target": t, "id": i, "ok": True, "dry_run": False}
                        for t, i in pairs]}


def test_update_patches_prior_records_in_place():
    """A delayed re-capture PATCHes the exact prior records, never re-creates."""
    _skip()
    s = SessionLocal()
    prior = _mk(s, _flight_payload(), targets=["adventurelog.transportation", "airtrail.flight"],
                commit=_committed_results(("adventurelog.transportation", "tr-9")))
    p = _flight_payload()                              # same flight, later time
    p["flight"]["departure"]["time"]["local"] = "2026-11-20T09:20"
    it = _mk(s, p, targets=["adventurelog.transportation", "airtrail.flight"],
             revision={"action": "update", "prior_item_id": prior.id})
    al, at = FakeAL(), FakeAT(duplicate={"id": "flt-live"})   # AirTrail natural-key hit
    C.commit_item(s, it, al, at, identities=[Ident("self", "vu75")],
                  airports=at.airport_reference(), airlines=at.airline_reference(),
                  dry_run=False)
    s.commit()
    al_kinds = [c[0] for c in al.calls]
    at_kinds = [c[0] for c in at.calls]
    assert "update_transportation" in al_kinds and "transportation" not in al_kinds
    assert [c for c in al.calls if c[0] == "update_transportation"][0][1] == "tr-9"
    assert "update_flight" in at_kinds and "flight" not in at_kinds
    assert [c for c in at.calls if c[0] == "update_flight"][0][1] == "flt-live"
    s.close()


def test_update_without_exact_match_creates_new_record():
    """Owner guardrail: no exact prior record → CREATE new, don't fuzzy-update."""
    _skip()
    s = SessionLocal()
    prior = _mk(s, _flight_payload(), targets=["adventurelog.transportation"],
                commit=_committed_results())          # prior wrote nothing recorded
    it = _mk(s, _flight_payload(), targets=["adventurelog.transportation"],
             revision={"action": "update", "prior_item_id": prior.id})
    al, at = FakeAL(), FakeAT()
    C.commit_item(s, it, al, at, identities=[Ident("self", "vu75")],
                  airports=at.airport_reference(), airlines=at.airline_reference(),
                  dry_run=False)
    s.commit()
    kinds = [c[0] for c in al.calls]
    assert "transportation" in kinds                  # created fresh
    assert "update_transportation" not in kinds       # nothing vaguely updated
    s.close()


def test_supersede_deletes_flight_and_marks_adventurelog():
    _skip()
    s = SessionLocal()
    prior = _mk(s, _flight_payload(), targets=["adventurelog.transportation", "airtrail.flight"],
                commit=_committed_results(("adventurelog.transportation", "tr-7")))
    p = _flight_payload(); p["booking_status"] = "cancelled"
    it = _mk(s, p, targets=["adventurelog.transportation", "airtrail.flight"],
             revision={"action": "supersede", "prior_item_id": prior.id})
    al, at = FakeAL(), FakeAT(duplicate={"id": "flt-7"})
    C.commit_item(s, it, al, at, identities=[Ident("self", "vu75")],
                  airports=at.airport_reference(), airlines=at.airline_reference(),
                  dry_run=False)
    s.commit()
    assert ("delete_flight", "flt-7") in at.calls     # flight removed (never flown)
    assert "flight" not in [c[0] for c in at.calls]   # not re-created
    umark = [c for c in al.calls if c[0] == "update_transportation"][0]
    assert umark[1] == "tr-7"
    assert umark[2]["name"].startswith("[CANCELLED]")  # kept + marked, not deleted
    assert "delete_transportation" not in [c[0] for c in al.calls]
    s.close()


def test_supersede_without_exact_flight_skips_delete():
    """Never delete a guess: no exact flight match → leave it, flag for review."""
    _skip()
    s = SessionLocal()
    prior = _mk(s, _flight_payload(), targets=["airtrail.flight"],
                commit=_committed_results())          # no stored id
    p = _flight_payload(); p["booking_status"] = "cancelled"
    it = _mk(s, p, targets=["airtrail.flight"],
             revision={"action": "supersede", "prior_item_id": prior.id})
    al, at = FakeAL(), FakeAT(duplicate=None)          # natural-key finds nothing
    env = C.commit_item(s, it, al, at, identities=[Ident("self", "vu75")],
                        airports=at.airport_reference(), airlines=at.airline_reference(),
                        dry_run=False)
    s.commit()
    assert "delete_flight" not in [c[0] for c in at.calls]
    row = [r for r in env["results"] if r["target"] == "airtrail.flight"][0]
    assert "could not locate" in row.get("skipped", "")
    s.close()


def test_transportation_skipped_when_already_in_collection():
    """D-60: a transportation already in the collection is NOT re-created, even
    with an empty DB commit record — AdventureLog has no server-side dedup, so a
    lost/rebuilt item (e.g. after a reprocess) must not duplicate it."""
    _skip()
    s = SessionLocal()
    it = _add(s, _flight_payload(), targets=["adventurelog.transportation"],
              collection_action="matched_exact", collection_id="coll-x")
    al, at = FakeAL(), FakeAT()
    al.collection_records["coll-x"] = {"transportations": [
        {"id": "tr-existing", "type": "plane", "start_code": "YYZ", "end_code": "DCA"}]}
    env = C.commit_item(s, it, al, at, identities=[Ident("self", "vu75")],
                        airports=at.airport_reference(), airlines=at.airline_reference(),
                        dry_run=False)
    assert "transportation" not in [c[0] for c in al.calls]      # not re-created
    row = [r for r in env["results"] if r["target"] == "adventurelog.transportation"][0]
    assert row.get("skipped") and row.get("id") == "tr-existing"
    s.close()


def test_transportation_created_when_not_in_collection():
    """The dedup only skips a real match — an empty collection still creates."""
    _skip()
    s = SessionLocal()
    it = _add(s, _flight_payload(), targets=["adventurelog.transportation"],
              collection_action="matched_exact", collection_id="coll-empty")
    al, at = FakeAL(), FakeAT()
    al.collection_records["coll-empty"] = {"transportations": []}
    C.commit_item(s, it, al, at, identities=[Ident("self", "vu75")],
                  airports=at.airport_reference(), airlines=at.airline_reference(),
                  dry_run=False)
    assert "transportation" in [c[0] for c in al.calls]          # created
    s.close()


def test_dry_run_update_plans_but_writes_nothing():
    _skip()
    s = SessionLocal()
    prior = _mk(s, _flight_payload(), targets=["adventurelog.transportation"],
                commit=_committed_results(("adventurelog.transportation", "tr-3")))
    it = _mk(s, _flight_payload(), targets=["adventurelog.transportation"],
             revision={"action": "update", "prior_item_id": prior.id})
    al, at = FakeAL(), FakeAT()
    env = C.commit_item(s, it, al, at, identities=[Ident("self", "vu75")],
                        airports=at.airport_reference(), airlines=at.airline_reference(),
                        dry_run=True)
    assert al.calls == [] and at.calls == []
    row = [r for r in env["results"] if r["target"] == "adventurelog.transportation"][0]
    assert row["operation"] == "update" and row["updated_id"] == "tr-3"
    s.close()


# -------------------------------------------------- Yonder first-party writes (2.2.0)

def _add_linked(s, targets):
    """An approved item linked to a fresh Yonder trip that spans its dates."""
    from app.models import Trip
    tr = Trip(name="United States — November 2026",
              start_date="2026-11-20", end_date="2026-11-29")
    s.add(tr); s.commit()
    it = _add(s, _flight_payload(), targets=targets)
    it.trip_id = tr.id
    s.commit()
    return it, tr


def _cleanup_trips(s):
    from app.models import Trip, TripActivity
    s.query(TripActivity).delete()
    s.query(Trip).delete()
    s.commit()


def test_yonder_dry_run_plans_a_trip_activity_but_writes_nothing():
    _skip()
    from app.models import TripActivity
    s = SessionLocal()
    it, tr = _add_linked(s, ["yonder.activity"])
    env = C.commit_item(s, it, FakeAL(), FakeAT(), identities=[Ident("self", "vu75")],
                        dry_run=True)
    ya = [r for r in env["results"] if r["target"] == "yonder.activity"]
    assert ya and ya[0]["operation"] == "create" and ya[0]["ok"]
    assert s.query(TripActivity).count() == 0            # dry run wrote nothing
    _cleanup_trips(s); s.close()


def test_yonder_activity_created_with_capture_key():
    _skip()
    from app.models import TripActivity
    s = SessionLocal()
    it, tr = _add_linked(s, ["yonder.activity"])
    C.commit_item(s, it, FakeAL(), FakeAT(), identities=[Ident("self", "vu75")],
                  dry_run=False)
    s.commit()
    acts = s.query(TripActivity).filter_by(trip_id=tr.id).all()
    assert len(acts) == 1
    a = acts[0]
    assert a.type == "flight"
    assert a.source_item_key == it.canonical_key
    assert a.values.get("flightNumber") == "AC9471"
    assert s.query(CommitAttempt).filter_by(
        item_id=it.id, target="yonder.activity").count() == 1     # tracked (D-19)
    _cleanup_trips(s); s.close()


def test_yonder_upserts_by_capture_key_never_duplicates():
    _skip()
    from app.models import TripActivity
    s = SessionLocal()
    it, tr = _add_linked(s, ["yonder.activity"])
    C.commit_item(s, it, FakeAL(), FakeAT(), identities=[Ident("self", "vu75")],
                  dry_run=False); s.commit()
    # A second capture of the SAME booking (same canonical_key) updates in place.
    it2 = _add(s, _flight_payload(), targets=["yonder.activity"])
    it2.trip_id = tr.id; s.commit()
    C.commit_item(s, it2, FakeAL(), FakeAT(), identities=[Ident("self", "vu75")],
                  dry_run=False); s.commit()
    assert s.query(TripActivity).filter_by(trip_id=tr.id).count() == 1
    _cleanup_trips(s); s.close()


def test_yonder_activity_removed_on_cancellation():
    _skip()
    from app.models import TripActivity
    s = SessionLocal()
    it, tr = _add_linked(s, ["yonder.activity"])
    C.commit_item(s, it, FakeAL(), FakeAT(), identities=[Ident("self", "vu75")],
                  dry_run=False); s.commit()
    assert s.query(TripActivity).filter_by(trip_id=tr.id).count() == 1
    it2 = _add(s, _flight_payload(), targets=["yonder.activity"])
    it2.trip_id = tr.id
    it2.revision = {"action": "supersede"}
    s.commit()
    C.commit_item(s, it2, FakeAL(), FakeAT(), identities=[Ident("self", "vu75")],
                  dry_run=False); s.commit()
    assert s.query(TripActivity).filter_by(trip_id=tr.id).count() == 0
    _cleanup_trips(s); s.close()


def test_yonder_write_refused_without_a_linked_trip():
    _skip()
    s = SessionLocal()
    it = _add(s, _flight_payload(), targets=["yonder.activity"])   # no trip_id
    env = C.commit_item(s, it, FakeAL(), FakeAT(), identities=[Ident("self", "vu75")],
                        dry_run=False)
    s.commit()
    ya = [r for r in env["results"] if r["target"] == "yonder.activity"][0]
    assert not ya["ok"] and "no Run.Dere trip" in ya["error"]
    s.close()


def test_yonder_trip_minted_alongside_new_adventurelog_collection():
    """Owner (2026-08-12): 'yonder.activity' already required a linked trip,
    but nothing ever CREATED one -- only an existing trip could be auto-linked
    at resolve. When the resolver decides a NEW AdventureLog collection is
    needed (mint_yonder_trip), the commit stage must mint a matching Yonder
    trip too: the minter leg creates it (linked to the collection it just
    created via adventurelog_collection_id), and the attach leg borrows the
    SAME trip through the shared mint_group -- exactly the pairing the
    AdventureLog side already does, so the two systems never disagree on
    whether a trip exists."""
    _skip()
    from app.models import Trip, TripActivity
    s = SessionLocal()
    s.query(TripActivity).delete(); s.query(Trip).delete(); s.commit()

    minter = _add(s, _flight_payload(),
                 targets=["adventurelog.collection", "yonder.activity"])
    minter.canonical_key = "flight:pnr:MINT1:AC:8780:YYZ"
    minter.resolution = {**minter.resolution,
                         "mint_group": "MINTTEST1:2026-11-20..2026-11-29",
                         "is_minter": True, "mint_yonder_trip": True}
    minter.owner_id = "user-minter"          # captured via a user-scoped key
    s.commit()

    attach_payload = _flight_payload()
    attach = _add(s, attach_payload, targets=["yonder.activity"])
    attach.canonical_key = "flight:pnr:ATTACH1:AC:8781:DCA"
    attach.resolution = {**attach.resolution,
                         "mint_group": "MINTTEST1:2026-11-20..2026-11-29",
                         "is_minter": False, "mint_yonder_trip": True}
    s.commit()

    al, at = FakeAL(), FakeAT()
    C.commit_item(s, minter, al, at, identities=[Ident("self", "vu75")], dry_run=False)
    s.commit()
    C.commit_item(s, attach, al, at, identities=[Ident("self", "vu75")], dry_run=False)
    s.commit()

    trips = s.query(Trip).all()
    assert len(trips) == 1                              # ONE trip for the journey
    trip = trips[0]
    assert trip.source == "resolver"
    assert trip.name == "United States — November 2026"
    assert trip.start_date == "2026-11-20" and trip.end_date == "2026-11-29"
    assert trip.adventurelog_collection_id == "coll-1"   # linked to the mint
    assert trip.owner_id == "user-minter"                # owned like the capture
    assert minter.trip_id == trip.id and attach.trip_id == trip.id
    acts = s.query(TripActivity).filter_by(trip_id=trip.id).all()
    assert len(acts) == 2                                # both legs filed

    s.query(TripActivity).delete(); s.query(Trip).delete()
    s.query(TravelItem).filter(TravelItem.id.in_([minter.id, attach.id])).delete(
        synchronize_session=False)
    s.commit()
    s.close()


def test_resolver_minted_trip_stays_open_for_open_capture():
    """A capture from an unscoped key (owner_id NULL) mints an open trip --
    no owner is invented for it."""
    _skip()
    from app.models import Trip, TripActivity
    s = SessionLocal()
    s.query(TripActivity).delete(); s.query(Trip).delete(); s.commit()

    minter = _add(s, _flight_payload(),
                 targets=["adventurelog.collection", "yonder.activity"])
    minter.canonical_key = "flight:pnr:MINT3:AC:8783:YYZ"
    minter.resolution = {**minter.resolution,
                         "mint_group": "MINTTEST3:2026-11-20..2026-11-29",
                         "is_minter": True, "mint_yonder_trip": True}
    s.commit()

    C.commit_item(s, minter, FakeAL(), FakeAT(),
                  identities=[Ident("self", "vu75")], dry_run=False)
    s.commit()
    trip = s.query(Trip).one()
    assert trip.source == "resolver" and trip.owner_id is None

    s.query(TripActivity).delete(); s.query(Trip).delete()
    s.query(TravelItem).filter(TravelItem.id == minter.id).delete(
        synchronize_session=False)
    s.commit()
    s.close()


def test_yonder_trip_mint_deferred_until_minter_commits():
    """An attach leg committed BEFORE its minter sibling (out-of-order) has no
    trip to borrow yet -- refused with a reason, same as an unresolved
    AdventureLog collection, not a crash or a wrongly-minted second trip."""
    _skip()
    from app.models import Trip, TripActivity
    s = SessionLocal()
    s.query(TripActivity).delete(); s.query(Trip).delete(); s.commit()

    attach_payload = _flight_payload()
    attach = _add(s, attach_payload, targets=["yonder.activity"])
    attach.canonical_key = "flight:pnr:ATTACH2:AC:8782:YUL"
    attach.resolution = {**attach.resolution,
                         "mint_group": "MINTTEST2:2026-11-20..2026-11-29",
                         "is_minter": False, "mint_yonder_trip": True}
    s.commit()

    env = C.commit_item(s, attach, FakeAL(), FakeAT(),
                        identities=[Ident("self", "vu75")], dry_run=False)
    s.commit()
    ya = [r for r in env["results"] if r["target"] == "yonder.activity"][0]
    assert not ya["ok"] and "no Run.Dere trip" in ya["error"]
    assert s.query(Trip).count() == 0                    # never guessed one into being

    s.query(TravelItem).filter(TravelItem.id == attach.id).delete(
        synchronize_session=False)
    s.commit()

    s.query(TripActivity).delete(); s.query(Trip).delete(); s.commit()
    s.close()


# ------------------------------------ AdventureLog coarse transport type (2.2.1)

def _ground(mode, ref="A498317861", provider="Hertz"):
    return {"item_type": "ground_transport", "booking_reference": ref,
            "provider": {"name": provider},
            "ground_transport": {"mode": mode, "service_number": "",
                "departure": {"place": {"raw": "LHR", "city": "London"}},
                "arrival": {"place": {"raw": "LHR", "city": "London"}}}}


def test_car_rental_maps_to_car_with_details_in_description():
    """AdventureLog's transportation type enum has no 'car_rental' — it must log
    as 'car' with the rental mode + operator kept in the description."""
    _skip()
    body, _ = C.build_transportation(_ground("car_rental"), collection_id="c1")
    assert body["type"] == "car"                       # not the rejected car_rental
    assert "Car rental" in body["description"]
    assert "Hertz" in body["description"]
    assert "A498317861" in body["description"]


def test_ground_modes_map_to_adventurelog_enum():
    _skip()
    cases = {"car_rental": "car", "rideshare": "car", "transfer": "car",
             "ferry": "boat", "train": "train", "bus": "bus", "other": "other"}
    for mode, expected in cases.items():
        body, _ = C.build_transportation(_ground(mode), collection_id="c1")
        assert body["type"] == expected, f"{mode} -> {body['type']}, want {expected}"


def test_committed_flight_tagged_with_its_passengers_on_a_shared_trip():
    """4.1: on a multi-traveller trip, a committed flight is tagged with the
    passengers the booking names (Amara's flight isn't everyone's), matched
    against the trip's own roster; an inferred passenger never tags."""
    _skip()
    from app.models import TravelerIdentity, TripActivity, TripTraveler
    from app.trips import who
    from resolve.identity import Identity
    s = SessionLocal()
    it, tr = _add_linked(s, ["yonder.activity"])
    me = TravelerIdentity(display_name="Tag Self", kind="self", aliases=["SELF/TAG MR"])
    her = TravelerIdentity(display_name="Tag Companion", kind="guest")
    s.add_all([me, her]); s.flush()
    s.add_all([TripTraveler(trip_id=tr.id, traveler_id=me.id, role="self"),
               TripTraveler(trip_id=tr.id, traveler_id=her.id, role="companion")])
    payload = _flight_payload()
    payload["travelers"] = [{"raw_name": "COMPANION/TAG MS", "role": "guest",
                             "match_source": "alias", "match_confidence": 1.0}]
    it.extraction = {**it.extraction, "payload": payload}
    s.commit()
    idents = [Identity(id=str(me.id), display_name="Tag Self", kind="self",
                       aliases=("SELF/TAG MR",), airtrail_user_id="vu75"),
              Identity(id=str(her.id), display_name="Tag Companion", kind="guest")]
    C.commit_item(s, it, FakeAL(), FakeAT(), identities=idents, dry_run=False)
    s.commit()
    act = s.query(TripActivity).filter_by(trip_id=tr.id).one()
    assert who.activity_map(s, [act.id]).get(act.id) == [her.id]

    who.clear_activity(s, act.id)
    s.query(TripTraveler).filter_by(trip_id=tr.id).delete()
    s.query(TravelerIdentity).filter(TravelerIdentity.id.in_([me.id, her.id])).delete(
        synchronize_session=False)
    s.query(TravelItem).filter(TravelItem.id == it.id).delete(synchronize_session=False)
    _cleanup_trips(s); s.close()


def test_commit_only_logs_the_committers_own_approvals():
    """YONDER-105: "Log approved" must not write someone else's approved
    captures. The web passes scope.capture_filter(user): own + open (+ sharing
    users' for an admin); the operator CLI passes nothing and logs everything."""
    _skip()
    from app import scope
    from app.models import User
    s = SessionLocal()
    s.query(User).filter(User.username.in_(["c105-me", "c105-sis"])).delete(
        synchronize_session=False)
    s.query(TravelItem).delete(); s.commit()
    me = User(username="c105-me", auth_source="local", is_admin=True)
    sis = User(username="c105-sis", auth_source="local")
    s.add_all([me, sis]); s.commit()
    mine = _add(s, _flight_payload(), targets=["airtrail.flight"])
    hers = _add(s, _flight_payload(), targets=["airtrail.flight"])
    hers.canonical_key = "flight:pnr:HERS:AC:1:YYZ"
    mine.owner_id, hers.owner_id = me.id, sis.id
    s.commit()

    def committed(user):
        env = C.commit_pending(s, FakeAL(), FakeAT(), identities=[Ident("self", "vu75")],
                               dry_run=True,
                               item_filter=None if user is None else scope.capture_filter(user))
        return {p["item_id"] for p in env["plans"]}

    assert committed(sis) == {hers.id}                  # hers only, never mine
    assert committed(me) == {mine.id, hers.id}          # admin: she shares by default
    sis.share_with_admins = False; s.commit()
    assert committed(me) == {mine.id}                   # opted out: not the admin's to log
    assert committed(None) == {mine.id, hers.id}        # operator CLI: everything

    s.query(TravelItem).delete()
    s.query(User).filter(User.id.in_([me.id, sis.id])).delete(synchronize_session=False)
    s.commit(); s.close()
