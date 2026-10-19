"""Deleting a trip removes its trip-only data and unlinks the person's own
content (app/trips/purge.py); `purge-orphans` cleans up after older deletes."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, DiscoveryExclusion, DiscoveryFinding, DiscoveryItem,
                            DiscoveryRun, JournalEntry, PackingItem, PackingPlan,
                            TravelItem, Trip, TripActivity, TripActivityTraveler,
                            TripMember, TripTraveler)
    from app.trips.purge import purge_orphans
    Base.metadata.create_all(engine)
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    TRIP_ONLY = (TripActivity, TripTraveler, TripMember, DiscoveryItem, DiscoveryFinding,
                 DiscoveryRun, DiscoveryExclusion, PackingItem, PackingPlan)

    def _populated_trip(s, name="Purge me"):
        t = Trip(name=name, start_date="2026-10-01", end_date="2026-10-05")
        s.add(t)
        s.flush()
        a = TripActivity(trip_id=t.id, type="note", starts_at="2026-10-01T09:00",
                         values={"name": "x"})
        s.add(a)
        s.flush()
        s.add_all([
            TripActivityTraveler(activity_id=a.id, traveler_id=1),
            TripTraveler(trip_id=t.id, traveler_id=1),
            TripMember(trip_id=t.id, user_id="purge-user"),
            DiscoveryRun(trip_id=t.id, status="done"),
            DiscoveryFinding(id=f"f-{t.id}", trip_id=t.id, topic="food", scope="city",
                             place_key="ET:Gondar", status="draft"),
            DiscoveryItem(finding_id=f"f-{t.id}", trip_id=t.id, topic="food",
                          place_key="ET:Gondar", kind="restaurant", name="Dar"),
            DiscoveryExclusion(trip_id=t.id, place_key="ET:Mekele"),
            PackingPlan(trip_id=t.id),
            PackingItem(trip_id=t.id, item_key="adapter", label="Adapter",
                        category="tech"),
            TravelItem(trip_id=t.id, status={"state": "received"}),
            JournalEntry(trip_id=t.id, entry_date="2026-10-02", body="kept"),
        ])
        s.commit()
        return t.id, a.id

    def _counts(s, tid):
        return {m.__tablename__: s.query(m).filter(m.trip_id == tid).count()
                for m in TRIP_ONLY}

    def test_deleting_a_trip_removes_its_data_and_keeps_captures_and_journal():
        with SessionLocal() as s:
            tid, aid = _populated_trip(s)
            assert all(_counts(s, tid).values())
        r = TestClient(app).post(f"/trips/{tid}/delete", follow_redirects=False)
        assert r.status_code in (200, 302, 303)   # HTMX redirect or plain redirect
        with SessionLocal() as s:
            assert s.get(Trip, tid) is None
            assert not any(_counts(s, tid).values())
            assert s.query(TripActivityTraveler).filter_by(activity_id=aid).count() == 0
            cap = s.query(TravelItem).filter(TravelItem.status["state"].as_string()
                                             == "received").all()
            journal = s.query(JournalEntry).filter_by(body="kept").all()
            assert journal and all(j.trip_id != tid for j in journal)
            assert all(c.trip_id != tid for c in cap)
            for j in journal:
                s.delete(j)
            s.commit()

    def test_purge_orphans_dry_run_then_execute():
        with SessionLocal() as s:
            tid, aid = _populated_trip(s, "Old delete")
            # simulate a pre-4.5 delete: only the trip row went
            s.query(Trip).filter_by(id=tid).delete()
            s.commit()
            dry = purge_orphans(s, execute=False)
            assert dry["discovery_items"] >= 1 and dry["packing_plans"] >= 1
            assert dry["journal_entries (unlinked)"] >= 1
            assert _counts(s, tid)["discovery_findings"] == 1       # nothing changed
            purge_orphans(s, execute=True)
            s.commit()
            assert not any(_counts(s, tid).values())
            assert s.query(TripActivityTraveler).filter_by(activity_id=aid).count() == 0
            kept = s.query(JournalEntry).filter_by(body="kept").all()
            assert kept and all(j.trip_id is None for j in kept)
            again = purge_orphans(s, execute=False)
            assert not any(again.values())
            for j in kept:
                s.delete(j)
            s.commit()
