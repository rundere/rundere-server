"""YONDER-200: the same place is never saved or filed twice (D-169, D-170) —
sequential repeats, near-duplicates, concurrent requests, and a list pushed twice.
Photon and link expansion are stubbed; SQLite via conftest."""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

try:
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import IntegrityError
    from app import authn, places as places_svc
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, SavedPlace, Trip, TripActivity, User
    Base.metadata.create_all(engine)
    client = TestClient(app)
except ImportError:
    pytest.skip("fastapi not installed", allow_module_level=True)

from extract import maps_url, place_url
from resolve import place_lookup

G_FTID_A = ("https://www.google.com/maps/place/Caf%C3%A9+Central/data=!4m2!3m1!"
            "!1s0x476d07a0b1e6c2d1:0x4f9b1e3c5a7d2e8f")
G_FTID_B = ("https://www.google.com/maps/place/Cafe+Central+Wien/@48.2102,16.3656,17z/"
            "data=!3m1!4b1!4m6!3m5!1s0x476d07a0b1e6c2d1:0x4f9b1e3c5a7d2e8f!8m2!3d48.2102!4d16.3656")
A_CAFE = "https://maps.apple.com/?q=Cafe+Central&ll=48.21025,16.36555"
G_CAFE = ("https://www.google.com/maps/place/Cafe+Central/@48.2102,16.3656,17z/"
          "data=!3m1!4b1!8m2!3d48.2102!4d16.3656")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: None)

    def boom(u):
        raise AssertionError("a batch must never expand a short link")
    monkeypatch.setattr(maps_url, "_expand_short", lambda u: u)


@pytest.fixture
def me():
    with SessionLocal() as s:
        u = s.query(User).filter_by(username="dd_a").one_or_none()
        if u is None:
            u = User(username="dd_a", password_hash=authn.hash_password("pw"),
                     auth_source="local")
            s.add(u)
            s.commit()
        s.refresh(u)
        s.expunge(u)
    yield u
    with SessionLocal() as s:
        s.query(SavedPlace).filter(SavedPlace.owner_id == u.id).delete(synchronize_session=False)
        tids = [t.id for t in s.query(Trip).filter(Trip.owner_id == u.id).all()]
        s.query(TripActivity).filter(TripActivity.trip_id.in_(tids)).delete(
            synchronize_session=False)
        s.query(Trip).filter(Trip.owner_id == u.id).delete(synchronize_session=False)
        s.commit()


def _h(u):
    return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}


def _trip(u, name="T"):
    with SessionLocal() as s:
        t = Trip(name=name, source="manual", owner_id=u.id)
        s.add(t)
        s.commit()
        return t.id


def _batch(u, places, **kw):
    r = client.post("/api/v1/places/batch", json={"places": places, **kw}, headers=_h(u))
    assert r.status_code == 200, r.text
    return r.json()


def _rows(u):
    with SessionLocal() as s:
        return s.query(SavedPlace).filter(SavedPlace.owner_id == u.id).all()


# --------------------------------------------------------------- the database backstop
def test_unique_index_refuses_a_second_row_for_the_same_place(me):
    with SessionLocal() as s:
        for _ in range(2):
            s.add(SavedPlace(owner_id=me.id, place_key="google:x", name="X", name_key="x"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()


# ----------------------------------------------------------------------- concurrency
def test_two_simultaneous_saves_of_one_place_make_one_row(me, monkeypatch):
    """Both requests pass the "already saved?" check before either inserts; the
    unique index decides, and the loser gets the winner's row, not an error."""
    gate = threading.Barrier(2)
    real = places_svc._plan

    def raced(*a, **k):
        out = real(*a, **k)
        gate.wait(timeout=5)                    # both have looked, neither has written
        return out
    monkeypatch.setattr(places_svc, "_plan", raced)

    def go():
        with SessionLocal() as s:
            u = s.get(User, me.id)
            place, outcome = places_svc.save(s, u, url=G_FTID_A)
            return place.id, outcome

    with ThreadPoolExecutor(2) as ex:
        a, b = ex.map(lambda _: go(), range(2))
    assert a[0] == b[0] and sorted([a[1], b[1]]) == ["created", "duplicate"]
    assert len(_rows(me)) == 1


def test_a_lost_race_is_a_duplicate_not_an_error(me, monkeypatch):
    with SessionLocal() as s:
        first, _ = places_svc.save(s, s.get(User, me.id), url=G_FTID_A)
    monkeypatch.setattr(places_svc, "_plan", lambda *a, **k: (places_svc.CREATED, None))
    with SessionLocal() as s:
        place, outcome = places_svc.save(s, s.get(User, me.id), url=G_FTID_A)
    assert outcome == "duplicate" and place.id == first.id and len(_rows(me)) == 1


def test_two_simultaneous_filings_of_one_place_file_it_once(me):
    tid = _trip(me)
    pid = client.post("/api/v1/places", json={"url": G_FTID_A}, headers=_h(me)).json()["id"]

    def file_it(_):
        return client.post(f"/api/v1/places/{pid}/file/{tid}", json={},
                           headers=_h(me)).status_code

    with ThreadPoolExecutor(2) as ex:
        codes = sorted(ex.map(file_it, range(2)))
    assert codes == [201, 409]
    with SessionLocal() as s:
        assert s.query(TripActivity).filter_by(trip_id=tid).count() == 1


# ------------------------------------------------------ one place, however it arrives
def test_same_google_place_by_feature_id_is_one_place(me):
    assert place_url.parse_place_url(G_FTID_A)["place_id"] == "0x476d07a0b1e6c2d1:0x4f9b1e3c5a7d2e8f"
    out = _batch(me, [{"url": G_FTID_A}, {"url": G_FTID_B}])
    assert out["counts"] == {"created": 1, "duplicate": 1} and len(_rows(me)) == 1


def test_same_place_from_apple_and_google_is_a_near_duplicate(me):
    first = _batch(me, [{"url": G_CAFE}])
    assert first["counts"] == {"created": 1}
    out = _batch(me, [{"url": A_CAFE}])
    r = out["results"][0]
    assert out["counts"] == {"near_duplicate": 1} and r["existing_status"] == "saved"
    assert len(_rows(me)) == 1
    forced = _batch(me, [{"url": A_CAFE}], allow_near_duplicates=True)
    assert forced["counts"] == {"created": 1} and len(_rows(me)) == 2


def test_same_name_far_apart_is_a_different_place(me):
    out = _batch(me, [{"name": "Starbucks", "lat": 41.38, "lon": 2.17},
                      {"name": "Starbucks", "lat": 48.85, "lon": 2.35}])
    assert out["counts"] == {"created": 2}


# ----------------------------------------------------------- a list pushed more than once
def test_pushing_a_list_twice_changes_nothing_the_second_time(me):
    lst = [{"name": f"Place {i}", "lat": 40 + i / 10, "lon": 3.0} for i in range(5)]
    assert _batch(me, lst)["counts"] == {"created": 5}
    again = _batch(me, lst)
    assert again["counts"] == {"duplicate": 5} and len(_rows(me)) == 5
    assert [r["index"] for r in again["results"]] == [0, 1, 2, 3, 4]


def test_duplicates_inside_one_payload_are_caught(me):
    out = _batch(me, [{"url": G_FTID_A}, {"url": G_FTID_B}, {"name": "Solo", "lat": 1, "lon": 1},
                      {"name": "Solo", "lat": 1, "lon": 1}])
    assert out["counts"] == {"created": 2, "duplicate": 2}


def test_dry_run_reports_the_same_plan_and_writes_nothing(me):
    lst = [{"url": G_FTID_A}, {"url": G_FTID_B}, {"name": "Solo", "lat": 1, "lon": 1}, {"name": ""}]
    dry = _batch(me, lst, dry_run=True)
    real = _batch(me, lst)
    assert dry["dry_run"] is True and dry["counts"] == real["counts"]
    assert [r["outcome"] for r in dry["results"]] == [r["outcome"] for r in real["results"]]
    assert len(_rows(me)) == 2
    assert _batch(me, lst, dry_run=True)["counts"] == {"duplicate": 3, "error": 1}


def test_filed_and_removed_places_are_not_added_back(me):
    tid = _trip(me)
    lst = [{"name": "Filed", "lat": 10, "lon": 10}, {"name": "Removed", "lat": 20, "lon": 20},
           {"name": "Kept", "lat": 30, "lon": 30}]
    _batch(me, lst)
    ids = {p["name"]: p["id"] for p in client.get("/api/v1/places", headers=_h(me)).json()}
    assert client.post(f"/api/v1/places/{ids['Filed']}/file/{tid}", json={},
                       headers=_h(me)).status_code == 201
    assert client.delete(f"/api/v1/places/{ids['Removed']}", headers=_h(me)).status_code == 204
    out = _batch(me, lst)
    assert out["counts"] == {"already_filed": 1, "removed_earlier": 1, "duplicate": 1}
    assert [p["name"] for p in client.get("/api/v1/places", headers=_h(me)).json()] == ["Kept"]
    assert len(_rows(me)) == 3                                  # tombstones kept
    assert _batch(me, lst, revive=True)["counts"] == {
        "already_filed": 1, "revived": 1, "duplicate": 1}       # a removed one comes back, never a filed one


# ----------------------------------------------------------------------- error conditions
def test_a_bad_entry_is_reported_and_does_not_stop_the_rest(me):
    out = _batch(me, [{"name": "Good", "lat": 1, "lon": 1},
                      {},
                      {"url": "https://www.google.com/maps/dir/A/B/@1,2,3z"},
                      {"url": "javascript:alert(1)"},
                      {"name": "Also good", "lat": 2, "lon": 2}])
    assert [r["outcome"] for r in out["results"]] == [
        "created", "error", "error", "error", "created"]
    assert all(r["error"] for r in out["results"] if r["outcome"] == "error")
    assert len(_rows(me)) == 2


def test_a_batch_never_expands_a_short_link(me, monkeypatch):
    def boom(u):
        raise AssertionError("expanded")
    monkeypatch.setattr(maps_url, "_expand_short", boom)
    out = _batch(me, [{"url": "https://maps.app.goo.gl/AbCdEf"}])
    assert out["counts"] == {"error": 1} and "full place URL" in out["results"][0]["error"]


def test_batch_size_limits_and_empty(me):
    h = _h(me)
    assert client.post("/api/v1/places/batch", json={"places": []}, headers=h).status_code == 400
    big = [{"name": f"P{i}", "lat": 1, "lon": i / 1000} for i in range(places_svc.MAX_BATCH + 1)]
    r = client.post("/api/v1/places/batch", json={"places": big}, headers=h)
    assert r.status_code == 400 and "200" in r.json()["detail"]
    r = client.post("/api/v1/places/batch", headers=h, json={
        "places": big[:places_svc.MAX_ENRICH + 1], "enrich": True})
    assert r.status_code == 400 and "25" in r.json()["detail"]
    assert _rows(me) == []


def test_batch_is_own_only(me):
    other = User(username="dd_b", password_hash=authn.hash_password("pw"), auth_source="local")
    with SessionLocal() as s:
        s.add(other)
        s.commit()
        s.refresh(other)
        s.expunge(other)
    try:
        lst = [{"name": "Same", "lat": 1, "lon": 1}]
        assert _batch(me, lst)["counts"] == {"created": 1}
        assert _batch(other, lst)["counts"] == {"created": 1}   # not a duplicate of someone else's
        assert client.get("/api/v1/places", headers=_h(other)).json()[0]["id"] != \
            client.get("/api/v1/places", headers=_h(me)).json()[0]["id"]
    finally:
        with SessionLocal() as s:
            s.query(SavedPlace).filter(SavedPlace.owner_id == other.id).delete()
            s.query(User).filter(User.id == other.id).delete()
            s.commit()


# ------------------------------------------- a retrying client (the iOS outbox) gets told
def test_single_save_retries_report_what_happened(me):
    h, tid = _h(me), _trip(me)
    body = {"url": G_FTID_A, "source": "ios"}
    r = client.post("/api/v1/places", json=body, headers=h)
    assert r.status_code == 201 and r.json()["outcome"] == "created"
    pid = r.json()["id"]
    r = client.post("/api/v1/places", json=body, headers=h)                 # outbox retry
    assert r.status_code == 200 and r.json()["outcome"] == "duplicate" and r.json()["id"] == pid
    client.post(f"/api/v1/places/{pid}/file/{tid}", json={}, headers=h)
    r = client.post("/api/v1/places", json=body, headers=h)                 # retry after filing
    assert r.status_code == 200 and r.json()["outcome"] == "already_filed"
    assert r.json()["status"] == "filed" and r.json()["suggested_trip_id"] is None
    assert client.get("/api/v1/places", headers=h).json() == []             # not resurrected


def test_a_removed_place_stays_removed_unless_asked(me):
    h = _h(me)
    pid = client.post("/api/v1/places", json={"url": G_FTID_A}, headers=h).json()["id"]
    client.delete(f"/api/v1/places/{pid}", headers=h)
    again = client.post("/api/v1/places", json={"url": G_FTID_A}, headers=h)
    assert again.status_code == 200 and again.json()["outcome"] == "removed_earlier"
    assert client.get("/api/v1/places", headers=h).json() == []
    back = client.post("/api/v1/places", json={"url": G_FTID_A, "revive": True}, headers=h)
    assert back.status_code == 201 and back.json()["outcome"] == "revived"
    assert len(client.get("/api/v1/places", headers=h).json()) == 1


def test_save_response_carries_a_suggested_trip(me):
    from datetime import date, timedelta
    tid = _trip(me, "Soon")
    with SessionLocal() as s:
        t = s.get(Trip, tid)
        t.start_date = (date.today() + timedelta(days=2)).isoformat()
        t.end_date = (date.today() + timedelta(days=4)).isoformat()
        s.commit()
    r = client.post("/api/v1/places", json={"url": G_FTID_A}, headers=_h(me))
    assert r.json()["suggested_trip_id"] == tid        # what the Share Extension's sheet shows


def test_filing_twice_or_editing_a_filed_place_is_a_409(me):
    h, tid = _h(me), _trip(me)
    pid = client.post("/api/v1/places", json={"url": G_FTID_A}, headers=h).json()["id"]
    assert client.post(f"/api/v1/places/{pid}/file/{tid}", json={}, headers=h).status_code == 201
    assert client.post(f"/api/v1/places/{pid}/file/{tid}", json={}, headers=h).status_code == 409
    assert client.patch(f"/api/v1/places/{pid}", json={"name": "x"}, headers=h).status_code == 409
    assert client.delete(f"/api/v1/places/{pid}", headers=h).status_code == 409
    with SessionLocal() as s:
        assert s.query(TripActivity).filter_by(trip_id=tid).count() == 1


def test_removing_twice_is_a_404_not_a_crash(me):
    h = _h(me)
    pid = client.post("/api/v1/places", json={"url": G_FTID_A}, headers=h).json()["id"]
    assert client.delete(f"/api/v1/places/{pid}", headers=h).status_code == 204
    assert client.delete(f"/api/v1/places/{pid}", headers=h).status_code == 404
