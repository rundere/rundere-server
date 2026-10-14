"""YONDER-262: the trip timeline is the design README §6 component: a line with a
node per item, and the node of the viewer's next moment (D-172) lit, the same rule
as Home's Next-up card."""
import re
import sys
from datetime import date, timedelta
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
    from app.models import Base, Trip, TripActivity

    Base.metadata.create_all(engine)
    client = TestClient(app)
    D = lambda n: (date.today() + timedelta(days=n)).isoformat()

    def _trip():
        with SessionLocal() as s:
            t = Trip(name="TL trip", source="manual", start_date=D(-1), end_date=D(5))
            s.add(t)
            s.flush()
            past = TripActivity(trip_id=t.id, type="dining", starts_at=f"{D(-1)}T19:00",
                                values={"when": f"{D(-1)}T19:00", "name": "TL past dinner"})
            nxt = TripActivity(trip_id=t.id, type="tour", starts_at=f"{D(2)}T10:00",
                               values={"when": f"{D(2)}T10:00", "name": "TL next tour"})
            later = TripActivity(trip_id=t.id, type="dining", starts_at=f"{D(4)}T20:00",
                                 values={"when": f"{D(4)}T20:00", "name": "TL later dinner"})
            s.add_all([past, nxt, later])
            s.commit()
            return t.id, nxt.id

    def _clean(tid):
        with SessionLocal() as s:
            s.query(TripActivity).filter_by(trip_id=tid).delete()
            s.query(Trip).filter_by(id=tid).delete()
            s.commit()

    def test_the_next_moment_has_the_lit_node():
        tid, nxt = _trip()
        try:
            html = client.get(f"/trips/{tid}").text
            assert 'class="card blueprint bp-daycard yn-tl"' in html
            assert html.count('class="yn-tl-node"') == 3           # one node per item
            rows = re.findall(r'<details class="bp-act( is-next)?">.*?bp-act-title">([^<]+)<',
                              html, re.S)
            assert [(lit.strip(), name) for lit, name in rows] == [
                ("", "TL past dinner"), ("is-next", "TL next tour"), ("", "TL later dinner")]
            assert html.count("is-next") == 1
            assert '<span class="sr-only">Next up: </span>' in html   # said, not only shown
        finally:
            _clean(tid)

    def test_nothing_is_lit_when_nothing_is_ahead():
        with SessionLocal() as s:
            t = Trip(name="TL done", source="manual", start_date=D(-9), end_date=D(-7))
            s.add(t)
            s.flush()
            s.add(TripActivity(trip_id=t.id, type="dining", starts_at=f"{D(-8)}T19:00",
                               values={"when": f"{D(-8)}T19:00", "name": "TL old"}))
            s.commit()
            tid = t.id
        try:
            html = client.get(f"/trips/{tid}").text
            assert 'class="yn-tl-node"' in html and "is-next" not in html
        finally:
            _clean(tid)
