"""Discovery items (Run·dere 4.5, YONDER-147): Food & Drink and Events &
Activities rows — mapping from resolver data, sync that keeps what the person
did, runner integration, API exposure, activity unlink, and the 0030 backfill.
"""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, DiscoveryFinding, DiscoveryItem, DiscoveryRun, Trip,
                            TripActivity, User)
    from discover import items
    from discover.resolvers.base import Finding
    from discover.runner import run_discovery
    Base.metadata.create_all(engine)
    DEPS = True
except ImportError:
    DEPS = False

FOOD = {"places": [
    {"name": "Café Meskel", "type": "cafe", "neighborhood": "Medina", "address": "7 Example St",
     "price_tier": 2, "why": "Camel burger and live music", "local_specialty": True,
     "source_url": "https://example.com/clock"},
    {"name": "Nomad", "type": "restaurant", "price_tier": 3, "why": "Rooftop views",
     "source_url": "https://example.com/nomad"},
    {"name": "  nomad ", "type": "restaurant", "price_tier": 3, "why": "dupe"},
    {"name": "Weird", "type": "spaceship", "price_tier": 9, "why": "x"},
    {"type": "cafe", "why": "no name"},
], "local_dishes": ["tagine"], "allergy_warnings": []}

EVENTS = {
    "impacts": [{"name": "Transport strike", "date_range": "2026-10-12 to 2026-10-13",
                 "effect": "Trains limited", "source_url": "https://example.com/strike"}],
    "notable": [
        {"name": "Film festival", "date_range": "2026-10-14", "matched_interest": "film",
         "why": "Open-air screenings", "source_url": "https://example.com/film"},
        {"name": "Hammam", "date_range": None, "why": "A classic scrub",
         "source_url": "https://example.com/hammam"},
    ],
}


def test_food_rows_map_kinds_tiers_and_dedupe():
    rows = items._dedupe(items.rows_from_finding("food", FOOD))
    by = {r["name"]: r for r in rows}
    assert set(by) == {"Café Meskel", "Nomad", "Weird"}
    assert by["Café Meskel"]["kind"] == "cafe" and by["Café Meskel"]["neighbourhood"] == "Medina"
    assert by["Café Meskel"]["extra"] == {"local_specialty": True}
    assert by["Café Meskel"]["sources"] == [{"url": "https://example.com/clock"}]
    assert by["Weird"]["kind"] == "restaurant" and by["Weird"]["price_tier"] is None


def test_event_rows_split_events_and_activities():
    rows = {r["name"]: r for r in items.rows_from_finding("events", EVENTS)}
    strike = rows["Transport strike"]
    assert strike["kind"] == "event" and strike["extra"] == {"heads_up": True}
    assert (strike["starts_on"], strike["ends_on"]) == ("2026-10-12", "2026-10-13")
    assert rows["Film festival"]["starts_on"] == "2026-10-14"
    assert rows["Film festival"]["extra"] == {"interest": "film"}
    assert rows["Hammam"]["kind"] == "activity" and rows["Hammam"]["starts_on"] is None
    assert items.rows_from_finding("weather", EVENTS) == []
    assert items.rows_from_finding("food", None) == []


if DEPS:
    def _finding(s, topic="food", data=None, user_id=None):
        t = Trip(name="Ethiopia", start_date="2026-10-10", end_date="2026-10-20")
        s.add(t)
        s.flush()
        f = DiscoveryFinding(trip_id=t.id, user_id=user_id, topic=topic, scope="city",
                             place_key="MA:Harar", status="draft", data=data or FOOD)
        s.add(f)
        s.flush()
        return f

    def test_sync_keeps_what_the_person_did():
        with SessionLocal() as s:
            f = _finding(s)
            items.sync_from_finding(s, f)
            s.flush()
            got = {i.name: i for i in s.query(DiscoveryItem).filter_by(finding_id=f.id)}
            assert set(got) == {"Café Meskel", "Nomad", "Weird"}
            got["Café Meskel"].hidden = True
            got["Nomad"].activity_id = "act-1"
            got["Weird"].edited = True
            got["Weird"].details = "my note"
            s.flush()
            # A newer result: Café Meskel renamed-in-place, Nomad and Weird gone,
            # a new place added.
            f.data = {"places": [
                {"name": "Cafe Meskel", "type": "cafe", "price_tier": 1, "why": "cheaper now"},
                {"name": "Le Jardin", "type": "restaurant", "price_tier": 2, "why": "garden"}]}
            items.sync_from_finding(s, f)
            s.flush()
            got = {i.name.lower(): i for i in s.query(DiscoveryItem).filter_by(finding_id=f.id)}
            assert set(got) == {"Cafe Meskel", "nomad", "weird", "le jardin"}
            clock = got["Cafe Meskel"]   # the same row, matched despite case and accent
            assert clock.hidden is True and clock.price_tier == 1
            assert got["nomad"].activity_id == "act-1"          # on the timeline: kept
            assert got["weird"].details == "my note"            # edited: kept as edited
            s.rollback()

    def test_untouched_rows_the_new_result_drops_are_removed():
        with SessionLocal() as s:
            f = _finding(s)
            items.sync_from_finding(s, f)
            f.data = {"places": []}
            items.sync_from_finding(s, f)
            s.flush()
            assert s.query(DiscoveryItem).filter_by(finding_id=f.id).count() == 0
            s.rollback()

    class _FakeFood:
        topic, scope = "food", "city"

        def resolve(self, place, ctx):
            return Finding(topic="food", scope="city", place_key=place.city_key,
                           body_md="food", resolver="fake", data=FOOD)

    def _airports(code):
        hit = {"YYZ": ("CA", "Toronto"), "ADD": ("MA", "Harar")}.get((code or "").upper())
        return hit and SimpleNamespace(country=hit[0], municipality=hit[1], lat=0.0,
                                       lon=0.0, icao=None)

    class _NoGeo:
        def geocode_place(self, place):
            return None

        def geocode_freeform(self, text):
            return None

    def test_a_discover_run_writes_items_idempotently():
        with SessionLocal() as s:
            t = Trip(name="Run items", start_date="2026-10-10", end_date="2026-10-20")
            s.add(t)
            s.commit()
            s.add(TripActivity(trip_id=t.id, type="flight", starts_at="2026-10-10T09:00",
                               values={"from": "YYZ", "to": "ADD"}))
            s.commit()
            run_discovery(s, t, resolve_airport=_airports, geocoder=_NoGeo(),
                          resolvers=[_FakeFood()], today="2026-09-27")
            f = s.query(DiscoveryFinding).filter_by(trip_id=t.id, topic="food",
                                                    place_key="MA:Harar").one()
            names = {i.name for i in s.query(DiscoveryItem).filter_by(finding_id=f.id)}
            assert names == {"Café Meskel", "Nomad", "Weird"}
            # a further run is idempotent
            before = s.query(DiscoveryItem).filter_by(trip_id=t.id).count()
            run_discovery(s, t, resolve_airport=_airports, geocoder=_NoGeo(),
                          resolvers=[_FakeFood()], today="2026-09-27")
            assert s.query(DiscoveryItem).filter_by(trip_id=t.id).count() == before
            s.query(DiscoveryItem).filter_by(trip_id=t.id).delete()
            s.query(DiscoveryFinding).filter_by(trip_id=t.id).delete()
            s.query(DiscoveryRun).filter_by(trip_id=t.id).delete()
            s.query(TripActivity).filter_by(trip_id=t.id).delete()
            s.delete(t)
            s.commit()

    def test_snapshot_lists_the_callers_items_and_delete_activity_unlinks():
        client = TestClient(app)
        with SessionLocal() as s:
            s.query(User).filter_by(username="items-user").delete()
            u = User(username="items-user", password_hash=authn.hash_password("pw"),
                     auth_source="local")
            s.add(u)
            s.flush()
            f = _finding(s, user_id=u.id)
            t = s.get(Trip, f.trip_id)
            t.owner_id = u.id
            items.sync_from_finding(s, f)
            a = TripActivity(trip_id=t.id, type="dining", starts_at="2026-10-12T19:00",
                             values={"name": "Nomad"})
            s.add(a)
            s.flush()
            s.query(DiscoveryItem).filter_by(finding_id=f.id, name="Nomad").update(
                {"activity_id": a.id})
            s.commit()
            trip_id, act_id = t.id, a.id
            h = {"Authorization": f"Bearer {authn.issue_access_token(u)}"}
        settings.auth_required = True
        try:
            snap = client.get(f"/api/v1/trips/{trip_id}", headers=h).json()
            got = {i["name"]: i for i in snap["discovery_items"]}
            assert set(got) == {"Café Meskel", "Nomad", "Weird"}
            assert got["Nomad"]["activity_id"] == act_id and got["Café Meskel"]["kind"] == "cafe"
            assert client.delete(f"/api/v1/trips/{trip_id}/activities/{act_id}",
                                 headers=h).status_code == 204
            snap = client.get(f"/api/v1/trips/{trip_id}", headers=h).json()
            assert {i["name"]: i for i in snap["discovery_items"]}["Nomad"]["activity_id"] is None
        finally:
            settings.auth_required = False
            with SessionLocal() as s:
                s.query(DiscoveryItem).filter_by(trip_id=trip_id).delete()
                s.query(DiscoveryFinding).filter_by(trip_id=trip_id).delete()
                s.query(Trip).filter_by(id=trip_id).delete()
                s.query(User).filter_by(username="items-user").delete()
                s.commit()

    def test_migration_0030_backfills_existing_findings(tmp_path):
        """Run the real alembic chain on a fresh SQLite file: stop at 0029, add a
        food finding the old way, then upgrade and check its items appear."""
        import sqlalchemy as sa
        db = tmp_path / "mig.db"
        env = {**os.environ, "TX_DATABASE_URL": f"sqlite:///{db}"}

        def alembic(*args):
            r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stderr[-2000:]

        alembic("upgrade", "0029")
        eng = sa.create_engine(f"sqlite:///{db}")
        import json
        with eng.begin() as c:
            c.execute(sa.text(
                "INSERT INTO discovery_findings (id, trip_id, topic, scope, place_key, status, "
                "data, generated_at) VALUES ('f1', 't1', 'food', 'city', 'MA:Harar', "
                "'draft', :d, '2026-09-27')"), {"d": json.dumps(FOOD)})
        alembic("upgrade", "head")
        with eng.begin() as c:
            names = {r[0] for r in c.execute(sa.text(
                "SELECT name FROM discovery_items WHERE finding_id = 'f1'"))}
        assert names == {"Café Meskel", "Nomad", "Weird"}


def test_event_kind_from_the_model_wins_for_notable_items():
    rows = {r["name"]: r for r in items.rows_from_finding("events", {
        "impacts": [{"name": "Strike", "kind": "activity", "source_url": "https://e.x/1"}],
        "notable": [{"name": "Souk tour", "date_range": "2026-10-14", "kind": "activity",
                     "venue": "Medina", "why": "x", "source_url": "https://e.x/2"}]})}
    assert rows["Strike"]["kind"] == "event"            # a heads-up is always an event
    assert rows["Souk tour"]["kind"] == "activity" and rows["Souk tour"]["venue"] == "Medina"
