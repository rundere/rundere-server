"""Places search suggestions, quoted values and kind chips (SPEC-11, D-235,
YONDER-369). Every person, place and list here is invented. SQLite via conftest."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

try:
    from fastapi.testclient import TestClient
    from app import authn, places as places_svc, scope
    from app.config import settings
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


@pytest.fixture
def world(monkeypatch):
    """Ada has three places in two cities and a two-word list; Ben has his own
    list and a city Ada has never saved."""
    with SessionLocal() as s:
        s.query(PlaceListMember).delete()
        s.query(PlaceList).delete()
        s.query(SavedPlace).delete()
        s.commit()
    ada, ben = _user("sg-ada"), _user("sg-ben")
    with SessionLocal() as s:
        def save(u, name, kind, city, cc):
            p, _ = places_svc.save(s, u, name=name, lat=38.7, lon=-9.1, source="web")
            p.activity_type, p.locality, p.country_code = kind, city, cc
            s.commit()
            return p
        a1 = save(ada, "Example Tasca", "dining", "Lisbon", "PT")
        save(ada, "Sample Viewpoint", "tour", "Lisbon", "PT")
        save(ada, "Example Bistro", "dining", "New York", "US")
        save(ben, "Quince Garden", "tour", "Kyoto", "JP")
        date = places_svc.create_list(s, ada, name="Date night")
        places_svc.add_to_list(s, ada, date.id, a1.id)
        places_svc.create_list(s, ben, name="Bens hidden list")
    acting = {"u": ada}
    monkeypatch.setattr(scope, "user_of", lambda request: acting["u"])
    yield {"ada": ada, "ben": ben, "acting": acting}
    with SessionLocal() as s:
        s.query(PlaceListMember).delete()
        s.query(PlaceList).delete()
        s.query(SavedPlace).delete()
        s.commit()


def _sg(user, text, cursor=None):
    with SessionLocal() as s:
        return places_svc.suggest(s, user, text, cursor)


# ------------------------------------------------------------------ quoted values

def test_quoted_values_and_chips_being_typed():
    f = places_svc.parse_query('list:"Date night" city:"New York" kind:restaurant must')
    assert f == {"q": None, "list_name": "Date night", "city": "New York",
                 "kind": "dining", "priority": "must"}
    assert places_svc.parse_query('list:"Date ni')["list_name"] == "Date ni"   # still typing
    assert places_svc.parse_query('"old town" tasca')["q"] == "old town tasca"
    assert places_svc.parse_query("tasca kind:") == {"q": "tasca"}               # no value yet


def test_a_two_word_list_search_finds_its_places(world):
    with SessionLocal() as s:
        lst = places_svc.list_by_name(s, world["ada"], "Date night")
        found = places_svc.search(s, world["ada"], list_id=lst.id)
    assert [p.name for p in found] == ["Example Tasca"]
    page = client.get('/places?q=list:"Date night"',
                      headers={"hx-request": "true", "hx-target": "pl-body"}).text
    assert "Example Tasca" in page and "Example Bistro" not in page


# ------------------------------------------------------------------ what's offered

def test_an_empty_box_offers_the_chips_and_a_prefix_narrows_them(world):
    every = _sg(world["ada"], "")
    assert [i["insert"] for i in every["items"]][:4] == ["must", "someday", "visited", "unvisited"]
    assert {"kind:", "list:", "city:", "country:"} <= {i["insert"] for i in every["items"]}
    got = _sg(world["ada"], "tasca k")
    assert (got["start"], got["end"]) == (6, 7)
    assert [(i["insert"], i["complete"]) for i in got["items"]] == [("kind:", False)]
    assert _sg(world["ada"], "tasca ")["items"] == []           # after a word: nothing


def test_kinds_come_with_the_callers_counts(world):
    got = _sg(world["ada"], "kind:re")
    assert [(i["label"], i["insert"], i["count"]) for i in got["items"]] == [
        ("Restaurant", "kind:restaurant", 2)]
    tour = {i["insert"]: i["count"] for i in _sg(world["ada"], "kind:")["items"]}
    assert tour["kind:tour"] == 1                                 # not Ben's tour


def test_lists_cities_and_countries_are_the_callers_own(world):
    lst = _sg(world["ada"], "list:")["items"]
    assert [(i["label"], i["insert"], i["count"]) for i in lst] == [
        ("Date night", 'list:"Date night"', 1)]
    cities = _sg(world["ada"], "city:")["items"]
    assert [(i["label"], i["insert"], i["count"]) for i in cities] == [
        ("Lisbon, Portugal", "city:Lisbon", 2), ("New York, United States", 'city:"New York"', 1)]
    assert _sg(world["ada"], "city:ky")["items"] == []            # Kyoto is Ben's
    assert [i["insert"] for i in _sg(world["ada"], "country:")["items"]] == [
        "country:pt", "country:us"]
    assert [i["label"] for i in _sg(world["ben"], "list:")["items"]] == ["Bens hidden list"]


def test_the_word_under_the_caret_is_the_one_completed(world):
    text = "kind:re city:Li"
    got = _sg(world["ada"], text, cursor=6)                       # inside kind:re
    assert (got["start"], got["end"]) == (0, 7)
    assert [i["insert"] for i in got["items"]] == ["kind:restaurant"]
    got = _sg(world["ada"], text)                                 # at the end
    assert (got["start"], got["end"]) == (8, 15)
    assert [i["insert"] for i in got["items"]] == ["city:Lisbon"]
    assert _sg(world["ada"], 'list:"Da')["items"][0]["insert"] == 'list:"Date night"'


# ------------------------------------------------------------------ routes

def test_the_web_route_is_own_only_and_not_the_place_sheet(world):
    r = client.get("/places/suggest", params={"q": "list:"})
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
    assert [i["label"] for i in r.json()["items"]] == ["Date night"]
    world["acting"]["u"] = world["ben"]
    assert [i["label"] for i in client.get("/places/suggest", params={"q": "list:"}).json()["items"]] == [
        "Bens hidden list"]


def test_the_api_route_matches_the_service(world):
    from app import authn as a
    with SessionLocal() as s:
        h = {"Authorization": f"Bearer {a.issue_access_token(s.get(User, world['ada'].id))}"}
    r = client.get("/api/v1/places/suggest", params={"q": "city:n"}, headers=h)
    assert r.status_code == 200
    assert r.json() == {"start": 0, "end": 6, "items": [
        {"kind": "city", "label": "New York, United States", "insert": 'city:"New York"',
         "count": 1, "complete": True}]}
    assert "/api/v1/places/suggest" in client.get("/api/v1/openapi.json", headers=h).json()["paths"]


def test_the_page_carries_the_combobox_and_the_kind_chips(world):
    page = client.get("/places?q=kind:restaurant").text
    assert 'role="combobox"' in page and 'aria-controls="pl-suggest"' in page
    assert 'id="pl-suggest" class="pl-suggest" role="listbox"' in page
    assert "/review/static/places_suggest.js" in page
    assert 'data-kind="restaurant"\n              aria-pressed="true"' in page
    assert 'data-kind="tour"\n              aria-pressed="false"' in page


def test_tokens_are_scanned_in_linear_time():
    """CodeQL py/polynomial-redos: the tokenizer is a one-pass scanner, so a long
    run of one character from the search box can't make it crawl."""
    import time
    t = time.perf_counter()
    for text in ("!" * 200_000, "a" * 200_000 + ":", '"' + "x" * 200_000):
        places_svc.search_tokens(text)
        places_svc.parse_query(text)
    assert time.perf_counter() - t < 2
    assert [tok for _, _, tok in places_svc.search_tokens(
        'kind:re list:"Date night" "old town" a"b c city:"New')] == [
        "kind:re", 'list:"Date night"', '"old town"', 'a"b', "c", 'city:"New']
