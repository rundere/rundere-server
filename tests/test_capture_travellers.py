"""Yonder 4.1 — capture → review → commit on a shared trip, end to end.

Two siblings share one trip. A booking the sister captures defaults to her;
in review she can re-point it to other travellers on that trip; the commit
tags the timeline activity accordingly. Her unnamed flight is assumed to be
hers (a guest, so it never lands in the owner's AirTrail). The review trip
picker offers only trips she can file into. SQLite via conftest.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

NOW = datetime.now(timezone.utc).isoformat()


def _lodging(ref):
    d = {"precision": "date", "tz": "Europe/Lisbon", "origin": "document"}
    return {"item_type": "lodging", "booking_status": "confirmed", "booking_reference": ref,
            "lodging": {"property_name": "Casa Sisters", "place": {
                "raw": "Casa Sisters", "name": "Casa Sisters", "city": "Lisbon",
                "country": "PT", "lat": 38.72, "lon": -9.14},
                "check_in": {**d, "local": "2027-05-10"},
                "check_out": {**d, "local": "2027-05-14"}}}


if DEPS:
    from app import authn
    from app import committing as C
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, TravelerIdentity, TravelItem, Trip, TripActivity,
                            TripTraveler, User)
    from app.trips import who

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    _TRIPS = ("CT Shared Lisbon", "CT Me Private", "CT Sis Private")
    _PEOPLE = ("CT Me", "CT Sis")

    def _setup():
        with SessionLocal() as s:
            _teardown(s)
            me_t = TravelerIdentity(display_name="CT Me", kind="guest")
            sis_t = TravelerIdentity(display_name="CT Sis", kind="guest")
            s.add_all([me_t, sis_t]); s.flush()
            me = User(username="ct-me", password_hash=authn.hash_password("pw"),
                      auth_source="local", is_admin=True, traveler_id=me_t.id)
            sis = User(username="ct-sis", password_hash=authn.hash_password("pw"),
                       auth_source="local", traveler_id=sis_t.id)
            s.add_all([me, sis]); s.flush()
            shared = Trip(name="CT Shared Lisbon", start_date="2027-05-01",
                          end_date="2027-05-31", owner_id=me.id)
            mine = Trip(name="CT Me Private", start_date="2027-05-01",
                        end_date="2027-05-31", owner_id=me.id)
            hers = Trip(name="CT Sis Private", start_date="2027-05-01",
                        end_date="2027-05-31", owner_id=sis.id)
            s.add_all([shared, mine, hers]); s.flush()
            s.add_all([TripTraveler(trip_id=shared.id, traveler_id=me_t.id, role="self"),
                       TripTraveler(trip_id=shared.id, traveler_id=sis_t.id, role="companion")])
            item = TravelItem(
                item_type="lodging", schema_version="1.4.0", owner_id=sis.id,
                canonical_key="lodging:ct:1", trip_id=shared.id,
                source={"type": "gmail", "ref": "m-ct", "received_at": NOW,
                        "capture_context": {}},
                extraction={"extractor": "llm", "model": "test", "ran_at": NOW,
                            "confidence": 0.6, "payload": _lodging("CT1")},
                review={"required": True, "reasons": ["missing_time"]},
                status={"extract": "ok", "resolve": "needs_review",
                        "airtrail": "pending", "adventurelog": {}})
            s.add(item); s.commit()
            return {"me": me.id, "sis": sis.id, "me_t": me_t.id, "sis_t": sis_t.id,
                    "shared": shared.id, "item": item.id}

    def _teardown(s):
        trips = [t.id for t in s.query(Trip).filter(Trip.name.in_(_TRIPS)).all()]
        acts = [a.id for a in s.query(TripActivity).filter(TripActivity.trip_id.in_(trips)).all()]
        who.clear_activities(s, acts)
        s.query(TripActivity).filter(TripActivity.trip_id.in_(trips)).delete(synchronize_session=False)
        s.query(TripTraveler).filter(TripTraveler.trip_id.in_(trips)).delete(synchronize_session=False)
        s.query(Trip).filter(Trip.id.in_(trips)).delete(synchronize_session=False)
        s.query(TravelItem).filter(TravelItem.canonical_key.like("lodging:ct:%")).delete(
            synchronize_session=False)
        s.query(User).filter(User.username.in_(["ct-me", "ct-sis"])).delete(synchronize_session=False)
        s.query(TravelerIdentity).filter(TravelerIdentity.display_name.in_(_PEOPLE)).delete(
            synchronize_session=False)
        s.commit()

    def _h(username):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=username).one()
            return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def test_sisters_capture_defaults_to_her_and_can_be_repointed():
        ids = _setup()
        settings.auth_required = True
        try:
            page = client.get(f"/review/item/{ids['item']}", headers=_h("ct-sis")).text
            assert "Who is this for" in page
            assert f'name="who:{ids["sis_t"]}" checked' in page        # default: her
            assert f'name="who:{ids["me_t"]}" checked' not in page     # not him
            # the trip picker: the shared trip and her own, never her brother's private one
            assert "CT Shared Lisbon" in page and "CT Sis Private" in page
            assert "CT Me Private" not in page
            # ...and as admin, her private trip isn't offered for his own filing either
            with SessionLocal() as s:
                s.add(TravelItem(item_type="lodging", schema_version="1.4.0",
                                 owner_id=ids["me"], canonical_key="lodging:ct:2",
                                 source={"type": "gmail", "ref": "m-ct2", "received_at": NOW,
                                         "capture_context": {}},
                                 extraction={"extractor": "llm", "model": "test", "ran_at": NOW,
                                             "confidence": 0.6, "payload": _lodging("CT2")},
                                 review={"required": True, "reasons": ["missing_time"]},
                                 status={"extract": "ok", "resolve": "needs_review",
                                         "airtrail": "pending", "adventurelog": {}}))
                s.commit()
                mine_item = s.query(TravelItem).filter_by(canonical_key="lodging:ct:2").one().id
            admin_page = client.get(f"/review/item/{mine_item}", headers=_h("ct-me")).text
            assert "CT Me Private" in admin_page and "CT Sis Private" not in admin_page

            # she re-points it to her brother only
            client.post(f"/review/item/{ids['item']}/save", headers=_h("ct-sis"),
                        data={"who_present": "1", f"who:{ids['me_t']}": "on"})
            with SessionLocal() as s:
                assert s.get(TravelItem, ids["item"]).review["traveler_ids"] == [ids["me_t"]]
            page = client.get(f"/review/item/{ids['item']}", headers=_h("ct-sis")).text
            assert f'name="who:{ids["me_t"]}" checked' in page
        finally:
            settings.auth_required = False
            with SessionLocal() as s:
                _teardown(s)

    def test_commit_tags_the_activity_with_the_review_pick_or_the_capturer():
        ids = _setup()
        try:
            with SessionLocal() as s:
                item = s.get(TravelItem, ids["item"])
                payload = item.extraction["payload"]
                a1 = TripActivity(trip_id=ids["shared"], type="lodging", values={}, starts_at="")
                s.add(a1); s.flush()
                # no pick in review -> the capturer (her)
                C._tag_booked_travelers(s, a1.id, ids["shared"], payload, None,
                                        keep_existing=False, item=item)
                assert who.activity_map(s, [a1.id])[a1.id] == [ids["sis_t"]]
                # a review pick wins
                item.review = {**item.review, "traveler_ids": [ids["me_t"]]}
                a2 = TripActivity(trip_id=ids["shared"], type="lodging", values={}, starts_at="")
                s.add(a2); s.flush()
                C._tag_booked_travelers(s, a2.id, ids["shared"], payload, None,
                                        keep_existing=False, item=item)
                assert who.activity_map(s, [a2.id])[a2.id] == [ids["me_t"]]
                s.commit()
        finally:
            with SessionLocal() as s:
                _teardown(s)

    def test_unnamed_flight_is_assumed_to_be_the_capturers():
        from app.resolving import _capturer_identity, load_identities
        from resolve.identity import resolve_travelers
        ids = _setup()
        try:
            with SessionLocal() as s:
                item = s.get(TravelItem, ids["item"])
                idents = load_identities(s)
                assumed = _capturer_identity(s, item, idents)
                assert assumed is not None and assumed.display_name == "CT Sis"
                tr = resolve_travelers({"item_type": "flight"}, idents, default=assumed)
                assert tr.travelers[0]["raw_name"] == "CT Sis"
                assert tr.self_on_booking is False          # not the owner's AirTrail
        finally:
            with SessionLocal() as s:
                _teardown(s)

    def test_review_shows_how_many_captures_are_waiting_to_be_processed():
        """The Review funnel's "to process" stage: emails not extracted yet +
        items not resolved yet, scoped to the viewer's own (+ open) captures."""
        import re
        from app.models import IngestBatch
        ids = _setup()
        settings.auth_required = True

        def n(user):
            html = client.get("/review/to-process", headers=_h(user)).text
            return int(re.search(r"<b>(\d+)</b>", html).group(1))
        try:
            base_me, base_sis = n("ct-me"), n("ct-sis")
            with SessionLocal() as s:
                s.add_all([IngestBatch(api_key_id="k-ct", user_id=ids["sis"],
                                       payload={}, status="received"),
                           IngestBatch(api_key_id="k-ct", user_id=ids["sis"],
                                       payload={}, status="processed")])     # done: not counted
                it = s.get(TravelItem, ids["item"])
                it.status = {**it.status, "resolve": "pending"}
                s.commit()
            assert n("ct-sis") == base_sis + 2          # her batch + her pending item
            assert n("ct-me") == base_me + 2            # admin sees all (unrestricted)
            assert 'hx-get="/review/to-process"' in client.get(
                "/review", headers=_h("ct-sis")).text
        finally:
            settings.auth_required = False
            with SessionLocal() as s:
                s.query(IngestBatch).filter_by(api_key_id="k-ct").delete()
                s.commit()
                _teardown(s)
