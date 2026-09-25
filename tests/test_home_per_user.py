"""Yonder 4.1 — home profiles are per person, not deployment-wide.

Each user sees and edits only the homes on their own traveller (admins too);
captures are judged against the capturing user's homes; legacy unassigned rows
belong to the deployment owner (`self`). SQLite via conftest.
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
    from app.models import Base, HomeProfile, TravelerIdentity, User
    from app.resolving import (OWNER_HOMES, load_home_profiles, load_homes_for_user)

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    _NAMES = ("HP Self", "HP Sister")

    def _setup():
        """self traveller (with a legacy unassigned home + an own home), a
        sister traveller with her own home, and a user for each + one unlinked."""
        with SessionLocal() as s:
            _teardown(s)
            me = TravelerIdentity(display_name="HP Self", kind="self")
            sis = TravelerIdentity(display_name="HP Sister", kind="guest")
            s.add_all([me, sis]); s.flush()
            s.add_all([
                HomeProfile(label="hp-legacy-yyz", kind="home_airport", iata="YYZ",
                            lat=43.68, lon=-79.63, radius_km=50),
                HomeProfile(label="hp-me-gta", kind="residence", lat=43.65, lon=-79.38,
                            radius_km=50, traveler_id=me.id),
                HomeProfile(label="hp-sis-sfo", kind="home_airport", iata="SFO",
                            lat=37.62, lon=-122.38, radius_km=50, traveler_id=sis.id),
            ])
            users = {}
            for name, tid, admin in (("hp-me", me.id, True), ("hp-sis", sis.id, False),
                                     ("hp-nobody", None, False)):
                u = User(username=name, password_hash=authn.hash_password("pw"),
                         auth_source="local", is_admin=admin, traveler_id=tid)
                s.add(u); s.flush()
                users[name] = u.id
            s.commit()
            return me.id, sis.id, users

    def _teardown(s):
        s.query(HomeProfile).filter(HomeProfile.label.like("hp-%")).delete(
            synchronize_session=False)
        s.query(User).filter(User.username.like("hp-%")).delete(synchronize_session=False)
        s.query(TravelerIdentity).filter(TravelerIdentity.display_name.in_(_NAMES)).delete(
            synchronize_session=False)
        s.commit()

    def _bearer(username):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=username).one()
            return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _labels(refs):
        return {r.label for r in refs if r.label.startswith("hp-")}

    def test_whose_homes_apply():
        me, sis, users = _setup()
        try:
            with SessionLocal() as s:
                # the owner gets their own + the legacy unassigned rows
                assert _labels(load_home_profiles(s, OWNER_HOMES)) == {"hp-legacy-yyz", "hp-me-gta"}
                assert _labels(load_home_profiles(s, me)) == {"hp-legacy-yyz", "hp-me-gta"}
                assert _labels(load_home_profiles(s, sis)) == {"hp-sis-sfo"}
                # a capture is judged by its owner's homes
                assert _labels(load_homes_for_user(s, users["hp-sis"])) == {"hp-sis-sfo"}
                assert _labels(load_homes_for_user(s, users["hp-me"])) == {"hp-legacy-yyz", "hp-me-gta"}
                assert _labels(load_homes_for_user(s, users["hp-nobody"])) == set()
                # an open capture keeps the pre-4.1 behaviour: the owner's homes
                assert _labels(load_homes_for_user(s, None)) == {"hp-legacy-yyz", "hp-me-gta"}
        finally:
            with SessionLocal() as s:
                _teardown(s)

    def test_editor_is_scoped_to_the_viewer():
        me, sis, users = _setup()
        settings.auth_required = True
        try:
            with SessionLocal() as s:
                mine = s.query(HomeProfile).filter_by(label="hp-me-gta").one().id
                hers = s.query(HomeProfile).filter_by(label="hp-sis-sfo").one().id
            # each sees only their own — the admin included
            page = client.get("/review/home", headers=_bearer("hp-sis")).text
            assert "hp-sis-sfo" in page and "hp-me-gta" not in page and "hp-legacy-yyz" not in page
            page = client.get("/review/home", headers=_bearer("hp-me")).text
            assert "hp-me-gta" in page and "hp-legacy-yyz" in page and "hp-sis-sfo" not in page
            # she can't open, overwrite or delete his by id
            assert "hp-me-gta" not in client.get(f"/review/home?edit={mine}",
                                                 headers=_bearer("hp-sis")).text
            client.post("/review/home", headers=_bearer("hp-sis"), data={
                "edit_id": str(mine), "kind": "home_airport", "label": "hp-sis-lax",
                "iata": "LAX", "lat": "33.94", "lon": "-118.41", "radius_km": "50"})
            client.post(f"/review/home/delete/{mine}", headers=_bearer("hp-sis"))
            with SessionLocal() as s:
                row = s.get(HomeProfile, mine)
                assert row is not None and row.label == "hp-me-gta"      # untouched
                new = s.query(HomeProfile).filter_by(label="hp-sis-lax").one()
                assert new.traveler_id == sis                           # a new row, hers
            # she can delete her own
            client.post(f"/review/home/delete/{hers}", headers=_bearer("hp-sis"))
            with SessionLocal() as s:
                assert s.get(HomeProfile, hers) is None
            # a user with no traveller gets the prompt, and can't save
            page = client.get("/review/home", headers=_bearer("hp-nobody")).text
            assert "linked to a traveller yet" in page
            assert not any(h in page for h in ("hp-me-gta", "hp-legacy-yyz", "hp-sis-lax"))
            client.post("/review/home", headers=_bearer("hp-nobody"), data={
                "kind": "home_airport", "label": "hp-orphan", "iata": "JFK",
                "lat": "40.6", "lon": "-73.8", "radius_km": "50"})
            with SessionLocal() as s:
                assert s.query(HomeProfile).filter_by(label="hp-orphan").count() == 0
        finally:
            settings.auth_required = False
            with SessionLocal() as s:
                _teardown(s)

    def test_editing_a_legacy_row_assigns_it_to_self():
        me, sis, users = _setup()
        settings.auth_required = True
        try:
            with SessionLocal() as s:
                legacy = s.query(HomeProfile).filter_by(label="hp-legacy-yyz").one().id
            client.post("/review/home", headers=_bearer("hp-me"), data={
                "edit_id": str(legacy), "kind": "home_airport", "label": "hp-legacy-yyz",
                "iata": "YYZ", "lat": "43.68", "lon": "-79.63", "radius_km": "50"})
            with SessionLocal() as s:
                assert s.get(HomeProfile, legacy).traveler_id == me
        finally:
            settings.auth_required = False
            with SessionLocal() as s:
                _teardown(s)
