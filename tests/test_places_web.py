"""YONDER-200 slice 3b: a Maps PLACE link pasted on /capture becomes a saved place
(not a booking capture), and the /places page files it to a trip. Own-only (D-166).
Photon and short-link expansion are stubbed — nothing here touches the network."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

try:
    from fastapi.testclient import TestClient
    from app import authn
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, IngestBatch, SavedPlace, Trip, TripActivity, User
    Base.metadata.create_all(engine)
    client = TestClient(app)
except ImportError:                                     # bare host: the container/CI runs these
    pytest.skip("fastapi not installed", allow_module_level=True)

from extract import maps_url
from resolve import place_lookup

G_PLACE = ("https://www.google.com/maps/place/Sagrada+Fam%C3%ADlia/@41.4036,2.1744,17z/"
           "data=!3m1!4b1!8m2!3d41.4036299!4d2.1743558")
G_DIR = "https://www.google.com/maps/dir/Toronto,+ON/Ottawa,+ON/@44.7,-77.0,8z"
A_Q = "https://maps.apple.com/?q=Le+Bernardin&ll=40.7615,-73.9817"
SHORT = "https://maps.app.goo.gl/AbCdEf"
_TRIP_NAMES = ("Barcelona", "B trip", "Soon")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(place_lookup, "_default_geocoder", lambda: None)
    monkeypatch.setattr(maps_url, "_expand_short", lambda u: u)


@pytest.fixture(autouse=True)
def _clean():
    yield
    with SessionLocal() as s:                  # only what these tests made
        ids = [t.id for t in s.query(Trip).filter(Trip.name.in_(_TRIP_NAMES)).all()]
        s.query(TripActivity).filter(TripActivity.trip_id.in_(ids)).delete(
            synchronize_session=False)
        s.query(Trip).filter(Trip.id.in_(ids)).delete(synchronize_session=False)
        s.query(SavedPlace).delete()
        s.commit()


def _places():
    with SessionLocal() as s:
        return s.query(SavedPlace).all()


def _batches():
    with SessionLocal() as s:
        return s.query(IngestBatch).count()


def _trip(name="Barcelona", owner_id=None):
    with SessionLocal() as s:
        t = Trip(name=name, source="manual", owner_id=owner_id)
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.id


def test_pasted_place_link_is_saved_not_captured():
    before = _batches()
    r = client.post("/capture/web", data={"content": G_PLACE})
    assert r.status_code == 200 and "Saved" in r.text and "Sagrada Família" in r.text
    assert 'href="/places"' in r.text
    assert [p.name for p in _places()] == ["Sagrada Família"]
    assert _batches() == before                 # never entered the bookings pipeline (D-167)


def test_pasting_the_same_place_twice_says_so():
    client.post("/capture/web", data={"content": A_Q})
    r = client.post("/capture/web", data={"content": A_Q})
    assert "already in your saved places" in r.text and len(_places()) == 1


def test_short_google_link_that_expands_to_a_place_is_saved(monkeypatch):
    monkeypatch.setattr(maps_url, "_expand_short", lambda u: G_PLACE)
    r = client.post("/capture/web", data={"content": SHORT})
    assert "Saved" in r.text and [p.provider for p in _places()] == ["google"]


def test_short_google_link_that_expands_to_directions_still_needs_a_date(monkeypatch):
    monkeypatch.setattr(maps_url, "_expand_short", lambda u: G_DIR)
    before = _batches()
    r = client.post("/capture/web", data={"content": SHORT})
    assert "Travel date" in r.text and _places() == [] and _batches() == before


def test_directions_links_are_unaffected():
    r = client.post("/capture/web", data={"content": G_DIR, "travel_date": "2026-09-23"})
    assert "captured" in r.text.lower() and _places() == []


def test_explicit_directions_path_refuses_a_place_link():
    r = client.post("/capture/web", data={"content": G_PLACE, "path": "google"})
    assert "place link, not directions" in r.text and _places() == []


def test_capture_page_mentions_saved_places_and_does_not_require_a_date_for_short_links():
    t = client.get("/capture").text
    assert 'href="/places"' in t and "isShort" in t


# ---------------------------------------------------------------- /places page
def _save(url=A_Q):
    client.post("/capture/web", data={"content": url})
    return _places()[-1].id


def test_places_page_lists_saved_places_and_empty_state():
    assert "No saved places" in client.get("/places").text
    _save(G_PLACE)
    t = client.get("/places").text
    assert "Sagrada Família" in t and "File to a trip" in t


def test_file_without_a_date_lands_undated_and_consumes_the_place():
    pid, tid = _save(), _trip()
    r = client.post(f"/places/{pid}/file", data={"trip_id": tid})
    assert r.status_code == 200 and "no date yet" in r.text and f"/trips/{tid}" in r.text
    assert _places() == []
    with SessionLocal() as s:
        a = s.query(TripActivity).filter_by(trip_id=tid).one()
        assert a.values["name"] == "Le Bernardin" and not a.starts_at


def test_file_with_a_date():
    pid, tid = _save(), _trip()
    r = client.post(f"/places/{pid}/file", data={"trip_id": tid, "starts_at": "2026-10-12"})
    assert "on 2026-10-12" in r.text
    with SessionLocal() as s:
        assert s.query(TripActivity).filter_by(trip_id=tid).one().starts_at == "2026-10-12"


def test_file_needs_a_trip_and_keeps_the_place():
    pid = _save()
    r = client.post(f"/places/{pid}/file", data={"trip_id": ""})
    assert "Pick a trip" in r.text and len(_places()) == 1
    r = client.post(f"/places/{pid}/file", data={"trip_id": "nope"})
    assert "cap-msg err" in r.text and len(_places()) == 1


def test_remove_a_saved_place():
    pid = _save()
    client.post(f"/places/{pid}/delete")
    assert _places() == []


def test_places_page_is_own_only(monkeypatch):
    """Another signed-in user sees none of it and cannot file or remove it. The web
    routes read the user the auth middleware put on the request, so the test sets
    that directly (test_journal_web.py covers the auth-off default)."""
    from app import scope

    def mk(name):
        with SessionLocal() as s:
            u = s.query(User).filter_by(username=name).one_or_none()
            if u is None:
                u = User(username=name, password_hash=authn.hash_password("pw"),
                         auth_source="local")
                s.add(u)
                s.commit()
            s.refresh(u)
            s.expunge(u)
            return u
    try:
        a, b = mk("pw_a"), mk("pw_b")
        acting = {"u": a}
        monkeypatch.setattr(scope, "user_of", lambda request: acting["u"])
        client.post("/capture/web", data={"content": G_PLACE},
                    headers={"Authorization": f"Bearer {authn.issue_access_token(a)}"})
        pid = _places()[0].id
        assert _places()[0].owner_id == a.id
        assert "Sagrada" in client.get("/places").text
        acting["u"] = b
        assert "Sagrada" not in client.get("/places").text
        tb = _trip("B trip", b.id)
        r = client.post(f"/places/{pid}/file", data={"trip_id": tb})
        assert "cap-msg err" in r.text and len(_places()) == 1
        client.post(f"/places/{pid}/delete")
        assert len(_places()) == 1
    finally:
        with SessionLocal() as s:
            s.query(User).filter(User.username.in_(["pw_a", "pw_b"])).delete(
                synchronize_session=False)
            s.commit()


# ------------------------------------------------- review follow-ups on this PR
def test_anonymous_paste_is_refused_when_sign_in_is_required(monkeypatch):
    """/capture is a public path, so the route itself must not write an ownerless
    row (YONDER-186) or do lookups for a stranger."""
    from app import capture
    monkeypatch.setattr(capture.settings, "auth_required", True)
    r = client.post("/capture/web", data={"content": G_PLACE})
    assert "Sign in to save places" in r.text and _places() == []


def test_rename_a_saved_place_from_the_page():
    pid = _save()
    r = client.post(f"/places/{pid}/rename", data={"name": "Le Bernardin NYC"})
    assert "Le Bernardin NYC" in r.text and _places()[0].name == "Le Bernardin NYC"
    r = client.post(f"/places/{pid}/rename", data={"name": "  "})
    assert "cap-msg err" in r.text and _places()[0].name == "Le Bernardin NYC"


def test_places_page_preselects_the_suggested_trip_but_does_not_file():
    from datetime import date, timedelta
    pid, tid = _save(), _trip("Soon")
    with SessionLocal() as s:
        t = s.get(Trip, tid)
        t.start_date = (date.today() + timedelta(days=2)).isoformat()
        t.end_date = (date.today() + timedelta(days=5)).isoformat()
        s.commit()
    page = client.get("/places").text
    assert f'<option value="{tid}" selected>' in page and "(suggested)" in page
    assert len(_places()) == 1                                   # nothing filed by suggesting


def test_nav_has_a_places_entry():
    t = client.get("/places").text
    assert 'href="/places"' in t and ">Places<" in t.replace("</span>Places<", ">Places<")
