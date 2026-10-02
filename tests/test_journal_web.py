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

    # ------------------------------------------------------------------ YONDER-188
    def _trips(*rows):
        return [{"id": i, "name": n, "start_date": s, "end_date": e} for i, n, s, e in rows]

    def test_default_trip_is_the_one_covering_the_date():
        from app import journal as j
        t = _trips(("a", "Past", "2026-01-01", "2026-01-10"),
                   ("b", "Now", "2026-09-30", "2026-10-05"),
                   ("c", "Future", "2027-03-01", "2027-03-09"))
        assert j.default_trip_id(t, "2026-10-02") == "b"

    def test_no_covering_trip_leaves_it_unattached():
        from app import journal as j
        t = _trips(("a", "Past", "2026-01-01", "2026-01-10"))
        assert j.default_trip_id(t, "2026-10-02") is None
        assert j.default_trip_id([], "2026-10-02") is None

    def test_overlap_is_deterministic_latest_start_then_shortest():
        from app import journal as j
        t = _trips(("long", "Long", "2026-09-01", "2026-10-30"),
                   ("late", "Late", "2026-10-01", "2026-10-20"),
                   ("short", "Short", "2026-10-01", "2026-10-03"))
        for order in (t, list(reversed(t))):
            assert j.default_trip_id(order, "2026-10-02") == "short"
            assert [x["id"] for x in j.rank_trips(order, "2026-10-02")] == ["short", "late", "long"]

    def test_far_future_trip_sorts_below_current_and_recent():
        from app import journal as j
        t = _trips(("f", "Far", "2027-06-01", "2027-06-09"),
                   ("n", "Now", "2026-10-01", "2026-10-09"),
                   ("r", "Recent", "2026-09-01", "2026-09-20"),
                   ("u", "Undated", None, None))
        assert [x["id"] for x in j.rank_trips(t, "2026-10-02")] == ["n", "r", "f", "u"]

    def test_trip_options_endpoint_preselects_and_respects_a_manual_pick():
        with SessionLocal() as s:
            s.add_all([Trip(name="Web Journal Trip", source="manual",
                            start_date="2026-09-30", end_date="2026-10-05"),
                       Trip(name="Web Journal Trip", source="manual",
                            start_date="2027-03-01", end_date="2027-03-09")])
            s.commit()
            ids = {t.start_date: t.id for t in
                   s.query(Trip).filter(Trip.name == "Web Journal Trip").all()}
        try:
            r = client.get("/journal/trip-options", params={"entry_date": "2026-10-02"})
            assert f'value="{ids["2026-09-30"]}" selected' in r.text
            assert f'value="{ids["2027-03-01"]}" selected' not in r.text
            r = client.get("/journal/trip-options",
                           params={"entry_date": "2026-10-02", "picked": ids["2027-03-01"]})
            assert f'value="{ids["2027-03-01"]}" selected' in r.text      # hand pick wins
            r = client.get("/journal/trip-options", params={"entry_date": "2026-12-25"})
            assert "selected" not in r.text                                # nothing covers it
        finally:
            _cleanup()

    def test_compose_form_preselects_todays_trip():
        from datetime import date
        today = date.today().isoformat()
        with SessionLocal() as s:
            t = Trip(name="Web Journal Trip", source="manual",
                     start_date=today, end_date=today)
            s.add(t); s.commit(); tid = t.id
        try:
            r = client.get("/journal")
            assert f'value="{tid}" selected' in r.text
        finally:
            _cleanup()

    def test_trip_options_only_list_trips_the_caller_can_see(monkeypatch):
        from app import authn
        from app.config import settings
        from app.models import User
        monkeypatch.setattr(settings, "auth_required", True)
        with SessionLocal() as s:
            alice, bob = User(username="jw-alice", auth_source="local"), \
                User(username="jw-bob", auth_source="local")
            s.add_all([alice, bob]); s.flush()
            s.add(Trip(name="Web Journal Trip", source="manual", owner_id=alice.id,
                       start_date="2026-09-30", end_date="2026-10-05"))
            s.commit()
            tok = {u.username: authn.issue_access_token(u) for u in (alice, bob)}
        try:
            mine = client.get("/journal/trip-options", params={"entry_date": "2026-10-02"},
                              cookies={settings.auth_cookie_name: tok["jw-alice"]})
            theirs = client.get("/journal/trip-options", params={"entry_date": "2026-10-02"},
                                cookies={settings.auth_cookie_name: tok["jw-bob"]})
            assert "Web Journal Trip" in mine.text and "selected" in mine.text
            assert "Web Journal Trip" not in theirs.text and "selected" not in theirs.text
        finally:
            with SessionLocal() as s:
                s.query(User).filter(User.username.like("jw-%")).delete(synchronize_session=False)
                s.commit()
            _cleanup()
