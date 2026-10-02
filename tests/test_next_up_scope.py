"""Home's Next-up is the viewer's own: a flight tagged to another traveller on a shared trip
is not their next up (untagged = everyone, app/trips/who.py)."""
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TODAY = date.today()


def _setup():
    from app.db import SessionLocal, engine
    from app.models import Base, Trip, TripActivity, TripActivityTraveler
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        t = Trip(name="Shared NU", source="manual", start_date=(TODAY + timedelta(days=1)).isoformat(),
                 end_date=(TODAY + timedelta(days=9)).isoformat())
        s.add(t); s.flush()
        mine_first = TripActivity(trip_id=t.id, type="flight", starts_at=(TODAY + timedelta(days=2)).isoformat() + "T09:00",
                                  values={"from": "AAA", "to": "BBB", "flight_number": "T1 1"})
        everyone = TripActivity(trip_id=t.id, type="flight", starts_at=(TODAY + timedelta(days=5)).isoformat() + "T09:00",
                                values={"from": "CCC", "to": "DDD", "flight_number": "T2 2"})
        s.add_all([mine_first, everyone]); s.flush()
        s.add(TripActivityTraveler(activity_id=mine_first.id, traveler_id=101))   # tagged to traveller 101 only
        s.commit()
        return t.id, [mine_first.id, everyone.id]


def _teardown(tid, aids):
    from app.db import SessionLocal
    from app.models import Trip, TripActivity, TripActivityTraveler
    with SessionLocal() as s:
        s.query(TripActivityTraveler).filter(TripActivityTraveler.activity_id.in_(aids)).delete(synchronize_session=False)
        s.query(TripActivity).filter(TripActivity.trip_id == tid).delete()
        s.query(Trip).filter(Trip.id == tid).delete()
        s.commit()


def test_next_up_only_shows_the_viewers_own_flight():
    from app.dashboard import _next_up
    from app.db import SessionLocal
    from app.models import Trip
    tid, aids = _setup()
    try:
        with SessionLocal() as s:
            trips = s.query(Trip).filter(Trip.id == tid).all()
            today = TODAY.isoformat()
            codes = lambda u: (lambda nu: (nu["from"], nu["to"]))(_next_up(s, trips, today, u))
            assert codes(None) == ("AAA", "BBB")                        # auth off: nothing filtered
            assert codes(NS(id="u1", traveler_id=101)) == ("AAA", "BBB")   # tagged to them
            assert codes(NS(id="u2", traveler_id=202)) == ("CCC", "DDD")   # the other traveller: skip 101's
            assert codes(NS(id="u3", traveler_id=None)) == ("CCC", "DDD")  # unlinked: only "everyone" flights
    finally:
        _teardown(tid, aids)
