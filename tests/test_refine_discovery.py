"""YONDER-264: Refine Discovery. The places Discovery researches are listed and can be
excluded or included again over /api/v1 (and MCP), not just on the web, and the web
button lives in the Discovery tab instead of the trip header."""
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

    from app import authn, mcp_server
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, DiscoveryExclusion, Trip, TripActivity, TripMember, User

    Base.metadata.create_all(engine)
    client = TestClient(app)

    def _user(name):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                u = User(username=name, auth_source="local")
                s.add(u)
                s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _bearer(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    @pytest.fixture()
    def trip():
        owner, viewer, stranger = _user("rd-owner"), _user("rd-viewer"), _user("rd-stranger")
        with SessionLocal() as s:
            t = Trip(name="RD trip", source="manual", owner_id=owner.id,
                     start_date="2031-05-01", end_date="2031-05-06")
            s.add(t)
            s.flush()
            s.add_all([
                TripActivity(trip_id=t.id, type="flight", starts_at="2031-05-01T07:00",
                             values={"when": "2031-05-01T07:00", "from": "LHR", "to": "LIS"}),
                TripActivity(trip_id=t.id, type="flight", starts_at="2031-05-03T09:00",
                             values={"when": "2031-05-03T09:00", "from": "LIS", "to": "CDG"}),
                TripMember(trip_id=t.id, user_id=viewer.id)])
            s.commit()
            tid = t.id
        yield tid, owner, viewer, stranger
        with SessionLocal() as s:
            s.query(DiscoveryExclusion).filter_by(trip_id=tid).delete()
            s.query(TripActivity).filter_by(trip_id=tid).delete()
            s.query(TripMember).filter_by(trip_id=tid).delete()
            s.query(Trip).filter_by(id=tid).delete()
            s.commit()

    def _keys(body):
        return {p["key"]: p["excluded"] for p in body["places"]}

    def test_list_exclude_and_include_again_over_the_api(trip):
        tid, owner, _, _ = trip
        h = _bearer(owner)
        r = client.get(f"/api/v1/trips/{tid}/discover/places", headers=h)
        assert r.status_code == 200
        places = _keys(r.json())
        lis = next(k for k in places if k.upper().startswith("PT"))
        assert places[lis] is False and len(places) >= 2
        assert all(p["sources"] for p in r.json()["places"])          # where each came from
        url = f"/api/v1/trips/{tid}/discover/places/{lis}"
        assert _keys(client.put(url, json={"excluded": True}, headers=h).json())[lis] is True
        assert _keys(client.put(url, json={"excluded": True}, headers=h).json())[lis] is True  # idempotent
        with SessionLocal() as s:
            assert s.query(DiscoveryExclusion).filter_by(trip_id=tid, place_key=lis).count() == 1
        assert _keys(client.put(url, json={"excluded": False}, headers=h).json())[lis] is False
        assert client.put(f"/api/v1/trips/{tid}/discover/places/XX:Nowhere",
                          json={"excluded": True}, headers=h).status_code == 404

    def test_only_editors_refine_and_strangers_see_nothing(trip):
        tid, _, viewer, stranger = trip
        assert client.get(f"/api/v1/trips/{tid}/discover/places",
                          headers=_bearer(stranger)).status_code == 404
        assert client.get(f"/api/v1/trips/{tid}/discover/places",
                          headers=_bearer(viewer)).status_code == 403      # view-only share

    def test_exclusions_are_per_person(trip):
        tid, owner, _, _ = trip
        mate = _user("rd-mate")
        with SessionLocal() as s:
            # Someone else's exclusion on the same trip (stored directly).
            s.add(DiscoveryExclusion(trip_id=tid, user_id=mate.id, place_key="PT:Lisbon"))
            s.commit()
        body = client.get(f"/api/v1/trips/{tid}/discover/places", headers=_bearer(owner)).json()
        assert not any(p["excluded"] for p in body["places"])          # mate's choice isn't mine

    def test_mcp_tools(trip):
        tid, owner, _, _ = trip
        token = mcp_server._caller_id.set(owner.id)
        try:
            listed = mcp_server.list_discovery_places(tid)
            key = listed["places"][0]["key"]
            out = mcp_server.set_discovery_place(tid, place_key=key, excluded=True)
            assert next(p for p in out["places"] if p["key"] == key)["excluded"] is True
        finally:
            mcp_server._caller_id.reset(token)

    def test_web_button_moved_into_the_discovery_tab(trip):
        tid, _, _, _ = trip
        html = client.get(f"/trips/{tid}").text
        assert "What Run·dere found" not in html
        disc = html[html.index('id="discovery-section"'):]
        assert "Refine Discovery" in disc[:3000]
        sheet = client.get(f"/trips/{tid}/derived-locations").text
        assert "Refine Discovery · RD trip" in sheet
