"""YONDER-219 / D-187 — each person's "now" is the time zone on their Travel
profile, not the server's (UTC in Docker) and not their home profiles."""
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import authn, clock
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, TravelerIdentity, Trip, TripTraveler, User

    Base.metadata.create_all(engine)
    client = TestClient(app)

    @pytest.fixture()
    def person(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        tag = uuid.uuid4().hex[:6]
        with SessionLocal() as s:
            t = TravelerIdentity(display_name=f"Pc Tess {tag}", kind="guest",
                                 timezone="Europe/London")
            s.add(t)
            s.flush()
            u = User(username=f"pc-{tag}", auth_source="local", traveler_id=t.id)
            s.add(u)
            s.commit()
            s.refresh(u)
            s.expunge(u)
        yield u
        with SessionLocal() as s:
            for tr in s.query(Trip).filter(Trip.name.like("Pc %")).all():
                s.query(TripTraveler).filter_by(trip_id=tr.id).delete()
                s.delete(tr)
            s.query(User).filter(User.id == u.id).delete()
            s.query(TravelerIdentity).filter(TravelerIdentity.id == u.traveler_id).delete()
            s.commit()

    def _ck(u):
        return {settings.auth_cookie_name: authn.issue_access_token(u)}

    def test_now_is_the_profile_zone_not_the_servers(person):
        at = datetime(2031, 3, 1, 23, 30, tzinfo=timezone.utc)        # 23:30 UTC
        with SessionLocal() as s:
            assert clock.now_for(s, person, _utcnow=at) == datetime(2031, 3, 1, 23, 30)
            t = s.get(TravelerIdentity, person.traveler_id)
            t.timezone = "America/Toronto"
            s.flush()
            assert clock.now_for(s, person, _utcnow=at) == datetime(2031, 3, 1, 18, 30)
            t.timezone = "Asia/Tokyo"
            s.flush()
            assert clock.today_for(s, person, _utcnow=at) == "2031-03-02"   # already tomorrow
            s.rollback()
            # No zone (or no traveller): the server clock, as before.
            assert abs((clock.now_for(s, SimpleNamespace(traveler_id=None))
                        - datetime.now()).total_seconds()) < 5

    def test_invalid_zones_are_refused():
        assert clock.valid_zone("Europe/London") == "Europe/London"
        assert clock.valid_zone("Mars/Olympus") is None and clock.valid_zone("") is None
        assert "America/Toronto" in clock.zone_names() and "US/Eastern" not in clock.zone_names()

    def test_travel_profile_saves_the_time_zone(person):
        r = client.post("/trips/settings/travel-profile", cookies=_ck(person),
                        data={"timezone": "America/Toronto"})
        assert r.status_code == 200 and 'value="America/Toronto" selected' in r.text
        client.post("/trips/settings/travel-profile", cookies=_ck(person),
                    data={"timezone": "Not/AZone"})
        with SessionLocal() as s:
            assert s.get(TravelerIdentity, person.traveler_id).timezone is None

    def test_home_and_the_api_count_days_on_your_clock(person, monkeypatch):
        """A trip tomorrow, as seen at 21:00 local the evening before — the
        countdown says one day, whatever the server's own date is."""
        with SessionLocal() as s:
            tr = Trip(name=f"Pc trip {uuid.uuid4().hex[:4]}", source="manual",
                      owner_id=person.id, start_date="2031-03-02", end_date="2031-03-05")
            s.add(tr)
            s.flush()
            s.add(TripTraveler(trip_id=tr.id, traveler_id=person.traveler_id))
            s.commit()
        monkeypatch.setattr(clock, "now_for", lambda session, user, **k: datetime(2031, 3, 1, 21, 0))
        home = client.get("/", cookies=_ck(person)).text.lower()
        assert "in 1 day" in home and "good evening" in home
        assert "new date().gethours()" not in home              # zone set: server is right
        api = client.get("/api/v1/dashboard",
                         headers={"Authorization": f"Bearer {authn.issue_access_token(person)}"})
        assert api.json()["next_trip"]["days_until"] == 1
