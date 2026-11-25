"""YONDER-404 — the apps can create a trip and manage who's on it.

POST /api/v1/trips makes a hand-made trip owned by the caller (D-121), with the
caller's own traveller on it. GET/POST /trips/{id}/travelers and DELETE
/trips/{id}/travelers/{tid} match the web's Who's-traveling section: add a
saved traveller, quick-add a guest by name (a D-100 placeholder), remove.
Being on the roster gives a linked account edit access (D-199), so every write
needs edit access: a stranger gets 404, a view-only share 403.
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
    import pytest

    from app import authn
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, TravelerIdentity, Trip, TripActivity,
                            TripActivityTraveler, TripMember, TripTraveler, User)

    Base.metadata.create_all(engine)
    client = TestClient(app)

    NAMES = ("Ro Owner", "Ro Friend", "Ro Deployment Self", "Ro New Guest")

    @pytest.fixture(autouse=True)
    def _clean():
        yield
        with SessionLocal() as s:
            uids = [u.id for u in s.query(User).filter(User.username.like("ro-%")).all()]
            tids = [t.id for t in s.query(Trip).filter(Trip.owner_id.in_(uids)).all()]
            acts = [a.id for a in s.query(TripActivity).filter(TripActivity.trip_id.in_(tids))]
            s.query(TripActivityTraveler).filter(
                TripActivityTraveler.activity_id.in_(acts)).delete(synchronize_session=False)
            s.query(TripActivity).filter(TripActivity.id.in_(acts)).delete(
                synchronize_session=False)
            for m in (TripTraveler, TripMember):
                s.query(m).filter(m.trip_id.in_(tids)).delete(synchronize_session=False)
            s.query(Trip).filter(Trip.id.in_(tids)).delete(synchronize_session=False)
            s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
            s.query(TravelerIdentity).filter(TravelerIdentity.display_name.in_(NAMES)).delete(
                synchronize_session=False)
            s.commit()

    def _traveller(name, **kw):
        with SessionLocal() as s:
            t = TravelerIdentity(display_name=name, kind=kw.pop("kind", "guest"), **kw)
            s.add(t)
            s.commit()
            return t.id

    def _user(name, traveler_id=None):
        with SessionLocal() as s:
            u = User(username=name, auth_source="local", traveler_id=traveler_id)
            s.add(u)
            s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _h(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _create(u, **body):
        return client.post("/api/v1/trips", headers=_h(u), json={"name": "Lisbon", **body})

    # ------------------------------------------------------------------ create
    def test_create_trip_owned_by_caller_with_their_traveller_on_it():
        me = _user("ro-owner", _traveller("Ro Owner"))
        r = _create(me, start_date="2027-04-01", end_date="2027-04-08", notes="  Tram 28  ")
        assert r.status_code == 201, r.text
        trip = r.json()
        assert trip["name"] == "Lisbon" and trip["source"] == "manual"
        assert trip["owner_id"] == me.id and trip["can_edit"] is True
        assert trip["notes"] == "Tram 28"
        assert trip["start_date"] == "2027-04-01" and trip["end_date"] == "2027-04-08"
        snap = client.get(f"/api/v1/trips/{trip['id']}", headers=_h(me)).json()
        assert [t["display_name"] for t in snap["travelers"]] == ["Ro Owner"]
        assert trip["id"] in [t["id"] for t in client.get("/api/v1/trips",
                                                          headers=_h(me)).json()]

    def test_create_trip_never_adds_the_deployment_self_traveller():
        # The deployment's kind=self person is someone else here. Putting them
        # on the roster would make the trip editable by them (D-199).
        _traveller("Ro Deployment Self", kind="self")
        me = _user("ro-noself")                              # no traveller linked
        trip = _create(me).json()
        with SessionLocal() as s:
            assert s.query(TripTraveler).filter_by(trip_id=trip["id"]).count() == 0

    def test_create_trip_needs_a_name():
        me = _user("ro-blank")
        assert _create(me, name="   ").status_code == 422
        assert client.post("/api/v1/trips", json={"name": "x"}).status_code == 401

    # ------------------------------------------------------------------ roster
    def test_add_saved_traveller_quick_add_guest_and_remove():
        me = _user("ro-editor", _traveller("Ro Owner"))
        friend = _traveller("Ro Friend")
        tid = _create(me).json()["id"]
        h = _h(me)
        roster = client.get(f"/api/v1/trips/{tid}/travelers", headers=h).json()
        assert friend in [a["id"] for a in roster["available"]]

        r = client.post(f"/api/v1/trips/{tid}/travelers", headers=h, json={"traveler_id": friend})
        assert r.status_code == 200, r.text
        assert "Ro Friend" in [t["display_name"] for t in r.json()["travelers"]]
        assert friend not in [a["id"] for a in r.json()["available"]]
        # again: a no-op
        r = client.post(f"/api/v1/trips/{tid}/travelers", headers=h, json={"traveler_id": friend})
        assert [t["display_name"] for t in r.json()["travelers"]].count("Ro Friend") == 1

        # quick-add mints a generic guest (D-100) with the companion role
        r = client.post(f"/api/v1/trips/{tid}/travelers", headers=h, json={"name": " Ro New Guest "})
        guest = next(t for t in r.json()["travelers"] if t["display_name"] == "Ro New Guest")
        assert guest["role"] == "companion" and guest["has_account"] is False
        with SessionLocal() as s:
            assert s.get(TravelerIdentity, guest["id"]).generic is True

        # removing someone drops their activity tags too
        with SessionLocal() as s:
            a = TripActivity(trip_id=tid, type="note", values={"title": "x"}, starts_at="")
            s.add(a)
            s.flush()
            s.add(TripActivityTraveler(activity_id=a.id, traveler_id=friend))
            s.commit()
        assert client.delete(f"/api/v1/trips/{tid}/travelers/{friend}",
                             headers=h).status_code == 204
        assert client.delete(f"/api/v1/trips/{tid}/travelers/{friend}",
                             headers=h).status_code == 204            # idempotent
        with SessionLocal() as s:
            assert s.get(TripTraveler, {"trip_id": tid, "traveler_id": friend}) is None
            assert s.query(TripActivityTraveler).filter_by(traveler_id=friend).count() == 0

    def test_add_needs_exactly_one_of_id_or_name_and_a_real_traveller():
        me = _user("ro-bad")
        tid = _create(me).json()["id"]
        url, h = f"/api/v1/trips/{tid}/travelers", _h(me)
        assert client.post(url, headers=h, json={}).status_code == 422
        assert client.post(url, headers=h, json={"name": "  "}).status_code == 422
        assert client.post(url, headers=h,
                           json={"traveler_id": 1, "name": "Both"}).status_code == 422
        assert client.post(url, headers=h, json={"traveler_id": 987654}).status_code == 404

    def test_roster_scoping_stranger_and_view_only_share():
        owner = _user("ro-own2", _traveller("Ro Owner"))
        friend = _traveller("Ro Friend")
        viewer, stranger = _user("ro-viewer"), _user("ro-stranger")
        tid = _create(owner).json()["id"]
        with SessionLocal() as s:
            s.add(TripMember(trip_id=tid, user_id=viewer.id))       # view-only share
            s.commit()
        url = f"/api/v1/trips/{tid}/travelers"
        # a stranger can't see the roster or change it, and can't tell it exists
        assert client.get(url, headers=_h(stranger)).status_code == 404
        assert client.post(url, headers=_h(stranger),
                           json={"traveler_id": friend}).status_code == 404
        assert client.delete(f"{url}/{friend}", headers=_h(stranger)).status_code == 404
        # a viewer sees who's going but not who could be added, and can't change it
        r = client.get(url, headers=_h(viewer))
        assert r.status_code == 200 and r.json()["available"] == []
        assert [t["display_name"] for t in r.json()["travelers"]] == ["Ro Owner"]
        assert client.post(url, headers=_h(viewer),
                           json={"name": "Ro New Guest"}).status_code == 403
        with SessionLocal() as s:
            owner_tid = s.get(User, owner.id).traveler_id
        assert client.delete(f"{url}/{owner_tid}", headers=_h(viewer)).status_code == 403
        with SessionLocal() as s:
            assert s.query(TripTraveler).filter_by(trip_id=tid).count() == 1

    def test_web_new_trip_links_the_creator_not_the_deployment_owner():
        # The web's New trip used to put the deployment's kind=self traveller on
        # every trip, which made anyone's hand-made trip editable by the owner
        # (D-199). Now it's the creator's own traveller, as on the API.
        from app.config import settings
        boss_t = _traveller("Ro Deployment Self", kind="self")
        boss = _user("ro-boss", boss_t)
        me = _user("ro-webuser", _traveller("Ro Owner"))
        was = settings.auth_required
        settings.auth_required = True
        try:
            r = client.post("/trips", headers=_h(me), follow_redirects=False,
                            data={"name": "Ro web trip", "start": "", "end": "", "notes": ""})
            assert r.status_code in (200, 303), r.text
        finally:
            settings.auth_required = was
        with SessionLocal() as s:
            trip = s.query(Trip).filter_by(name="Ro web trip").one()
            assert trip.owner_id == me.id
            on = {r.traveler_id for r in s.query(TripTraveler).filter_by(trip_id=trip.id)}
            assert on == {s.get(User, me.id).traveler_id} and boss_t not in on
            tid = trip.id
        assert client.get(f"/api/v1/trips/{tid}", headers=_h(boss)).status_code == 404
