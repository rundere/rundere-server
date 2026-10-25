"""The Places Library, phase 1 (SPEC-11, YONDER-314): filing keeps the place
(D-212), lists (D-213), the new fields (D-214), locality with and without Photon
(D-215), search, Atlas, clusters and nearby (D-216), and own-only scoping for
all of them. Photon is stubbed and short links never expand: nothing here
touches the network. Places, notes and lists are invented."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

try:
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from app import authn, places as places_svc
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

# Real cities (the Atlas needs real countries), invented places.
PHX = (33.4484, -112.0740)
LIS = (38.7223, -9.1393)
SEA = (0.0, -140.0)                     # mid-Pacific: no airport anywhere near


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: None)
    monkeypatch.setattr(maps_url, "_expand_short", lambda u: u)


def _mk_user(name):
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
def me():
    u = _mk_user("lib_a")
    yield u
    _wipe(u)


@pytest.fixture
def other():
    u = _mk_user("lib_b")
    yield u
    _wipe(u)


def _wipe(u):
    with SessionLocal() as s:
        trips = [t.id for t in s.query(Trip).filter(Trip.owner_id == u.id)]
        if trips:
            s.query(TripActivity).filter(TripActivity.trip_id.in_(trips)).delete(
                synchronize_session=False)
        for model in (SavedPlaceFiling, PlaceListMember, PlaceList, SavedPlace, Trip):
            s.query(model).filter(model.owner_id == u.id).delete(synchronize_session=False)
        s.commit()


def _h(u):
    return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}


def _trip(u, name="Library trip"):
    with SessionLocal() as s:
        t = Trip(name=name, source="manual", owner_id=u.id)
        s.add(t)
        s.commit()
        return t.id


def _save(u, name, where=None, **kw):
    lat, lon = where or (None, None)
    with SessionLocal() as s:
        p, _ = places_svc.save(s, u, name=name, lat=lat, lon=lon, **kw)
        return p.id


def _search(u, **kw):
    with SessionLocal() as s:
        return [p.name for p in places_svc.search(s, u, **kw)]


# ------------------------------------------------------------------ D-212: filing keeps it
def test_a_place_files_onto_two_trips_but_not_the_same_one_twice(me):
    h, t1, t2 = _h(me), _trip(me, "First"), _trip(me, "Second")
    pid = _save(me, "Larkspur Diner", PHX)
    assert client.post(f"/api/v1/places/{pid}/file/{t1}", json={}, headers=h).status_code == 201
    assert client.post(f"/api/v1/places/{pid}/file/{t2}", json={}, headers=h).status_code == 201
    r = client.post(f"/api/v1/places/{pid}/file/{t1}", json={}, headers=h)
    assert r.status_code == 409 and "already on that trip" in r.json()["detail"]
    [p] = client.get("/api/v1/places", headers=h).json()
    assert p["status"] == "saved" and sorted(p["trip_ids"]) == sorted([t1, t2])


def test_deleting_the_activity_or_the_trip_takes_the_place_off_it(me):
    h, t1, t2 = _h(me), _trip(me, "First"), _trip(me, "Second")
    pid = _save(me, "Larkspur Diner", PHX)
    act = client.post(f"/api/v1/places/{pid}/file/{t1}", json={}, headers=h).json()
    client.post(f"/api/v1/places/{pid}/file/{t2}", json={}, headers=h)
    assert client.delete(f"/api/v1/trips/{t1}/activities/{act['id']}",
                         headers=h).status_code == 204
    assert client.post(f"/api/v1/places/{pid}/file/{t1}", json={}, headers=h).status_code == 201
    r = client.post(f"/trips/{t2}/delete", headers=h, follow_redirects=False)
    assert r.status_code in (200, 303)
    with SessionLocal() as s:
        assert {f.trip_id for f in s.query(SavedPlaceFiling).filter_by(place_id=pid)} == {t1}
        assert s.get(SavedPlace, pid).status == "saved"          # the place itself stays


# ------------------------------------------------------------------ D-214: the new fields
def test_patch_sets_todo_priority_visit_and_a_corrected_city(me):
    h = _h(me)
    pid = _save(me, "Tile museum", LIS)
    r = client.patch(f"/api/v1/places/{pid}", headers=h, json={
        "todo": "See the blue rooms", "priority": "must", "visited_on": "2031-05-14",
        "locality": "Lisboa", "country_code": "pt"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["todo"], p["priority"], p["visited_on"]) == ("See the blue rooms", "must",
                                                          "2031-05-14")
    assert (p["locality"], p["country_code"], p["continent"]) == ("Lisboa", "PT", "EU")
    for bad in ({"priority": "soon"}, {"visited_on": "14/05/2031"}, {"country_code": "ZZ"}):
        assert client.patch(f"/api/v1/places/{pid}", headers=h, json=bad).status_code == 400
    p = client.patch(f"/api/v1/places/{pid}", headers=h, json={"todo": "", "visited_on": ""}).json()
    assert p["todo"] is None and p["visited_on"] is None


def test_save_keeps_todo_priority_and_the_phones_locality(me):
    r = client.post("/api/v1/places", headers=_h(me), json={
        "name": "Harbour stall", "lat": LIS[0], "lon": LIS[1], "todo": "Grilled sardines",
        "priority": "must", "locality": "Belém", "country_code": "PT", "source": "ios"})
    p = r.json()
    assert r.status_code == 201 and p["todo"] == "Grilled sardines" and p["priority"] == "must"
    assert p["locality"] == "Belém"                    # what MapKit said wins (D-215)


# ------------------------------------------------------------------ D-215: locality
def test_offline_locality_comes_from_the_nearest_airport(me):
    pids = [_save(me, "Desert garden", PHX), _save(me, "Raft", SEA), _save(me, "Name only")]
    with SessionLocal() as s:
        got = [(p.locality, p.country_code, p.locality_source)
               for p in (s.get(SavedPlace, i) for i in pids)]
    assert got == [("Phoenix", "US", "airport"), (None, None, None), (None, None, None)]


class _Photon:
    def __init__(self):
        self.calls = 0

    def reverse(self, lat, lon):
        self.calls += 1
        return {"locality": "Exampleton", "region": "Example County", "country_code": "US"}


def test_photon_upgrades_an_airport_guess_but_never_a_clients_answer(me):
    guessed = _save(me, "Desert garden", PHX)
    told = _save(me, "Canal walk", PHX, locality="Tempe", country_code="US")
    photon = _Photon()
    with SessionLocal() as s:
        places_svc.backfill(s, geocoder=photon)
        a, b = s.get(SavedPlace, guessed), s.get(SavedPlace, told)
        assert (a.locality, a.region, a.locality_source) == ("Exampleton", "Example County",
                                                            "photon")
        assert (b.locality, b.locality_source) == ("Tempe", "client")
        calls = photon.calls
        places_svc.backfill(s, geocoder=photon)            # safe to re-run: nothing re-asked
        assert s.get(SavedPlace, guessed).locality == "Exampleton"
        assert photon.calls == calls


# ------------------------------------------------------------------ D-216: search
def test_search_matches_every_word_ignoring_case_and_accents_and_ranks_names_first(me):
    _save(me, "Noodle counter", LIS, notes="Recommended by a café owner")
    _save(me, "Café Zéphyr", LIS, todo="Try the tiki menu")
    _save(me, "Zephyr Gardens", PHX)
    assert _search(me, q="zephyr") == ["Zephyr Gardens", "Café Zéphyr"]   # name prefix first
    assert _search(me, q="CAFE") == ["Café Zéphyr", "Noodle counter"]    # name before notes
    assert _search(me, q="tiki lisbon") == ["Café Zéphyr"]                # to-do + city
    assert _search(me, q="portugal noodle") == ["Noodle counter"]         # country name
    assert _search(me, q="100%_") == []                                   # LIKE is escaped


def test_search_filters_combine(me):
    _save(me, "Desert garden", PHX, priority="must")
    _save(me, "Canal walk", PHX)
    _save(me, "Tile museum", LIS, priority="must")
    assert sorted(_search(me, country="us")) == ["Canal walk", "Desert garden"]
    assert _search(me, country="US", priority="must") == ["Desert garden"]
    assert sorted(_search(me, city="phoenix")) == ["Canal walk", "Desert garden"]
    assert _search(me, near=LIS, radius_km=5) == ["Tile museum"]
    assert _search(me, bbox=(30, -115, 35, -110), q="canal") == ["Canal walk"]


def test_search_heals_rows_saved_before_the_library(me):
    pid = _save(me, "Old favourite", LIS)
    with SessionLocal() as s:                       # as the migration leaves them
        p = s.get(SavedPlace, pid)
        p.search_text, p.geohash, p.locality, p.locality_source = "", None, None, None
        s.commit()
    assert _search(me, q="lisbon") == ["Old favourite"]


# ------------------------------------------------------------------ Atlas, clusters, nearby
def test_atlas_drills_from_continent_to_city(me):
    _save(me, "Desert garden", PHX, priority="must")
    _save(me, "Canal walk", PHX)
    _save(me, "Tile museum", LIS)
    _save(me, "Raft", SEA)
    with SessionLocal() as s:
        top = places_svc.atlas(s, me)
        assert [(g["key"], g["count"]) for g in top] == [("NA", 2), ("EU", 1), ("-", 1)]
        assert top[0]["must"] == 1 and top[0]["kinds"] == {"tour": 2}
        assert [(g["key"], g["name"]) for g in places_svc.atlas(s, me, level="country",
                                                               parent="NA")] == [
            ("US", "United States")]
        [city] = places_svc.atlas(s, me, level="city", parent="US")
        assert (city["name"], city["count"]) == ("Phoenix", 2)
        with pytest.raises(ValueError):
            places_svc.atlas(s, me, level="planet")


def test_clusters_group_at_low_zoom_and_show_pins_up_close(me):
    _save(me, "Desert garden", PHX)
    _save(me, "Canal walk", (PHX[0] + 0.01, PHX[1] + 0.01))
    _save(me, "Tile museum", LIS)
    world = (-90, -180, 90, 180)
    with SessionLocal() as s:
        far = places_svc.clusters(s, me, bbox=world, zoom=2)["clusters"]
        assert sorted(c["count"] for c in far) == [1, 2]
        near = places_svc.clusters(s, me, bbox=(33, -113, 34, -111), zoom=16)["places"]
        assert sorted(p.name for p in near) == ["Canal walk", "Desert garden"]


def test_nearby_offers_what_is_not_already_on_the_trip(me):
    h, tid = _h(me), _trip(me)
    on = _save(me, "Desert garden", PHX)
    _save(me, "Canal walk", (PHX[0] + 0.05, PHX[1]))
    _save(me, "Tile museum", LIS)
    client.post(f"/api/v1/places/{on}/file/{tid}", json={}, headers=h)
    with SessionLocal() as s:
        names = [p.name for p in places_svc.nearby(s, me, [PHX], radius_km=25,
                                                   exclude_trip_id=tid)]
    assert names == ["Canal walk"]


# ------------------------------------------------------------------ D-213: lists
def test_lists_hold_places_many_to_many_and_deleting_one_keeps_the_places(me):
    a, b = _save(me, "Desert garden", PHX), _save(me, "Tile museum", LIS)
    with SessionLocal() as s:
        want = places_svc.create_list(s, me, name="Want to go", emoji="⭐")
        food = places_svc.create_list(s, me, name="Food")
        assert places_svc.add_to_list(s, me, want.id, a) is True
        assert places_svc.add_to_list(s, me, want.id, a) is False         # once
        places_svc.add_to_list(s, me, want.id, b)
        places_svc.add_to_list(s, me, food.id, b)
        assert [(lst.name, n) for lst, n in places_svc.lists(s, me)] == [("Want to go", 2),
                                                                        ("Food", 1)]
        assert sorted(places_svc.lists_for(s, [b])[b]) == sorted([want.id, food.id])
        assert [p.name for p in places_svc.search(s, me, list_id=food.id)] == ["Tile museum"]
        places_svc.delete_list(s, me, want.id)
        assert s.query(SavedPlace).filter(SavedPlace.id.in_([a, b])).count() == 2
        assert [lst.name for lst, _ in places_svc.lists(s, me)] == ["Food"]
        with pytest.raises(ValueError):
            places_svc.create_list(s, me, name="  ")


def test_an_imported_list_is_not_made_twice(me):
    with SessionLocal() as s:
        one = places_svc.create_list(s, me, name="Want to go", source="google:Want to go")
        two = places_svc.create_list(s, me, name="Want to go", source="google:Want to go")
        assert one.id == two.id


def test_a_removed_place_drops_out_of_list_counts(me):
    a = _save(me, "Desert garden", PHX)
    with SessionLocal() as s:
        lst = places_svc.create_list(s, me, name="Want to go")
        places_svc.add_to_list(s, me, lst.id, a)
        places_svc.delete_place(s, me, a)
        assert places_svc.lists(s, me)[0][1] == 0


# ------------------------------------------------------------------ own-only, everywhere
def test_nobody_else_sees_searches_clusters_lists_or_files_your_places(me, other):
    mine = _save(me, "Desert garden", PHX)
    with SessionLocal() as s:
        lst = places_svc.create_list(s, me, name="Mine")
        places_svc.add_to_list(s, me, lst.id, mine)
        assert places_svc.search(s, other) == [] and places_svc.search(s, other, q="desert") == []
        assert places_svc.atlas(s, other) == []
        assert places_svc.clusters(s, other, bbox=(-90, -180, 90, 180), zoom=2) == {
            "clusters": []}
        assert places_svc.nearby(s, other, [PHX]) == []
        assert places_svc.lists(s, other) == []
        theirs = places_svc.create_list(s, other, name="Theirs")
        for call in (lambda: places_svc.require_list(s, other, lst.id),
                     lambda: places_svc.add_to_list(s, other, theirs.id, mine),   # not their place
                     lambda: places_svc.add_to_list(s, me, theirs.id, mine),      # not my list
                     lambda: places_svc.delete_list(s, other, lst.id),
                     lambda: places_svc.search(s, other, list_id=lst.id)):
            with pytest.raises(HTTPException) as exc:
                call()
            assert exc.value.status_code == 404
    tid = _trip(other, "Their trip")
    r = client.post(f"/api/v1/places/{mine}/file/{tid}", json={}, headers=_h(other))
    assert r.status_code == 404
    assert client.patch(f"/api/v1/places/{mine}", json={"todo": "x"},
                        headers=_h(other)).status_code == 404


# ------------------------------------------------------------------ the migration (0044)
def test_migration_returns_filed_places_to_the_library_with_their_filing(tmp_path):
    """Run the real alembic chain: stop at 0043, file a place the old way, then
    upgrade and check it is back in the library and still on its trip."""
    import json

    import sqlalchemy as sa
    db = tmp_path / "mig.db"
    env = {**os.environ, "TX_DATABASE_URL": f"sqlite:///{db}"}

    def alembic(*args):
        r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-2000:]

    alembic("upgrade", "0043")
    eng = sa.create_engine(f"sqlite:///{db}")
    link = "https://maps.apple.com/?q=Example+Bistro&ll=38.7223,-9.1393"
    with eng.begin() as c:
        c.execute(sa.text("INSERT INTO trips (id, name, source, owner_id, created_at, updated_at) "
                          "VALUES ('t1', 'Old trip', 'manual', 'u1', '2026-10-01', '2026-10-01')"))
        c.execute(sa.text(
            "INSERT INTO trip_activities (id, trip_id, type, \"values\", starts_at, created_at, "
            "updated_at) VALUES ('a1', 't1', 'dining', :v, '', '2026-10-01', '2026-10-01')"),
            {"v": json.dumps({"name": "Example Bistro", "mapLink": link})})
        for pid, status, name in (("p1", "filed", "Example Bistro"),
                                  ("p2", "filed", "Lost activity"),
                                  ("p3", "dismissed", "Removed")):
            c.execute(sa.text(
                "INSERT INTO saved_places (id, owner_id, place_key, name_key, status, name, "
                "activity_type, provider, source_url, source, created_at, updated_at) "
                "VALUES (:id, 'u1', :id, '', :st, :nm, 'dining', 'apple', :url, 'web', "
                "'2026-10-01', '2026-10-01')"),
                {"id": pid, "st": status, "nm": name, "url": link})
    alembic("upgrade", "head")
    with eng.begin() as c:
        status = dict(c.execute(sa.text("SELECT id, status FROM saved_places")).fetchall())
        filings = c.execute(sa.text(
            "SELECT place_id, trip_id, activity_id FROM saved_place_filings")).fetchall()
    assert status == {"p1": "saved", "p2": "saved", "p3": "dismissed"}
    assert [tuple(f) for f in filings] == [("p1", "t1", "a1")]
    alembic("downgrade", "0043")
    with eng.begin() as c:
        assert dict(c.execute(sa.text("SELECT id, status FROM saved_places")).fetchall())[
            "p1"] == "filed"
