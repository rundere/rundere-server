"""Planning runner tests — real DB session, offline geocode, so plan persistence
and the packed-survives-regenerate contract are covered without network.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, PackingItem, PackingPlan, Trip, TripActivity)
    from planning.runner import generate_plan
    Base.metadata.create_all(engine)
    _client = TestClient(app)
    DEPS = True
except ImportError:
    DEPS = False


def _airports(code):
    t = {"YYZ": ("CA", "Toronto", 43.68, -79.63), "GVA": ("CH", "Geneva", 46.23, 6.11)}
    h = t.get((code or "").upper())
    return None if h is None else SimpleNamespace(
        country=h[0], municipality=h[1], lat=h[2], lon=h[3], icao=None)


class _NoGeo:
    def geocode_place(self, p):
        return None

    def geocode_freeform(self, text):
        return None


def _trip():
    with SessionLocal() as s:
        for m in (PackingItem, PackingPlan, TripActivity, Trip):
            s.query(m).delete()
        s.commit()
        t = Trip(name="Alpine trip", start_date="2026-09-01", end_date="2026-09-08")
        s.add(t); s.commit()
        s.add(TripActivity(trip_id=t.id, type="flight",
                           values={"from": "YYZ", "to": "GVA"}))
        s.add(TripActivity(trip_id=t.id, type="lodging",
                           values={"name": "Hotel Lac Bleu", "type": "hotel"}))
        s.commit()
        return t.id


def _gen(tid):
    with SessionLocal() as s:
        return generate_plan(s, s.get(Trip, tid), resolve_airport=_airports,
                             geocoder=_NoGeo()).id


def test_generate_persists_a_plan_with_essentials():
    if not DEPS:
        return
    tid = _trip()
    _gen(tid)
    with SessionLocal() as s:
        plan = s.query(PackingPlan).filter_by(trip_id=tid).one()
        assert plan.status == "done"
        assert plan.archetypes                       # something was chosen
        items = s.query(PackingItem).filter_by(trip_id=tid).all()
        keys = {i.item_key for i in items}
        assert "documents:passport" in keys
        # CH is international vs an empty home set → international essentials in
        assert "documents:travel_insurance" in keys
        # no weather findings → a heads-up warning
        assert any("weather" in w.lower() for w in (plan.warnings or []))


def test_packed_item_survives_regenerate():
    if not DEPS:
        return
    tid = _trip()
    _gen(tid)
    with SessionLocal() as s:
        pi = s.query(PackingItem).filter_by(trip_id=tid,
                                            item_key="documents:passport").one()
        pi.status = "packed"
        s.commit()
        pid = pi.id
    _gen(tid)                                        # re-generate
    with SessionLocal() as s:
        assert s.get(PackingItem, pid).status == "packed"   # not reset to draft
        # exactly one passport row — upserted, not duplicated
        assert s.query(PackingItem).filter_by(
            trip_id=tid, item_key="documents:passport").count() == 1


def test_trip_detail_renders_the_packing_plan():
    """Render smoke — catches template traps (e.g. Jinja `grp.items` resolving to
    a dict method) that unit tests on the data alone miss."""
    if not DEPS:
        return
    tid = _trip()
    _gen(tid)
    r = _client.get(f"/trips/{tid}")
    assert r.status_code == 200
    assert "Packing" in r.text and "yn-pack-row" in r.text
