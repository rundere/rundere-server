"""D-199 (YONDER-185, absorbing YONDER-182): the four tiers on a trip. `my_role` says
which one the caller is (owner | traveller | admin | viewer), every roster person
says whether they have a login, and a viewer (an explicit share for a
non-traveller, view-only since D-122) gets the trip without its edit controls,
packing or travellers' profile details, on the web, the API and MCP."""
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

    from app import authn, mcp_server, scope
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, TravelerIdentity, Trip, TripActivity, TripMember,
                            TripTraveler, User)

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    def _user(name, admin=False, traveler_id=None, share=True):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                u = User(username=name, auth_source="local", is_admin=admin)
                s.add(u)
            u.traveler_id, u.share_with_admins = traveler_id, share
            s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _bearer(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    @pytest.fixture()
    def world():
        """A trip owned by `owner`, travelled on by `mate` (who has a login) and a
        guest (who doesn't), shared to view with `viewer`; `stranger` has nothing."""
        with SessionLocal() as s:
            me = TravelerIdentity(display_name="Tier Owner", kind="self", nationality="CA")
            mate = TravelerIdentity(display_name="Tier Mate", kind="guest", nationality="GB")
            guest = TravelerIdentity(display_name="Tier Guest", kind="guest", nationality="FR")
            s.add_all([me, mate, guest])
            s.commit()
            ids = {"me": me.id, "mate": mate.id, "guest": guest.id}
        users = {"owner": _user("tier-owner", traveler_id=ids["me"]),
                 "mate": _user("tier-mate", traveler_id=ids["mate"]),
                 "viewer": _user("tier-viewer"),
                 "stranger": _user("tier-stranger"),
                 "admin": _user("tier-admin", admin=True)}
        with SessionLocal() as s:
            t = Trip(name="Tier trip", source="manual", owner_id=users["owner"].id,
                     start_date="2031-05-01", end_date="2031-05-05")
            s.add(t)
            s.flush()
            s.add_all([TripTraveler(trip_id=t.id, traveler_id=ids["me"], role="self"),
                       TripTraveler(trip_id=t.id, traveler_id=ids["mate"], role="companion"),
                       TripTraveler(trip_id=t.id, traveler_id=ids["guest"], role="companion"),
                       TripMember(trip_id=t.id, user_id=users["viewer"].id),
                       TripActivity(trip_id=t.id, type="dining", starts_at="2031-05-02T19:00",
                                    values={"when": "2031-05-02T19:00", "name": "Tier dinner"})])
            s.commit()
            tid = t.id
        yield tid, users, ids
        with SessionLocal() as s:
            s.query(TripActivity).filter_by(trip_id=tid).delete()
            s.query(TripTraveler).filter_by(trip_id=tid).delete()
            s.query(TripMember).filter_by(trip_id=tid).delete()
            s.query(Trip).filter_by(id=tid).delete()
            s.query(User).filter(User.traveler_id.in_(ids.values())).update(
                {User.traveler_id: None}, synchronize_session=False)
            s.query(TravelerIdentity).filter(TravelerIdentity.id.in_(ids.values())).delete(
                synchronize_session=False)
            s.commit()

    # ------------------------------------------------------------------ scope
    def test_trip_role_matrix_matches_edit_access(world):
        tid, u, _ = world
        with SessionLocal() as s:
            trip = s.get(Trip, tid)
            roles = {k: scope.trip_role(s, v, trip) for k, v in u.items()}
            assert roles == {"owner": "owner", "mate": "traveller", "viewer": "viewer",
                             "stranger": None, "admin": "admin"}
            assert scope.trip_role(s, None, trip) == "owner"          # auth off
            assert scope.trip_role(s, u["owner"], None) is None
            # The invariant the API leans on: an editor is exactly a non-viewer.
            for v in u.values():
                role = scope.trip_role(s, v, trip)
                if role is not None:
                    assert scope.can_edit_trip(s, v, trip) == (role != "viewer")

    def test_an_admin_who_isnt_shared_with_is_just_a_viewer_or_nothing(world):
        tid, u, _ = world
        _user("tier-owner", traveler_id=u["owner"].traveler_id, share=False)
        try:
            with SessionLocal() as s:
                assert scope.trip_role(s, u["admin"], s.get(Trip, tid)) is None
        finally:
            _user("tier-owner", traveler_id=u["owner"].traveler_id, share=True)

    # ------------------------------------------------------------------ API
    def test_api_reports_my_role_and_who_has_a_login(world):
        tid, u, ids = world
        snaps = {k: client.get(f"/api/v1/trips/{tid}", headers=_bearer(u[k]))
                 for k in ("owner", "mate", "viewer", "stranger")}
        assert snaps["stranger"].status_code == 404                  # scoping: nothing leaks
        for k, role, edit in (("owner", "owner", True), ("mate", "traveller", True),
                              ("viewer", "viewer", False)):
            trip = snaps[k].json()["trip"]
            assert (trip["my_role"], trip["can_edit"]) == (role, edit)
        logins = {t["id"]: t["has_account"] for t in snaps["viewer"].json()["travelers"]}
        assert logins == {ids["me"]: True, ids["mate"]: True, ids["guest"]: False}
        listed = {t["id"]: t["my_role"] for t in
                  client.get("/api/v1/trips", headers=_bearer(u["viewer"])).json()}
        assert listed[tid] == "viewer"

    def test_api_refuses_a_viewers_writes(world):
        tid, u, _ = world
        h = _bearer(u["viewer"])
        assert client.patch(f"/api/v1/trips/{tid}", json={"name": "x"}, headers=h).status_code == 403
        assert client.post(f"/api/v1/trips/{tid}/activities",
                           json={"type": "note", "values": {"title": "x"}},
                           headers=h).status_code == 403
        # D-199: starting a Discovery run needs edit access, as on the web.
        assert client.post(f"/api/v1/trips/{tid}/discover", headers=h).status_code == 403
        settings.auth_required = True
        try:
            assert client.post(f"/trips/{tid}/discover", headers=h).status_code == 403
        finally:
            settings.auth_required = False
        assert client.post(f"/api/v1/trips/{tid}/discover",
                           headers=_bearer(u["stranger"])).status_code == 404

    # ------------------------------------------------------------------ web
    def test_web_trip_page_for_a_viewer_has_no_edit_controls(world):
        tid, u, _ = world
        settings.auth_required = True
        try:
            viewer = client.get(f"/trips/{tid}", headers=_bearer(u["viewer"]))
            owner = client.get(f"/trips/{tid}", headers=_bearer(u["owner"]))
            stranger = client.get(f"/trips/{tid}", headers=_bearer(u["stranger"]))
        finally:
            settings.auth_required = False
        assert viewer.status_code == 200 and owner.status_code == 200
        assert stranger.status_code == 404
        v, o = viewer.text, owner.text
        assert "View only" in v and "View only" not in o
        for control in ("+ Add activity", "Edit trip", 'data-tab="packing"', "Run discovery",
                        "Add guest", "/delete"):
            assert control in o, control
            assert control not in v, control
        assert "Tier dinner" in v                                   # the timeline is there
        # Who can sign in, in words; nationality stays with the editors.
        assert "Has login" in v and "Guest · no login" in v
        assert 'title="Nationality">GB' in o and 'title="Nationality">GB' not in v

    def test_web_sharing_tab_names_can_edit_and_can_view(world):
        tid, u, _ = world
        settings.auth_required = True
        try:
            page = client.get(f"/trips/{tid}", headers=_bearer(u["owner"])).text
        finally:
            settings.auth_required = False
        assert "Can edit (travellers with a login)" in page
        assert "tier-mate · can edit" in page and "tier-viewer · can view" in page

    # ------------------------------------------------------------------ MCP
    def test_mcp_get_trip_says_viewer_and_write_tools_refuse(world):
        tid, u, _ = world
        token = mcp_server._caller_id.set(u["viewer"].id)
        try:
            snap = mcp_server.get_trip(tid)
            assert snap["trip"]["my_role"] == "viewer"
            with pytest.raises(mcp_server.ToolError, match="403"):
                mcp_server.update_trip(tid, name="Hijacked")
            with pytest.raises(mcp_server.ToolError, match="403"):
                mcp_server.add_activity(tid, type="note", values={"title": "x"})
        finally:
            mcp_server._caller_id.reset(token)
        with SessionLocal() as s:
            assert s.get(Trip, tid).name == "Tier trip"

