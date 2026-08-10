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
    """Records create_collection calls; returns a canned (status, parsed, raw)."""
    def __init__(self, result):
        self.result = result
        self.calls = []

    def create_collection(self, name, start_date, end_date, *, is_public=False):
        self.calls.append({"name": name, "start": start_date, "end": end_date,
                           "is_public": is_public})
        return self.result


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
    monkeypatch.setattr(trips_routes, "_adventurelog", lambda: fake)
    r = client.post(f"/trips/{tid}/push-adventurelog")
    assert r.headers.get("HX-Redirect") == f"/trips/{tid}"
    assert fake.calls and fake.calls[0]["is_public"] is True     # PUBLIC (owner req.)
    assert fake.calls[0]["name"] == "Iceland"
    with SessionLocal() as s:
        assert s.get(Trip, tid).adventurelog_collection_id == "col_1"


def test_push_requires_dates(monkeypatch):
    if not DEPS:
        return
    tid = _mk_trip("No dates", None, None)
    fake = FakeAL((201, {"id": "x"}, ""))
    monkeypatch.setattr(trips_routes, "_adventurelog", lambda: fake)
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
    monkeypatch.setattr(trips_routes, "_adventurelog", lambda: fake)
    r = client.post(f"/trips/{tid}/push-adventurelog")
    assert r.headers.get("HX-Redirect") == f"/trips/{tid}"
    assert fake.calls == []                                       # no second collection
    with SessionLocal() as s:
        assert s.get(Trip, tid).adventurelog_collection_id == "existing"


def test_push_reports_adventurelog_rejection(monkeypatch):
    if not DEPS:
        return
    tid = _mk_trip("Rejected", "2026-01-01", "2026-01-05")
    monkeypatch.setattr(trips_routes, "_adventurelog",
                        lambda: FakeAL((400, {"detail": "bad"}, "bad")))
    r = client.post(f"/trips/{tid}/push-adventurelog")
    assert "HX-Redirect" not in r.headers and "400" in r.text
    with SessionLocal() as s:
        assert s.get(Trip, tid).adventurelog_collection_id is None


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
    monkeypatch.setattr(settings, "adventurelog_public_url", "https://al.example.com")
    tid = _mk_trip("Linked view", "2026-01-01", "2026-01-05", collection_id="col_9")
    html = client.get(f"/trips/{tid}").text
    assert 'href="https://al.example.com/collections/col_9"' in html
    assert "View in AdventureLog" in html


def test_push_button_disabled_without_dates(monkeypatch):
    if not DEPS:
        return
    monkeypatch.setattr(settings, "adventurelog_api_key", "k")   # "configured"
    monkeypatch.setattr(settings, "adventurelog_public_url", "")
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
