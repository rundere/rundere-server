"""Yonder 4.1 — who each timeline activity is for (migration 0025).

No tags = everyone on the trip; tags only ever name the trip's own travellers;
the picker appears only on a multi-traveller trip. Also covers the web
activity routes' access checks. SQLite via conftest.
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
    from app.models import (Base, TravelerIdentity, Trip, TripActivity,
                            TripActivityTraveler, TripTraveler, User)
    from app.trips import who

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    def _user(name, admin=False):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                u = User(username=name, password_hash=authn.hash_password("pw"),
                         auth_source="local", is_admin=admin)
                s.add(u)
                s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _bearer(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _trip(name, travellers, owner_id=None):
        """A trip with the named travellers on its roster -> (trip_id, {name: id})."""
        with SessionLocal() as s:
            t = Trip(name=name, source="manual", owner_id=owner_id)
            s.add(t)
            s.flush()
            ids = {}
            for n in travellers:
                ident = s.query(TravelerIdentity).filter_by(display_name=n).one_or_none()
                if ident is None:
                    ident = TravelerIdentity(display_name=n, kind="guest")
                    s.add(ident)
                    s.flush()
                s.add(TripTraveler(trip_id=t.id, traveler_id=ident.id, role="companion"))
                ids[n] = ident.id
            s.commit()
            return t.id, ids

    def _cleanup(*trip_names, travellers=()):
        with SessionLocal() as s:
            for t in s.query(Trip).filter(Trip.name.in_(trip_names)).all():
                acts = [a.id for a in s.query(TripActivity).filter_by(trip_id=t.id).all()]
                who.clear_activities(s, acts)
                s.query(TripActivity).filter_by(trip_id=t.id).delete()
                s.query(TripTraveler).filter_by(trip_id=t.id).delete()
                s.delete(t)
            s.query(TravelerIdentity).filter(
                TravelerIdentity.display_name.in_(travellers)).delete(synchronize_session=False)
            s.commit()

    def _tags(activity_id):
        with SessionLocal() as s:
            return who.activity_map(s, [activity_id]).get(activity_id, [])

    def test_normalize_rules():
        roster = [1, 2, 3]
        assert who.normalize([2], roster) == [2]
        assert who.normalize(["1", "3"], roster) == [1, 3]
        assert who.normalize([1, 2, 3], roster) == []        # whole roster = everyone
        assert who.normalize([], roster) == []               # nothing = everyone
        assert who.normalize([9], roster) == []              # not on the trip
        assert who.normalize(["x", 2], roster) == [2]

    def test_picker_only_on_multi_traveller_trip():
        solo, _ = _trip("at-solo", ["AT Solo"])
        duo, ids = _trip("at-duo", ["AT Kenji", "AT Kenta"])
        try:
            assert "Who is this for" not in client.get(f"/trips/{solo}/activity/new").text
            html = client.get(f"/trips/{duo}/activity/new").text
            assert "Who is this for" in html and "AT Kenta" in html
        finally:
            _cleanup("at-solo", "at-duo", travellers=("AT Solo", "AT Kenji", "AT Kenta"))

    def test_web_tag_show_edit_and_cleanup():
        tid, ids = _trip("at-web", ["AT Web A", "AT Web B"])
        a, b = ids["AT Web A"], ids["AT Web B"]
        try:
            # B's flight only
            client.post(f"/trips/{tid}/activity", data={
                "activity_kind": "flight", "when": "2027-11-19T07:20",
                "travelers_shown": "1", "traveler_ids": [str(b)]})
            with SessionLocal() as s:
                act = s.query(TripActivity).filter_by(trip_id=tid).one().id
            assert _tags(act) == [b]
            page = client.get(f"/trips/{tid}").text
            assert 'class="tag tag-neutral bp-who-tag">AT Web B<' in page
            assert 'bp-who-tag">AT Web A<' not in page
            # edit modal pre-ticks only B
            modal = client.get(f"/trips/{tid}/activity/{act}/edit").text
            assert f'value="{b}"\n                 checked' in modal
            assert f'value="{a}"\n                 checked' not in modal
            # ticking both = everyone -> stored as no rows, no chips shown
            client.post(f"/trips/{tid}/activity/{act}", data={
                "activity_kind": "flight", "when": "2027-11-19T07:20",
                "travelers_shown": "1", "traveler_ids": [str(a), str(b)]})
            assert _tags(act) == []
            assert "bp-who-tag" not in client.get(f"/trips/{tid}").text
            # removing a traveller drops their tags
            client.post(f"/trips/{tid}/activity/{act}", data={
                "activity_kind": "flight", "when": "2027-11-19T07:20",
                "travelers_shown": "1", "traveler_ids": [str(a)]})
            assert _tags(act) == [a]
            client.post(f"/trips/{tid}/travelers/{a}/remove")
            assert _tags(act) == []
            # deleting an activity deletes its tags
            with SessionLocal() as s:
                s.add(TripTraveler(trip_id=tid, traveler_id=a, role="companion"))
                s.commit()
            client.post(f"/trips/{tid}/activity/{act}", data={
                "activity_kind": "flight", "when": "2027-11-19T07:20",
                "travelers_shown": "1", "traveler_ids": [str(b)]})
            client.post(f"/trips/{tid}/activity/{act}/delete")
            with SessionLocal() as s:
                assert s.query(TripActivityTraveler).filter_by(activity_id=act).count() == 0
        finally:
            _cleanup("at-web", travellers=("AT Web A", "AT Web B"))

    def test_saving_without_picker_keeps_tags():
        # A form that never showed the picker must not wipe existing tags.
        tid, ids = _trip("at-keep", ["AT Keep A", "AT Keep B"])
        try:
            with SessionLocal() as s:
                act = TripActivity(trip_id=tid, type="flight", values={}, starts_at="")
                s.add(act)
                s.flush()
                who.set_travelers(s, act.id, tid, [ids["AT Keep A"]])
                s.commit()
                aid = act.id
            client.post(f"/trips/{tid}/activity/{aid}",
                        data={"activity_kind": "flight", "when": "2026-11-21T09:00"})
            assert _tags(aid) == [ids["AT Keep A"]]
        finally:
            _cleanup("at-keep", travellers=("AT Keep A", "AT Keep B"))

    def test_activity_routes_enforce_trip_access():
        owner, stranger = _user("at-owner"), _user("at-stranger")
        tid, _ = _trip("at-private", [], owner_id=owner.id)
        with SessionLocal() as s:
            act = TripActivity(trip_id=tid, type="flight",
                               values={"when": "2027-11-19T07:20"}, starts_at="2027-11-19T07:20")
            s.add(act)
            s.commit()
            aid = act.id
        settings.auth_required = True
        try:
            h = _bearer(stranger)
            assert client.get(f"/trips/{tid}/activity/{aid}/edit", headers=h).status_code == 404
            assert client.get(f"/trips/{tid}/activity/new", headers=h).status_code == 404
            assert client.post(f"/trips/{tid}/activity/{aid}", headers=h,
                               data={"activity_kind": "event", "when": ""}).status_code == 404
            client.post(f"/trips/{tid}/activity/{aid}/delete", headers=h)
            with SessionLocal() as s:
                a = s.get(TripActivity, aid)
                assert a is not None and a.type == "flight"      # untouched
            # the owner still can
            assert client.get(f"/trips/{tid}/activity/{aid}/edit",
                              headers=_bearer(owner)).status_code == 200
        finally:
            settings.auth_required = False
            _cleanup("at-private")

    def test_api_traveler_ids():
        u = _user("at-api", admin=True)
        tid, ids = _trip("at-api-trip", ["AT Api A", "AT Api B"])
        a, b = ids["AT Api A"], ids["AT Api B"]
        h = _bearer(u)
        try:
            r = client.post(f"/api/v1/trips/{tid}/activities", headers=h,
                            json={"type": "flight", "values": {"when": "2027-11-19T07:20"},
                                  "traveler_ids": [a]})
            assert r.status_code == 201 and r.json()["traveler_ids"] == [a]
            aid = r.json()["id"]
            # off-trip id is refused
            assert client.patch(f"/api/v1/trips/{tid}/activities/{aid}", headers=h,
                                json={"traveler_ids": [999999]}).status_code == 400
            # omitted = unchanged
            r = client.patch(f"/api/v1/trips/{tid}/activities/{aid}", headers=h,
                             json={"values": {"when": "2026-11-21T07:20"}})
            assert r.json()["traveler_ids"] == [a]
            snap = client.get(f"/api/v1/trips/{tid}", headers=h).json()
            assert [x["traveler_ids"] for x in snap["activities"] if x["id"] == aid] == [[a]]
            # [] = everyone
            r = client.patch(f"/api/v1/trips/{tid}/activities/{aid}", headers=h,
                             json={"traveler_ids": []})
            assert r.json()["traveler_ids"] == []
            # whole roster also = everyone
            r = client.patch(f"/api/v1/trips/{tid}/activities/{aid}", headers=h,
                             json={"traveler_ids": [a, b]})
            assert r.json()["traveler_ids"] == []
        finally:
            _cleanup("at-api-trip", travellers=("AT Api A", "AT Api B"))
