"""The Places Library page (SPEC-11 phase 2, YONDER-314): the Unsorted, Atlas, Lists
and All lenses, search with chips, the place panel (type picker, priority, to-do,
lists, filing), the Atlas maps, and own-only scoping for every one of them.
Photon is stubbed and short links never expand. Places and lists are invented;
the cities are real so the Atlas has countries to group by."""
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
    from app.models import (Base, PlaceList, PlaceListMember, SavedPlace, SavedPlaceFiling,
                            Trip, TripActivity, User)
    Base.metadata.create_all(engine)
    client = TestClient(app)
except ImportError:                                     # bare host: the container/CI runs these
    pytest.skip("fastapi not installed", allow_module_level=True)

from extract import maps_url
from resolve import place_lookup

PHX = (33.4484, -112.0740)
LIS = (38.7223, -9.1393)


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: None)
    monkeypatch.setattr(maps_url, "_expand_short", lambda u: u)
    monkeypatch.setattr(settings, "map_tile_cache_dir", str(tmp_path / "tiles"))


def _mk(name):
    with SessionLocal() as s:
        u = s.query(User).filter_by(username=name).one_or_none()
        if u is None:
            u = User(username=name, password_hash=authn.hash_password("pw"), auth_source="local")
            s.add(u); s.commit()
        s.refresh(u); s.expunge(u)
        return u


def _wipe(*users):
    with SessionLocal() as s:
        for u in users:
            trips = [t.id for t in s.query(Trip).filter(Trip.owner_id == u.id)]
            if trips:
                s.query(TripActivity).filter(TripActivity.trip_id.in_(trips)).delete(
                    synchronize_session=False)
            for m in (SavedPlaceFiling, PlaceListMember, PlaceList, SavedPlace, Trip):
                s.query(m).filter(m.owner_id == u.id).delete(synchronize_session=False)
        s.commit()


@pytest.fixture
def me(monkeypatch):
    """A signed-in person; the web routes read the user off the request."""
    u = _mk("ui_a")
    acting = {"u": u}
    monkeypatch.setattr(scope, "user_of", lambda request: acting["u"])
    yield acting
    _wipe(u, _mk("ui_b"))


def _save(owner, name, where=None, **kw):
    lat, lon = where or (None, None)
    with SessionLocal() as s:
        p, _ = places_svc.save(s, owner, name=name, lat=lat, lon=lon, **kw)
        return p.id


def _page(**params):
    from urllib.parse import urlencode
    return client.get("/places?" + urlencode(params)).text


# ------------------------------------------------------------------ lenses
def test_new_places_wait_in_unsorted_until_listed_or_filed(me):
    a = me["u"]
    pid = _save(a, "Desert garden", PHX)
    _save(a, "Raft somewhere")                                  # no pin
    page = _page()
    assert 'class="pl-lens on"' in page and "Desert garden" in page
    assert "Needs a pin" in page and "Raft somewhere" in page
    with SessionLocal() as s:
        lst = places_svc.create_list(s, a, name="Want to go")
        places_svc.add_to_list(s, a, lst.id, pid)
    assert "Desert garden" not in _page(lens="unsorted")              # triaged
    assert "Desert garden" in _page(lens="all")


def test_the_atlas_drills_from_continent_to_country_to_city(me):
    a = me["u"]
    _save(a, "Desert garden", PHX)
    _save(a, "Canal walk", (PHX[0] + 0.01, PHX[1]))
    _save(a, "Tile museum", LIS)
    world = _page(lens="atlas")
    assert "North America" in world and "Europe" in world and "2 places" in world
    country = _page(lens="atlas", continent="NA")
    assert "United States" in country and "Europe" not in country.split("pl-boards")[1]
    city = _page(lens="atlas", country="US")
    assert "Phoenix" in city and "country=US&amp;city=phoenix" in city
    rows = _page(lens="atlas", country="US", city="phoenix")
    assert "Desert garden" in rows and "Canal walk" in rows and "Tile museum" not in rows
    assert "World</a>" in rows and "North America</a>" in rows        # breadcrumb


def test_search_words_and_chips(me):
    a = me["u"]
    _save(a, "Café Zéphyr", LIS, todo="Try the tiki menu", priority="must")
    _save(a, "Tile museum", LIS)
    _save(a, "Desert garden", PHX)
    assert "Café Zéphyr" in _page(q="tiki") and "Tile museum" not in _page(q="tiki")
    musts = _page(q="must")
    assert "Café Zéphyr" in musts and "Desert garden" not in musts
    dining = _page(q="kind:restaurant")                          # the name guessed "dining"
    assert "Café Zéphyr" in dining and "Tile museum" not in dining
    assert "Desert garden" in _page(q="country:us") and "Desert garden" not in _page(q="city:lisbon")
    assert "no list called" in _page(q="list:Nope")


def test_lists_are_made_renamed_and_deleted_without_losing_places(me):
    a = me["u"]
    pid = _save(a, "Desert garden", PHX)
    r = client.post("/places/lists", data={"name": "Rainy days"}, follow_redirects=False)
    assert r.status_code == 303 and "list=" in r.headers["location"]
    with SessionLocal() as s:
        lid = s.query(PlaceList).filter_by(owner_id=a.id).one().id
    client.post(f"/places/{pid}/lists", data={"list_id": lid, "on": "1"})
    assert "Desert garden" in _page(lens="lists", list=lid)
    assert "Rainy days" in _page(lens="lists") and "1 place" in _page(lens="lists")
    client.post(f"/places/lists/{lid}", data={"name": "Wet weather"})
    assert "Wet weather" in _page(lens="lists")
    client.post(f"/places/lists/{lid}/delete")
    assert "Wet weather" not in _page(lens="lists")
    with SessionLocal() as s:
        assert s.get(SavedPlace, pid) is not None                # the place stays (D-213)


# ------------------------------------------------------------------ the place panel
def test_the_panel_changes_the_type_priority_and_to_do(me):
    a = me["u"]
    pid = _save(a, "Larkspur Diner Annex", PHX)
    sheet = client.get(f"/places/{pid}").text
    assert 'name="activity_type"' in sheet and "Restaurant" in sheet and "Lodging" in sheet
    assert "Flight" not in sheet.split('name="activity_type"')[1].split("</select>")[0]
    r = client.post(f"/places/{pid}/update", data={"activity_type": "dining"})
    assert r.headers.get("hx-trigger") == "places-changed" and "Saved." in r.text
    client.post(f"/places/{pid}/update", data={"priority": "must"})
    client.post(f"/places/{pid}/update", data={"todo": "Green chile breakfast"})
    with SessionLocal() as s:
        p = s.get(SavedPlace, pid)
        assert (p.activity_type, p.priority, p.todo) == ("dining", "must", "Green chile breakfast")
    assert "Add as Restaurant" in client.get(f"/places/{pid}").text
    bad = client.post(f"/places/{pid}/update", data={"activity_type": "spaceship"})
    assert "cap-msg err" in bad.text


def test_the_panel_adds_to_a_new_list_and_files_onto_a_trip(me):
    a = me["u"]
    pid = _save(a, "Desert garden", PHX)
    client.post(f"/places/{pid}/lists", data={"new_list": "Want to go"})
    with SessionLocal() as s:
        assert [lst.name for lst, _ in places_svc.lists(s, a)] == ["Want to go"]
        t = Trip(name="UI trip", source="manual", owner_id=a.id); s.add(t); s.commit()
        tid = t.id
    r = client.post(f"/places/{pid}/file", data={"trip_id": tid})
    assert "no date yet" in r.text and f"/trips/{tid}" in r.text
    assert "UI trip</a>" in client.get(f"/places/{pid}").text       # "On UI trip."


def test_a_name_suggests_the_type_when_the_link_has_no_category(me):
    a = me["u"]
    with SessionLocal() as s:
        cafe = s.get(SavedPlace, _save(a, "Bistro Exemplo", LIS))
        museum = s.get(SavedPlace, _save(a, "Tile museum", LIS))
        told = s.get(SavedPlace, _save(a, "Hotel Exemplo", LIS, category="tourism=museum"))
        assert (cafe.activity_type, museum.activity_type) == ("dining", "tour")
        assert told.activity_type == "tour"                     # a category beats the name


# ------------------------------------------------------------------ Atlas maps
def test_atlas_maps_are_drawn_once_and_only_from_your_own_places(me):
    a = me["u"]
    _save(a, "Desert garden", PHX)
    r = client.get("/places/atlas/map.png?country=US")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert r.headers["cache-control"].startswith("private")
    cached = list((Path(settings.map_tile_cache_dir) / "place-thumbs").glob("*.png"))
    assert len(cached) == 1
    client.get("/places/atlas/map.png?country=US")
    assert len(list((Path(settings.map_tile_cache_dir) / "place-thumbs").glob("*.png"))) == 1
    me["u"] = _mk("ui_b")                                       # someone else: nothing to draw
    assert client.get("/places/atlas/map.png?country=US").status_code == 404


# ------------------------------------------------------------------ own-only
def test_nobody_else_can_see_open_change_or_list_your_places(me):
    a = me["u"]
    pid = _save(a, "Desert garden", PHX)
    with SessionLocal() as s:
        lid = places_svc.create_list(s, a, name="Mine").id
    me["u"] = b = _mk("ui_b")
    for lens in ("unsorted", "atlas", "all", "lists"):
        assert "Desert garden" not in _page(lens=lens) and "Mine" not in _page(lens=lens)
    assert "Desert garden" not in _page(q="desert")
    assert client.get(f"/places/{pid}").status_code == 404
    assert client.post(f"/places/{pid}/update", data={"name": "x"}).status_code == 404
    assert client.post(f"/places/{pid}/lists", data={"list_id": lid, "on": "1"}).status_code == 404
    assert client.get(f"/places?lens=lists&list={lid}").status_code == 404
    client.post(f"/places/lists/{lid}/delete")
    with SessionLocal() as s:
        assert s.get(PlaceList, lid) is not None and s.get(SavedPlace, pid).name == "Desert garden"
        assert places_svc.lists(s, b) == []


def test_the_demo_shows_places_without_edit_controls(me, monkeypatch):
    a = me["u"]
    pid = _save(a, "Desert garden", PHX)
    monkeypatch.setattr(settings, "demo_mode", True)
    sheet = client.get(f"/places/{pid}").text
    assert "Desert garden" in sheet and 'name="activity_type"' not in sheet
    assert "Remove from library" not in sheet and "Save a place" not in _page()


# ------------------------------------------------------------------ address → pin (owner, 2026-10-26)
class _Geo:
    """A stand-in Photon, shaped like the real one (`_query` returns GeoJSON
    features). It has the building at 12 Example Row, Lisbon; another branch of the
    same café two streets away (the decoy a name match would take); and the city
    itself, which is never a pin. The behaviour it mimics was measured against a
    real Photon on 2026-10-29 (YONDER-339)."""
    def __init__(self):
        self.asked = []

    @staticmethod
    def _f(lat, lon, **props):
        props.setdefault("countrycode", "PT")
        props.setdefault("city", "Lisbon")
        return {"geometry": {"coordinates": [lon, lat]}, "properties": props}

    def _query(self, q, limit=5, near=None):
        self.asked.append(q)
        out = []
        if "Example Row" in q:
            out.append(self._f(38.71, -9.14, osm_key="place", osm_value="house",
                               housenumber="12", street="Example Row"))
        if "Café Inventado" in q:
            out.insert(0, self._f(38.73, -9.15, osm_key="amenity", osm_value="cafe",
                                  name="Café Inventado", housenumber="88",
                                  street="Rua Fictícia"))
        out.append(self._f(38.72, -9.13, osm_key="place", osm_value="city",
                           type="city", name="Lisbon"))
        return out

    def reverse(self, lat, lon):
        return None


def test_the_address_saves_with_its_button_and_pins_a_place_without_one(me, monkeypatch):
    geo = _Geo()
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: geo)
    pid = _save(me["u"], "Raft somewhere")                      # no pin
    sheet = client.get(f"/places/{pid}").text
    # An explicit Save for the address group, not save-on-change (YONDER-339).
    assert f'hx-post="/places/{pid}/address"' in sheet and "Save address" in sheet
    assert "Not on the map yet" in sheet
    r = client.post(f"/places/{pid}/address", data={"address": "12 Example Row",
                                                    "locality": "Lisbon", "country_code": "pt"})
    assert "Address saved. Pinned on the map from the address" in r.text
    assert r.headers.get("hx-trigger") == "places-changed"
    assert f"/places/{pid}/map.png" in r.text                   # the pin's small map
    with SessionLocal() as s:
        p = s.get(SavedPlace, pid)
        assert (p.address, p.locality, p.country_code) == ("12 Example Row", "Lisbon", "PT")
        assert (p.lat, p.lon) == (38.71, -9.14) and p.geohash
    # The country goes in by name: "PT" or "CA" alone can read as something else.
    assert geo.asked[0] == "Raft somewhere, 12 Example Row, Lisbon, Portugal"
    assert "Not on the map yet" not in client.get(f"/places/{pid}").text


def test_another_branch_with_the_same_name_is_not_a_pin(me, monkeypatch):
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: _Geo())
    pid = _save(me["u"], "Café Inventado")
    r = client.post(f"/places/{pid}/address", data={"address": "5 Rua Fictícia",
                                                    "locality": "Lisbon", "country_code": "PT"})
    assert "on the map, so there" in r.text and "Find on map" in r.text
    with SessionLocal() as s:
        assert s.get(SavedPlace, pid).lat is None               # number 88 isn't number 5


def test_a_town_alone_places_no_pin_and_says_what_to_add(me, monkeypatch):
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: _Geo())
    pid = _save(me["u"], "Raft somewhere")
    r = client.post(f"/places/{pid}/address", data={"address": "", "locality": "Lisbon",
                                                    "country_code": "PT"})
    assert "Without a street and number" in r.text and "Find on map" in r.text
    with SessionLocal() as s:
        p = s.get(SavedPlace, pid)
        assert p.locality == "Lisbon" and p.lat is None          # saved, but not pinned


def test_find_on_map_retries_a_saved_address(me, monkeypatch):
    pid = _save(me["u"], "Raft somewhere", address="12 Example Row", locality="Lisbon",
                country_code="PT")
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: _Geo())
    assert "Find on map" in client.get(f"/places/{pid}").text
    r = client.post(f"/places/{pid}/pin")
    assert "Pinned on the map from the address" in r.text
    assert client.get(f"/places/{pid}/map.png").status_code == 200


def test_without_a_geocoder_the_address_still_saves_and_says_why_there_is_no_pin(me):
    pid = _save(me["u"], "Raft somewhere")
    r = client.post(f"/places/{pid}/address", data={"address": "12 Example Row"})
    assert "no address lookup set up" in r.text and "Find on map" not in r.text
    with SessionLocal() as s:
        assert s.get(SavedPlace, pid).address == "12 Example Row"


def test_an_existing_pin_moves_only_when_asked(me, monkeypatch):
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: _Geo())
    pid = _save(me["u"], "Tile museum", LIS)
    r = client.post(f"/places/{pid}/address", data={"address": "12 Example Row",
                                                    "locality": "Lisbon", "country_code": "PT"})
    assert "Address saved." in r.text and "Pinned" not in r.text
    with SessionLocal() as s:
        assert (s.get(SavedPlace, pid).lat, s.get(SavedPlace, pid).lon) == LIS   # kept
    assert "Move the pin to this address" in client.get(f"/places/{pid}").text
    r = client.post(f"/places/{pid}/pin")
    assert "Moved the pin to the address" in r.text
    with SessionLocal() as s:
        assert (s.get(SavedPlace, pid).lat, s.get(SavedPlace, pid).lon) == (38.71, -9.14)


def test_the_api_patch_pins_from_a_new_address_too(me, monkeypatch):
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: _Geo())
    pid = _save(me["u"], "Raft somewhere")
    with SessionLocal() as s:
        tok = authn.issue_access_token(s.get(User, me["u"].id))
    r = client.patch(f"/api/v1/places/{pid}", headers={"Authorization": f"Bearer {tok}"},
                     json={"address": "12 Example Row", "locality": "Lisbon",
                           "country_code": "PT"})
    assert r.status_code == 200 and (r.json()["lat"], r.json()["lon"]) == (38.71, -9.14)


def test_a_google_share_with_an_address_gets_its_pin_on_arrival(me, monkeypatch):
    """The iOS share sheet sends `maps?q=Name, street, city&ftid=…` with no
    coordinates (YONDER-339): the name and address come apart, and the place is
    pinned from the address when it's saved."""
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: _Geo())
    url = ("https://www.google.com/maps?q=Raft+somewhere,+12+Example+Row,+Lisbon,+1100-001"
           "&ftid=0x1:0x2")
    with SessionLocal() as s:
        p, outcome = places_svc.save(s, me["u"], url=url, enrich=True, source="ios-share")
        assert outcome == places_svc.CREATED
        assert (p.name, p.address) == ("Raft somewhere", "12 Example Row, Lisbon, 1100-001")
        assert (p.lat, p.lon, p.locality, p.country_code) == (38.71, -9.14, "Lisbon", "PT")


def test_the_backfill_rereads_old_names_and_pins_with_a_dry_run_first(me, monkeypatch):
    url = "https://www.google.com/maps?q=Raft+somewhere,+12+Example+Row,+Lisbon&ftid=0x3:0x4"
    with SessionLocal() as s:                                   # as the old parse stored it
        p, _ = places_svc.save(s, me["u"], name="Raft somewhere, 12 Example Row, Lisbon",
                               url=url, country_code="PT")
        pid = p.id
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: _Geo())
    with SessionLocal() as s:
        counts = places_svc.backfill(s, dry_run=True)
        assert counts["renamed"] >= 1 and counts["pinned"] >= 1
    with SessionLocal() as s:
        p = s.get(SavedPlace, pid)
        assert p.name == "Raft somewhere, 12 Example Row, Lisbon" and p.lat is None
    with SessionLocal() as s:
        places_svc.backfill(s)
    with SessionLocal() as s:
        p = s.get(SavedPlace, pid)
        assert (p.name, p.address) == ("Raft somewhere", "12 Example Row, Lisbon")
        assert (p.lat, p.lon) == (38.71, -9.14)


def test_the_backfill_leaves_a_name_the_person_changed(me, monkeypatch):
    url = "https://www.google.com/maps?q=Raft+somewhere,+12+Example+Row,+Lisbon&ftid=0x5:0x6"
    with SessionLocal() as s:
        p, _ = places_svc.save(s, me["u"], name="Our raft", url=url)
        pid = p.id
    with SessionLocal() as s:
        places_svc.backfill(s)
    with SessionLocal() as s:
        assert s.get(SavedPlace, pid).name == "Our raft"


def test_nobody_else_can_pin_or_see_the_map_of_your_place(me, monkeypatch):
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: _Geo())
    pid = _save(me["u"], "Tile museum", LIS, address="12 Example Row")
    me["u"] = _mk("ui_b")
    assert client.post(f"/places/{pid}/pin").status_code == 404
    assert client.post(f"/places/{pid}/address", data={"address": "1 Elsewhere"}).status_code == 404
    assert client.get(f"/places/{pid}/map.png").status_code == 404
    with SessionLocal() as s:
        p = s.get(SavedPlace, pid)
        assert (p.lat, p.lon, p.address) == (*LIS, "12 Example Row")
