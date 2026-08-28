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
    from app.models import Base, IngestBatch, Trip, TripActivity, User

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
        # These tests add trips/activities to the shared SQLite DB; remove them so
        # other files (e.g. test_committing) don't see stray rows.
        with SessionLocal() as s:
            for t in s.query(Trip).filter(Trip.name.in_(names)).all():
                s.query(TripActivity).filter(TripActivity.trip_id == t.id).delete()
                s.delete(t)
            s.commit()

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
