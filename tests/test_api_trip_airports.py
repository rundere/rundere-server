"""YONDER-177 — TripSummary carries the airport codes the trip's flights visit, in
flight order and without repeats, for the app's Upcoming cards ("LHR · NRT · KIX")."""
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
    from app.models import Base, Trip, TripActivity, User

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

    def test_trip_summary_lists_airports_in_flight_order():
        owner, stranger = _user("ap-owner"), _user("ap-stranger")
        with SessionLocal() as s:
            t = Trip(name="AP Japan", source="manual", owner_id=owner.id,
                     start_date="2031-04-01", end_date="2031-04-12")
            s.add(t)
            s.flush()
            for when, frm, to in [("2031-04-10T09:00", "NRT", "KIX"),
                                  ("2031-04-01T11:00", "LHR", "NRT"),
                                  ("2031-04-12T08:00", "Kansai", "LHR")]:   # free text: no code
                s.add(TripActivity(trip_id=t.id, type="flight", starts_at=when,
                                   values={"from": frm, "to": to}))
            s.add(TripActivity(trip_id=t.id, type="lodging", starts_at="2031-04-01T15:00",
                               values={"name": "Harbour Park Hotel"}))
            s.commit()
            tid = t.id
        try:
            rows = client.get("/api/v1/trips", headers=_bearer(owner)).json()
            mine = next(r for r in rows if r["id"] == tid)
            assert mine["airports"] == ["LHR", "NRT", "KIX"]
            assert mine["activity_count"] == 4
            snap = client.get(f"/api/v1/trips/{tid}", headers=_bearer(owner)).json()
            assert snap["trip"]["airports"] == ["LHR", "NRT", "KIX"]
            # Another user sees neither the trip nor its codes.
            assert all(r["id"] != tid for r in client.get(
                "/api/v1/trips", headers=_bearer(stranger)).json())
            assert client.get(f"/api/v1/trips/{tid}", headers=_bearer(stranger)).status_code == 404
        finally:
            with SessionLocal() as s:
                s.query(TripActivity).filter(TripActivity.trip_id == tid).delete()
                s.query(Trip).filter(Trip.id == tid).delete()
                s.commit()
