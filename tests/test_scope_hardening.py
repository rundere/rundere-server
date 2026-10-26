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

    # ------------------------------------------------ roster removal (IDOR)
    def test_stranger_cannot_remove_a_traveller():
        """POST /trips/{id}/travelers/{tid}/remove had no access check: any
        signed-in user could empty someone else's roster and read it back."""
        from app.models import TravelerIdentity, TripTraveler
        _, tid = _owner_with_trip("scope-roster", "Scope Roster Trip")
        _owner_with_trip("scope-stranger", "Scope Stranger Trip")
        with SessionLocal() as s:
            t = TravelerIdentity(display_name="Scope Roster Guest", kind="guest")
            s.add(t)
            s.flush()
            s.add(TripTraveler(trip_id=tid, traveler_id=t.id))
            s.commit()
            trav_id = t.id
        settings.auth_required = True
        try:
            c = TestClient(app)
            c.post("/login", data={"username": "scope-stranger", "password": "pw-owner-123"})
            r = c.post(f"/trips/{tid}/travelers/{trav_id}/remove")
            assert r.status_code == 404 and "Scope Roster Guest" not in r.text
            with SessionLocal() as s:
                assert s.get(TripTraveler, {"trip_id": tid, "traveler_id": trav_id})
            owner = TestClient(app)
            owner.post("/login", data={"username": "scope-roster", "password": "pw-owner-123"})
            assert owner.post(f"/trips/{tid}/travelers/{trav_id}/remove").status_code == 200
            with SessionLocal() as s:
                assert s.get(TripTraveler, {"trip_id": tid, "traveler_id": trav_id}) is None
        finally:
            settings.auth_required = False

    # ------------------------------------------------------ job panels
    def test_job_panel_is_its_starters_only():
        from app.review import jobs
        uid, _ = _owner_with_trip("scope-jobber", "Scope Job Trip")
        _owner_with_trip("scope-peeker", "Scope Peek Trip")
        with jobs._LOCK:
            jobs._JOBS["scope-job-1"] = {"id": "scope-job-1", "kind": "process",
                                         "owner": uid, "state": "done",
                                         "message": "Scope Secret Headline", "items": []}
        settings.auth_required = True
        try:
            c = TestClient(app)
            c.post("/login", data={"username": "scope-peeker", "password": "pw-owner-123"})
            r = c.get("/review/jobs/scope-job-1")
            assert r.status_code == 200 and r.text == ""
            c.get("/review/jobs/scope-job-1/dismiss")
            assert jobs.get("scope-job-1") is not None          # not someone else's to dismiss
        finally:
            settings.auth_required = False
            jobs.dismiss("scope-job-1")

    # ------------------------------------------------- registration claims
    def test_register_cannot_claim_a_linked_traveller():
        """/register took ANY traveller id: a stranger could link themselves to
        someone's traveller and inherit their trips. Now the YONDER-145 rule."""
        from app.models import TravelerIdentity
        with SessionLocal() as s:
            for name in ("scope-reg-thief", "scope-reg-victim"):
                old = s.query(User).filter_by(username=name).one_or_none()
                if old:
                    s.delete(old)
            s.query(TravelerIdentity).filter_by(display_name="Scope Reg Victim").delete()
            s.commit()
            t = TravelerIdentity(display_name="Scope Reg Victim", kind="guest")
            s.add(t)
            s.flush()
            authn.create_user(s, "scope-reg-victim", "pw-owner-123", traveler_id=t.id)
            victim_tid = t.id
        settings.auth_required = True
        settings.auth_registration_open = True
        try:
            c = TestClient(app)
            page = c.get("/register")
            assert "Scope Reg Victim" not in page.text          # not offered…
            r = c.post("/register", data={"username": "scope-reg-thief",
                                          "password": "pw-thief-123",
                                          "email": "thief@example.com",
                                          "traveler_id": str(victim_tid)},
                       follow_redirects=False)
            assert r.status_code == 409                          # …and not accepted
            with SessionLocal() as s:
                assert s.query(User).filter_by(username="scope-reg-thief").count() == 0
        finally:
            settings.auth_required = False
            settings.auth_registration_open = False
