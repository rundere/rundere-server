"""5.1, D-251: trip preferences. Everyone's discovery constraints on a trip,
combined and unattributed, for trip editors only; an editor's Discover run uses
them, a viewer's never does. Placeholder guests are never counted (D-100).
Fictional people; SQLite via conftest.
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
    from app.models import (Base, DiscoveryProfile, DiscoveryRun, TravelerIdentity, Trip,
                            TripMember, TripTraveler, User)
    from app.trips import trip_preferences as tp

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    NAMES = ["TP Ana", "TP Ben", "TP Guest", "TP Vera"]
    USERS = ["tp-ana", "tp-ben", "tp-vera", "tp-stranger"]

    def _setup():
        with SessionLocal() as s:
            _teardown(s)
            ana_t = TravelerIdentity(display_name="TP Ana", kind="guest")
            ben_t = TravelerIdentity(display_name="TP Ben", kind="guest")
            guest_t = TravelerIdentity(display_name="TP Guest", kind="guest", generic=True)
            vera_t = TravelerIdentity(display_name="TP Vera", kind="guest")
            s.add_all([ana_t, ben_t, guest_t, vera_t]); s.flush()
            users = {}
            for name, t in (("tp-ana", ana_t), ("tp-ben", ben_t), ("tp-vera", vera_t),
                            ("tp-stranger", None)):
                u = User(username=name, password_hash=authn.hash_password("pw"),
                         auth_source="local", traveler_id=t.id if t else None)
                s.add(u); users[name] = u
            s.flush()
            trip = Trip(name="TP Group", start_date="2027-06-01", end_date="2027-06-20",
                        owner_id=users["tp-ana"].id)
            s.add(trip); s.flush()
            s.add_all([
                TripTraveler(trip_id=trip.id, traveler_id=ana_t.id, role="self"),
                TripTraveler(trip_id=trip.id, traveler_id=ben_t.id, role="companion"),
                TripTraveler(trip_id=trip.id, traveler_id=guest_t.id, role="companion"),
                # Vera only has a view-only share (D-122), she isn't travelling
                TripMember(trip_id=trip.id, user_id=users["tp-vera"].id),
                DiscoveryProfile(traveler_id=ana_t.id, values={
                    "food": {"dietary_restrictions": ["vegetarian"], "budget_ceiling": 0,
                             "allergies": "tp-peanuts"},
                    "activity": {"interests": ["markets"]},
                    "practical": {"mobility": "moderate"},
                    "notes": "tp-ana-private-notes"}),
                DiscoveryProfile(traveler_id=ben_t.id, values={
                    "food": {"dietary_restrictions": ["vegan"], "budget_ceiling": 2,
                             "cuisine_dislikes": ["tp-cuisine"]},
                    "activity": {"interests": ["hidden_gems"]},
                    "practical": {"mobility": "wheelchair"}}),
                # a placeholder guest never counts, even with a stray profile row
                DiscoveryProfile(traveler_id=guest_t.id, values={
                    "food": {"allergies": "tp-guest-allergy", "budget_ceiling": 1}}),
                DiscoveryProfile(traveler_id=vera_t.id, values={
                    "food": {"allergies": "tp-vera-allergy"}}),
            ])
            s.commit()
            return {"trip": trip.id, **{k: u.id for k, u in users.items()}}

    def _teardown(s):
        tids = [t.id for t in s.query(Trip).filter_by(name="TP Group").all()]
        for M in (TripTraveler, TripMember, DiscoveryRun):
            s.query(M).filter(M.trip_id.in_(tids)).delete(synchronize_session=False)
        s.query(Trip).filter(Trip.id.in_(tids)).delete(synchronize_session=False)
        ids = [t.id for t in s.query(TravelerIdentity)
               .filter(TravelerIdentity.display_name.in_(NAMES)).all()]
        s.query(DiscoveryProfile).filter(DiscoveryProfile.traveler_id.in_(ids)).delete(
            synchronize_session=False)
        s.query(User).filter(User.username.in_(USERS)).delete(synchronize_session=False)
        s.query(TravelerIdentity).filter(TravelerIdentity.id.in_(ids)).delete(
            synchronize_session=False)
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

    def test_the_merge_keeps_the_strictest_and_skips_placeholder_guests():
        def body(ids):
            with SessionLocal() as s:
                c = tp.combined_values(s, ids["trip"])
            assert c["food"]["dietary_restrictions"] == ["vegetarian", "vegan"]
            assert c["food"]["cuisine_dislikes"] == ["tp-cuisine"]
            assert c["food"]["allergies"] == ["tp-peanuts"]       # not the guest's
            assert c["food"]["budget_ceiling"] == 2               # 0 = no limit, ignored
            assert c["practical"]["mobility"] == "wheelchair"
            assert c["activity"]["interests"] == ["markets", "hidden_gems"]
        _run(body)

    def test_editors_see_it_unattributed_viewers_and_strangers_dont():
        def body(ids):
            snap = client.get(f"/api/v1/trips/{ids['trip']}", headers=_h("tp-ben")).json()
            prefs = snap["trip_preferences"]
            assert prefs["food"]["budget_ceiling"]["tier"] == 2
            assert prefs["practical"]["mobility"]["key"] == "wheelchair"
            text = str(prefs)
            for leak in ("TP Ana", "TP Ben", "tp-ana", "tp-ana-private-notes",
                         "tp-guest-allergy", "tp-vera-allergy"):
                assert leak not in text
            # a view-only share gets no combined set
            vera = client.get(f"/api/v1/trips/{ids['trip']}", headers=_h("tp-vera"))
            assert vera.status_code == 200
            assert vera.json().get("trip_preferences") is None
            # a stranger can't see the trip at all
            assert client.get(f"/api/v1/trips/{ids['trip']}",
                              headers=_h("tp-stranger")).status_code == 404
        _run(body)

    def test_an_editors_run_plans_for_the_group(monkeypatch):
        from app.trips import discovery_jobs as dj
        launched = {}
        monkeypatch.setattr(dj, "launch", lambda trip_id, **kw: launched.update(kw) or "r")

        def body(ids):
            client.post(f"/trips/{ids['trip']}/discover", headers=_h("tp-ben"))
            prof = launched["discovery_profile"]
            assert prof["food"]["dietary_restrictions"] == ["vegetarian", "vegan"]
            assert prof["food"]["allergies"] == "tp-peanuts"
            assert prof["food"]["budget_ceiling"] == 2
            assert prof["practical"]["mobility"] == "wheelchair"
            # his own interests, never her notes
            assert prof["activity"]["interests"] == ["hidden_gems"]
            assert "notes" not in prof
        _run(body)

    def test_a_viewer_cant_start_a_run_and_the_guard_keeps_their_own(monkeypatch):
        """View-only access can't launch Discover at all (403); should that ever
        change, `discover_profile` still hands a viewer only their own profile."""
        from app.trips import discovery_jobs as dj
        launched = {}
        monkeypatch.setattr(dj, "launch", lambda trip_id, **kw: launched.update(kw) or "r")

        def body(ids):
            r = client.post(f"/trips/{ids['trip']}/discover", headers=_h("tp-vera"))
            assert r.status_code == 403 and launched == {}
            with SessionLocal() as s:
                vera = s.get(User, ids["tp-vera"])
                trip = s.get(Trip, ids["trip"])
                own = {"food": {"allergies": "tp-vera-allergy"}}
                assert tp.discover_profile(s, trip, vera, own) == own
        _run(body)

    def test_no_roster_profiles_means_no_change():
        assert tp.for_discover({"food": {"allergies": "x"}}, None) == {"food": {"allergies": "x"}}
        assert tp.for_discover(None, None) is None
