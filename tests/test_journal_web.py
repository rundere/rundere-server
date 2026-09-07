"""Yonder 4.x — Travel Journal web surface (SPEC-6).

Exercises the HTMX `/journal` page: compose, attach, duplicate, delete. Runs in
the auth-off single-user world (no request.state.user), which is exactly the
pre-4.0 posture the web UI must still work under; per-user isolation is covered
by test_journal.py against the API. SQLite via conftest.
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
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, JournalEntry, Trip

    Base.metadata.create_all(engine)
    client = TestClient(app)

    def _latest_id(body):
        with SessionLocal() as s:
            e = (s.query(JournalEntry).filter(JournalEntry.body == body)
                 .order_by(JournalEntry.created_at.desc()).first())
            return e.id if e else None

    def _cleanup(*bodies):
        with SessionLocal() as s:
            s.query(JournalEntry).filter(JournalEntry.body.in_(bodies)).delete(
                synchronize_session=False)
            s.query(Trip).filter(Trip.name.in_(("Web Journal Trip",))).delete(
                synchronize_session=False)
            s.commit()

    def test_page_renders():
        r = client.get("/journal")
        assert r.status_code == 200
        assert "Travel journal" in r.text

    def test_compose_creates_and_shows_entry():
        body = "web-note ferry was late uid-A"
        r = client.post("/journal", data={"body": body, "entry_date": "2026-09-07"})
        assert r.status_code == 200
        assert body in r.text                      # rendered back into the panel
        assert _latest_id(body) is not None        # persisted
        _cleanup(body)

    def test_blank_body_is_noop():
        r = client.post("/journal", data={"body": "   "})
        assert r.status_code == 200
        with SessionLocal() as s:
            assert s.query(JournalEntry).filter(JournalEntry.body == "").count() == 0

    def test_attach_then_delete():
        body = "web-note attach uid-B"
        with SessionLocal() as s:
            t = Trip(name="Web Journal Trip", source="manual")
            s.add(t)
            s.commit()
            trip_id = t.id
        client.post("/journal", data={"body": body})
        eid = _latest_id(body)
        r = client.post(f"/journal/{eid}/attach", data={"trip_id": trip_id})
        assert r.status_code == 200
        with SessionLocal() as s:
            assert s.get(JournalEntry, eid).trip_id == trip_id
        r = client.post(f"/journal/{eid}/delete")
        assert r.status_code == 200
        with SessionLocal() as s:
            assert s.get(JournalEntry, eid) is None
        _cleanup(body)

    def test_duplicate_creates_second_row():
        body = "web-note dup uid-C"
        client.post("/journal", data={"body": body})
        eid = _latest_id(body)
        r = client.post(f"/journal/{eid}/duplicate")
        assert r.status_code == 200
        with SessionLocal() as s:
            copies = s.query(JournalEntry).filter(
                JournalEntry.duplicated_from == eid).count()
            assert copies == 1
        _cleanup(body)

    def test_dashboard_links_to_journal():
        r = client.get("/")
        assert r.status_code == 200
        assert "/journal" in r.text
