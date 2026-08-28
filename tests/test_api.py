"""Yonder 4.0 Phase 3 — the JSON /api/v1 surface (SPEC-4 Workstream A).

Auth is required on every route (bearer JWT), independent of TX_AUTH_REQUIRED,
and every response is user-scoped via app/scope.py. SQLite via conftest.
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
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, IngestBatch, PackingItem, Trip, TripActivity,
                            TripMember, User)

    Base.metadata.create_all(engine)
    client = TestClient(app)

    def _user(name):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                u = User(username=name, password_hash=authn.hash_password("pw"),
                         auth_source="local")
                s.add(u)
                s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _bearer(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _cleanup_trips(*names):
        # These tests add trips/activities/packing/shares to the shared SQLite DB;
        # remove them so other files (e.g. test_committing) don't see stray rows.
        with SessionLocal() as s:
            for t in s.query(Trip).filter(Trip.name.in_(names)).all():
                s.query(TripActivity).filter(TripActivity.trip_id == t.id).delete()
                s.query(PackingItem).filter(PackingItem.trip_id == t.id).delete()
                s.query(TripMember).filter(TripMember.trip_id == t.id).delete()
                s.delete(t)
            s.commit()

    def test_api_token_is_accepted():
        # A long-lived machine token (MCP, D-126) authenticates like an access token.
        u = _user("api-machine")
        tok = authn.issue_api_token(u, ttl=3600)
        r = client.get("/api/v1/trips", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200

    def test_api_requires_auth():
        # No bearer → 401 everywhere (auth is always on for the API).
        assert client.get("/api/v1/dashboard").status_code == 401
        assert client.get("/api/v1/trips").status_code == 401
        assert client.get("/api/v1/review").status_code == 401
        assert client.post("/api/v1/capture", json={"html": "<p>x</p>"}).status_code == 401

    def test_api_trips_scoped():
        owner, stranger = _user("api-owner"), _user("api-stranger")
        with SessionLocal() as s:
            priv = Trip(name="api-priv", source="manual", owner_id=owner.id)
            opn = Trip(name="api-open", source="manual", owner_id=None)
            s.add_all([priv, opn])
            s.commit()
            pid, oid = priv.id, opn.id

        names_owner = {t["name"] for t in client.get("/api/v1/trips", headers=_bearer(owner)).json()}
        names_stranger = {t["name"] for t in client.get("/api/v1/trips", headers=_bearer(stranger)).json()}
        assert "api-priv" in names_owner and "api-open" in names_owner
        assert "api-priv" not in names_stranger and "api-open" in names_stranger

        # snapshot: 404 for a hidden trip, 200 for the owner
        assert client.get(f"/api/v1/trips/{pid}", headers=_bearer(stranger)).status_code == 404
        snap = client.get(f"/api/v1/trips/{pid}", headers=_bearer(owner))
        assert snap.status_code == 200
        body = snap.json()
        assert body["trip"]["id"] == pid
        for key in ("activities", "travelers", "places", "discovery", "captures"):
            assert key in body
        _cleanup_trips("api-priv", "api-open")

    def test_api_dashboard_and_review():
        owner = _user("api-dash")
        d = client.get("/api/v1/dashboard", headers=_bearer(owner))
        assert d.status_code == 200
        for key in ("counts", "capture", "trip_count", "upcoming_count"):
            assert key in d.json()
        r = client.get("/api/v1/review", headers=_bearer(owner))
        assert r.status_code == 200 and "counts" in r.json() and "items" in r.json()

    def test_api_capture_is_owned():
        u = _user("api-capper")
        r = client.post("/api/v1/capture", headers=_bearer(u),
                        json={"html": "<html><body>a hotel booking</body></html>",
                              "title": "Test"})
        assert r.status_code == 202, r.text
        bid = r.json()["batch_id"]
        with SessionLocal() as s:
            assert s.get(IngestBatch, bid).user_id == u.id      # owned by the caller

    def test_api_snapshot_includes_activities():
        owner = _user("api-snap")
        with SessionLocal() as s:
            t = Trip(name="api-snaptrip", source="manual", owner_id=owner.id)
            s.add(t)
            s.commit()
            s.add(TripActivity(trip_id=t.id, type="lodging", starts_at="2026-09-01",
                               values={"property": "Riad X"}))
            s.commit()
            tid = t.id
        snap = client.get(f"/api/v1/trips/{tid}", headers=_bearer(owner)).json()
        assert snap["trip"]["activity_count"] == 1
        assert snap["activities"][0]["type"] == "lodging"
        assert snap["activities"][0]["values"]["property"] == "Riad X"
        _cleanup_trips("api-snaptrip")

    def _owned_trip(name, owner):
        with SessionLocal() as s:
            t = Trip(name=name, source="manual", owner_id=owner.id)
            s.add(t)
            s.commit()
            return t.id

    def test_api_edit_trip_and_activities():
        owner = _user("api-editor")
        tid = _owned_trip("api-edit", owner)
        # add an activity (an event, "on the go")
        r = client.post(f"/api/v1/trips/{tid}/activities", headers=_bearer(owner),
                        json={"type": "event", "values": {"title": "Concert",
                              "when": "2026-09-10T20:00"}})
        assert r.status_code == 201, r.text
        aid = r.json()["id"]
        assert r.json()["type"] == "event" and r.json()["starts_at"] == "2026-09-10T20:00"
        # it appears in the snapshot
        snap = client.get(f"/api/v1/trips/{tid}", headers=_bearer(owner)).json()
        assert any(a["id"] == aid for a in snap["activities"])
        # edit the trip name
        r = client.patch(f"/api/v1/trips/{tid}", headers=_bearer(owner), json={"name": "Renamed"})
        assert r.status_code == 200 and r.json()["name"] == "Renamed"
        # edit the activity (starts_at follows values.when)
        r = client.patch(f"/api/v1/trips/{tid}/activities/{aid}", headers=_bearer(owner),
                         json={"values": {"title": "Concert 2", "when": "2026-09-11T20:00"}})
        assert r.status_code == 200 and r.json()["starts_at"] == "2026-09-11T20:00"
        # delete it
        assert client.delete(f"/api/v1/trips/{tid}/activities/{aid}",
                             headers=_bearer(owner)).status_code == 204
        _cleanup_trips("api-edit", "Renamed")

    def test_api_packing_checkoff():
        owner = _user("api-packer")
        tid = _owned_trip("api-pack", owner)
        r = client.post(f"/api/v1/trips/{tid}/packing-items", headers=_bearer(owner),
                        json={"label": "Passport", "category": "documents"})
        assert r.status_code == 201, r.text
        iid = r.json()["id"]
        assert r.json()["status"] == "draft"
        # check it off from the phone
        r = client.patch(f"/api/v1/trips/{tid}/packing-items/{iid}", headers=_bearer(owner),
                         json={"status": "packed"})
        assert r.status_code == 200 and r.json()["status"] == "packed"
        # the snapshot's packing reflects it
        snap = client.get(f"/api/v1/trips/{tid}", headers=_bearer(owner)).json()
        assert snap["packing"] and any(i["id"] == iid and i["status"] == "packed"
                                       for i in snap["packing"]["items"])
        _cleanup_trips("api-pack")

    def test_api_view_only_cannot_edit():
        owner, viewer, stranger = _user("api-eo2"), _user("api-vo2"), _user("api-str2")
        tid = _owned_trip("api-vt", owner)
        with SessionLocal() as s:
            s.add(TripMember(trip_id=tid, user_id=viewer.id))     # explicit view-only share
            s.commit()
        # viewer can READ
        assert client.get(f"/api/v1/trips/{tid}", headers=_bearer(viewer)).status_code == 200
        # viewer CANNOT edit (403), stranger cannot even see it (404)
        assert client.patch(f"/api/v1/trips/{tid}", headers=_bearer(viewer),
                            json={"name": "x"}).status_code == 403
        assert client.patch(f"/api/v1/trips/{tid}", headers=_bearer(stranger),
                            json={"name": "x"}).status_code == 404
        _cleanup_trips("api-vt")
