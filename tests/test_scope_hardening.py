"""Security review, area 2: scoping (YONDER-293).

With sign-in on, "no user" means nobody: every scope helper answers nothing.
It used to mean "unrestricted", so a route that skips the sign-in gate (the
/capture pages) listed every trip on the server to an anonymous visitor.
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
    from app import authn, places, scope
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, JournalEntry, SavedPlace, TravelItem, Trip, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False        # tests talk plain http

    def _owner_with_trip(username, trip_name):
        with SessionLocal() as s:
            old = s.query(User).filter_by(username=username).one_or_none()
            if old:
                s.query(Trip).filter_by(owner_id=old.id).delete()
                s.delete(old)
                s.commit()
            u = authn.create_user(s, username, "pw-owner-123")
            t = Trip(name=trip_name, owner_id=u.id,
                     start_date="2031-06-01", end_date="2031-06-10")
            s.add(t)
            s.commit()
            return u.id, t.id

    def test_nobody_sees_nothing_with_sign_in_on():
        uid, tid = _owner_with_trip("scope-owner", "Scope Secret Trip")
        settings.auth_required = True
        try:
            with SessionLocal() as s:
                trip = s.get(Trip, tid)
                assert not scope.unrestricted(None)
                assert s.query(Trip).filter(scope.trip_filter(s, None)).count() == 0
                assert s.query(Trip).filter(scope.filing_trip_filter(s, None)).count() == 0
                assert scope.visible_trip_ids(s, None) == set()
                assert not scope.can_view_trip(s, None, trip)
                assert not scope.can_edit_trip(s, None, trip)
                assert scope.trip_role(s, None, trip) is None
                assert s.query(TravelItem).filter(scope.capture_filter(None)).count() == 0
                assert s.query(JournalEntry).filter(scope.journal_filter(None)).count() == 0
                assert s.query(SavedPlace).filter(places.visible(None)).count() == 0
        finally:
            settings.auth_required = False

    def test_sign_in_off_keeps_the_single_user_world():
        """Auth off is still the pre-4.0 single-user server: no user, everything."""
        _, tid = _owner_with_trip("scope-solo", "Scope Solo Trip")
        assert not settings.auth_required
        with SessionLocal() as s:
            assert scope.unrestricted(None)
            assert scope.can_view_trip(s, None, s.get(Trip, tid))

    def test_anonymous_capture_page_lists_no_trips():
        _owner_with_trip("scope-capture", "Scope Capture Honeymoon")
        settings.auth_required = True
        try:
            c = TestClient(app)
            r = c.get("/capture", follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"].startswith("/login")
            for path in ("/capture/setup", "/capture/setup?tab=email",
                         "/capture/setup?tab=mcp", "/capture/sheet"):
                r = c.get(path, follow_redirects=False)
                assert "Scope Capture Honeymoon" not in r.text, path
            # The owner, signed in, still gets their own trip in the picker.
            c.post("/login", data={"username": "scope-capture", "password": "pw-owner-123"})
            assert "Scope Capture Honeymoon" in c.get("/capture").text
        finally:
            settings.auth_required = False

    def test_another_user_cannot_see_the_trip():
        _, tid = _owner_with_trip("scope-a", "Scope A Private")
        _owner_with_trip("scope-b", "Scope B Private")
        settings.auth_required = True
        try:
            c = TestClient(app)
            c.post("/login", data={"username": "scope-b", "password": "pw-owner-123"})
            assert "Scope A Private" not in c.get("/capture").text
            assert c.get(f"/api/v1/trips/{tid}").status_code == 404
        finally:
            settings.auth_required = False
