"""A5 key spec tests. The load-bearing cases are the cross-channel collapses:
one real-world thing captured via different channels MUST yield intersecting
key sets (HANDOFF Task 2). Runnable under pytest, or directly:
python3 tests/test_keys.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resolve.keys import (KeyResult, event_key, flight_key, geohash, keys_for,
                          lodging_key, matches, normalize_flight_number,
                          road_key, scheduled_ground_key, token_signature)


# ---------------------------------------------------------------- flight

def test_flight_cross_channel_collapse():
    """Airline email (PNR) + gcal entry (no PNR) + manage-booking capture (PNR)
    → one flight. THE core A5 requirement."""
    email = flight_key("AC", "0652", "2026-09-14", "YYZ", pnr="T4WNX8")
    gcal = flight_key("AC", 652, "2026-09-14", "yyz")          # no PNR
    web = flight_key("ac", "AC652", "2026-09-14", "YYZ", pnr="T4WNX8")

    assert email.primary == web.primary == "flight:pnr:T4WNX8:AC:652:YYZ"
    assert gcal.primary == "flight:AC:652:2026-09-14:YYZ"
    assert matches(email, gcal) and matches(email, web) and matches(gcal, web)


def test_flight_survives_past_midnight_schedule_change():
    """OQ-10: the exact failure the date-anchored key had."""
    before = flight_key("AC", "652", "2026-09-14", "YYZ", pnr="T4WNX8")
    after = flight_key("AC", "652", "2026-09-15", "YYZ", pnr="T4WNX8")
    assert before.primary == after.primary          # update chain intact


def test_flight_number_normalization():
    assert normalize_flight_number("0652") == "652"
    assert normalize_flight_number("AC 0652", "AC") == "652"
    assert normalize_flight_number(652) == "652"
    assert normalize_flight_number("652A") == "652A"
    # PNR reuse across legs: same PNR, different legs → different keys
    leg1 = flight_key("AC", "652", "2026-09-14", "YYZ", pnr="T4WNX8")
    leg2 = flight_key("AC", "889", "2026-09-22", "LHR", pnr="T4WNX8")
    assert not matches(leg1, leg2)


def test_flight_force_date_disambiguates_repeated_segment():
    """Weekly commuter: same PNR, same flight number, two dates (RULE-17c)."""
    mon1 = flight_key("AC", "444", "2026-09-14", "YYZ", pnr="ABC123", force_date=True)
    mon2 = flight_key("AC", "444", "2026-09-21", "YYZ", pnr="ABC123", force_date=True)
    assert mon1.primary != mon2.primary
    assert not matches(mon1, mon2)


# ---------------------------------------------------------------- lodging

def test_lodging_name_variants_collapse():
    """OQ-12: the three Hoxton spellings from HANDOFF."""
    a = lodging_key("The Hoxton, Shoreditch", "2026-09-15", city="London")
    b = lodging_key("Hoxton Shoreditch", "2026-09-15", city="London")
    c = lodging_key("The Hoxton — Shoreditch London", "2026-09-15", city="London")
    assert a.primary == b.primary == c.primary == "lodging:hoxton-shoreditch:2026-09-15"


def test_lodging_confirmation_number_bridges_date_change():
    """A modified booking (new check-in) keeps its conf number → still matches."""
    orig = lodging_key("The Hoxton, Shoreditch", "2026-09-15",
                       city="London", confirmation="HX-99201")
    moved = lodging_key("Hoxton Shoreditch", "2026-09-16",
                        city="London", confirmation="HX99201")
    assert orig.primary == moved.primary == "lodging:conf:HX99201"
    # and a conf-less capture of the same stay still collapses via name form
    bare = lodging_key("Hoxton, Shoreditch", "2026-09-15", city="London")
    assert matches(orig, bare)


def test_lodging_diacritics_and_articles():
    a = lodging_key("Hôtel du Nord", "2026-05-01", city="Paris")
    b = lodging_key("hotel du nord, Paris", "2026-05-01", city="Paris")
    assert a.primary == b.primary


# ---------------------------------------------------------------- event

def test_event_title_variants_collapse():
    """OQ-13: ticketing email vs calendar invite wording."""
    email = event_key("Your tickets: Hamilton", "2026-09-18",
                      venue_name="Victoria Palace Theatre", city="London")
    gcal = event_key("Hamilton — Victoria Palace Theatre", "2026-09-18",
                     venue_name="Victoria Palace Theatre", city="London")
    assert email.primary == gcal.primary == "event:2026-09-18:hamilton"


def test_event_gcal_id_alternate():
    a = event_key("Hamilton", "2026-09-18", gcal_event_id="abc123xyz")
    b = event_key("Completely Different Listing Title", "2026-09-18",
                  gcal_event_id="abc123xyz")
    assert matches(a, b)                     # gcal id bridges title divergence


# ---------------------------------------------------------------- road

def test_road_coordinate_jitter_collapses():
    """OQ-11: same Toronto→Ottawa drive, coords differing in later decimals."""
    a = road_key("2026-08-08", 43.6532, -79.3832, 45.4215, -75.6972)
    b = road_key("2026-08-08", 43.6501, -79.3790, 45.4189, -75.7011)
    assert a.primary == b.primary
    assert matches(a, b)


def test_road_date_and_route_distinguish():
    a = road_key("2026-08-08", 43.6532, -79.3832, 45.4215, -75.6972)
    other_day = road_key("2026-08-09", 43.6532, -79.3832, 45.4215, -75.6972)
    other_dest = road_key("2026-08-08", 43.6532, -79.3832, 46.8139, -71.2080)
    assert not matches(a, other_day)
    assert not matches(a, other_dest)


def test_geohash_known_value():
    assert geohash(57.64911, 10.40744, 11) == "u4pruydqqvj"   # published vector


# ---------------------------------------------------------------- ground

def test_rail_mirrors_flight_shape():
    r = scheduled_ground_key("train", "EUR", "9024", "2026-09-20", "GBSPX")
    assert r.primary == "rail:EUR:9024:2026-09-20:GBSPX"
    with_pnr = scheduled_ground_key("train", "EUR", "9024", "2026-09-20",
                                    "GBSPX", pnr="XZ88P1")
    assert matches(r, with_pnr)



def test_booked_ground_modes_key_on_confirmation():
    """Measured gap: a car rental has no timetable, and a real Amtrak eTicket
    carried a PNR but no train number — both were unkeyable before."""
    from resolve.keys import booked_ground_key
    rental = keys_for({"item_type": "ground_transport",
                       "booking_reference": "A900123456",
                       "ground_transport": {"mode": "car_rental",
                                            "departure": {"place": {"raw": "PHX"},
                                                          "time": {"local": "2026-10-02T18:00",
                                                                   "precision": "datetime"}},
                                            "arrival": {"place": {"raw": "PHX"},
                                                        "time": {"local": "2026-10-09T10:00",
                                                                 "precision": "datetime"}}}})
    assert rental is not None
    assert rental.primary == "carrental:conf:A900123456"

    train = keys_for({"item_type": "ground_transport",
                      "booking_reference": "512048",
                      "ground_transport": {"mode": "train",
                                           "departure": {"place": {"raw": "PHL"},
                                                         "time": {"local": "2024-02-10T12:31",
                                                                  "precision": "datetime"}},
                                           "arrival": {"place": {"raw": "WAS"},
                                                       "time": {"local": "2024-02-10T14:31",
                                                                "precision": "datetime"}}}})
    assert train is not None and train.primary == "rail:conf:512048"
    # a re-issued confirmation for the same booking still collapses
    assert matches(train, booked_ground_key("train", "512048", "2024-02-10"))

# ---------------------------------------------------------------- dispatcher

def _dt(local, tz="America/Toronto", precision="minute"):
    return {"local": local, "tz": tz, "precision": precision, "origin": "document"}


def test_dispatcher_flight_payload():
    payload = {
        "item_type": "flight",
        "booking_reference": "T4WNX8",
        "flight": {
            # Provider object, matching the schema (a plain string here hid a
            # real dispatcher bug until the jsonld extractor emitted the true shape)
            "marketing_carrier": {"name": "Air Canada", "iata": "AC", "kind": "airline"},
            "flight_number": "652",
            "departure": {"place": {"name": "Toronto Pearson", "iata": "YYZ"},
                          "time": _dt("2026-09-14T18:30:00")},
            "arrival": {"place": {"name": "Heathrow", "iata": "LHR"},
                        "time": _dt("2026-09-15T06:40:00", tz="Europe/London")},
        },
    }
    r = keys_for(payload)
    assert r.primary == "flight:pnr:T4WNX8:AC:652:YYZ"
    assert "flight:AC:652:2026-09-14:YYZ" in r.alternates


def test_dispatcher_road_uses_capture_context_date():
    payload = {
        "item_type": "ground_transport",
        "ground_transport": {
            "mode": "car",
            # Endpoints are departure/arrival (waypoints are intermediate) — the
            # structure maps_url.py and the committer use.
            "departure": {"place": {"name": "Home-ish", "lat": 43.6532, "lon": -79.3832}},
            "arrival": {"place": {"name": "Ottawa", "lat": 45.4215, "lon": -75.6972}},
            "waypoints": [],
        },
    }
    r = keys_for(payload, capture_context={"travel_date": "2026-08-08"})
    assert r is not None and r.primary.startswith("road:2026-08-08:")
    # no capture date and no departure time → unkeyable, not an error (RULE-31)
    assert keys_for(payload) is None


def test_dispatcher_road_with_intermediate_waypoints():
    """A drive with intermediate stops still keys on its endpoints (D-61)."""
    payload = {
        "item_type": "ground_transport",
        "ground_transport": {
            "mode": "car",
            "departure": {"place": {"name": "Toronto", "lat": 43.6532, "lon": -79.3832}},
            "arrival": {"place": {"name": "Ottawa", "lat": 45.4215, "lon": -75.6972}},
            "waypoints": [{"place": {"name": "Port Calloway", "lat": 44.23, "lon": -76.48}}],
        },
    }
    r = keys_for(payload, capture_context={"travel_date": "2026-08-08"})
    assert r is not None and r.primary.startswith("road:2026-08-08:")


def test_dispatcher_unknown_and_malformed_return_none():
    assert keys_for({"item_type": "unknown"}) is None
    assert keys_for({"item_type": "flight", "flight": {}}) is None


def test_flight_without_iata_is_unkeyable_but_names_the_reason():
    """A real Air Canada e-ticket (2026-08-04) printed airport NAMES but no IATA
    codes. The extractor is right not to guess codes, and the flight key needs
    one — so the item cannot be keyed, but the reason must be actionable
    ('enter the code'), NOT the misleading 'unknown_type' that made a perfectly
    obvious flight look unclassifiable in the queue."""
    from resolve.keys import unkeyable_reason
    payload = {
        "item_type": "flight",
        "flight": {
            "marketing_carrier": {"name": "Air Canada", "iata": "AC"},
            "flight_number": "9471",
            "departure": {"place": {"name": "Toronto-Pearson International",
                                    "city": "Toronto", "country": "CA"},
                          "time": {"local": "2026-11-20", "precision": "date"}},
            "arrival": {"place": {"name": "Chicago O'Hare International",
                                  "city": "Chicago", "country": "US"},
                        "time": {"local": "2026-11-20", "precision": "date"}},
        },
    }
    assert keys_for(payload) is None
    assert unkeyable_reason(payload) == "missing_airport_code"

    # Supplying the codes makes it key normally — this is what the review-queue
    # edit does on save, so the item then leaves the queue.
    payload["flight"]["departure"]["place"]["iata"] = "YYZ"
    payload["flight"]["arrival"]["place"]["iata"] = "ORD"
    r = keys_for(payload)
    assert r is not None and r.primary == "flight:AC:9471:2026-11-20:YYZ"


def test_normalize_flight_number_strips_foreign_prefix():
    """A number carrying its own marketing code ('AC9471') must normalise even
    when a DIFFERENT operating carrier is supplied — the regional-operator case
    (2026-08-04) that used to raise and discard a whole JSON-LD extraction."""
    assert normalize_flight_number("AC9471", "QK") == "9471"
    assert normalize_flight_number("AC9471") == "9471"
    assert normalize_flight_number("AC 0652", "AC") == "652"
    assert normalize_flight_number("652A") == "652A"          # suffix survives
    assert normalize_flight_number(652) == "652"


def test_unkeyable_reason_falls_back_to_unknown_type():
    """When the type itself is unclear, or a flight is missing more than just
    the airport code, entering a code would not help — keep 'unknown_type'."""
    from resolve.keys import unkeyable_reason
    assert unkeyable_reason({"item_type": "unknown"}) == "unknown_type"
    # flight missing carrier/number, not just the code
    assert unkeyable_reason({"item_type": "flight", "flight": {}}) == "unknown_type"


def test_purity_and_determinism():
    args = ("AC", "652", "2026-09-14", "YYZ")
    assert flight_key(*args, pnr="T4WNX8") == flight_key(*args, pnr="T4WNX8")
    assert token_signature("The Hoxton — Shoreditch") == \
        token_signature("the hoxton shoreditch")


# ---------------------------------------------------------------- runner

if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as ex:
                failures += 1
                print(f"  FAIL {name}: {ex}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)


def test_drive_without_coordinates_names_an_actionable_reason():
    """YONDER-121: a Google Maps link that names its stops but embeds no
    coordinates is a perfectly-typed drive. When a stop can't be located it must
    say so ('add an address or city') — not 'unknown_type', whose 'set the type'
    can't be done because the Kind field isn't editable."""
    from resolve.keys import unkeyable_reason
    payload = {
        "item_type": "ground_transport",
        "ground_transport": {
            "mode": "car",
            "departure": {"place": {"name": "Phoenix International Sunport"}},
            "arrival": {"place": {"name": "Heritage Square, Phoenix, NM",
                                  "city": "Phoenix", "region": "NM"}},
            "waypoints": [],
        },
    }
    ctx = {"travel_date": "2026-10-10"}
    assert keys_for(payload, ctx) is None
    assert unkeyable_reason(payload, ctx) == "geocode_failed"
    # no date at all is the other missing piece of a road key
    assert unkeyable_reason(payload) == "missing_travel_date"

    # once both stops are located (the geocoder, or a reviewer's edit), it keys
    payload["ground_transport"]["departure"]["place"].update(lat=35.04, lon=-106.61)
    payload["ground_transport"]["arrival"]["place"].update(lat=35.10, lon=-106.67)
    r = keys_for(payload, ctx)
    assert r is not None and r.primary.startswith("road:2026-10-10:")


def test_scheduled_ground_transport_keeps_the_generic_reason():
    """A train with a service number keys on the service, not on coordinates,
    so a missing coordinate is not what blocks it."""
    from resolve.keys import unkeyable_reason
    payload = {"item_type": "ground_transport",
               "ground_transport": {"mode": "train", "service_number": "3",
                                    "departure": {}, "arrival": {}}}
    assert keys_for(payload) is None
    assert unkeyable_reason(payload) == "unknown_type"
