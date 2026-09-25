"""Yonder 4.1 — Discover is per person; travellers + timeline are shared.

Two siblings on one trip: each sees only their own findings and excluded
places, can't open or edit the other's findings by id, has their own discovery
profile, and a run launched by one uses THEIR profile, nationality, currency
and homes. SQLite via conftest.
"""
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
    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, DiscoveryExclusion, DiscoveryFinding, DiscoveryProfile,
                            DiscoveryRun, TravelerIdentity, Trip, TripTraveler, User)

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    def _setup():
        with SessionLocal() as s:
            _teardown(s)
            me_t = TravelerIdentity(display_name="DU Me", kind="guest", nationality="CA",
                                    base_currency="CAD")
            sis_t = TravelerIdentity(display_name="DU Sis", kind="guest", nationality="US",
                                     base_currency="USD")
            s.add_all([me_t, sis_t]); s.flush()
            me = User(username="du-me", password_hash=authn.hash_password("pw"),
                      auth_source="local", traveler_id=me_t.id)
            sis = User(username="du-sis", password_hash=authn.hash_password("pw"),
                       auth_source="local", traveler_id=sis_t.id)
            s.add_all([me, sis]); s.flush()
            trip = Trip(name="DU Shared", start_date="2027-05-01", end_date="2027-05-31",
                        owner_id=me.id)
            s.add(trip); s.flush()
            s.add_all([TripTraveler(trip_id=trip.id, traveler_id=me_t.id, role="self"),
                       TripTraveler(trip_id=trip.id, traveler_id=sis_t.id, role="companion")])
            mine = DiscoveryFinding(trip_id=trip.id, user_id=me.id, topic="food", scope="city",
                                    place_key="PT:Lisbon", body_md="du-my-food-notes")
            hers = DiscoveryFinding(trip_id=trip.id, user_id=sis.id, topic="food", scope="city",
                                    place_key="PT:Lisbon", body_md="du-her-food-notes")
            s.add_all([mine, hers,
                       DiscoveryProfile(traveler_id=me_t.id,
                                        values={"food": {"allergies": "du-my-allergy"}})])
            s.commit()
            return {"me": me.id, "sis": sis.id, "me_t": me_t.id, "sis_t": sis_t.id,
                    "trip": trip.id, "mine": mine.id, "hers": hers.id}

    def _teardown(s):
        tids = [t.id for t in s.query(Trip).filter_by(name="DU Shared").all()]
        for M in (DiscoveryFinding, DiscoveryExclusion, DiscoveryRun):
            s.query(M).filter(M.trip_id.in_(tids)).delete(synchronize_session=False)
        s.query(TripTraveler).filter(TripTraveler.trip_id.in_(tids)).delete(synchronize_session=False)
        s.query(Trip).filter(Trip.id.in_(tids)).delete(synchronize_session=False)
        ids = [t.id for t in s.query(TravelerIdentity).filter(
            TravelerIdentity.display_name.in_(["DU Me", "DU Sis"])).all()]
        s.query(DiscoveryProfile).filter(DiscoveryProfile.traveler_id.in_(ids)).delete(
            synchronize_session=False)
        s.query(User).filter(User.username.in_(["du-me", "du-sis"])).delete(synchronize_session=False)
        s.query(TravelerIdentity).filter(TravelerIdentity.id.in_(ids)).delete(synchronize_session=False)
        s.commit()

    def _h(username):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=username).one()
            return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _run(fn):
        ids = _setup()
        settings.auth_required = True
        try:
            fn(ids)
        finally:
            settings.auth_required = False
            with SessionLocal() as s:
                _teardown(s)

    def test_each_sees_only_their_own_findings_but_the_same_timeline():
        def body(ids):
            me = client.get(f"/trips/{ids['trip']}", headers=_h("du-me")).text
            her = client.get(f"/trips/{ids['trip']}", headers=_h("du-sis")).text
            assert "du-my-food-notes" in me and "du-her-food-notes" not in me
            assert "du-her-food-notes" in her and "du-my-food-notes" not in her
            # the shared part: both see both travellers
            assert "DU Me" in her and "DU Sis" in me
            # API snapshot is per caller too
            snap = client.get(f"/api/v1/trips/{ids['trip']}", headers=_h("du-sis")).json()
            assert [f["body_md"] for f in snap["discovery"]] == ["du-her-food-notes"]
        _run(body)

    def test_cannot_open_or_edit_someone_elses_finding():
        def body(ids):
            h = _h("du-sis")
            assert client.get(f"/trips/{ids['trip']}/finding/{ids['mine']}/edit",
                              headers=h).status_code == 404
            client.post(f"/trips/{ids['trip']}/finding/{ids['mine']}/edit", headers=h,
                        data={"body": "overwritten"})
            with SessionLocal() as s:
                assert s.get(DiscoveryFinding, ids["mine"]).body_md == "du-my-food-notes"
            # her own is fine
            assert client.get(f"/trips/{ids['trip']}/finding/{ids['hers']}/edit",
                              headers=h).status_code == 200
        _run(body)

    def test_excluding_a_place_is_per_person():
        def body(ids):
            client.post(f"/trips/{ids['trip']}/derived-locations/toggle",
                        headers=_h("du-sis"), data={"place_key": "PT:Lisbon"})
            with SessionLocal() as s:
                rows = s.query(DiscoveryExclusion).filter_by(trip_id=ids["trip"]).all()
                assert [(r.user_id, r.place_key) for r in rows] == [(ids["sis"], "PT:Lisbon")]
                # her exclusion dropped HER draft, not mine
                assert s.get(DiscoveryFinding, ids["hers"]) is None
                assert s.get(DiscoveryFinding, ids["mine"]) is not None
        _run(body)

    def test_discovery_profile_is_per_person():
        def body(ids):
            assert "du-my-allergy" in client.get("/trips/settings/discovery-profile",
                                                 headers=_h("du-me")).text
            # she never sees his tastes, and saving creates HER row
            assert "du-my-allergy" not in client.get("/trips/settings/discovery-profile",
                                                     headers=_h("du-sis")).text
            client.post("/trips/settings/discovery-profile", headers=_h("du-sis"),
                        data={"allergies": "du-her-allergy", "budget_ceiling": "0",
                              "mobility": "no_limits"})
            with SessionLocal() as s:
                hers = s.query(DiscoveryProfile).filter_by(traveler_id=ids["sis_t"]).one()
                mine = s.query(DiscoveryProfile).filter_by(traveler_id=ids["me_t"]).one()
                assert hers.values["food"]["allergies"] == "du-her-allergy"
                assert mine.values["food"]["allergies"] == "du-my-allergy"
        _run(body)

    def test_a_run_uses_the_viewers_inputs_and_belongs_to_them(monkeypatch):
        from app.trips import discovery_jobs as dj
        launched = {}
        monkeypatch.setattr(dj, "launch", lambda trip_id, **kw: launched.update(kw) or "r")

        def body(ids):
            client.post(f"/trips/{ids['trip']}/discover", headers=_h("du-sis"))
            assert launched["user_id"] == ids["sis"]
            assert launched["nationality"] == "US"
            assert launched["base_currencies"][0] == "USD"
            assert launched["discovery_profile"] is None          # hers, not his
        _run(body)
