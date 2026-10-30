"""Trip ↔ AdventureLog link/push (Yonder 2.1).

Push creates a PUBLIC collection and links it; a missing-dates push is refused;
an already-linked trip is never re-pushed; a manually entered collection id
persists; and the detail page renders a direct link when a public base URL is
configured. The AdventureLog client is faked — these never touch a real server.
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
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, Trip
    from app.trips import routes as trips_routes
    Base.metadata.create_all(engine)
    client = TestClient(app)


class FakeAL:
    """Records create_* calls; returns a canned (status, parsed, raw) for the
    collection and a fixed 201 for every child object."""
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.transportations = []
        self.lodging = []
        self.notes = []

    def create_collection(self, name, start_date, end_date, *, is_public=False):
        self.calls.append({"name": name, "start": start_date, "end": end_date,
                           "is_public": is_public})
        return self.result

    def create_transportation(self, body):
        self.transportations.append(body)
        return (201, {"id": f"trans_{len(self.transportations)}"}, "")

    def create_lodging(self, body):
        self.lodging.append(body)
        return (201, {"id": f"lodge_{len(self.lodging)}"}, "")

    def create_note(self, body):
        self.notes.append(body)
        return (201, {"id": f"note_{len(self.notes)}"}, "")

    # Sync-page push (3.8) dedups against the collection's existing objects and can
    # update in place. An empty collection here means every push is a fresh create.
    def get_collection(self, collection_id):
        return (200, {"transportations": [], "lodging": [], "notes": []}, "")

    def update_transportation(self, rid, body):
        self.transportations.append(body)
        return (200, {}, "")

    def update_lodging(self, rid, body):
        self.lodging.append(body)
        return (200, {}, "")

    def update_note(self, rid, body):
        self.notes.append(body)
        return (200, {}, "")


def _sync_push_all(tid):
    """Push every one of the trip's activities via the 3.8 sync endpoint (the home
    of the activity→AdventureLog mapping since the create-collection button stopped
    seeding). Returns the HTTP response."""
    from app.models import TripActivity
    with SessionLocal() as s:
        ids = [f"activity:{a.id}" for a in
               s.query(TripActivity).filter_by(trip_id=tid).all()]
    return client.post(f"/trips/{tid}/adventurelog/push", data={"item_ids": ids})


class FakePhoton:
    """Records every geocode_freeform query; returns a canned result (or None)."""
    def __init__(self, result=None):
        self.result = result
        self.queries = []

    def geocode_freeform(self, text):
        self.queries.append(text)
        return self.result


def _no_geocoding(monkeypatch):
    """Push tests that don't care about coordinates get this: no Photon hit —
    keeps them off the network and deterministic (airports come from Run·dere's
    own table, D-147)."""
    monkeypatch.setattr(trips_routes, "_photon", lambda: FakePhoton(None))


def _connect(monkeypatch, fake, public_url=None):
    """The viewer has their own AdventureLog connection (D-150): the trip page
    uses `fake` as their client."""
    from app.integrations import Clients
    monkeypatch.setattr(trips_routes, "_al_clients",
                        lambda session, user: Clients(None, adventurelog=fake,
                                                      public_url=public_url))


def _mk_trip(name, start=None, end=None, collection_id=None):
    with SessionLocal() as s:
        t = Trip(name=name, start_date=start, end_date=end,
                 adventurelog_collection_id=collection_id, source="manual")
        s.add(t)
        s.commit()
        return t.id


# ------------------------------------------------------------------ push

def test_push_creates_public_collection_and_links(monkeypatch):
    if not DEPS:
        return
    tid = _mk_trip("Iceland", "2026-09-01", "2026-09-10")
    fake = FakeAL((201, {"id": "col_1"}, ""))
    _connect(monkeypatch, fake)
    r = client.post(f"/trips/{tid}/push-adventurelog")
    assert r.headers.get("HX-Redirect") == f"/trips/{tid}"
    assert fake.calls and fake.calls[0]["is_public"] is True     # PUBLIC (owner req.)
    assert fake.calls[0]["name"] == "Iceland"
    with SessionLocal() as s:
        assert s.get(Trip, tid).adventurelog_collection_id == "col_1"


def test_push_seeds_flight_as_transportation(monkeypatch):
    if not DEPS:
        return
    tid = _mk_trip("Iceland flights", "2026-09-01", "2026-09-10",
                   collection_id="col_flight")
    client.post(f"/trips/{tid}/activity", data={
        "activity_kind": "flight", "when": "2026-09-01T09:00", "endWhen": "2026-09-01T11:30",
        "airline": "Air Canada", "flightNumber": "AC4826", "from": "YYZ", "to": "KEF",
        "confirmation": "ABC123", "sourceLink": "https://example.com/eticket"})
    fake = FakeAL((201, {"id": "col_flight"}, ""))
    _connect(monkeypatch, fake)
    _no_geocoding(monkeypatch)
    r = _sync_push_all(tid)
    assert r.status_code == 200
    assert len(fake.transportations) == 1
    body = fake.transportations[0]
    assert body["type"] == "plane"
    assert body["flight_number"] == "AC4826"
    assert body["from_location"] == "YYZ" and body["to_location"] == "KEF"
    assert body["date"] == "2026-09-01T09:00:00Z"
    assert body["end_date"] == "2026-09-01T11:30:00Z"
    assert body["is_public"] is True
    assert body["collection"] == "col_flight"
    assert "ABC123" in body["description"]
    assert "yonder:activity:" in body["description"]      # ownership marker stamped
    assert fake.lodging == [] and fake.notes == []


def test_push_seeds_lodging_and_note(monkeypatch):
    if not DEPS:
        return
    tid = _mk_trip("Iceland stay", "2026-09-01", "2026-09-10", collection_id="col_stay")
    client.post(f"/trips/{tid}/activity", data={
        "activity_kind": "lodging", "when": "2026-09-01T15:00", "endWhen": "2026-09-05T11:00",
        "name": "Hotel Nordljos", "address": "Posthusstraeti 11", "confirmation": "H-99"})
    client.post(f"/trips/{tid}/activity", data={
        "activity_kind": "event", "when": "2026-09-02T20:00", "name": "Northern lights tour",
        "venue": "Reykjavik"})
    fake = FakeAL((201, {"id": "col_stay"}, ""))
    _connect(monkeypatch, fake)
    _no_geocoding(monkeypatch)
    r = _sync_push_all(tid)
    assert r.status_code == 200
    assert len(fake.lodging) == 1
    lodge = fake.lodging[0]
    assert lodge["name"] == "Hotel Nordljos" and lodge["reservation_number"] == "H-99"
    assert lodge["is_public"] is True and lodge["collection"] == "col_stay"
    assert len(fake.notes) == 1
    assert fake.notes[0]["name"] == "Northern lights tour"
    assert fake.transportations == []


def test_push_activity_failure_does_not_block_others(monkeypatch):
    if not DEPS:
        return
    tid = _mk_trip("Partial failure", "2026-09-01", "2026-09-10",
                   collection_id="col_partial")
    client.post(f"/trips/{tid}/activity", data={
        "activity_kind": "flight", "when": "2026-09-01T09:00", "flightNumber": "AC1",
        "from": "YYZ", "to": "KEF"})
    client.post(f"/trips/{tid}/activity", data={
        "activity_kind": "flight", "when": "2026-09-05T09:00", "flightNumber": "AC2",
        "from": "KEF", "to": "YYZ"})
    fake = FakeAL((201, {"id": "col_partial"}, ""))
    calls = {"n": 0}
    def flaky_create_transportation(body):
        calls["n"] += 1
        if calls["n"] == 1:
            return (400, {"detail": "bad"}, "bad")
        fake.transportations.append(body)
        return (201, {"id": "trans_ok"}, "")
    fake.create_transportation = flaky_create_transportation
    _connect(monkeypatch, fake)
    _no_geocoding(monkeypatch)
    r = _sync_push_all(tid)
    assert r.status_code == 200                                  # one bad item doesn't abort
    assert len(fake.transportations) == 1                       # second flight still pushed


def test_push_resolves_transportation_coordinates(monkeypatch):
    if not DEPS:
        return
    tid = _mk_trip("Geocoded flights", "2026-09-01", "2026-09-10", collection_id="col_geo")
    client.post(f"/trips/{tid}/activity", data={
        "activity_kind": "flight", "when": "2026-09-01T09:00", "flightNumber": "AC4826",
        "from": "YYZ", "to": "KEF"})
    fake = FakeAL((201, {"id": "col_geo"}, ""))
    _connect(monkeypatch, fake)
    # Both airports come from Run·dere's own table (D-147): no AirTrail, no Photon.
    fake_photon = FakePhoton(None)
    monkeypatch.setattr(trips_routes, "_photon", lambda: fake_photon)

    r = _sync_push_all(tid)
    assert r.status_code == 200
    body = fake.transportations[0]
    assert body["origin_latitude"] == 43.6772 and body["origin_longitude"] == -79.6306
    assert round(body["destination_latitude"], 1) == 64.0
    assert round(body["destination_longitude"], 1) == -22.6
    assert fake_photon.queries == []                # never needed Photon


def test_push_requires_dates(monkeypatch):
    if not DEPS:
        return
    tid = _mk_trip("No dates", None, None)
    fake = FakeAL((201, {"id": "x"}, ""))
    _connect(monkeypatch, fake)
    r = client.post(f"/trips/{tid}/push-adventurelog")
    assert "HX-Redirect" not in r.headers
    assert "start and end date" in r.text.lower()
    assert fake.calls == []                                       # never created
    with SessionLocal() as s:
        assert s.get(Trip, tid).adventurelog_collection_id is None


def test_push_is_idempotent_when_already_linked(monkeypatch):
    if not DEPS:
        return
    tid = _mk_trip("Linked", "2026-01-01", "2026-01-05", collection_id="existing")
    fake = FakeAL((201, {"id": "new"}, ""))
    _connect(monkeypatch, fake)
    r = client.post(f"/trips/{tid}/push-adventurelog")
    assert r.headers.get("HX-Redirect") == f"/trips/{tid}"
    assert fake.calls == []                                       # no second collection
    with SessionLocal() as s:
        assert s.get(Trip, tid).adventurelog_collection_id == "existing"


def test_push_reports_adventurelog_rejection(monkeypatch):
    if not DEPS:
        return
    tid = _mk_trip("Rejected", "2026-01-01", "2026-01-05")
    _connect(monkeypatch, FakeAL((400, {"detail": "bad"}, "bad")))
    r = client.post(f"/trips/{tid}/push-adventurelog")
    assert "HX-Redirect" not in r.headers and "400" in r.text
    with SessionLocal() as s:
        assert s.get(Trip, tid).adventurelog_collection_id is None



def test_push_failure_shows_no_server_text(monkeypatch):
    """CodeQL alert 40: an adapter error can quote the AdventureLog server's
    response, so it never reaches the page, escaped or not."""
    if not DEPS:
        return

    class Broken:
        def create_collection(self, *a, **k):
            raise OSError("GET https://al.example.test/api: <img src=x onerror=alert(1)>")

    tid = _mk_trip("Broken", "2026-01-01", "2026-01-05")
    _connect(monkeypatch, Broken())
    r = client.post(f"/trips/{tid}/push-adventurelog")
    assert "AdventureLog request failed" in r.text
    assert "<img" not in r.text and "al.example.test" not in r.text


def test_push_message_is_escaped():
    if not DEPS:
        return
    assert "&lt;b&gt;" in trips_routes._push_msg("<b>").body.decode()

# ------------------------------------------------------------------ manual id + link

def test_manual_collection_id_persists():
    if not DEPS:
        return
    r = client.post("/trips", data={"name": "Manual", "start": "", "end": "",
                                    "notes": "", "adventurelog_collection_id": "col_manual"})
    assert r.headers.get("HX-Redirect") == "/trips"
    with SessionLocal() as s:
        assert s.query(Trip).filter(Trip.name == "Manual").one() \
            .adventurelog_collection_id == "col_manual"


def test_detail_renders_link_when_public_url_set(monkeypatch):
    if not DEPS:
        return
    _connect(monkeypatch, FakeAL((200, {}, "")), public_url="https://al.example.com")
    tid = _mk_trip("Linked view", "2026-01-01", "2026-01-05", collection_id="col_9")
    html = client.get(f"/trips/{tid}").text
    # A linked trip now leads with the managed sync page, plus a direct "View".
    assert f'/trips/{tid}/adventurelog' in html and "AdventureLog sync" in html
    assert 'href="https://al.example.com/collections/col_9"' in html


def test_push_button_disabled_without_dates(monkeypatch):
    if not DEPS:
        return
    _connect(monkeypatch, FakeAL((200, {}, "")))                  # connected
    tid = _mk_trip("Needs dates", None, None)
    html = client.get(f"/trips/{tid}").text
    assert "Push to AdventureLog" in html and "disabled" in html


# ------------------------------------------------------------------ adapter

def test_adapter_sends_is_public_only_when_asked():
    from adapters.targets.adventurelog import AdventureLogClient
    c = AdventureLogClient("http://x", "key")
    seen = {}
    c._request = lambda m, p, body=None: (seen.update(body or {}) or (201, {"id": "1"}, ""))
    c.create_collection("N", "2026-01-01", "2026-01-02", is_public=True)
    assert seen.get("is_public") is True
    seen.clear()
    c.create_collection("N", "2026-01-01", "2026-01-02")
    assert "is_public" not in seen                                # default private


def test_adventurelog_actions_sit_under_advanced(monkeypatch):
    """Run·dere is first-party: the optional AdventureLog sync/View/Push live in
    an Advanced menu, not as header buttons."""
    if not DEPS:
        return
    import re
    _connect(monkeypatch, FakeAL((200, {}, "")), public_url="https://al.example.com")
    tid = _mk_trip("Advanced linked", "2026-01-01", "2026-01-05", collection_id="col_adv")
    html = client.get(f"/trips/{tid}").text
    menu = re.search(r'<details class="yn-menu">\s*<summary[^>]*>Advanced</summary>(.*?)</details>',
                     html, re.S)
    assert menu and "AdventureLog sync" in menu.group(1) and "View in AdventureLog" in menu.group(1)
    assert html.count("AdventureLog sync") == 1               # not also a header button
    # Unlinked + connected: Push is under Advanced too.
    tid2 = _mk_trip("Advanced unlinked", "2026-01-01", "2026-01-05")
    html2 = client.get(f"/trips/{tid2}").text
    menu2 = re.search(r'<summary[^>]*>Advanced</summary>(.*?)</details>', html2, re.S)
    assert menu2 and "Push to AdventureLog" in menu2.group(1)


def test_no_advanced_menu_without_the_integration(monkeypatch):
    if not DEPS:
        return
    monkeypatch.setattr(settings, "adventurelog_api_key", "")
    tid = _mk_trip("Plain trip", "2026-01-01", "2026-01-05")
    assert ">Advanced</summary>" not in client.get(f"/trips/{tid}").text
    # Linked to a collection, but this viewer has no AdventureLog: still nothing.
    tid2 = _mk_trip("Linked, not mine", "2026-01-01", "2026-01-05", collection_id="col_x")
    assert ">Advanced</summary>" not in client.get(f"/trips/{tid2}").text
