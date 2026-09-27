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
    from app.models import (Base, Trip, TripActivity, TravelerIdentity,
                            TripTraveler)
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


def test_health_reports_version():
    if not DEPS:
        return
    # Assert against the configured version so a release bump doesn't break this.
    from app.config import settings
    assert client.get("/health").json()["version"] == settings.app_version


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

    # Defaults to the Upcoming tab: in-progress + upcoming, never past.
    html = client.get("/trips").text
    for label in ("In progress", "Upcoming", "Past"):
        assert label in html
    assert "Now trip" in html and "Upcoming trip" in html
    assert "Past trip" not in html

    only_past = client.get("/trips/list", params={"status": "past"}).text
    assert "Past trip" in only_past
    assert "Upcoming trip" not in only_past and "Now trip" not in only_past
    # Past trips are grouped by year.
    assert f">{(TODAY - timedelta(days=40)).year}<" in only_past


def test_search_miss_points_to_other_tab():
    if not DEPS:
        return
    _clear()
    _mk("Old Kyoto", (TODAY - timedelta(days=400)).isoformat(),
        (TODAY - timedelta(days=395)).isoformat())
    html = client.get("/trips/list", params={"q": "kyoto"}).text
    assert "No upcoming trips match your search" in html
    assert "1 in Past" in html


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
        "activity_kind": "flight", "when": "2026-08-15T09:00", "endWhen": "2026-08-15T11:30",
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
        "activity_kind": "flight", "when": "2026-08-16T09:00", "airline": "Air Canada",
        "flightNumber": "AC4826"})
    assert r.headers.get("HX-Redirect") == f"/trips/{tid}"
    with SessionLocal() as s:
        assert s.get(TripActivity, a.id).starts_at == "2026-08-16T09:00"

    # delete
    r = client.post(f"/trips/{tid}/activity/{a.id}/delete")
    assert r.headers.get("HX-Redirect") == f"/trips/{tid}"
    with SessionLocal() as s:
        assert s.query(TripActivity).filter(TripActivity.trip_id == tid).count() == 0


def test_lodging_with_type_field_saves_as_lodging_not_flight():
    if not DEPS:
        return
    # Regression: the lodging detail field is named "type", same as the activity
    # selector. Before the fix, form.get("type") returned the detail field (last
    # duplicate), resolving to DEFAULT_TYPE=flight — every manual lodging saved as
    # a flight. The selector is now `activity_kind`.
    # The real post-fix form has distinct keys: activity_kind (selector) + type
    # (lodging sub-type). Before the fix both were "type" and the selector lost.
    tid = _mk("Lodging trip", "2026-10-05", "2026-10-12")
    r = client.post(f"/trips/{tid}/activity", data={
        "activity_kind": "lodging", "when": "2026-10-08T15:00",
        "name": "Inn of the Sandpiper", "type": "hotel",
        "address": "113 Washington Ave"})
    assert r.headers.get("HX-Redirect") == f"/trips/{tid}"
    with SessionLocal() as s:
        a = s.query(TripActivity).filter(TripActivity.trip_id == tid).one()
        assert a.type == "lodging"                      # not "flight"
        assert a.values["name"] == "Inn of the Sandpiper"
        assert a.values["type"] == "hotel"              # sub-type preserved
        assert a.starts_at == "2026-10-08T15:00"

    # editing a (mis-saved) activity to lodging now sticks
    r = client.post(f"/trips/{tid}/activity/{a.id}", data={
        "activity_kind": "lodging", "when": "2026-10-08T15:00",
        "name": "Sandpiper", "type": "resort"})
    assert r.headers.get("HX-Redirect") == f"/trips/{tid}"
    with SessionLocal() as s:
        a = s.query(TripActivity).filter(TripActivity.trip_id == tid).one()
        assert a.type == "lodging" and a.values["type"] == "resort"


def test_activity_fields_swap_reads_activity_kind():
    if not DEPS:
        return
    html = client.get("/trips/x/activity/fields", params={"activity_kind": "lodging"}).text
    assert "Property name" in html and 'name="name"' in html


def test_undated_activity_groups_last():
    if not DEPS:
        return
    tid = _mk("Mixed trip", "2026-08-14", "2026-08-18")
    client.post(f"/trips/{tid}/activity",
                data={"activity_kind": "note", "title": "Pack passports"})           # no when
    client.post(f"/trips/{tid}/activity",
                data={"activity_kind": "lodging", "when": "2026-08-14T15:00", "name": "Hotel Nordljos"})
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
    link = next(f for f in fields if f["label"] == "Source")
    assert link["is_link"] is True
    # The Source may be plain text — an email has no URL (owner, 2026-09-26)
    text = next(f for f in activity_types.detail_fields(
        "flight", {"sourceLink": "Email: Your Delta booking"}) if f["key"] == "sourceLink")
    assert text["is_link"] is False


def test_activity_map_url_per_type():
    if not DEPS:
        return
    m = activity_types.map_url
    # flight -> departure airport search
    u = m("flight", {"from": "YYZ", "to": "LIS"})
    assert u.startswith("https://www.google.com/maps/search/") and "YYZ%20airport" in u
    # lodging -> name + address
    assert "Sandpiper" in m("lodging", {"name": "Inn of the Sandpiper", "address": "113 Washington"})
    # event -> venue
    assert "Meow%20Wolf" in m("event", {"name": "Show", "venue": "Meow Wolf"})
    # restaurant -> name + address
    assert "Geronimo" in m("dining", {"name": "Geronimo", "address": "724 Canyon Rd"})
    # train -> station; cruise -> port
    assert "station" in m("train", {"from": "Gare du Nord"})
    assert "terminal" in m("cruise", {"departurePort": "Miami"})
    # road trip prefers a shared Google Maps directions URL
    assert m("drive", {"sourceLink": "https://www.google.com/maps/dir/A/B"}) == \
        "https://www.google.com/maps/dir/A/B"
    # road trip without a maps url falls back to searching the route
    assert "Toronto" in m("drive", {"route": "Toronto to Montreal",
                                    "sourceLink": "https://example.com/doc.pdf"})
    # note has no location
    assert m("note", {"title": "Pack passports"}) is None
    # a locationless lodging returns None
    assert m("lodging", {}) is None


def test_unknown_type_falls_back():
    if not DEPS:
        return
    assert activity_types.get_type("zzz").key == activity_types.DEFAULT_TYPE


def test_activity_fields_partial_reflects_type():
    if not DEPS:
        return
    tid = _mk("Partial trip")
    html = client.get(f"/trips/{tid}/activity/fields", params={"activity_kind": "lodging"}).text
    assert 'name="name"' in html and "Property name" in html
    assert 'name="flightNumber"' not in html          # not a lodging field


# ------------------------------------------------------------------ travelers (3.2)

def _clear_travelers():
    with SessionLocal() as s:
        s.query(TripTraveler).delete()
        s.query(TravelerIdentity).delete()
        s.query(Trip).delete()
        s.commit()


def _mk_identity(name, kind="guest", nationality=None):
    with SessionLocal() as s:
        t = TravelerIdentity(display_name=name, kind=kind, nationality=nationality)
        s.add(t)
        s.commit()
        return t.id


def test_create_trip_auto_links_self():
    if not DEPS:
        return
    _clear_travelers()
    me = _mk_identity("Me", kind="self", nationality="CA")
    client.post("/trips", data={"name": "Solo trip", "start": "", "end": "",
                                "notes": ""})
    with SessionLocal() as s:
        tid = s.query(Trip).filter(Trip.name == "Solo trip").one().id
        rows = s.query(TripTraveler).filter(TripTraveler.trip_id == tid).all()
        assert len(rows) == 1
        assert rows[0].traveler_id == me and rows[0].role == "self"


def test_create_trip_without_self_links_no_one():
    if not DEPS:
        return
    _clear_travelers()
    _mk_identity("A guest", kind="guest")
    client.post("/trips", data={"name": "No-self trip", "start": "", "end": "",
                                "notes": ""})
    with SessionLocal() as s:
        tid = s.query(Trip).filter(Trip.name == "No-self trip").one().id
        assert s.query(TripTraveler).filter(
            TripTraveler.trip_id == tid).count() == 0


def test_detail_shows_traveler_roster():
    if not DEPS:
        return
    _clear_travelers()
    _mk_identity("Traveler One", kind="self", nationality="CA")
    client.post("/trips", data={"name": "Roster trip", "start": "", "end": "",
                                "notes": ""})
    with SessionLocal() as s:
        tid = s.query(Trip).filter(Trip.name == "Roster trip").one().id
    html = client.get(f"/trips/{tid}").text
    assert "Who's traveling" in html
    assert "Traveler One" in html and ">You<" in html
    assert "CA" in html                                  # nationality tag


def test_add_and_remove_companion():
    if not DEPS:
        return
    _clear_travelers()
    _mk_identity("Owner", kind="self")
    companion = _mk_identity("Companion Carol", kind="guest")
    client.post("/trips", data={"name": "Duo trip", "start": "", "end": "",
                                "notes": ""})
    with SessionLocal() as s:
        tid = s.query(Trip).filter(Trip.name == "Duo trip").one().id

    # add the companion — the section partial comes back with both names
    r = client.post(f"/trips/{tid}/travelers", data={"traveler_id": companion})
    assert r.status_code == 200
    assert "Companion Carol" in r.text and "Companion" in r.text
    with SessionLocal() as s:
        assert s.query(TripTraveler).filter(
            TripTraveler.trip_id == tid).count() == 2

    # a duplicate add is a no-op
    client.post(f"/trips/{tid}/travelers", data={"traveler_id": companion})
    with SessionLocal() as s:
        assert s.query(TripTraveler).filter(
            TripTraveler.trip_id == tid).count() == 2

    # remove the companion — gone from the roster list (she reappears in the
    # picker dropdown, which is correct, so assert on the roster markup itself)
    r = client.post(f"/trips/{tid}/travelers/{companion}/remove")
    assert r.status_code == 200
    assert 'yn-traveler-name">Companion Carol<' not in r.text
    with SessionLocal() as s:
        left = s.query(TripTraveler).filter(TripTraveler.trip_id == tid).all()
        assert len(left) == 1 and left[0].role == "self"


def test_add_guest_by_name_mints_identity():
    if not DEPS:
        return
    _clear_travelers()
    _mk_identity("Owner4", kind="self")
    client.post("/trips", data={"name": "Guest trip", "start": "", "end": "",
                                "notes": ""})
    with SessionLocal() as s:
        tid = s.query(Trip).filter(Trip.name == "Guest trip").one().id
    r = client.post(f"/trips/{tid}/travelers/new", data={"name": "  Casual Pat  "})
    assert r.status_code == 200
    assert "Casual Pat" in r.text and "Companion" in r.text
    with SessionLocal() as s:
        pat = s.query(TravelerIdentity).filter(
            TravelerIdentity.display_name == "Casual Pat").one()   # trimmed, minted
        assert pat.kind == "guest" and pat.nationality is None
        assert pat.generic is True                                  # D-100 placeholder
        assert s.query(TripTraveler).filter(
            TripTraveler.trip_id == tid).count() == 2               # self + guest


def test_add_guest_reuses_existing_name():
    if not DEPS:
        return
    _clear_travelers()
    _mk_identity("Owner5", kind="self")
    existing = _mk_identity("Jordan Lee", kind="guest")
    client.post("/trips", data={"name": "Reuse trip", "start": "", "end": "",
                                "notes": ""})
    with SessionLocal() as s:
        tid = s.query(Trip).filter(Trip.name == "Reuse trip").one().id
    client.post(f"/trips/{tid}/travelers/new", data={"name": "Jordan Lee"})
    with SessionLocal() as s:
        # no duplicate identity created; the existing one is linked
        assert s.query(TravelerIdentity).filter(
            TravelerIdentity.display_name == "Jordan Lee").count() == 1
        jordan = s.get(TravelerIdentity, existing)
        assert jordan.generic is False          # reuse never flips a real record
        row = s.query(TripTraveler).filter(
            TripTraveler.trip_id == tid,
            TripTraveler.traveler_id == existing).one()
        assert row.role == "companion"


def test_discovery_travelers_excludes_generic_guests():
    if not DEPS:
        return
    _clear_travelers()
    me = _mk_identity("Owner7", kind="self", nationality="CA")
    real = _mk_identity("Real Companion", kind="guest")     # generic defaults False
    client.post("/trips", data={"name": "Consider trip", "start": "", "end": "",
                                "notes": ""})
    with SessionLocal() as s:
        tid = s.query(Trip).filter(Trip.name == "Consider trip").one().id
    client.post(f"/trips/{tid}/travelers", data={"traveler_id": real})
    client.post(f"/trips/{tid}/travelers/new", data={"name": "Placeholder Guest"})
    with SessionLocal() as s:
        considered = {t.display_name for t in
                      trips_routes.discovery_travelers(s, tid)}
        # self + the real companion are in; the generic guest is filtered out
        assert considered == {"Owner7", "Real Companion"}
        assert "Placeholder Guest" not in considered


def test_add_guest_blank_name_is_noop():
    if not DEPS:
        return
    _clear_travelers()
    _mk_identity("Owner6", kind="self")
    client.post("/trips", data={"name": "Blank trip", "start": "", "end": "",
                                "notes": ""})
    with SessionLocal() as s:
        tid = s.query(Trip).filter(Trip.name == "Blank trip").one().id
        before = s.query(TravelerIdentity).count()
    r = client.post(f"/trips/{tid}/travelers/new", data={"name": "   "})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.query(TravelerIdentity).count() == before          # nothing minted
        assert s.query(TripTraveler).filter(
            TripTraveler.trip_id == tid).count() == 1               # only self


def test_add_traveler_rejects_bad_id():
    if not DEPS:
        return
    _clear_travelers()
    _mk_identity("Owner2", kind="self")
    client.post("/trips", data={"name": "Guard trip", "start": "", "end": "",
                                "notes": ""})
    with SessionLocal() as s:
        tid = s.query(Trip).filter(Trip.name == "Guard trip").one().id
    # non-numeric and non-existent ids never create a row
    client.post(f"/trips/{tid}/travelers", data={"traveler_id": "abc"})
    client.post(f"/trips/{tid}/travelers", data={"traveler_id": "99999"})
    with SessionLocal() as s:
        assert s.query(TripTraveler).filter(
            TripTraveler.trip_id == tid).count() == 1     # only self


def test_delete_trip_sweeps_travelers():
    if not DEPS:
        return
    _clear_travelers()
    _mk_identity("Owner3", kind="self")
    client.post("/trips", data={"name": "Sweep trip", "start": "", "end": "",
                                "notes": ""})
    with SessionLocal() as s:
        tid = s.query(Trip).filter(Trip.name == "Sweep trip").one().id
    client.post(f"/trips/{tid}/delete")
    with SessionLocal() as s:
        assert s.query(TripTraveler).filter(
            TripTraveler.trip_id == tid).count() == 0


# ------------------------------------------------------ currency finding edit

def test_currency_edit_updates_conversion_numbers():
    """Editing a currency finding rewrites data.rates (what the calculator reads)
    and regenerates the body — not a text-only edit (the fixed bug)."""
    if not DEPS:
        return
    from app.db import SessionLocal
    from app.models import DiscoveryFinding
    tid = _mk("Rates trip")
    with SessionLocal() as s:
        f = DiscoveryFinding(
            trip_id=tid, topic="currency", scope="country", place_key="CH",
            status="draft", confidence="high", resolver="currency.frankfurter",
            body_md=("**Geneva, CH** — official currency **Swiss Franc (CHF)**.\n\n"
                     "- 1 CAD = 0.58 CHF\n- 1 USD = 0.72 CHF\n"),
            data={"currency": "CHF", "currency_name": "Swiss Franc",
                  "date": "2026-08-13", "bases": ["CAD", "USD"],
                  "rates": {"CHF": {"per_CAD": 0.58, "per_USD": 0.72}},
                  "usd_useful": False})
        s.add(f)
        s.commit()
        fid = f.id

    r = client.post(f"/trips/{tid}/finding/{fid}/edit",
                    data={"cur_base": ["CAD", "USD"], "cur_rate": ["0.60", "0.75"]})
    assert r.status_code < 400

    with SessionLocal() as s:
        f = s.get(DiscoveryFinding, fid)
        assert f.status == "edited"
        # The numbers the calculator computes from now reflect the edit.
        assert f.data["rates"]["CHF"] == {"per_CAD": 0.60, "per_USD": 0.75}
        # Body regenerated from those numbers, so text and math agree.
        assert "1 CAD = 0.6 CHF" in f.body_md
        assert "1 USD = 0.75 CHF" in f.body_md
        assert "manually edited" in f.body_md


def test_currency_edit_blank_rate_drops_it():
    """A blank rate row drops that rate; clearing all rates flags the finding."""
    if not DEPS:
        return
    from app.db import SessionLocal
    from app.models import DiscoveryFinding
    tid = _mk("Drop-rate trip")
    with SessionLocal() as s:
        f = DiscoveryFinding(
            trip_id=tid, topic="currency", scope="country", place_key="CH",
            status="draft", confidence="high", resolver="currency.frankfurter",
            body_md="**Geneva, CH** — official currency **Swiss Franc (CHF)**.",
            data={"currency": "CHF", "currency_name": "Swiss Franc",
                  "bases": ["CAD", "USD"],
                  "rates": {"CHF": {"per_CAD": 0.58, "per_USD": 0.72}},
                  "usd_useful": False})
        s.add(f)
        s.commit()
        fid = f.id

    # Keep CAD, blank out USD.
    client.post(f"/trips/{tid}/finding/{fid}/edit",
                data={"cur_base": ["CAD", "USD"], "cur_rate": ["0.60", ""]})
    with SessionLocal() as s:
        f = s.get(DiscoveryFinding, fid)
        assert f.data["rates"]["CHF"] == {"per_CAD": 0.60}
        assert f.confidence == "high"

    # Blank both — no rates left, finding is flagged.
    client.post(f"/trips/{tid}/finding/{fid}/edit",
                data={"cur_base": ["CAD", "USD"], "cur_rate": ["", ""]})
    with SessionLocal() as s:
        f = s.get(DiscoveryFinding, fid)
        assert f.data["rates"] == {}
        assert f.confidence == "flagged"


# ------------------------------ base currencies from the travel profile (4.0)

def _mk_traveller_on_trip(trip_id, name, base_currency=None):
    """A TravelerIdentity (optionally with a chosen base currency) linked to a trip."""
    from app.db import SessionLocal
    from app.models import TravelerIdentity, TripTraveler
    with SessionLocal() as s:
        t = TravelerIdentity(display_name=name, kind="guest",
                             base_currency=base_currency)
        s.add(t)
        s.commit()
        s.add(TripTraveler(trip_id=trip_id, traveler_id=t.id, role="companion"))
        s.commit()
        return t.id


def test_base_currencies_profile_first_then_env():
    """The viewer's EXPLICIT pick leads, and the deployment's always-on
    currencies (TX_DISCOVERY_BASE_CURRENCIES) are unioned in for extra
    visibility — the Cuba case: profile stays put, USD still shown."""
    if not DEPS:
        return
    from types import SimpleNamespace
    from app.config import settings
    from app.trips.routes import _base_currencies
    bases = _base_currencies(SimpleNamespace(base_currency="GBP"))
    assert bases[0] == "GBP"                                    # pick leads
    assert set(bases) == {"GBP"} | set(settings.base_currency_list)  # + env extras


def test_base_currencies_are_the_viewers_not_the_whole_roster():
    """4.1: Discover is per person — a companion's currency doesn't leak into
    mine. Only my own pick (plus the deployment extras) is used."""
    if not DEPS:
        return
    from types import SimpleNamespace
    from app.config import settings
    from app.trips.routes import _base_currencies
    mine = _base_currencies(SimpleNamespace(base_currency="JPY"))
    assert "JPY" in mine and ("GBP" not in mine or "GBP" in settings.base_currency_list)


def test_base_currencies_env_only_when_no_pick():
    if not DEPS:
        return
    from types import SimpleNamespace
    from app.config import settings
    from app.trips.routes import _base_currencies
    assert _base_currencies(SimpleNamespace(base_currency=None)) == tuple(settings.base_currency_list)
    assert _base_currencies(None) == tuple(settings.base_currency_list)


def test_calculator_pivots_without_cad_home():
    """The calculator no longer hardcodes a CAD pivot: a GBP-only snapshot still
    builds a working conversion table."""
    if not DEPS:
        return
    from app.db import SessionLocal
    from app.models import DiscoveryFinding
    tid = _mk("GBP calc trip")
    with SessionLocal() as s:
        f = DiscoveryFinding(
            trip_id=tid, topic="currency", scope="country", place_key="CH",
            status="draft", confidence="high", resolver="currency.frankfurter",
            body_md="**Geneva, CH** — official currency **Swiss Franc (CHF)**.",
            data={"currency": "CHF", "currency_name": "Swiss Franc",
                  "date": "2026-08-13", "bases": ["GBP"],
                  "rates": {"CHF": {"per_GBP": 1.15}}, "usd_useful": False})
        s.add(f)
        s.commit()
        fid = f.id
    r = client.get(f"/trips/{tid}/finding/{fid}/calculator")
    assert r.status_code == 200
    # Pivot is GBP (the only home base) — no CAD involved, table still built.
    assert "GBP" in r.text and "CHF" in r.text
    assert '"GBP": 1.0' in r.text
    assert "1.15" in r.text
