"""YONDER-190 — Run·dere works on its own data (SPEC-8 D-146–D-148).

With no AirTrail and no AdventureLog, a flight capture
resolves its airports (with time zones) from Run·dere's own table, attaches to a
Run·dere trip, is approved and appears on the timeline. External targets exist
only for systems the capture's owner connected, and RULE-10's "self" is the
owner's own traveller (D-150)."""
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import sqlalchemy  # noqa: F401
    DEPS = True
except ImportError:
    DEPS = False

NOW = datetime.now(timezone.utc).isoformat()


def test_bundled_airports_cover_codes_with_time_zones():
    from resolve import airports
    for code, tz, country in (("YYZ", "America/Toronto", "CA"), ("LIS", "Europe/Lisbon", "PT"),
                              ("BLR", "Asia/Kolkata", "IN"), ("GCM", "America/Cayman", "KY")):
        a = airports.lookup(code)
        assert a is not None and a.tz == tz and a.country == country
    assert airports.lookup("EGLL").iata == "LHR"                      # ICAO works too
    assert all(a.tz for a in airports.bundled().values())             # every one has a zone
    # AirTrail only fills gaps; it never overrides the table.
    from adapters.targets.airtrail import Airport
    flown = {"YYZ": Airport("YYZ", None, "x", None, "ZZ", 0, 0, "UTC"),
             "QQQ": Airport("QQQ", None, "Tiny field", None, "CA", 1, 1, "America/Toronto")}
    ref = airports.reference(flown)
    assert ref["YYZ"].country == "CA" and ref["QQQ"].name == "Tiny field"


def test_country_names_come_from_cldr():
    from resolve.countries import country_names
    names = country_names()
    assert names["KY"] == "Cayman Islands" and names["IN"] == "India"


def test_external_targets_only_for_connected_systems():
    from resolve.targets import compute_targets
    flight = {"item_type": "flight"}
    kw = dict(self_on_booking=True, within_home=False, has_collection=True,
              unsuppressed_places=2, creating_collection=True)
    assert compute_targets(flight, external=frozenset(), **kw) == []
    assert compute_targets(flight, external={"airtrail"}, **kw) == ["airtrail.flight"]
    both = compute_targets(flight, **kw)                              # None = both (CLI)
    assert "airtrail.flight" in both and "adventurelog.transportation" in both


def test_rule10_self_is_the_capture_owners_traveller():
    """A companion's own flight goes to THEIR AirTrail (self_on_booking), and the
    deployment owner's `self` traveller on someone else's booking doesn't count."""
    from resolve.identity import Identity, resolve_travelers
    owner = Identity(id="1", display_name="Olive Owner", kind="self")
    pat = Identity(id="2", display_name="Pat Companion", kind="guest")
    ids = [owner, pat]
    pats = {"travelers": [{"raw_name": "Pat Companion"}]}
    olives = {"travelers": [{"raw_name": "Olive Owner"}]}
    assert resolve_travelers(pats, ids, default=pat, me=pat).self_on_booking
    assert not resolve_travelers(olives, ids, default=pat, me=pat).self_on_booking
    assert resolve_travelers({}, ids, default=pat, me=pat).self_on_booking   # unnamed: theirs
    # No owner (an open capture): the `self` kind decides, as before.
    assert resolve_travelers(olives, ids).self_on_booking
    assert not resolve_travelers(pats, ids).self_on_booking


def _dt(local):
    # No time zone in the document: the airport table has to supply it.
    return {"local": local, "precision": "datetime", "origin": "document"}


def _flight(ref, num, frm, to, dep, arr, who):
    return {"item_type": "flight", "booking_status": "confirmed", "booking_reference": ref,
            "travelers": [{"raw_name": who}],
            "flight": {"flight_number": num,
                       "marketing_carrier": {"name": "Test Air", "iata": "TA"},
                       "departure": {"place": {"raw": frm, "iata": frm}, "time": _dt(dep)},
                       "arrival": {"place": {"raw": to, "iata": to}, "time": _dt(arr)}}}


@pytest.mark.skipif(not DEPS, reason="sqlalchemy not installed")
def test_flight_to_timeline_with_nothing_connected(monkeypatch):
    from app import integrations
    from app.committing import commit_pending
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.models import (Base, CommitAttempt, HomeProfile, ItemKey, TravelerIdentity,
                            TravelItem, Trip, TripActivity, User)
    from app.resolving import load_identities, resolve_pending
    Base.metadata.create_all(engine)
    for name in ("airtrail_token", "adventurelog_api_key"):
        monkeypatch.setattr(settings, name, "")

    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as s:
        # _journey_minter looks at every flight: start from a clean queue, as
        # the resolver tests do.
        s.query(ItemKey).delete()
        s.query(TravelItem).delete()
        s.commit()
        me = TravelerIdentity(display_name=f"Fp Traveller {tag}", kind="guest")
        s.add(me)
        s.flush()
        u = User(username=f"fp-{tag}", auth_source="local", traveler_id=me.id)
        s.add(u)
        s.flush()
        # Only an airport code and coordinates: no AirTrail to fill the rest.
        s.add(HomeProfile(kind="home_airport", label=f"Fp home {tag}", iata="YYZ",
                          lat=43.6772, lon=-79.6306, radius_km=50.0, country="CA",
                          traveler_id=me.id))
        out = _flight(f"FPO{tag}", "101", "YYZ", "LIS", "2031-03-01T21:00",
                      "2031-03-02T09:30", me.display_name)
        back = _flight(f"FPB{tag}", "102", "LIS", "YYZ", "2031-03-10T12:00",
                       "2031-03-10T15:00", me.display_name)
        items = []
        for p in (out, back):
            it = TravelItem(item_type="flight", schema_version="1.4.0", owner_id=u.id,
                            source={"type": "gmail", "ref": p["booking_reference"],
                                    "received_at": NOW, "capture_context": {}},
                            extraction={"extractor": "llm", "model": "test", "ran_at": NOW,
                                        "confidence": 0.9, "payload": p},
                            status={"extract": "ok", "resolve": "pending"})
            s.add(it)
            items.append(it)
        s.commit()
        ids, uid = [i.id for i in items], u.id

        try:
            assert integrations.clients_for_user(s, uid).external == frozenset()
            resolve_pending(s, clients_for=integrations.ClientCache(s))
            for iid in ids:
                it = s.get(TravelItem, iid)
                fl = it.extraction["payload"]["flight"]
                zones = {fl["departure"]["place"].get("tz"), fl["arrival"]["place"].get("tz")}
                assert zones == {"America/Toronto", "Europe/Lisbon"}        # from the table
                assert it.status["resolve"] == "ok", (it.status, it.review)
                assert it.resolution["targets"] == ["yonder.activity"]      # Run·dere only
                assert it.resolution.get("mint_yonder_trip") is True
                it.review = {**(it.review or {}), "required": False,
                             "action_taken": "approved"}
            s.commit()

            summary = commit_pending(s, identities=load_identities(s), dry_run=False,
                                     clients_for=integrations.ClientCache(s))
            assert summary["writes_failed"] == 0 and not summary.get("external_skipped")
            trip_ids = {s.get(TravelItem, iid).trip_id for iid in ids}
            assert len(trip_ids) == 1 and None not in trip_ids               # one trip
            trip = s.get(Trip, trip_ids.pop())
            assert trip.owner_id == uid and trip.name.startswith("Portugal")
            acts = s.query(TripActivity).filter(TripActivity.trip_id == trip.id).all()
            assert len(acts) == 2                                            # on the timeline
            external = (s.query(CommitAttempt).filter(CommitAttempt.item_id.in_(ids),
                                                      ~CommitAttempt.target.like("yonder.%"))
                        .count())
            assert external == 0                                   # nothing sent anywhere
        finally:
            for iid in ids:
                it = s.get(TravelItem, iid)
                if it is not None and it.trip_id:
                    t = s.get(Trip, it.trip_id)
                    if t is not None:
                        s.query(TripActivity).filter(TripActivity.trip_id == t.id).delete()
                        s.delete(t)
            s.query(CommitAttempt).filter(CommitAttempt.item_id.in_(ids)).delete(
                synchronize_session=False)
            s.query(ItemKey).filter(ItemKey.item_id.in_(ids)).delete(synchronize_session=False)
            s.query(TravelItem).filter(TravelItem.id.in_(ids)).delete(synchronize_session=False)
            s.query(HomeProfile).filter(HomeProfile.label == f"Fp home {tag}").delete()
            s.query(User).filter(User.id == uid).delete()
            s.query(TravelerIdentity).filter(
                TravelerIdentity.display_name == f"Fp Traveller {tag}").delete()
            s.commit()
