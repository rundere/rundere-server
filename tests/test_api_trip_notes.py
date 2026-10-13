"""YONDER-239 — the app shows and edits a trip's notes: TripSummary carries
`notes` and `can_edit`, PATCH /trips/{id} saves notes the way the web form does,
and a view-only share (D-122) or a stranger can't change them."""
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
    from app.models import Base, Trip, TripMember, User

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

    def _trip(owner, notes=None):
        with SessionLocal() as s:
            t = Trip(name="Notes trip", source="manual", owner_id=owner.id, notes=notes)
            s.add(t)
            s.commit()
            return t.id

    def _drop(tid):
        with SessionLocal() as s:
            s.query(TripMember).filter_by(trip_id=tid).delete()
            s.query(Trip).filter_by(id=tid).delete()
            s.commit()

    def test_notes_and_can_edit_in_list_and_snapshot():
        owner = _user("tn-owner")
        tid = _trip(owner, notes="Bring the rail passes")
        try:
            row = next(r for r in client.get("/api/v1/trips", headers=_bearer(owner)).json()
                       if r["id"] == tid)
            assert row["notes"] == "Bring the rail passes" and row["can_edit"] is True
            snap = client.get(f"/api/v1/trips/{tid}", headers=_bearer(owner)).json()
            assert snap["trip"]["notes"] == "Bring the rail passes"
            assert snap["trip"]["can_edit"] is True
        finally:
            _drop(tid)

    def test_patch_notes_trims_clears_and_leaves_alone():
        owner = _user("tn-editor")
        tid = _trip(owner)
        h = _bearer(owner)
        try:
            r = client.patch(f"/api/v1/trips/{tid}", headers=h,
                             json={"notes": "  Line one\nLine two  "})
            assert r.status_code == 200, r.text
            assert r.json()["notes"] == "Line one\nLine two"     # trimmed, newlines kept
            # a rename without `notes` doesn't touch them
            r = client.patch(f"/api/v1/trips/{tid}", headers=h, json={"name": "Renamed"})
            assert r.json()["notes"] == "Line one\nLine two"
            # blank (or whitespace) clears them
            r = client.patch(f"/api/v1/trips/{tid}", headers=h, json={"notes": "   "})
            assert r.json()["notes"] is None
            with SessionLocal() as s:
                assert s.get(Trip, tid).notes is None
        finally:
            _drop(tid)

    def test_view_only_share_sees_notes_but_cannot_edit_them():
        owner, viewer, stranger = _user("tn-own2"), _user("tn-view2"), _user("tn-str2")
        tid = _trip(owner, notes="Owner's plan")
        with SessionLocal() as s:
            s.add(TripMember(trip_id=tid, user_id=viewer.id))   # explicit view-only share
            s.commit()
        try:
            snap = client.get(f"/api/v1/trips/{tid}", headers=_bearer(viewer)).json()
            assert snap["trip"]["notes"] == "Owner's plan"
            assert snap["trip"]["can_edit"] is False
            assert client.patch(f"/api/v1/trips/{tid}", headers=_bearer(viewer),
                                json={"notes": "hijack"}).status_code == 403
            # a stranger can't see the trip at all — 404, not 403
            assert client.get(f"/api/v1/trips/{tid}", headers=_bearer(stranger)).status_code == 404
            assert client.patch(f"/api/v1/trips/{tid}", headers=_bearer(stranger),
                                json={"notes": "hijack"}).status_code == 404
            assert all(r["id"] != tid for r in
                       client.get("/api/v1/trips", headers=_bearer(stranger)).json())
            with SessionLocal() as s:
                assert s.get(Trip, tid).notes == "Owner's plan"
        finally:
            _drop(tid)
