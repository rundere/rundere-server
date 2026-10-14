"""YONDER-238 — start Discovery from the app: POST /api/v1/trips/{id}/discover
uses the same per-person launch as the web button, one run per person per trip,
refused on an ended trip (D-157); GET …/discover/status reports the caller's OWN
run. A stranger can't start or watch a run (404)."""
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
    from app.models import (Base, DiscoveryRun, TravelerIdentity, Trip, TripMember,
                            TripTraveler, User)
    from app.trips import discovery_jobs as dj

    Base.metadata.create_all(engine)
    client = TestClient(app)

    def _user(name, nationality=None, currency=None):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                t = TravelerIdentity(display_name=name, kind="guest",
                                     nationality=nationality, base_currency=currency)
                s.add(t)
                s.flush()
                u = User(username=name, auth_source="local", traveler_id=t.id)
                s.add(u)
                s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _bearer(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _trip(owner, start="2031-05-01", end="2031-05-20"):
        with SessionLocal() as s:
            t = Trip(name="Disc API trip", source="manual", owner_id=owner.id,
                     start_date=start, end_date=end)
            s.add(t)
            s.flush()
            s.add(TripTraveler(trip_id=t.id, traveler_id=owner.traveler_id, role="self"))
            s.commit()
            return t.id

    def _drop(tid):
        """The trip and every dapi-* person: leftover travellers would change
        who other suites' resolvers think is travelling."""
        with SessionLocal() as s:
            s.query(DiscoveryRun).filter_by(trip_id=tid).delete()
            s.query(TripMember).filter_by(trip_id=tid).delete()
            s.query(TripTraveler).filter_by(trip_id=tid).delete()
            s.query(Trip).filter_by(id=tid).delete()
            users = s.query(User).filter(User.username.like("dapi-%")).all()
            tids = [u.traveler_id for u in users if u.traveler_id is not None]
            for u in users:
                s.delete(u)
            s.flush()
            if tids:
                s.query(TravelerIdentity).filter(TravelerIdentity.id.in_(tids)).delete(
                    synchronize_session=False)
            s.commit()

    def _fake_launch(monkeypatch):
        """A launch that writes the run row like the real one, minus the worker."""
        calls = []

        def launch(trip_id, **kw):
            calls.append({"trip_id": trip_id, **kw})
            with SessionLocal() as s:
                run = DiscoveryRun(trip_id=trip_id, user_id=kw["user_id"], status="running",
                                   phase="weather", done=2, total=8)
                s.add(run)
                s.commit()
                rid = run.id
            dj._LIVE.add(rid)
            return rid
        monkeypatch.setattr(dj, "launch", launch)
        return calls

    def test_start_uses_the_callers_inputs_and_reports_progress(monkeypatch):
        calls = _fake_launch(monkeypatch)
        owner = _user("dapi-owner", "CA", "CAD")
        tid = _trip(owner)
        try:
            r = client.post(f"/api/v1/trips/{tid}/discover", headers=_bearer(owner))
            assert r.status_code == 202, r.text
            body = r.json()
            assert body["running"] is True and body["phase"] == "weather"
            assert (body["done"], body["total"], body["pct"]) == (2, 8, 25)
            assert body["last_status"] == "running" and body["ended"] is False
            assert len(calls) == 1
            assert calls[0]["user_id"] == owner.id
            assert calls[0]["nationality"] == "CA"
            assert calls[0]["base_currencies"][0] == "CAD"
            # a second tap while it runs: same run, no new launch
            r = client.post(f"/api/v1/trips/{tid}/discover", headers=_bearer(owner))
            assert r.status_code == 202 and len(calls) == 1
            st = client.get(f"/api/v1/trips/{tid}/discover/status", headers=_bearer(owner))
            assert st.status_code == 200 and st.json()["running"] is True
            # the run finishes → status settles, the app reloads the trip
            with SessionLocal() as s:
                run = s.query(DiscoveryRun).filter_by(trip_id=tid).one()
                run.status = "done"
                s.commit()
                dj._LIVE.discard(run.id)
            st = client.get(f"/api/v1/trips/{tid}/discover/status",
                            headers=_bearer(owner)).json()
            assert st["running"] is False and st["last_status"] == "done"
        finally:
            _drop(tid)

    def test_never_run_status_is_empty():
        owner = _user("dapi-fresh")
        tid = _trip(owner)
        try:
            st = client.get(f"/api/v1/trips/{tid}/discover/status",
                            headers=_bearer(owner)).json()
            assert st["running"] is False and st["last_status"] is None
        finally:
            _drop(tid)

    def test_ended_trip_is_refused_with_the_reason(monkeypatch):
        calls = _fake_launch(monkeypatch)
        owner = _user("dapi-past")
        tid = _trip(owner, start="2020-01-01", end="2020-01-10")
        try:
            r = client.post(f"/api/v1/trips/{tid}/discover", headers=_bearer(owner))
            assert r.status_code == 409 and "ended" in r.json()["detail"]
            assert calls == []
            st = client.get(f"/api/v1/trips/{tid}/discover/status",
                            headers=_bearer(owner)).json()
            assert st["ended"] is True and "ended" in st["ended_message"]
        finally:
            _drop(tid)

    def test_runs_are_per_person_and_strangers_get_404(monkeypatch):
        calls = _fake_launch(monkeypatch)
        owner, viewer = _user("dapi-own2", "CA", "CAD"), _user("dapi-view2", "US", "USD")
        stranger = _user("dapi-str2")
        tid = _trip(owner)
        with SessionLocal() as s:
            s.add(TripMember(trip_id=tid, user_id=viewer.id))       # view-only share
            s.commit()
        try:
            client.post(f"/api/v1/trips/{tid}/discover", headers=_bearer(owner))
            # the viewer sees no run of theirs — the owner's run isn't theirs
            st = client.get(f"/api/v1/trips/{tid}/discover/status",
                            headers=_bearer(viewer)).json()
            assert st["running"] is False and st["last_status"] is None
            # D-199: a view-only sharer can't start runs on someone else's trip. The
            # web's Run button always refused them (403); the API now agrees.
            r = client.post(f"/api/v1/trips/{tid}/discover", headers=_bearer(viewer))
            assert r.status_code == 403
            # a stranger can't start or watch anything
            assert client.post(f"/api/v1/trips/{tid}/discover",
                               headers=_bearer(stranger)).status_code == 404
            assert client.get(f"/api/v1/trips/{tid}/discover/status",
                              headers=_bearer(stranger)).status_code == 404
            assert len(calls) == 1
        finally:
            with SessionLocal() as s:
                for r in s.query(DiscoveryRun).filter_by(trip_id=tid):
                    dj._LIVE.discard(r.id)
            _drop(tid)

    def test_targeted_refresh_passes_topic_and_place(monkeypatch):
        calls = _fake_launch(monkeypatch)
        owner = _user("dapi-own3", "CA", "CAD")
        tid = _trip(owner)
        try:
            h = _bearer(owner)
            assert client.post(f"/api/v1/trips/{tid}/discover", headers=h,
                               json={"topic": "astrology"}).status_code == 400
            assert client.post(f"/api/v1/trips/{tid}/discover", headers=h,
                               json={"place_key": "PT:Lisbon"}).status_code == 400
            assert calls == []
            r = client.post(f"/api/v1/trips/{tid}/discover", headers=h,
                            json={"topic": "weather", "place_key": "PT:Lisbon"})
            assert r.status_code == 202, r.text
            assert calls[0]["topic"] == "weather" and calls[0]["place_key"] == "PT:Lisbon"
            with SessionLocal() as s:                      # what launch records
                run = s.query(DiscoveryRun).filter_by(trip_id=tid).one()
                run.params = {"topic": "weather", "place_key": "PT:Lisbon"}
                s.commit()
            st = client.get(f"/api/v1/trips/{tid}/discover/status", headers=h).json()
            assert st["last_topic"] == "weather" and st["last_place_key"] == "PT:Lisbon"
        finally:
            with SessionLocal() as s:
                for r in s.query(DiscoveryRun).filter_by(trip_id=tid):
                    dj._LIVE.discard(r.id)
            _drop(tid)
