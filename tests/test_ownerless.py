"""YONDER-186 / 165: ownerless ("open") rows are closed, and none get made.

With sign-in on, a trip or capture with no owner is visible to admins (and a
trip's own travellers) only — it used to be visible AND editable by every
signed-in user. An unscoped ingest key is refused so no new ownerless captures
(or resolver-minted trips) appear, and an admin can give an orphan an owner.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, authn
from app.config import settings
from app.db import SessionLocal, engine
from app.main import app, ownerless_counts
from app.models import ApiKey, Base, IngestBatch, TravelerIdentity, Trip, TripTraveler, User

Base.metadata.create_all(engine)
settings.auth_cookie_secure = False
client = TestClient(app)


@pytest.fixture(autouse=True)
def _cleanup_orphans():
    """Trips and traveller identities left in the shared test DB change what
    the resolver matches in other files — remove the ones made here."""
    yield
    with SessionLocal() as s:
        ids = [t.id for t in s.query(Trip).filter(Trip.name.like("own-orphan-%"))]
        if ids:
            s.query(TripTraveler).filter(TripTraveler.trip_id.in_(ids)).delete(
                synchronize_session=False)
            s.query(Trip).filter(Trip.id.in_(ids)).delete(synchronize_session=False)
        # …and the roster identities: the resolver reads every traveller
        tids = [t.id for t in s.query(TravelerIdentity)
                .filter(TravelerIdentity.display_name.like("Own Roster%"))]
        if tids:
            s.query(User).filter(User.traveler_id.in_(tids)).update(
                {User.traveler_id: None}, synchronize_session=False)
            s.query(TravelerIdentity).filter(TravelerIdentity.id.in_(tids)).delete(
                synchronize_session=False)
        s.commit()


def _user(name, admin=False, traveler_id=None):
    with SessionLocal() as s:
        u = s.query(User).filter_by(username=name).one_or_none()
        if u is None:
            u = User(username=name, password_hash=authn.hash_password("pw"),
                     auth_source="local", is_admin=admin, share_with_admins=True,
                     sharing_prompt=False, traveler_id=traveler_id)
            s.add(u)
            s.commit()
        s.refresh(u)
        s.expunge(u)
        return u


def _bearer(user):
    return {"Authorization": f"Bearer {authn.issue_access_token(user)}"}


def _orphan(name):
    with SessionLocal() as s:
        t = Trip(name=name, source="resolver", owner_id=None)
        s.add(t)
        s.commit()
        return t.id


def _key(name, user_id=None):
    key = auth.generate_key()
    with SessionLocal() as s:
        s.add(ApiKey(client_name=name, key_hash=auth.hash_key(key), user_id=user_id))
        s.commit()
    return key


def _ingest(key, body=None):
    raw = json.dumps(body or {"subject": "x", "html": "<p>x</p>"}).encode()
    ts = str(int(time.time()))
    return client.post("/ingest", content=raw, headers={
        "Content-Type": "application/json", "X-Api-Key": key,
        "X-Timestamp": ts, "X-Signature": auth.sign(key, ts, raw)})


# --------------------------------------------------------------------------- ingest
def test_unscoped_key_refused_with_sign_in_on(monkeypatch):
    key = _key("own-unscoped")
    monkeypatch.setattr(settings, "auth_required", True)
    with SessionLocal() as s:
        before = s.query(IngestBatch).count()
    r = _ingest(key)
    assert r.status_code == 403
    assert "isn't linked to a user" in r.json()["error"]
    with SessionLocal() as s:
        assert s.query(IngestBatch).count() == before


def test_unscoped_key_still_works_with_sign_in_off(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", False)
    assert _ingest(_key("own-unscoped-off")).status_code == 202


def test_user_key_ingests_owned_batch(monkeypatch):
    u = _user("own-keyholder")
    monkeypatch.setattr(settings, "auth_required", True)
    r = _ingest(_key("own-scoped", user_id=u.id))
    assert r.status_code == 202
    with SessionLocal() as s:
        assert s.get(IngestBatch, r.json()["batch_id"]).user_id == u.id


# --------------------------------------------------------------------------- trip access
def test_orphan_trip_hidden_from_strangers_but_not_admins_or_travellers(monkeypatch):
    with SessionLocal() as s:
        tr = TravelerIdentity(display_name="Own Roster Person")
        s.add(tr)
        s.commit()
        trav_id = tr.id
    stranger = _user("own-stranger")
    admin = _user("own-admin", admin=True)
    traveller = _user("own-traveller", traveler_id=trav_id)
    tid = _orphan("own-orphan-a")
    with SessionLocal() as s:
        s.add(TripTraveler(trip_id=tid, traveler_id=trav_id))
        s.commit()
    monkeypatch.setattr(settings, "auth_required", True)
    assert client.get(f"/trips/{tid}", headers=_bearer(stranger)).status_code == 404
    assert client.get(f"/api/v1/trips/{tid}", headers=_bearer(stranger)).status_code == 404
    assert client.post(f"/trips/{tid}", headers=_bearer(stranger),
                       data={"name": "hijacked"}).status_code == 404
    assert client.get(f"/trips/{tid}", headers=_bearer(admin)).status_code == 200
    assert client.get(f"/api/v1/trips/{tid}", headers=_bearer(traveller)).status_code == 200
    with SessionLocal() as s:
        assert s.get(Trip, tid).name == "own-orphan-a"


# --------------------------------------------------------------------------- assign owner
def test_admin_assigns_owner_to_orphan(monkeypatch):
    admin = _user("own-admin2", admin=True)
    new_owner = _user("own-new-owner")
    tid = _orphan("own-orphan-b")
    monkeypatch.setattr(settings, "auth_required", True)
    page = client.get(f"/trips/{tid}", headers=_bearer(admin))
    assert "Assign owner" in page.text
    r = client.post(f"/trips/{tid}/owner", headers=_bearer(admin),
                    data={"user_id": new_owner.id}, follow_redirects=False)
    assert r.status_code in (200, 303)
    with SessionLocal() as s:
        assert s.get(Trip, tid).owner_id == new_owner.id
    # now the owner sees it, and it can't be reassigned through this route
    assert client.get(f"/api/v1/trips/{tid}", headers=_bearer(new_owner)).status_code == 200
    again = client.post(f"/trips/{tid}/owner", headers=_bearer(admin),
                        data={"user_id": admin.id})
    # D-211: once it has an owner it is theirs. This owner lets admins look, so
    # the admin may still view it, read-only: the write is refused.
    assert again.status_code == 403


def test_non_admin_cannot_assign_owner(monkeypatch):
    with SessionLocal() as s:
        tr = TravelerIdentity(display_name="Own Roster Two")
        s.add(tr)
        s.commit()
        trav_id = tr.id
    traveller = _user("own-traveller2", traveler_id=trav_id)
    tid = _orphan("own-orphan-c")
    with SessionLocal() as s:
        s.add(TripTraveler(trip_id=tid, traveler_id=trav_id))
        s.commit()
    monkeypatch.setattr(settings, "auth_required", True)
    # a traveller can see the orphan, but not take ownership of it
    assert "Assign owner" not in client.get(f"/trips/{tid}", headers=_bearer(traveller)).text
    r = client.post(f"/trips/{tid}/owner", headers=_bearer(traveller),
                    data={"user_id": traveller.id})
    assert r.status_code == 403
    with SessionLocal() as s:
        assert s.get(Trip, tid).owner_id is None


# --------------------------------------------------------------------------- drift
def test_ownerless_counts_reports_orphans():
    _orphan("own-orphan-d")
    with SessionLocal() as s:
        assert ownerless_counts(s).get("trips", 0) >= 1
