"""AdventureLog sync (Yonder 3.8) — push (marker dedup + note-overwrite confirm)
and pull (AdventureLog transport/lodging → Yonder Timeline).

Offline: a fake AdventureLog client; the AirTrail/Photon seams are stubbed so no
network is touched.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import pytest
    from fastapi.testclient import TestClient
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, DiscoveryFinding, Trip, TripActivity
    import app.trips.routes as R
    Base.metadata.create_all(engine)
    _client = TestClient(app)
    DEPS = True
except ImportError:
    DEPS = False


if DEPS:
    @pytest.fixture(autouse=True)
    def _cleanup_after():
        """Leave the shared SQLite DB clean — the pull test writes a real
        TripActivity, and other suites assert on global row counts."""
        yield
        with SessionLocal() as s:
            s.query(DiscoveryFinding).delete()
            s.query(TripActivity).delete()
            s.query(Trip).delete()
            s.commit()


# ------------------------------------------------------------- pure helpers

def test_marker_index_parses_all_types():
    collection = {
        "transportations": [{"id": 1, "name": "Flight",
                             "description": "seat 4A\n\n<!-- yonder:activity:t1 -->"}],
        "lodging": [{"id": 2, "name": "Hotel",
                     "description": "<!-- yonder:activity:l1 -->"}],
        "notes": [{"id": 3, "name": "Food — MA",
                   "content": "body\n<!-- yonder:discovery:food:ET:Harar -->"},
                  {"id": 4, "name": "unmarked note", "content": "no marker here"}],
    }

    class _C:
        def get_collection(self, cid):
            return (200, collection, "")

    idx = R._al_collection_index(_C(), "9")
    assert idx["yonder:activity:t1"] == {"kind": "transportation", "id": "1", "name": "Flight"}
    assert idx["yonder:activity:l1"]["kind"] == "lodging"
    assert idx["yonder:discovery:food:ET:Harar"]["id"] == "3"
    assert len(idx) == 3                                  # the unmarked note is ignored


def test_inject_marker_into_note_and_transport():
    note = R._inject_marker("note", {"content": "hello"}, "yonder:discovery:food:MA")
    assert note["content"].endswith("<!-- yonder:discovery:food:MA -->")
    tr = R._inject_marker("transportation", {}, "yonder:activity:x")
    assert tr["description"] == "<!-- yonder:activity:x -->"


def test_target_kind():
    assert R._activity_target_kind("flight") == "transportation"
    assert R._activity_target_kind("lodging") == "lodging"
    assert R._activity_target_kind("dining") == "note"


def test_from_al_iso_drops_midnight():
    assert R._from_al_iso("2026-10-02T00:00:00Z") == "2026-10-02"
    assert R._from_al_iso("2026-10-02T14:30:00Z") == "2026-10-02T14:30"
    assert R._from_al_iso(None) == ""


def test_al_transport_record_to_activity():
    rec = {"type": "plane", "name": "AC123", "date": "2026-10-02T09:00:00Z",
           "start_code": "YYZ", "end_code": "ADD", "flight_number": "AC123",
           "description": "window seat\n<!-- yonder:activity:x -->"}
    a = R._al_record_to_activity(rec, "transportation", "trip1")
    assert a.type == "flight" and a.trip_id == "trip1"
    assert a.values["from"] == "YYZ" and a.values["to"] == "ADD"
    assert a.values["flightNumber"] == "AC123"
    assert a.starts_at == "2026-10-02T09:00"
    assert a.values["notes"] == "window seat"            # marker stripped


def test_al_lodging_record_to_activity():
    rec = {"type": "hotel", "name": "Guesthouse", "check_in": "2026-10-02T00:00:00Z",
           "location": "Piassa", "reservation_number": "R9"}
    a = R._al_record_to_activity(rec, "lodging", "trip1")
    assert a.type == "lodging" and a.values["name"] == "Guesthouse"
    assert a.values["when"] == "2026-10-02" and a.values["address"] == "Piassa"


# ------------------------------------------------------------- routes (fake client)

class _FakeAL:
    def __init__(self, collection):
        self.collection = collection
        self.created, self.updated = [], []

    def get_collection(self, cid):
        return (200, self.collection, "")

    def create_note(self, body):
        self.created.append(("note", body)); return (201, {"id": "n"}, "")

    def create_lodging(self, body):
        self.created.append(("lodging", body)); return (201, {"id": "l"}, "")

    def create_transportation(self, body):
        self.created.append(("transportation", body)); return (201, {"id": "t"}, "")

    def update_note(self, rid, body):
        self.updated.append(("note", rid, body)); return (200, {}, "")

    def update_lodging(self, rid, body):
        self.updated.append(("lodging", rid, body)); return (200, {}, "")

    def update_transportation(self, rid, body):
        self.updated.append(("transportation", rid, body)); return (200, {}, "")


def _fresh_trip(collection_id="99"):
    with SessionLocal() as s:
        s.query(DiscoveryFinding).delete()
        s.query(TripActivity).delete()
        s.query(Trip).delete()
        s.commit()
        t = Trip(name="Ethiopia", start_date="2026-10-02", end_date="2026-10-11",
                 adventurelog_collection_id=collection_id)
        s.add(t)
        s.commit()
        tid = t.id
        f = DiscoveryFinding(trip_id=tid, topic="food", place_key="ET:Harar",
                             scope="city", status="draft", body_md="Eat tagine.")
        s.add(f)
        s.commit()
        return tid, f.id


def _wire(monkeypatch, fake):
    from app.integrations import Clients
    monkeypatch.setattr(R, "_al_clients",
                        lambda session, user: Clients(None, adventurelog=fake))
    monkeypatch.setattr(R, "_photon", lambda: None)


def test_push_new_note_creates(monkeypatch):
    if not DEPS:
        return
    tid, fid = _fresh_trip()
    fake = _FakeAL({"transportations": [], "lodging": [], "notes": []})
    _wire(monkeypatch, fake)
    r = _client.post(f"/trips/{tid}/adventurelog/push",
                     data={"item_ids": [f"discovery:{fid}"]})
    assert r.status_code == 200
    assert len(fake.created) == 1 and fake.created[0][0] == "note"
    body = fake.created[0][1]
    assert "yonder:discovery:food:ET:Harar" in body["content"]
    assert "Eat tagine." in body["content"]


def test_push_matching_note_needs_confirm_then_overwrites(monkeypatch):
    if not DEPS:
        return
    tid, fid = _fresh_trip()
    # collection already has the marked note
    fake = _FakeAL({"transportations": [], "lodging": [], "notes": [
        {"id": 55, "name": "Food & drink — ET:Harar",
         "content": "old\n<!-- yonder:discovery:food:ET:Harar -->"}]})
    _wire(monkeypatch, fake)
    # first push → warning panel, nothing written
    r1 = _client.post(f"/trips/{tid}/adventurelog/push",
                      data={"item_ids": [f"discovery:{fid}"]})
    assert "already exist" in r1.text and "overwrite" in r1.text.lower()
    assert not fake.created and not fake.updated
    # confirmed push → update in place, no duplicate create
    r2 = _client.post(f"/trips/{tid}/adventurelog/push",
                      data={"item_ids": [f"discovery:{fid}"], "confirm": "1"})
    assert r2.status_code == 200
    assert not fake.created
    assert fake.updated and fake.updated[0][:2] == ("note", "55")


def test_pull_creates_timeline_activity(monkeypatch):
    if not DEPS:
        return
    tid, _ = _fresh_trip()
    fake = _FakeAL({"transportations": [
        {"id": 7, "type": "plane", "name": "AC123", "date": "2026-10-02T09:00:00Z",
         "start_code": "YYZ", "end_code": "ADD"}], "lodging": [], "notes": []})
    _wire(monkeypatch, fake)
    r = _client.post(f"/trips/{tid}/adventurelog/pull",
                     data={"record_ids": ["transportation:7"]})
    assert r.status_code == 200
    with SessionLocal() as s:
        acts = s.query(TripActivity).filter_by(trip_id=tid).all()
        assert len(acts) == 1 and acts[0].type == "flight"
        assert acts[0].values["from"] == "YYZ"
