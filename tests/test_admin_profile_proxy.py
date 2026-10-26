"""Yonder 4.1 — an admin manages profiles on someone's behalf (app/acting.py).

Allowed: travellers with no account, and users who share with admins. Never a
user who opted out; a non-admin can't act for anyone. Edits land on the chosen
traveller, and are audited when that traveller is a user. SQLite via conftest.
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
    from app.models import (AccountEvent, Base, DiscoveryProfile, HomeProfile,
                            PackingProfile, TravelerIdentity, User)

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    _NAMES = ("PX Admin", "PX Kid", "PX Sharer", "PX Private")

    def _setup():
        with SessionLocal() as s:
            _teardown(s)
            t = {n: TravelerIdentity(display_name=n, kind="guest") for n in _NAMES}
            s.add_all(t.values()); s.flush()
            admin = User(username="px-admin", password_hash=authn.hash_password("pw"),
                         auth_source="local", is_admin=True, traveler_id=t["PX Admin"].id)
            sharer = User(username="px-sharer", password_hash=authn.hash_password("pw"),
                          auth_source="local", traveler_id=t["PX Sharer"].id,
                          share_with_admins=True)
            private = User(username="px-private", password_hash=authn.hash_password("pw"),
                           auth_source="local", traveler_id=t["PX Private"].id,
                           share_with_admins=False, email="p@example.com")
            s.add_all([admin, sharer, private]); s.commit()
            return {n: x.id for n, x in t.items()} | {"sharer_uid": sharer.id}

    def _teardown(s):
        ids = [t.id for t in s.query(TravelerIdentity)
               .filter(TravelerIdentity.display_name.in_(_NAMES)).all()]
        for M in (HomeProfile, PackingProfile, DiscoveryProfile):
            s.query(M).filter(M.traveler_id.in_(ids)).delete(synchronize_session=False)
        uids = [u.id for u in s.query(User).filter(User.username.like("px-%")).all()]
        s.query(AccountEvent).filter(AccountEvent.user_id.in_(uids)).delete(synchronize_session=False)
        s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
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

    def test_picker_offers_only_travellers_without_an_account():
        """D-211: profiles of people with an account are theirs to set up, whether
        or not they let admins look."""
        def body(ids):
            page = client.get("/trips/settings/travel-profile", headers=_h("px-admin")).text
            assert "Profile of" in page
            assert "PX Kid" in page
            assert "PX Sharer" not in page and "PX Private" not in page
            # a regular user gets no picker at all
            assert "Profile of" not in client.get("/trips/settings/travel-profile",
                                                  headers=_h("px-sharer")).text
        _run(body)

    def test_admin_edits_a_travel_profile_for_someone_without_an_account():
        def body(ids):
            h = _h("px-admin")
            client.post(f"/trips/settings/travel-profile?for={ids['PX Kid']}", headers=h,
                        data={"nationality": "gb", "base_currency": "GBP"})
            with SessionLocal() as s:
                assert s.get(TravelerIdentity, ids["PX Kid"]).nationality == "GB"
                assert s.get(TravelerIdentity, ids["PX Admin"]).nationality is None   # not his
        _run(body)

    def test_admin_cannot_edit_a_sharing_users_homes():
        """D-211: support access never includes profiles; a `for` naming someone
        with an account is ignored, so the edit lands on the admin's own."""
        def body(ids):
            client.post(f"/review/home?for={ids['PX Sharer']}", headers=_h("px-admin"),
                        data={"kind": "home_airport", "label": "px-sharer-sfo", "iata": "SFO",
                              "lat": "37.6", "lon": "-122.4", "radius_km": "50"})
            with SessionLocal() as s:
                rows = s.query(HomeProfile).filter_by(label="px-sharer-sfo").all()
                assert all(r.traveler_id != ids["PX Sharer"] for r in rows)
                assert s.query(AccountEvent).filter_by(user_id=ids["sharer_uid"]).count() == 0
            assert "px-sharer-sfo" not in client.get("/review/home", headers=_h("px-sharer")).text
        _run(body)

    def test_admin_cannot_act_for_an_opted_out_user():
        def body(ids):
            h = _h("px-admin")
            # the ?for= is ignored: it's his own profile that's shown and saved
            page = client.get(f"/trips/settings/discovery-profile?for={ids['PX Private']}",
                              headers=h).text
            assert "PX Private" not in page and "as an admin" not in page
            client.post(f"/trips/settings/discovery-profile?for={ids['PX Private']}", headers=h,
                        data={"allergies": "px-not-hers", "budget_ceiling": "0",
                              "mobility": "no_limits"})
            with SessionLocal() as s:
                assert s.query(DiscoveryProfile).filter_by(traveler_id=ids["PX Private"]).count() == 0
                assert (s.query(DiscoveryProfile).filter_by(traveler_id=ids["PX Admin"]).one()
                        .values["food"]["allergies"] == "px-not-hers")
        _run(body)

    def test_non_admin_cannot_act_for_anyone():
        def body(ids):
            client.post(f"/trips/settings/packing-profile?for={ids['PX Kid']}",
                        headers=_h("px-sharer"), data={"bag_mode": "carry_on"})
            with SessionLocal() as s:
                assert s.query(PackingProfile).filter_by(traveler_id=ids["PX Kid"]).count() == 0
                assert s.query(PackingProfile).filter_by(traveler_id=ids["PX Sharer"]).count() == 1
        _run(body)

    def test_admin_sets_packing_and_discovery_for_someone():
        def body(ids):
            h = _h("px-admin")
            client.post(f"/trips/settings/packing-profile?for={ids['PX Kid']}", headers=h,
                        data={"bag_mode": "checked"})
            client.post(f"/trips/settings/discovery-profile?for={ids['PX Kid']}", headers=h,
                        data={"allergies": "px-kid-nuts", "budget_ceiling": "0",
                              "mobility": "no_limits"})
            with SessionLocal() as s:
                assert s.query(PackingProfile).filter_by(traveler_id=ids["PX Kid"]).count() == 1
                assert (s.query(DiscoveryProfile).filter_by(traveler_id=ids["PX Kid"]).one()
                        .values["food"]["allergies"] == "px-kid-nuts")
            page = client.get(f"/trips/settings/discovery-profile?for={ids['PX Kid']}",
                              headers=h).text
            assert "px-kid-nuts" in page and "PX Kid" in page
        _run(body)
