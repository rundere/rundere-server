"""Places lists for the apps, and sorting places into them (SPEC-11 build step 6,
D-213, D-236, YONDER-370). Every person, place and list here is invented. SQLite
via conftest."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

try:
    from fastapi.testclient import TestClient
    from app import authn, places as places_svc
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, PlaceList, PlaceListMember, SavedPlace, User
    Base.metadata.create_all(engine)
    client = TestClient(app)
except ImportError:                                     # bare host: the container/CI runs these
    pytest.skip("fastapi not installed", allow_module_level=True)


def _user(name):
    with SessionLocal() as s:
        u = s.query(User).filter_by(username=name).one_or_none()
        if u is None:
            u = User(username=name, password_hash=authn.hash_password("pw"), auth_source="local")
            s.add(u)
            s.commit()
        s.refresh(u)
        s.expunge(u)
        return u


def _headers(user):
    with SessionLocal() as s:
        return {"Authorization": f"Bearer {authn.issue_access_token(s.get(User, user.id))}"}


def _clear():
    with SessionLocal() as s:
        s.query(PlaceListMember).delete()
        s.query(PlaceList).delete()
        s.query(SavedPlace).delete()
        s.commit()


@pytest.fixture
def world():
    """Ada has two places and two lists, one place in one list; Ben has a list of
    his own."""
    _clear()
    ada, ben = _user("pl-ada"), _user("pl-ben")
    with SessionLocal() as s:
        tasca, _ = places_svc.save(s, ada, name="Example Tasca", lat=38.71, lon=-9.14,
                                   source="web")
        view, _ = places_svc.save(s, ada, name="Sample Viewpoint", lat=38.72, lon=-9.13,
                                  source="web")
        want = places_svc.create_list(s, ada, name="Want to go", emoji="⭐")
        food = places_svc.create_list(s, ada, name="Date night")
        places_svc.add_to_list(s, ada, food.id, tasca.id)
        bens = places_svc.create_list(s, ben, name="Bens hidden list")
        ids = {"tasca": tasca.id, "view": view.id, "want": want.id, "food": food.id,
               "bens": bens.id}
    yield {"ada": _headers(ada), "ben": _headers(ben), **ids}
    _clear()


def _lists(h):
    r = client.get("/api/v1/places/lists", headers=h)
    assert r.status_code == 200
    return r.json()


# ------------------------------------------------------------------ read

def test_lists_come_in_order_with_counts_and_match_list_ids(world):
    got = _lists(world["ada"])
    assert [(l["name"], l["emoji"], l["position"], l["count"]) for l in got] == [
        ("Want to go", "⭐", 0, 0), ("Date night", None, 1, 1)]
    places = client.get("/api/v1/places", headers=world["ada"]).json()
    tasca = next(p for p in places if p["id"] == world["tasca"])
    assert tasca["list_ids"] == [world["food"]]                     # an id the app can look up
    assert "/api/v1/places/lists" in client.get("/api/v1/openapi.json",
                                                headers=world["ada"]).json()["paths"]


def test_nobody_sees_anyone_elses_lists(world):
    assert [l["name"] for l in _lists(world["ben"])] == ["Bens hidden list"]
    assert world["bens"] not in [l["id"] for l in _lists(world["ada"])]
    assert client.get("/api/v1/places/lists").status_code == 401


# ------------------------------------------------------------------ write lists

def test_create_rename_move_and_delete_a_list(world):
    h = world["ada"]
    r = client.post("/api/v1/places/lists", json={"name": "  Ramen to try "}, headers=h)
    assert r.status_code == 201
    new = r.json()
    assert (new["name"], new["position"], new["count"]) == ("Ramen to try", 2, 0)
    assert client.post("/api/v1/places/lists", json={"name": " "}, headers=h).status_code == 400

    r = client.patch(f"/api/v1/places/lists/{new['id']}",
                     json={"name": "Noodles", "emoji": "🍜", "position": -1}, headers=h)
    assert r.status_code == 200 and r.json()["name"] == "Noodles"
    assert [l["name"] for l in _lists(h)] == ["Noodles", "Want to go", "Date night"]

    # Deleting a list keeps its places (D-213).
    assert client.delete(f"/api/v1/places/lists/{world['food']}", headers=h).status_code == 204
    assert [l["name"] for l in _lists(h)] == ["Noodles", "Want to go"]
    ids = [p["id"] for p in client.get("/api/v1/places", headers=h).json()]
    assert world["tasca"] in ids


def test_another_persons_list_404s_on_every_write(world):
    h, bens = world["ada"], world["bens"]
    assert client.patch(f"/api/v1/places/lists/{bens}", json={"name": "Mine now"},
                        headers=h).status_code == 404
    assert client.delete(f"/api/v1/places/lists/{bens}", headers=h).status_code == 404
    assert [l["name"] for l in _lists(world["ben"])] == ["Bens hidden list"]


# ------------------------------------------------------------------ sorting places (D-236)

def test_put_sets_exactly_the_lists_a_place_is_in(world):
    h = world["ada"]
    url = f"/api/v1/places/{world['tasca']}/lists"
    r = client.put(url, json={"list_ids": [world["want"], world["food"], world["want"]]},
                   headers=h)
    assert r.status_code == 200
    assert sorted(r.json()["list_ids"]) == sorted([world["want"], world["food"]])
    assert client.put(url, json={"list_ids": [world["want"]]},
                      headers=h).json()["list_ids"] == [world["want"]]
    assert [l["count"] for l in _lists(h)] == [1, 0]
    # [] takes it out of every list: it's Unsorted again.
    assert client.put(url, json={"list_ids": []}, headers=h).json()["list_ids"] == []
    with SessionLocal() as s:
        from app.models import User as U
        ada = s.query(U).filter_by(username="pl-ada").one()
        assert world["tasca"] in [p.id for p in places_svc.unsorted(s, ada)]


def test_sorting_into_someone_elses_list_or_their_place_changes_nothing(world):
    h = world["ada"]
    r = client.put(f"/api/v1/places/{world['tasca']}/lists",
                   json={"list_ids": [world["want"], world["bens"]]}, headers=h)
    assert r.status_code == 404
    assert [l["count"] for l in _lists(h)] == [0, 1]                # nothing half-done
    assert client.put(f"/api/v1/places/{world['tasca']}/lists",
                      json={"list_ids": [world["bens"]]},
                      headers=world["ben"]).status_code == 404       # not Ben's place


def test_a_save_can_carry_its_kind_and_lists(world):
    h = world["ada"]
    r = client.post("/api/v1/places", headers=h, json={
        "name": "Example Noodle Bar", "lat": 38.70, "lon": -9.15, "source": "ios-share",
        "activity_type": "dining", "list_ids": [world["food"]]})
    assert r.status_code == 201
    new = r.json()
    assert (new["activity_type"], new["list_ids"]) == ("dining", [world["food"]])

    # Saved again into another list: it stays in the first, and the kind the
    # person may have chosen since isn't replaced by the app's guess.
    client.patch(f"/api/v1/places/{new['id']}", json={"activity_type": "event"}, headers=h)
    r = client.post("/api/v1/places", headers=h, json={
        "name": "Example Noodle Bar", "lat": 38.70, "lon": -9.15,
        "activity_type": "dining", "list_ids": [world["want"]]})
    assert r.status_code == 200 and r.json()["outcome"] == "duplicate"
    assert r.json()["activity_type"] == "event"
    assert sorted(r.json()["list_ids"]) == sorted([world["food"], world["want"]])


def test_a_save_naming_a_bad_list_or_kind_saves_nothing(world):
    h = world["ada"]
    before = len(client.get("/api/v1/places", headers=h).json())
    for extra, code in (({"list_ids": [world["bens"]]}, 404),
                        ({"activity_type": "flight-of-fancy"}, 400)):
        r = client.post("/api/v1/places", headers=h,
                        json={"name": "Example Hidden Cafe", "lat": 38.6, "lon": -9.2, **extra})
        assert r.status_code == code
    assert len(client.get("/api/v1/places", headers=h).json()) == before


def test_a_batch_sorts_each_saved_place_but_not_on_a_dry_run(world):
    h = world["ada"]
    body = {"places": [
        {"name": "Sample Gallery", "lat": 38.73, "lon": -9.12, "list_ids": [world["want"]]},
        {"name": "Example Tasca", "lat": 38.71, "lon": -9.14, "list_ids": [world["want"]]}]}
    dry = client.post("/api/v1/places/batch", json={**body, "dry_run": True}, headers=h)
    assert dry.status_code == 200 and _lists(h)[0]["count"] == 0
    r = client.post("/api/v1/places/batch", json=body, headers=h)
    assert [x["outcome"] for x in r.json()["results"]] == ["created", "duplicate"]
    assert _lists(h)[0]["count"] == 2
    bad = client.post("/api/v1/places/batch", headers=h, json={"places": [
        {"name": "Sample Pier", "lat": 38.69, "lon": -9.16, "list_ids": [world["bens"]]}]})
    assert bad.status_code == 404
