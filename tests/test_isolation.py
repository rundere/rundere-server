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
            # YONDER-186: ownerless is closed — admins only, never everyone
            assert not scope.can_access_trip(s, stranger, opn)
            assert not scope.can_edit_trip(s, stranger, opn)
            assert scope.can_access_trip(s, admin, opn)
            assert scope.can_edit_trip(s, admin, opn)
            assert not scope.can_access_trip(s, stranger, priv)   # not shared
            assert scope.can_access_trip(s, stranger, shared)     # shared
            assert not scope.can_access_trip(s, admin, priv)      # D-220: private by default
            assert scope.can_access_trip(s, None, priv)           # auth off

            own_ids = scope.visible_trip_ids(s, owner)
            assert priv.id in own_ids and opn.id not in own_ids
            assert opn.id in scope.visible_trip_ids(s, admin)
            # …and never offered as a filing target to a non-admin
            filing = {t.id for t in s.query(Trip).filter(scope.filing_trip_filter(s, stranger))}
            assert opn.id not in filing
            assert opn.id in {t.id for t in s.query(Trip).filter(scope.filing_trip_filter(s, admin))}
            # D-220: a new owner is private, so not even an admin sees the trip…
            assert priv.id not in scope.visible_trip_ids(s, admin)
            # …until the owner opts in to sharing with admins (4.1)
            s.get(User, owner.id).share_with_admins = True
            s.commit()
            assert scope.can_access_trip(s, admin, priv)
            assert priv.id in scope.visible_trip_ids(s, admin)
            assert priv.id not in scope.visible_trip_ids(s, stranger)
            assert shared.id in scope.visible_trip_ids(s, stranger)

    def test_capture_access_matrix():
        owner, stranger, admin = _user("iso-co"), _user("iso-cs"), _user("iso-ca", admin=True)
        with SessionLocal() as s:
            mine = TravelItem(owner_id=owner.id, source={}, status={})
            opn = TravelItem(owner_id=None, source={}, status={})
            theirs = TravelItem(owner_id=stranger.id, source={}, status={})
            s.add_all([mine, opn, theirs])
            s.commit()
            ids = {i.id for i in s.query(TravelItem).filter(scope.capture_filter(owner)).all()}
            assert mine.id in ids and opn.id not in ids and theirs.id not in ids
            assert not scope.can_access_capture(owner, opn)       # YONDER-186
            admin_ids = {i.id for i in s.query(TravelItem).filter(scope.capture_filter(admin))}
            assert opn.id in admin_ids
            assert scope.can_access_capture(admin, opn)
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

    def test_traveller_who_is_user_gets_full_access():
        from app.models import TravelerIdentity, TripTraveler
        owner = _user("iso-to")
        with SessionLocal() as s:
            trav = TravelerIdentity(display_name="iso-trav-person", kind="companion")
            s.add(trav)
            s.commit()
            s.refresh(trav)
            u = User(username="iso-traveller", password_hash=authn.hash_password("pw"),
                     auth_source="local", traveler_id=trav.id)
            s.add(u)
            t = Trip(name="iso-travtrip", source="manual", owner_id=owner.id)
            s.add(t)
            s.commit()
            s.refresh(u)
            s.expunge(u)
            assert not scope.can_view_trip(s, u, t)              # not on the roster yet
            s.add(TripTraveler(trip_id=t.id, traveler_id=trav.id))
            s.commit()
            assert scope.can_view_trip(s, u, t)                  # now a traveller
            assert scope.can_edit_trip(s, u, t)                  # travellers get FULL access
            assert t.id in scope.visible_trip_ids(s, u)
            s.query(TripTraveler).filter_by(trip_id=t.id).delete()
            s.query(TravelerIdentity).filter_by(id=trav.id).delete()
            s.commit()

    def test_explicit_share_is_view_only():
        owner, viewer = _user("iso-eo"), _user("iso-ev")         # viewer has no traveler_id
        with SessionLocal() as s:
            t = Trip(name="iso-viewtrip", source="manual", owner_id=owner.id)
            s.add(t)
            s.commit()
            tid = t.id
            s.add(TripMember(trip_id=tid, user_id=viewer.id))
            s.commit()
            assert scope.can_view_trip(s, viewer, t)
            assert not scope.can_edit_trip(s, viewer, t)         # explicit share = view-only
        settings.auth_required = True
        try:
            # can VIEW the trip
            assert client.get(f"/trips/{tid}", headers=_bearer(viewer),
                              follow_redirects=False).status_code == 200
            # but a WRITE route is 403 (method-gated edit)
            r = client.post(f"/trips/{tid}", headers=_bearer(viewer),
                            data={"name": "hacked", "start": "", "end": "", "notes": "",
                                  "adventurelog_collection_id": "", "next": ""},
                            follow_redirects=False)
            assert r.status_code == 403
        finally:
            settings.auth_required = False

    def test_packing_is_per_user():
        """D-123: each user sees only their own packing lines on a shared trip."""
        from app.models import PackingItem
        from app.trips.routes import _packing_context
        ua, ub = _user("pk-a"), _user("pk-b")
        with SessionLocal() as s:
            t = Trip(name="pk-trip", source="manual", owner_id=ua.id)
            s.add(t)
            s.commit()
            tid = t.id
            for u, lbl in ((ua, "A-socks"), (ub, "B-socks")):
                s.add(PackingItem(trip_id=tid, user_id=u.id, item_key="clothing:socks",
                                  label=lbl, category="clothing", status="draft"))
            s.commit()
            rows_a = [r for c in _packing_context(s, tid, ua.id)["packing_cats"] for r in c["rows"]]
            rows_b = [r for c in _packing_context(s, tid, ub.id)["packing_cats"] for r in c["rows"]]
            assert [r["label"] for r in rows_a] == ["A-socks"]     # only A's line
            assert [r["label"] for r in rows_b] == ["B-socks"]     # only B's line

    def test_packing_profile_per_traveller():
        """D-123: generation loads the viewing user's own profile via their traveller."""
        from planning.runner import load_profile
        from app.models import PackingProfile, TravelerIdentity
        with SessionLocal() as s:
            tr = TravelerIdentity(display_name="pk-trav", kind="companion")
            s.add(tr)
            s.commit()
            s.refresh(tr)
            s.add(PackingProfile(traveler_id=tr.id, values={"packing_style": "ultralight"}))
            s.commit()
            assert load_profile(s, tr.id)["packing_style"] == "ultralight"
            s.query(PackingProfile).filter_by(traveler_id=tr.id).delete()
            s.query(TravelerIdentity).filter_by(id=tr.id).delete()
            s.commit()

    def test_travel_profile_self_edit():
        """D-124: a user edits their OWN traveller's personal attributes; a user
        with no linked traveller sees the 'ask an admin' state."""
        from app.models import TravelerIdentity
        with SessionLocal() as s:
            tr = TravelerIdentity(display_name="tp-person", kind="companion")
            s.add(tr)
            s.commit()
            s.refresh(tr)
            tid = tr.id
            u = User(username="tp-user", password_hash=authn.hash_password("pw"),
                     auth_source="local", traveler_id=tid)
            s.add(u)
            s.commit()
            s.refresh(u)
            s.expunge(u)
        settings.auth_required = True
        try:
            r = client.post("/trips/settings/travel-profile", headers=_bearer(u),
                            data={"nationality": "ca", "aliases": "NAKAMURA/KENJI\nK NAKAMURA",
                                  "airtrail_user_id": "", "notes": "vip"},
                            follow_redirects=False)
            assert r.status_code == 200
            with SessionLocal() as s:
                t = s.get(TravelerIdentity, tid)
                assert t.nationality == "CA" and "K NAKAMURA" in (t.aliases or []) and t.notes == "vip"
            # a user with no linked traveller gets the guidance state, not an editor
            u2 = _user("tp-nolink")            # no traveler_id
            r2 = client.get("/trips/settings/travel-profile", headers=_bearer(u2),
                            follow_redirects=False)
            assert r2.status_code == 200 and "isn't linked to a traveller" in r2.text
        finally:
            settings.auth_required = False
            with SessionLocal() as s:
                s.query(TravelerIdentity).filter_by(id=tid).delete()
                s.commit()

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
