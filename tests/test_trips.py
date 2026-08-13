"""Trips (Yonder 2.0) — the in-house trip manager.

Covers the model + migration-equivalent schema, the CRUD routes (which answer
with an HX-Redirect rather than a body, so we assert on the stored rows), status
grouping, the day-grouped timeline, and the activity-type registry.
"""
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
    from app.trips import routes as trips_routes
    from app.trips import types as activity_types
    Base.metadata.create_all(engine)
    client = TestClient(app)

TODAY = date.today()


def _clear():
    with SessionLocal() as s:
        s.query(TripActivity).delete()
        s.query(Trip).delete()
        s.commit()


def _mk(name, start=None, end=None, notes=None, source="manual"):
    with SessionLocal() as s:
        t = Trip(name=name, start_date=start, end_date=end, notes=notes,
                 source=source)
        s.add(t)
        s.commit()
        return t.id


# ------------------------------------------------------------------ schema

def test_schema_has_trip_tables():
    if not DEPS:
        return
    from sqlalchemy import inspect
    names = set(inspect(engine).get_table_names())
    assert {"trips", "trip_activities"} <= names


def test_health_reports_2_0():
    if not DEPS:
        return
    assert client.get("/health").json()["version"] == "2.3.1"


def test_nav_shows_trips_link():
    if not DEPS:
        return
    html = client.get("/trips").text
    assert 'href="/trips"' in html and ">Trips<" in html


# ------------------------------------------------------------------ trip CRUD

def test_create_trip_persists_and_redirects():
    if not DEPS:
        return
    r = client.post("/trips", data={"name": "  Iceland Ring Road  ",
                                     "start": "2026-09-01", "end": "2026-09-10",
                                     "notes": " Ring road ", "next": "/trips"})
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/trips"
    with SessionLocal() as s:
        t = s.query(Trip).filter(Trip.name == "Iceland Ring Road").one()
        assert (t.start_date, t.end_date, t.notes) == ("2026-09-01", "2026-09-10", "Ring road")
        assert t.source == "manual"


def test_create_trip_requires_name():
    if not DEPS:
        return
    before = None
    with SessionLocal() as s:
        before = s.query(Trip).count()
    r = client.post("/trips", data={"name": "   ", "start": "", "end": "", "notes": ""})
    assert r.status_code == 200
    assert "HX-Redirect" not in r.headers          # stayed on the modal
    assert "A trip needs a name." in r.text
    with SessionLocal() as s:
        assert s.query(Trip).count() == before      # nothing created


def test_update_and_delete_trip():
    if not DEPS:
        return
    tid = _mk("Old name", "2026-01-01", "2026-01-05")
    r = client.post(f"/trips/{tid}", data={"name": "New name",
                                           "start": "2026-01-02", "end": "2026-01-06",
                                           "notes": "", "next": f"/trips/{tid}"})
    assert r.headers.get("HX-Redirect") == f"/trips/{tid}"
    with SessionLocal() as s:
        assert s.get(Trip, tid).name == "New name"
    r = client.post(f"/trips/{tid}/delete")
    assert r.headers.get("HX-Redirect") == "/trips"
    with SessionLocal() as s:
        assert s.get(Trip, tid) is None


# ------------------------------------------------------------------ grouping

def test_status_grouping_and_filter():
    if not DEPS:
        return
    _clear()
    _mk("Past trip", (TODAY - timedelta(days=40)).isoformat(),
        (TODAY - timedelta(days=30)).isoformat())
    _mk("Upcoming trip", (TODAY + timedelta(days=30)).isoformat(),
        (TODAY + timedelta(days=40)).isoformat())
    _mk("Now trip", (TODAY - timedelta(days=1)).isoformat(),
        (TODAY + timedelta(days=1)).isoformat())

    html = client.get("/trips").text
    for label in ("In progress", "Upcoming", "Past"):
        assert label in html

    only_past = client.get("/trips/list", params={"status": "past"}).text
    assert "Past trip" in only_past
    assert "Upcoming trip" not in only_past and "Now trip" not in only_past


def test_search_filters_by_name():
    if not DEPS:
        return
    _clear()
    _mk("Reykjavik weekend", (TODAY + timedelta(days=5)).isoformat(),
        (TODAY + timedelta(days=7)).isoformat())
    _mk("Tokyo autumn", (TODAY + timedelta(days=5)).isoformat(),
        (TODAY + timedelta(days=9)).isoformat())
    html = client.get("/trips/list", params={"q": "tokyo"}).text
    assert "Tokyo autumn" in html and "Reykjavik weekend" not in html


# ------------------------------------------------------------------ activities

def test_activity_crud_and_timeline():
    if not DEPS:
        return
    tid = _mk("Activity trip", "2026-08-14", "2026-08-18")
    # create a flight
    r = client.post(f"/trips/{tid}/activity", data={
        "type": "flight", "when": "2026-08-15T09:00", "endWhen": "2026-08-15T11:30",
        "airline": "Air Canada", "flightNumber": "AC4826", "from": "YYZ", "to": "KEF",
        "sourceLink": "https://example.com/eticket"})
    assert r.headers.get("HX-Redirect") == f"/trips/{tid}"
    with SessionLocal() as s:
        a = s.query(TripActivity).filter(TripActivity.trip_id == tid).one()
        assert a.type == "flight"
        assert a.starts_at == "2026-08-15T09:00"     # denormalized from values.when
        assert a.values["airline"] == "Air Canada"

    detail = client.get(f"/trips/{tid}").text
    assert "Air Canada AC4826" in detail             # registry title
    assert "YYZ → KEF" in detail                      # registry subtitle
    assert "Aug 15, 2026" in detail                   # day-group label

    # edit: move the flight to a new day -> starts_at + grouping follow
    r = client.post(f"/trips/{tid}/activity/{a.id}", data={
        "type": "flight", "when": "2026-08-16T09:00", "airline": "Air Canada",
        "flightNumber": "AC4826"})
    assert r.headers.get("HX-Redirect") == f"/trips/{tid}"
    with SessionLocal() as s:
        assert s.get(TripActivity, a.id).starts_at == "2026-08-16T09:00"

    # delete
    r = client.post(f"/trips/{tid}/activity/{a.id}/delete")
    assert r.headers.get("HX-Redirect") == f"/trips/{tid}"
    with SessionLocal() as s:
        assert s.query(TripActivity).filter(TripActivity.trip_id == tid).count() == 0


def test_undated_activity_groups_last():
    if not DEPS:
        return
    tid = _mk("Mixed trip", "2026-08-14", "2026-08-18")
    client.post(f"/trips/{tid}/activity",
                data={"type": "note", "title": "Pack passports"})           # no when
    client.post(f"/trips/{tid}/activity",
                data={"type": "lodging", "when": "2026-08-14T15:00", "name": "Hotel Nordljos"})
    html = client.get(f"/trips/{tid}").text
    assert "Date not set" in html and "Hotel Nordljos" in html
    # the dated day-group renders before the undated bucket
    assert html.index("Aug 14, 2026") < html.index("Date not set")


def test_delete_trip_cascades_activities():
    if not DEPS:
        return
    tid = _mk("Cascade trip", "2026-08-14", "2026-08-18")
    client.post(f"/trips/{tid}/activity",
                data={"type": "note", "title": "n", "when": "2026-08-15T10:00"})
    with SessionLocal() as s:
        aid = s.query(TripActivity).filter(TripActivity.trip_id == tid).one().id
    client.post(f"/trips/{tid}/delete")
    with SessionLocal() as s:
        assert s.get(TripActivity, aid) is None


# ------------------------------------------------------------------ registry

def test_type_registry_titles_and_fields():
    if not DEPS:
        return
    assert list(activity_types.TRIP_ACTIVITY_TYPES) == [
        "flight", "train", "drive", "carRental", "lodging", "tour", "event",
        "dining", "cruise", "note"]
    t = activity_types.get_type("lodging")
    assert t.title({"name": "Hotel Nordljos"}) == "Hotel Nordljos"
    assert t.title({}) == "Lodging"                   # fallback


def test_detail_fields_excludes_when_flags_link():
    if not DEPS:
        return
    fields = activity_types.detail_fields("flight", {
        "when": "2026-08-15T09:00", "airline": "AC", "sourceLink": "https://x",
        "notes": "hi"})
    labels = [f["label"] for f in fields]
    assert "Departure" not in labels                  # `when` becomes the time chip
    assert "Airline" in labels
    assert fields[-1]["label"] == "Notes"             # long text pushed to the end
    link = next(f for f in fields if f["label"] == "Source document")
    assert link["is_link"] is True


def test_unknown_type_falls_back():
    if not DEPS:
        return
    assert activity_types.get_type("zzz").key == activity_types.DEFAULT_TYPE


def test_activity_fields_partial_reflects_type():
    if not DEPS:
        return
    tid = _mk("Partial trip")
    html = client.get(f"/trips/{tid}/activity/fields", params={"type": "lodging"}).text
    assert 'name="name"' in html and "Property name" in html
    assert 'name="flightNumber"' not in html          # not a lodging field
