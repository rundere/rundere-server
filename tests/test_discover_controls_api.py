"""5.1, YONDER-411 part 1: the web's Discover controls for the apps — edit,
lock and re-run your own findings, hide or bring back your own rows.

Discover is per person (4.1): someone else's finding or row is missing (404),
even on a trip you both edit; a view-only share gets 403 (it can't run
Discover). Fictional people; SQLite via conftest.
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
    from app.models import (Base, DiscoveryFinding, DiscoveryItem, TravelerIdentity, Trip,
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
    """Ana and Ben travel on one trip (each with their own Discover); Vera may
    view it; Sam is a stranger."""
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as s:
        travs = {n: TravelerIdentity(display_name=f"DC {n} {tag}", kind="guest")
                 for n in ("ana", "ben")}
        s.add_all(travs.values())
        s.flush()
        users = {n: User(username=f"dc-{n}-{tag}", auth_source="local",
                         traveler_id=travs[n].id if n in travs else None)
                 for n in ("ana", "ben", "vera", "sam")}
        s.add_all(users.values())
        s.flush()
        trip = Trip(name=f"DC trip {tag}", owner_id=users["ana"].id,
                    start_date="2030-05-01", end_date="2030-05-09")
        s.add(trip)
        s.flush()
        s.add_all([TripTraveler(trip_id=trip.id, traveler_id=travs["ana"].id, role="self"),
                   TripTraveler(trip_id=trip.id, traveler_id=travs["ben"].id,
                                role="companion"),
                   TripMember(trip_id=trip.id, user_id=users["vera"].id)])

        def finding(who, topic, **kw):
            f = DiscoveryFinding(trip_id=trip.id, user_id=users[who].id, topic=topic,
                                 scope="country", place_key="ZZ", status="draft",
                                 body_md=f"**Testland**\n\nDC {who} {topic}", **kw)
            s.add(f)
            return f

        fs = {"plugs": finding("ana", "plugs"),
              "weather": finding("ana", "weather"),
              "food": finding("ana", "food", data={"summary": "DC old summary"}),
              "research": finding("ana", "research"),
              "ben_plugs": finding("ben", "plugs")}
        s.flush()
        items = {}
        for who, key in (("ana", "food"), ("ben", "food")):
            i = DiscoveryItem(finding_id=fs["food"].id, trip_id=trip.id,
                              user_id=users[who].id, topic="food", place_key="ZZ",
                              kind="restaurant", name=f"DC {who}'s noodle bar")
            s.add(i)
            items[who] = i
        s.commit()
        ids = {"trip": trip.id, **{k: f.id for k, f in fs.items()},
               **{f"item_{k}": i.id for k, i in items.items()}}
        heads = {n: {"Authorization": f"Bearer {authn.issue_access_token(u)}"}
                 for n, u in users.items()}
        uids = [u.id for u in users.values()]
        tids = [t.id for t in travs.values()]
    yield ids, heads
    with SessionLocal() as s:
        s.query(DiscoveryItem).filter_by(trip_id=ids["trip"]).delete()
        s.query(DiscoveryFinding).filter_by(trip_id=ids["trip"]).delete()
        for M in (TripTraveler, TripMember):
            s.query(M).filter_by(trip_id=ids["trip"]).delete()
        s.query(Trip).filter_by(id=ids["trip"]).delete()
        s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
        s.query(TravelerIdentity).filter(TravelerIdentity.id.in_(tids)).delete(
            synchronize_session=False)
        s.commit()


def _f(fid):
    with SessionLocal() as s:
        return s.get(DiscoveryFinding, fid)


# ------------------------------------------------------------------ edit
def test_findings_say_how_they_are_edited(world):
    ids, h = world
    snap = client.get(f"{T}/{ids['trip']}", headers=h["ana"]).json()
    kinds = {f["topic"]: (f["edit_kind"], f["lockable"]) for f in snap["discovery"]}
    assert kinds["plugs"] == ("body", True)
    assert kinds["food"] == ("summary", False)
    assert kinds["weather"] == ("body", False)


def test_editing_a_lockable_finding_locks_it(world):
    ids, h = world
    r = client.patch(f"{T}/{ids['trip']}/findings/{ids['plugs']}", headers=h["ana"],
                     json={"body": "**Testland**\n\nDC type Z sockets"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "edited" and r.json()["locked"] is True
    assert _f(ids["plugs"]).body_md.endswith("DC type Z sockets")


def test_food_edits_the_city_summary(world):
    ids, h = world
    r = client.patch(f"{T}/{ids['trip']}/findings/{ids['food']}", headers=h["ana"],
                     json={"summary": "DC the night markets"})
    assert r.status_code == 200
    assert _f(ids["food"]).data["summary"] == "DC the night markets"
    # The wrong field for the topic is refused, and so is two at once.
    for body in ({"body": "x"}, {"summary": "x", "body": "y"}):
        assert client.patch(f"{T}/{ids['trip']}/findings/{ids['food']}", headers=h["ana"],
                            json=body).status_code == 422


# ------------------------------------------------------------------ lock
def test_lock_and_unlock(world):
    ids, h = world
    url = f"{T}/{ids['trip']}/findings/{ids['plugs']}/lock"
    r = client.post(url, headers=h["ana"], json={"locked": True})
    assert r.json()["status"] == "approved" and r.json()["locked"]
    r = client.post(url, headers=h["ana"], json={"locked": False})
    assert r.json()["status"] == "draft" and not r.json()["locked"]
    # Weather is refreshed every run: it can't be locked.
    assert client.post(f"{T}/{ids['trip']}/findings/{ids['weather']}/lock",
                       headers=h["ana"], json={"locked": True}).status_code == 422


# ------------------------------------------------------------------ re-run
def test_rerun_regenerates_one_finding(world, monkeypatch):
    ids, h = world
    from discover import runner
    seen = {}

    def fake(session, f, **kw):
        seen["base"] = kw["base_currencies"]
        f.body_md = "**Testland**\n\nDC fresh forecast"
        session.commit()
        return f
    monkeypatch.setattr(runner, "rerun_finding", fake)
    r = client.post(f"{T}/{ids['trip']}/findings/{ids['weather']}/rerun", headers=h["ana"])
    assert r.status_code == 200
    assert r.json()["body_md"].endswith("DC fresh forecast") and seen["base"]
    # An AI agent's research isn't re-run locally.
    assert client.post(f"{T}/{ids['trip']}/findings/{ids['research']}/rerun",
                       headers=h["ana"]).status_code == 409


def test_an_ended_trip_isnt_rerun(world, monkeypatch):
    ids, h = world
    from discover import runner
    monkeypatch.setattr(runner, "trip_has_ended", lambda trip: True)
    monkeypatch.setattr(runner, "rerun_finding",
                        lambda *a, **k: pytest.fail("must not re-run an ended trip"))
    assert client.post(f"{T}/{ids['trip']}/findings/{ids['weather']}/rerun",
                       headers=h["ana"]).status_code == 409


# ------------------------------------------------------------------ hide
def test_hide_and_bring_back_a_row(world):
    ids, h = world
    url = f"{T}/{ids['trip']}/discovery-items/{ids['item_ana']}/hide"
    r = client.post(url, headers=h["ana"], json={"hidden": True})
    assert r.status_code == 200
    assert r.json()["hidden"] and r.json()["hidden_reason"] == "not_my_thing"
    # It's what agents read as a dislike.
    prof = client.get("/api/v1/me/discovery-profile", headers=h["ana"]).json()
    assert any("noodle" in str(d) for d in prof["not_my_thing"])
    r = client.post(url, headers=h["ana"], json={"hidden": True, "reason": "closed"})
    assert r.json()["hidden_reason"] == "closed"
    r = client.post(url, headers=h["ana"], json={"hidden": False})
    assert not r.json()["hidden"] and r.json()["hidden_reason"] is None
    assert client.post(url, headers=h["ana"], json={"reason": "dc-meh"}).status_code == 422


# ------------------------------------------------------------------ scoping
def test_someone_elses_findings_and_rows_are_missing(world):
    ids, h = world
    t = ids["trip"]
    for who in ("ben", "ana"):
        other_f = ids["plugs"] if who == "ben" else ids["ben_plugs"]
        other_i = ids["item_ana"] if who == "ben" else ids["item_ben"]
        for method, path, body in (
                ("patch", f"{T}/{t}/findings/{other_f}", {"body": "mine now"}),
                ("post", f"{T}/{t}/findings/{other_f}/lock", {"locked": True}),
                ("post", f"{T}/{t}/findings/{other_f}/rerun", None),
                ("post", f"{T}/{t}/discovery-items/{other_i}/hide", {"hidden": True})):
            r = client.request(method, path, headers=h[who], json=body)
            assert r.status_code == 404, (who, method, path, r.status_code)
    assert _f(ids["plugs"]).status == "draft" and _f(ids["ben_plugs"]).status == "draft"
    with SessionLocal() as s:
        assert not s.get(DiscoveryItem, ids["item_ben"]).hidden


def test_a_viewer_gets_403_and_a_stranger_404(world):
    ids, h = world
    t = ids["trip"]
    for who, code in (("vera", 403), ("sam", 404)):
        for method, path, body in (
                ("patch", f"{T}/{t}/findings/{ids['plugs']}", {"body": "x"}),
                ("post", f"{T}/{t}/findings/{ids['plugs']}/lock", {"locked": True}),
                ("post", f"{T}/{t}/findings/{ids['weather']}/rerun", None),
                ("post", f"{T}/{t}/discovery-items/{ids['item_ana']}/hide", {"hidden": True})):
            r = client.request(method, path, headers=h[who], json=body)
            assert r.status_code == code, (who, method, path, r.status_code)
    assert _f(ids["plugs"]).status == "draft"
