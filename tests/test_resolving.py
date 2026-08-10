"""Resolve-stage integration tests: pending TravelItems -> resolved.
Synthetic data only (D-21). SQLite via tests/conftest.py."""
import sys
from datetime import datetime, timezone
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
    from app.models import Base, HomeProfile, ItemKey, TravelItem
    from app.resolving import resolve_pending
    from resolve.collections import CollectionRef
    Base.metadata.create_all(engine)

NOW = datetime.now(timezone.utc).isoformat()


def _dt(local, precision="datetime", tz="America/Toronto"):
    return {"local": local, "precision": precision, "tz": tz, "origin": "document"}


def _lodging_payload(prop="Test Hotel", ref="LODGE1", ci="2027-05-10",
                     co="2027-05-14"):
    return {"item_type": "lodging", "booking_status": "confirmed",
            "booking_reference": ref,
            "lodging": {"property_name": prop,
                        "place": {"raw": prop, "name": prop, "city": "Lisbon",
                                  "country": "PT", "lat": 38.72, "lon": -9.14},
                        "check_in": _dt(ci, "date"), "check_out": _dt(co, "date")}}


def _add_item(session, payload, *, item_type=None, doc_ts=None, batch="b1"):
    it = TravelItem(
        batch_id=None, item_type=item_type or payload["item_type"],
        schema_version="1.4.0",
        source={"type": "gmail", "ref": f"m-{payload.get('booking_reference')}",
                "received_at": NOW, "document_timestamp": doc_ts,
                "capture_context": {}},
        extraction={"extractor": "llm", "model": "test", "ran_at": NOW,
                    "confidence": 0.6, "payload": payload},
        status={"extract": "ok", "resolve": "pending", "airtrail": "pending",
                "adventurelog": {}})
    session.add(it)
    session.commit()
    return it


def _seed_home(session):
    # Idempotent on THIS test's own home, not "any profile" — other test files
    # share the SQLite DB and may have left rows, which must not suppress the
    # airport this file's flight tests depend on.
    if session.query(HomeProfile).filter_by(label="Home Airport (HHH)").first():
        return
    session.add(HomeProfile(kind="home_airport", label="Home Airport (HHH)",
                            iata="HHH", lat=43.68, lon=-79.63, radius_km=50.0,
                            country="CA", effective_from=None, effective_to=None))
    session.add(HomeProfile(kind="residence", label="Home City",
                            lat=43.65, lon=-79.38, radius_km=50.0, country="CA",
                            effective_from=None, effective_to=None))
    session.commit()


def _skip_or(fn):
    if not DEPS:
        import pytest
        pytest.skip("sqlalchemy not installed on this host")
    return fn()


def test_duplicate_captures_collapse_to_no_change():
    """THE headline case: the same booking captured twice must not produce two
    write sets — the second is no_change with zero targets (D-13)."""
    def run():
        with SessionLocal() as s:
            _seed_home(s)
            p = _lodging_payload(ref="DUP1")
            a = _add_item(s, p)
            b = _add_item(s, dict(p))          # identical recapture
            resolve_pending(s, adventurelog=None)
            a2, b2 = s.get(TravelItem, a.id), s.get(TravelItem, b.id)
            assert a2.canonical_key == b2.canonical_key      # same identity
            actions = {a2.revision["action"], b2.revision["action"]}
            assert actions == {"create", "no_change"}
            second = b2 if b2.revision["action"] == "no_change" else a2
            assert second.resolution["targets"] == []        # writes nothing
            # item_keys holds ONE row per key: the second item does not steal it
            rows = s.query(ItemKey).filter_by(key=a2.canonical_key).all()
            assert len(rows) == 1
    _skip_or(run)


def test_cancellation_supersedes_its_confirmation():
    def run():
        with SessionLocal() as s:
            _seed_home(s)
            conf = _lodging_payload(prop="Cancel Hotel", ref="CX1",
                                    ci="2027-06-01", co="2027-06-04")
            first = _add_item(s, conf, doc_ts="2026-01-01T00:00:00Z")
            cancelled = {**conf, "booking_status": "cancelled"}
            second = _add_item(s, cancelled, doc_ts="2026-02-01T00:00:00Z")
            resolve_pending(s, adventurelog=None)
            b = s.get(TravelItem, second.id)
            assert b.revision["action"] == "supersede"
            assert b.resolution.get("supersedes") == first.id
    _skip_or(run)


def test_home_place_is_suppressed_but_retained():
    """D-07: the Location write is suppressed; the place data stays."""
    def run():
        with SessionLocal() as s:
            _seed_home(s)
            payload = {
                "item_type": "flight", "booking_status": "confirmed",
                "booking_reference": "HOME1",
                "flight": {"flight_number": "100",
                           "marketing_carrier": {"name": "Test Air", "iata": "TA"},
                           "departure": {"place": {"raw": "Home Airport",
                                                   "iata": "HHH", "name": "Home",
                                                   "lat": 43.68, "lon": -79.63},
                                         "time": _dt("2027-07-01T08:00")},
                           "arrival": {"place": {"raw": "Lisbon", "iata": "LIS",
                                                 "name": "Lisbon", "country": "PT",
                                                 "lat": 38.77, "lon": -9.13},
                                       "time": _dt("2027-07-01T20:00", tz="Europe/Lisbon")}}}
            it = _add_item(s, payload)
            resolve_pending(s, adventurelog=None)
            done = s.get(TravelItem, it.id)
            dep = done.extraction["payload"]["flight"]["departure"]["place"]
            arr = done.extraction["payload"]["flight"]["arrival"]["place"]
            assert dep["is_home"] is True and dep["suppressed"] is True
            assert dep["suppression_reason"] == "home"
            assert dep["name"] == "Home"                  # data retained
            assert arr.get("is_home") is False            # destination is not home
    _skip_or(run)


def test_keys_are_written_for_dedup_lookup():
    def run():
        with SessionLocal() as s:
            _seed_home(s)
            it = _add_item(s, _lodging_payload(prop="Keyed Inn", ref="KEY1"))
            resolve_pending(s, adventurelog=None)
            done = s.get(TravelItem, it.id)
            assert done.canonical_key
            keys = {r.key for r in s.query(ItemKey).filter_by(item_id=it.id).all()}
            assert done.canonical_key in keys
    _skip_or(run)


def test_no_collections_available_defers_lodging():
    """A6: lodging with no matching collection defers (2-day TTL), it does not
    invent a trip — only flights mint (D-04)."""
    def run():
        with SessionLocal() as s:
            _seed_home(s)
            it = _add_item(s, _lodging_payload(prop="Orphan Inn", ref="ORPH1"))
            resolve_pending(s, adventurelog=None)
            done = s.get(TravelItem, it.id)
            assert done.resolution["outcome"] == "deferred"
            assert done.resolution["collection_action"] == "none"
            assert done.status["resolve"] == "deferred"
    _skip_or(run)


def test_resolved_items_are_not_reresolved():
    def run():
        with SessionLocal() as s:
            before = resolve_pending(s, adventurelog=None)
            assert before == {}          # nothing left pending after prior tests
    _skip_or(run)


if __name__ == "__main__":
    if not DEPS:
        print("SKIP: sqlalchemy not installed on this host")
        sys.exit(0)
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as ex:
                failures += 1
                print(f"  FAIL {name}: {str(ex)[:400]}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)


def test_matched_collection_is_not_a_create_target():
    """D-04: only a MINTING flight writes a collection. An item that matched an
    existing trip references it. Getting this wrong made every matched hotel and
    event advertise "New trip …" in the review queue — reading exactly like the
    rule being broken while the resolver was in fact correct."""
    def run():
        from resolve.targets import compute_targets
        payload = {"item_type": "lodging"}
        matched = compute_targets(payload, self_on_booking=True, within_home=False,
                                  has_collection=True, unsuppressed_places=1,
                                  creating_collection=False)
        assert "adventurelog.collection" not in matched
        assert "adventurelog.lodging" in matched

        minting = compute_targets({"item_type": "flight"}, self_on_booking=True,
                                  within_home=False, has_collection=True,
                                  unsuppressed_places=1, creating_collection=True)
        assert "adventurelog.collection" in minting
    _skip_or(run)


def test_no_trip_is_minted_until_a_flight_comes_home():
    """Owner rule (D-34): flying AWAY from home is half a journey. Until a
    flight brings you back, the trip has no known end date, so minting would
    invent one — and every later leg outside that invented range would mint
    ANOTHER trip for the same journey."""
    def run():
        from app.resolving import _trip_envelope
        with SessionLocal() as s:
            _seed_home(s)
            out = {"item_type": "flight", "booking_status": "confirmed",
                   "booking_reference": "ENV01",
                   "flight": {"flight_number": "1",
                              "departure": {"place": {"raw": "HHH", "iata": "HHH",
                                                      "is_home": True},
                                            "time": _dt("2027-09-01T08:00")},
                              "arrival": {"place": {"raw": "LIS", "iata": "LIS"},
                                          "time": _dt("2027-09-01T12:00")}}}
            outbound = _add_item(s, out)
            # Only the outbound exists: not a closed trip, so not mintable.
            assert _trip_envelope(s, outbound, out) is None

            ret = {"item_type": "flight", "booking_status": "confirmed",
                   "booking_reference": "ENV01",
                   "flight": {"flight_number": "2",
                              "departure": {"place": {"raw": "LIS", "iata": "LIS"},
                                            "time": _dt("2027-09-14T09:00")},
                              "arrival": {"place": {"raw": "HHH", "iata": "HHH",
                                                    "is_home": True},
                                          "time": _dt("2027-09-14T15:00")}}}
            _add_item(s, ret)
            # Now the journey is closed: the span is home-to-home, covering
            # every leg between, not just the one that happened to mint.
            assert _trip_envelope(s, outbound, out) == ("2027-09-01", "2027-09-14")
    _skip_or(run)


def _leg(ref, num, frm, to, dep_at, arr_at, *, from_home=False, to_home=False,
         dest_country=None, dep_country=None, arr_country=None):
    fp = {"raw": frm, "iata": frm, **({"is_home": True} if from_home else {})}
    tp = {"raw": to, "iata": to, **({"is_home": True} if to_home else {})}
    # Per-airport countries — in real data the airport reference fills every
    # airport's country. `dest_country` is the simple single-destination case
    # (applied to whichever end isn't home); dep_country/arr_country override it
    # for multi-country itineraries (a layover in a different country).
    dc = dep_country or (None if from_home else dest_country)
    ac = arr_country or (None if to_home else dest_country)
    if dc:
        fp["country"] = dc
    if ac:
        tp["country"] = ac
    return {"item_type": "flight", "booking_status": "confirmed",
            "booking_reference": ref,
            # a passenger, so the leg doesn't stop at "confirm who travelled"
            # (RULE-11) before reaching the collection/defer decision under test
            "travelers": [{"raw_name": "Pat Example"}],
            "flight": {"flight_number": num,
                       "marketing_carrier": {"name": "Test Air", "iata": "TA"},
                       "departure": {"place": fp, "time": _dt(dep_at)},
                       "arrival": {"place": tp, "time": _dt(arr_at)}}}


def test_journey_destination_picks_longest_dwell_not_a_layover():
    """The destination heuristic (2026-08-05): among a journey's legs, the trip's
    destination is where the traveller dwells longest. Munich (arrival to next
    departure ~12h) is a layover; Bangalore (~3 weeks) is the destination."""
    from app.resolving import _journey_destination
    members = [
        ("2026-12-18T08:00", {"to_home": False, "arr_order": "2026-12-18T22:00",
                              "arr_country": "DE"}, "a"),   # layover, ~12h
        ("2026-12-19T10:00", {"to_home": False, "arr_order": "2026-12-20T01:00",
                              "arr_country": "IN"}, "b"),   # destination, ~3wk
        ("2027-01-10T06:00", {"to_home": False, "arr_order": "2027-01-10T08:00",
                              "arr_country": "IN"}, "c"),   # layover home-bound
        ("2027-01-10T12:00", {"to_home": True, "arr_order": "2027-01-10T20:00",
                              "arr_country": None}, "d"),
    ]
    assert _journey_destination(members) == "IN"


def test_multileg_journey_named_by_destination_not_layover():
    """End-to-end: a Toronto→Munich→Bangalore … →Toronto trip must be ONE journey
    named after India (the long stay), not Germany (the Munich layover), with
    every leg agreeing on that one name — the real-data bug the owner reported."""
    def run():
        with SessionLocal() as s:
            _seed_home(s)
            s.query(ItemKey).delete(); s.query(TravelItem).delete(); s.commit()
            for lg in (
                _leg("J1", "1", "HHH", "MUC", "2026-12-18T08:00", "2026-12-18T22:00",
                     from_home=True, arr_country="DE"),
                _leg("J2", "2", "MUC", "BLR", "2026-12-19T10:00", "2026-12-20T01:00",
                     dep_country="DE", arr_country="IN"),
                _leg("J3", "3", "BLR", "DEL", "2027-01-10T06:00", "2027-01-10T08:00",
                     dep_country="IN", arr_country="IN"),
                _leg("J4", "4", "DEL", "HHH", "2027-01-10T12:00", "2027-01-10T20:00",
                     to_home=True, dep_country="IN"),
            ):
                _add_item(s, lg)
            resolve_pending(s, adventurelog=None)
            names, minters = set(), 0
            for it in s.query(TravelItem).filter(TravelItem.item_type == "flight").all():
                r = it.resolution or {}
                names.add(r.get("collection_name"))
                minters += 1 if r.get("is_minter") else 0
            assert len(names) == 1                        # every leg, one name
            name = names.pop()
            assert name.startswith("IN")                  # India, not DE (layover)
            assert minters == 1                           # one minter for the journey
    _skip_or(run)


def test_journey_legs_share_one_trip_with_a_single_minter():
    """Owner (2026-08-05): the two legs of one home-to-home journey, even booked
    separately, must share ONE trip. The earliest leg MINTS it (collection
    target + is_minter); the sibling ATTACHES (same mint_group, same trip name,
    NO collection target) instead of minting a second trip or deferring alone."""
    def run():
        with SessionLocal() as s:
            _seed_home(s)
            # _journey_minter queries ALL flights (correct in prod), so isolate
            # from other tests' leftover home-return legs in the shared test DB.
            s.query(ItemKey).delete(); s.query(TravelItem).delete(); s.commit()
            # separate bookings (different PNRs), no existing collections to match
            out = _leg("OUT9", "1", "HHH", "LIS", "2027-09-01T08:00",
                       "2027-09-01T12:00", from_home=True, dest_country="PT")
            ret = _leg("RET9", "2", "LIS", "HHH", "2027-09-14T09:00",
                       "2027-09-14T15:00", to_home=True, dest_country="PT")
            o, r = _add_item(s, out), _add_item(s, ret)
            resolve_pending(s, adventurelog=None)      # no collections -> must mint

            ro = s.get(TravelItem, o.id).resolution
            rr = s.get(TravelItem, r.id).resolution
            assert ro["is_minter"] != rr["is_minter"]          # exactly one minter
            minter, attach = (ro, rr) if ro["is_minter"] else (rr, ro)
            assert minter["collection_action"] == "created"
            assert "adventurelog.collection" in minter["targets"]
            assert attach["collection_action"] == "created"    # same trip
            assert "adventurelog.collection" not in attach["targets"]  # not a 2nd
            assert minter["mint_group"] == attach["mint_group"]
            assert minter["collection_name"] == attach["collection_name"]
            # neither leg defers now — the journey is closed and shared
            assert s.get(TravelItem, o.id).status["resolve"] == "ok"
            assert s.get(TravelItem, r.id).status["resolve"] == "ok"
    _skip_or(run)


def test_unresolved_trip_flight_still_targets_airtrail():
    """Owner (2026-08-10): a flight's AirTrail write needs no trip, so an
    unsettled collection (here an open one-way journey with no return leg yet)
    must NOT block it — backfilled legs that never map cleanly to a trip were
    stranded. The flight resolves as APPROVABLE with only the collection-
    independent AirTrail target; the AdventureLog transportation is withheld
    (would break RULE-08 with no collection) and waits for a trip.

    Supersedes the old 'a deferred flight advertises NO targets' contract."""
    def run():
        with SessionLocal() as s:
            _seed_home(s)
            s.query(ItemKey).delete(); s.query(TravelItem).delete(); s.commit()
            out = _leg("SOLO9", "1", "HHH", "LIS", "2027-09-01T08:00",
                       "2027-09-01T12:00", from_home=True, dest_country="PT")
            o = _add_item(s, out)
            resolve_pending(s, adventurelog=None)
            it = s.get(TravelItem, o.id)
            assert it.status["resolve"] == "ok"                 # approvable now
            assert it.resolution["targets"] == ["airtrail.flight"]
            assert "adventurelog.transportation" not in it.resolution["targets"]
            assert it.resolution.get("collection_pending") is True
            # Still needs a human eye (which trip?), but with a REASON, not a
            # reasonless dead end — and an enabled Approve.
            assert (it.review or {}).get("required") is True
            assert it.review["reasons"] == ["no_collection_match"]
    _skip_or(run)


def test_middle_leg_inherits_the_enclosing_trip():
    """A leg that never touches home (IAD->PHX) still belongs to the journey
    bounded by the home departure and the home return."""
    def run():
        from app.resolving import _trip_envelope
        with SessionLocal() as s:
            _seed_home(s)
            def leg(num, frm, to, dep_at, arr_at, from_home=False, to_home=False):
                p = {"item_type": "flight", "booking_status": "confirmed",
                     "booking_reference": "ENV02",
                     "flight": {"flight_number": num,
                                "departure": {"place": {"raw": frm, "iata": frm,
                                                        **({"is_home": True} if from_home else {})},
                                              "time": _dt(dep_at)},
                                "arrival": {"place": {"raw": to, "iata": to,
                                                      **({"is_home": True} if to_home else {})},
                                            "time": _dt(arr_at)}}}
                return p, _add_item(s, p)
            # Two legs on the SAME DAY, as a real connection: ordering them by
            # date alone leaves the tie to chance, which can put the middle leg
            # before the departure from home and hide the closed journey.
            leg("10", "HHH", "IAD", "2027-10-02T08:00", "2027-10-02T10:30",
                from_home=True)
            mid_payload, mid = leg("11", "IAD", "PHX", "2027-10-02T13:00",
                                   "2027-10-02T15:40")
            leg("12", "PHX", "HHH", "2027-10-11T07:00", "2027-10-11T14:20",
                to_home=True)
            assert _trip_envelope(s, mid, mid_payload) == ("2027-10-02", "2027-10-11")
    _skip_or(run)


def test_events_never_trigger_passenger_review():
    """Passengers only matter for FLIGHTS (AirTrail). An event assuming self
    and demanding 'confirm who travelled' is meaningless — AdventureLog does
    not track who was at a dinner."""
    def run():
        from app.models import TravelerIdentity
        with SessionLocal() as s:
            _seed_home(s)
            if not s.query(TravelerIdentity).filter_by(kind="self").first():
                s.add(TravelerIdentity(display_name="Me", kind="self"))
                s.commit()
            ev = {"item_type": "event", "booking_status": "confirmed",
                  "event": {"title": "Dinner w Friends",
                            "start": _dt("2028-05-10T18:30")}}
            it = _add_item(s, ev)
            resolve_pending(s, adventurelog=None)
            done = s.get(TravelItem, it.id)
            reasons = (done.review or {}).get("reasons") or []
            assert "missing_passenger_list" not in reasons, reasons
            assert not done.extraction["payload"].get("travelers")
    _skip_or(run)


def test_apply_maps_coords_from_url_and_bare_coordinate():
    """D-61: a google_maps drive gets its stops' coords at RESOLVE — from the
    embedded !1d<lon>!2d<lat> pairs in source_url, and from a bare-coordinate
    stop's own raw — so the geocoder never has to locate Google's addresses."""
    if not DEPS:
        return
    from app.resolving import _apply_maps_coords
    src = ("https://www.google.com/maps/dir/100+Harbour+Ridge+Pkwy/Port Calloway/@x/data=!1m1!2m2"
           "!1d-79.3832123!2d43.6532456!1m1!2m2!1d-76.4930295!2d44.2334401!3e0")
    pay = {"item_type": "ground_transport", "ground_transport": {
        "departure": {"place": {"raw": "2074 Donald Harbour Pkwy, Locust Hill, ON"}},
        "arrival": {"place": {"raw": "Port Calloway, Ontario", "city": "Port Calloway"}},
        "waypoints": [],
        "route": {"provider": "google_maps", "source_url": src}}}
    out = _apply_maps_coords(pay)["ground_transport"]
    assert out["departure"]["place"]["lat"] == 43.6532456
    assert out["departure"]["place"]["lon"] == -79.3832123
    assert out["arrival"]["place"]["lat"] == 44.2334401

    # a bare-coordinate stop reads its own raw
    pay2 = {"item_type": "ground_transport", "ground_transport": {
        "departure": {"place": {"raw": "43.6532,-79.3832"}},
        "arrival": {"place": {"raw": "45.4215,-75.6972"}},
        "waypoints": [],
        "route": {"provider": "google_maps", "source_url": ""}}}
    out2 = _apply_maps_coords(pay2)["ground_transport"]
    assert out2["departure"]["place"]["lat"] == 43.6532
    assert out2["arrival"]["place"]["lon"] == -75.6972

    # non-maps ground transport is untouched
    pay3 = {"item_type": "ground_transport", "ground_transport": {
        "departure": {"place": {"raw": "Union Station"}}, "arrival": {"place": {"raw": "X"}},
        "waypoints": [], "route": {"provider": "amtrak", "source_url": "!1d-1!2d2"}}}
    assert "lat" not in _apply_maps_coords(pay3)["ground_transport"]["departure"]["place"]


# --------------------------------------------------- Yonder trip auto-link (2.1.3)

def _reset_items_and_trips(s):
    from app.models import Trip
    s.query(ItemKey).delete()
    s.query(TravelItem).delete()
    s.query(Trip).delete()
    s.commit()


def test_resolve_auto_links_a_single_matching_yonder_trip():
    """2.1.3: exactly one Yonder trip whose dates contain the item -> auto-linked
    on resolve. Independent of AdventureLog (resolve here runs with none)."""
    def run():
        from app.models import Trip
        with SessionLocal() as s:
            _reset_items_and_trips(s); _seed_home(s)
            trip = Trip(name="Lisbon, May 2027", start_date="2027-05-01",
                        end_date="2027-05-31")
            s.add(trip); s.commit(); tid = trip.id
            it = _add_item(s, _lodging_payload())     # 2027-05-10 -> 2027-05-14
            resolve_pending(s, adventurelog=None)
            assert s.get(TravelItem, it.id).trip_id == tid
    _skip_or(run)


def test_resolve_does_not_link_when_two_yonder_trips_match():
    """Overlapping trips -> ambiguous -> left for the operator; never guess."""
    def run():
        from app.models import Trip
        with SessionLocal() as s:
            _reset_items_and_trips(s); _seed_home(s)
            s.add(Trip(name="A", start_date="2027-05-01", end_date="2027-05-31"))
            s.add(Trip(name="B", start_date="2027-04-01", end_date="2027-06-30"))
            s.commit()
            it = _add_item(s, _lodging_payload())
            resolve_pending(s, adventurelog=None)
            assert s.get(TravelItem, it.id).trip_id is None
    _skip_or(run)


def test_resolve_never_overrides_a_pinned_yonder_link():
    """A manual link/unlink pins the item (review.trip_pinned); the resolver must
    then leave the link alone even when exactly one trip would match."""
    def run():
        from app.models import Trip
        with SessionLocal() as s:
            _reset_items_and_trips(s); _seed_home(s)
            s.add(Trip(name="Lisbon, May 2027", start_date="2027-05-01",
                       end_date="2027-05-31"))
            s.commit()
            it = _add_item(s, _lodging_payload())
            it.review = {"trip_pinned": True}         # operator unlinked earlier
            s.commit()
            resolve_pending(s, adventurelog=None)
            assert s.get(TravelItem, it.id).trip_id is None
    _skip_or(run)
