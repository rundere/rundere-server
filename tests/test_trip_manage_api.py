"""5.1, YONDER-409: delete a trip, share and unshare it, and give an ownerless
trip its owner, from the apps.

The web's rules: the owner (or an admin, for an ownerless trip, D-211) shares
and deletes; only an admin assigns an owner, and only to an ownerless trip. A
stranger, or an admin on an owned trip (D-211), always gets 404; a viewer or a
traveller 403. Deleting keeps the trip's captures and journal entries, unlinked
(house rule 3). Fictional people; SQLite via conftest.
"""
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest                                               # noqa: E402

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

pytestmark = pytest.mark.skipif(not DEPS, reason="needs the app")

if DEPS:
    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, JournalEntry, TravelerIdentity, TravelItem, Trip,
                            TripMember, TripTraveler, User)

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

T = "/api/v1/trips"


@pytest.fixture(autouse=True)
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    yield


@pytest.fixture
def world():
    """Ana owns a trip Ben travels on and Vera may view; Sam is a stranger, Ada
    an admin, Cleo a spare account. Plus an ownerless trip."""
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as s:
        travs = {n: TravelerIdentity(display_name=f"TM {n} {tag}", kind="guest")
                 for n in ("ana", "ben")}
        s.add_all(travs.values())
        s.flush()
        users = {}
        for n in ("ana", "ben", "vera", "sam", "ada", "cleo"):
            u = User(username=f"tm-{n}-{tag}", auth_source="local", is_admin=n == "ada",
                     traveler_id=travs[n].id if n in travs else None)
            s.add(u)
            users[n] = u
        s.flush()
        trip = Trip(name=f"TM trip {tag}", owner_id=users["ana"].id,
                    start_date="2027-04-01", end_date="2027-04-08")
        open_trip = Trip(name=f"TM open {tag}", owner_id=None)
        s.add_all([trip, open_trip])
        s.flush()
        cap = TravelItem(trip_id=trip.id, owner_id=users["ana"].id, item_type="lodging")
        note = JournalEntry(owner_id=users["ana"].id, entry_date="2027-04-02",
                            trip_id=trip.id, body="TM a quiet morning")
        s.add_all([TripTraveler(trip_id=trip.id, traveler_id=travs["ana"].id, role="self"),
                   TripTraveler(trip_id=trip.id, traveler_id=travs["ben"].id,
                                role="companion"),
                   TripMember(trip_id=trip.id, user_id=users["vera"].id), cap, note])
        s.commit()
        ids = {"trip": trip.id, "open": open_trip.id, "capture": cap.id, "journal": note.id,
               **{n: u.id for n, u in users.items()}}
        heads = {n: {"Authorization": f"Bearer {authn.issue_access_token(u)}"}
                 for n, u in users.items()}
        tids = [t.id for t in travs.values()]
    yield ids, heads
    with SessionLocal() as s:
        trips = [ids["trip"], ids["open"]]
        for M in (TripTraveler, TripMember):
            s.query(M).filter(M.trip_id.in_(trips)).delete(synchronize_session=False)
        s.query(Trip).filter(Trip.id.in_(trips)).delete(synchronize_session=False)
        s.query(TravelItem).filter_by(id=ids["capture"]).delete()
        s.query(JournalEntry).filter_by(id=ids["journal"]).delete()
        s.query(User).filter(User.id.in_([ids[n] for n in heads])).delete(
            synchronize_session=False)
        s.query(TravelerIdentity).filter(TravelerIdentity.id.in_(tids)).delete(
            synchronize_session=False)
        s.commit()


def _shares(trip, h):
    return {s["username"].split("-")[1] for s in
            client.get(f"{T}/{trip}/shares", headers=h).json()["shares"]}


# ------------------------------------------------------------------ shares
def test_the_owner_sees_who_can_see_it_and_who_to_share_with(world):
    ids, h = world
    r = client.get(f"{T}/{ids['trip']}/shares", headers=h["ana"])
    assert r.status_code == 200
    body = r.json()
    assert body["can_share"] is True and body["owner"].startswith("tm-ana")
    assert [s["user_id"] for s in body["shares"]] == [ids["vera"]]
    assert {s["user_id"] for s in body["travellers"]} == {ids["ana"], ids["ben"]}
    choice_ids = {c["user_id"] for c in body["choices"]}
    assert ids["cleo"] in choice_ids
    assert not choice_ids & {ids["ana"], ids["ben"], ids["vera"]}


def test_a_traveller_or_viewer_reads_the_shares_but_gets_no_choices(world):
    ids, h = world
    for who in ("ben", "vera"):
        body = client.get(f"{T}/{ids['trip']}/shares", headers=h[who]).json()
        assert body["can_share"] is False and body["choices"] == []
    assert client.get(f"{T}/{ids['trip']}/shares", headers=h["sam"]).status_code == 404


def test_the_owner_shares_and_unshares(world):
    ids, h = world
    r = client.post(f"{T}/{ids['trip']}/shares", headers=h["ana"],
                    json={"user_id": ids["cleo"]})
    assert r.status_code == 201
    assert _shares(ids["trip"], h["ana"]) == {"vera", "cleo"}
    # Sharing again, or with the owner, changes nothing.
    client.post(f"{T}/{ids['trip']}/shares", headers=h["ana"], json={"user_id": ids["cleo"]})
    client.post(f"{T}/{ids['trip']}/shares", headers=h["ana"], json={"user_id": ids["ana"]})
    assert _shares(ids["trip"], h["ana"]) == {"vera", "cleo"}
    # Cleo can now see it, view-only.
    assert client.get(f"{T}/{ids['trip']}", headers=h["cleo"]).status_code == 200
    assert client.delete(f"{T}/{ids['trip']}/shares/{ids['cleo']}",
                         headers=h["ana"]).status_code == 204
    assert _shares(ids["trip"], h["ana"]) == {"vera"}
    assert client.get(f"{T}/{ids['trip']}", headers=h["cleo"]).status_code == 404
    assert client.post(f"{T}/{ids['trip']}/shares", headers=h["ana"],
                       json={"user_id": "tm-nobody"}).status_code == 422


def test_only_the_owner_changes_sharing(world):
    ids, h = world
    for who, code in (("ben", 403), ("vera", 403), ("ada", 404), ("sam", 404)):
        r = client.post(f"{T}/{ids['trip']}/shares", headers=h[who],
                        json={"user_id": ids["cleo"]})
        assert r.status_code == code, (who, r.status_code)
        r = client.delete(f"{T}/{ids['trip']}/shares/{ids['vera']}", headers=h[who])
        assert r.status_code == code, (who, r.status_code)
    assert _shares(ids["trip"], h["ana"]) == {"vera"}


def test_an_admin_shares_an_ownerless_trip(world):
    ids, h = world
    r = client.post(f"{T}/{ids['open']}/shares", headers=h["ada"],
                    json={"user_id": ids["cleo"]})
    assert r.status_code == 201
    assert _shares(ids["open"], h["ada"]) == {"cleo"}


# ------------------------------------------------------------------ owner
def test_only_an_admin_gives_an_ownerless_trip_its_owner(world):
    ids, h = world
    assert client.put(f"{T}/{ids['open']}/owner", headers=h["ana"],
                      json={"user_id": ids["ana"]}).status_code in (403, 404)
    r = client.put(f"{T}/{ids['open']}/owner", headers=h["ada"], json={"user_id": ids["cleo"]})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.get(Trip, ids["open"]).owner_id == ids["cleo"]
    # An owned trip's owner isn't changed.
    assert client.put(f"{T}/{ids['trip']}/owner", headers=h["ada"],
                      json={"user_id": ids["cleo"]}).status_code in (404, 409)
    with SessionLocal() as s:
        assert s.get(Trip, ids["trip"]).owner_id == ids["ana"]


def test_assigning_an_owner_needs_an_active_user(world):
    ids, h = world
    assert client.put(f"{T}/{ids['open']}/owner", headers=h["ada"],
                      json={"user_id": "tm-nobody"}).status_code == 422


# ------------------------------------------------------------------ delete
def test_only_the_owner_deletes_and_the_rest_get_403_or_404(world):
    ids, h = world
    for who, code in (("ben", 403), ("vera", 403), ("ada", 404), ("sam", 404)):
        assert client.delete(f"{T}/{ids['trip']}", headers=h[who]).status_code == code, who
    with SessionLocal() as s:
        assert s.get(Trip, ids["trip"]) is not None


def test_deleting_keeps_captures_and_journal_entries_unlinked(world):
    ids, h = world
    assert client.delete(f"{T}/{ids['trip']}", headers=h["ana"]).status_code == 204
    with SessionLocal() as s:
        assert s.get(Trip, ids["trip"]) is None
        assert s.query(TripMember).filter_by(trip_id=ids["trip"]).count() == 0
        assert s.query(TripTraveler).filter_by(trip_id=ids["trip"]).count() == 0
        cap, note = s.get(TravelItem, ids["capture"]), s.get(JournalEntry, ids["journal"])
        assert cap is not None and cap.trip_id is None
        assert note is not None and note.trip_id is None
    assert client.get(f"{T}/{ids['trip']}", headers=h["ana"]).status_code == 404
    assert client.delete(f"{T}/{ids['trip']}", headers=h["ana"]).status_code == 404


def test_signed_out_is_401(world):
    ids, _ = world
    for method, path in (("delete", f"{T}/{ids['trip']}"), ("get", f"{T}/{ids['trip']}/shares"),
                         ("put", f"{T}/{ids['open']}/owner")):
        assert client.request(method, path, json={}).status_code == 401
