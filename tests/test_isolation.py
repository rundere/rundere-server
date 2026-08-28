"""Yonder 4.0 Phase 2 — per-user isolation & trip sharing (SPEC-4 D-119–D-121).

Scope primitives (pure) + route-level enforcement (auth on). SQLite via conftest.
Assertions use membership/exclusion, never exact sets — the shared test DB holds
many null-owner (open) trips/captures from other files.
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
    from app import authn, scope
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, TravelItem, Trip, TripMember, User

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

    def _bearer(user):
        return {"Authorization": f"Bearer {authn.issue_access_token(user)}"}

    # ------------------------------------------------------------------ primitives
    def test_trip_access_matrix():
        owner, stranger, admin = _user("iso-o"), _user("iso-s"), _user("iso-a", admin=True)
        with SessionLocal() as s:
            priv = Trip(name="iso-priv", source="manual", owner_id=owner.id)
            opn = Trip(name="iso-open", source="manual", owner_id=None)
            shared = Trip(name="iso-shared", source="manual", owner_id=owner.id)
            s.add_all([priv, opn, shared])
            s.commit()
            s.add(TripMember(trip_id=shared.id, user_id=stranger.id))
            s.commit()

            assert scope.can_access_trip(s, owner, priv)          # own
            assert scope.can_access_trip(s, stranger, opn)        # open
            assert not scope.can_access_trip(s, stranger, priv)   # not shared
            assert scope.can_access_trip(s, stranger, shared)     # shared
            assert scope.can_access_trip(s, admin, priv)          # admin
            assert scope.can_access_trip(s, None, priv)           # auth off

            own_ids = scope.visible_trip_ids(s, owner)
            assert priv.id in own_ids and opn.id in own_ids
            assert scope.visible_trip_ids(s, admin) is None       # "all"
            assert priv.id not in scope.visible_trip_ids(s, stranger)
            assert shared.id in scope.visible_trip_ids(s, stranger)

    def test_capture_access_matrix():
        owner, stranger = _user("iso-co"), _user("iso-cs")
        with SessionLocal() as s:
            mine = TravelItem(owner_id=owner.id, source={}, status={})
            opn = TravelItem(owner_id=None, source={}, status={})
            theirs = TravelItem(owner_id=stranger.id, source={}, status={})
            s.add_all([mine, opn, theirs])
            s.commit()
            ids = {i.id for i in s.query(TravelItem).filter(scope.capture_filter(owner)).all()}
            assert mine.id in ids and opn.id in ids and theirs.id not in ids
            assert scope.can_access_capture(None, theirs)         # auth off
            assert not scope.can_access_capture(owner, theirs)

    # ------------------------------------------------------------------ routes (auth on)
    def test_create_trip_stamps_owner_and_hides_others():
        owner, other = _user("rt-owner"), _user("rt-other")
        settings.auth_required = True
        try:
            # owner creates a trip via the real POST — middleware sets the user
            r = client.post("/trips", headers=_bearer(owner),
                            data={"name": "rt-mytrip", "start": "", "end": "",
                                  "notes": "", "adventurelog_collection_id": "",
                                  "next": "/trips"}, follow_redirects=False)
            assert r.status_code in (200, 303)
            with SessionLocal() as s:
                t = s.query(Trip).filter_by(name="rt-mytrip").one()
                assert t.owner_id == owner.id
                tid = t.id
            # a different user can't see it (404, not 403)
            assert client.get(f"/trips/{tid}", headers=_bearer(other),
                              follow_redirects=False).status_code == 404
            # owner shares it → now visible
            assert client.post(f"/trips/{tid}/share", headers=_bearer(owner),
                               data={"user_id": other.id},
                               follow_redirects=False).status_code in (200, 303)
            assert client.get(f"/trips/{tid}", headers=_bearer(other),
                              follow_redirects=False).status_code == 200
            # a non-owner cannot share
            third = _user("rt-third")
            assert client.post(f"/trips/{tid}/share", headers=_bearer(third),
                               data={"user_id": third.id},
                               follow_redirects=False).status_code in (403, 404)
        finally:
            settings.auth_required = False

    def test_capture_owner_hidden_from_others():
        u = _user("ing-user")
        with SessionLocal() as s:
            it = TravelItem(owner_id=u.id, source={}, status={})
            s.add(it)
            s.commit()
            # capture_filter excludes it for a different user
            other = _user("ing-other")
            got = {x.id for x in s.query(TravelItem).filter(scope.capture_filter(other)).all()}
            assert it.id not in got
