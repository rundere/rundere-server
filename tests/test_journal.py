"""Yonder 4.x — Travel Journal (SPEC-6) API + scope tests.

Auth is required on every /api/v1 route; the journal is a PERSONAL resource
(own-only, D-134), so the sharp edge under test is isolation between users.
SQLite via conftest, same bootstrap as test_api.py.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import authn, journal as journal_svc
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, JournalEntry, Trip, User

    Base.metadata.create_all(engine)
    client = TestClient(app)

    def _user(name):
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

    def _bearer(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _trip(name, owner_id):
        with SessionLocal() as s:
            t = Trip(name=name, source="manual", owner_id=owner_id)
            s.add(t)
            s.commit()
            s.refresh(t)
            s.expunge(t)
            return t

    def _cleanup(*usernames):
        with SessionLocal() as s:
            ids = [u.id for u in s.query(User).filter(User.username.in_(usernames)).all()]
            if ids:
                s.query(JournalEntry).filter(JournalEntry.owner_id.in_(ids)).delete(
                    synchronize_session=False)
                s.query(Trip).filter(Trip.owner_id.in_(ids)).delete(
                    synchronize_session=False)
            s.commit()

    # ------------------------------------------------------------------ service unit
    def test_normalize_entry_date_defaults_and_validates():
        import re
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", journal_svc.normalize_entry_date(None))
        assert journal_svc.normalize_entry_date("2026-09-07") == "2026-09-07"
        try:
            journal_svc.normalize_entry_date("Sept 7")
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_captured_at_falls_back_to_now_not_error():
        # A bad stamp must never lose the note (D-142).
        assert journal_svc.parse_captured_at("not-a-time") is not None

    # ------------------------------------------------------------------ create/read
    def test_create_defaults_date_and_time():
        u = _user("journ_a")
        r = client.post("/api/v1/journal", headers=_bearer(u),
                        json={"body": "ferry was late, best fish tacos by the harbour"})
        assert r.status_code == 201, r.text
        j = r.json()
        assert j["entry_date"] and j["captured_at"]  # both stamped (D-142)
        assert j["trip_id"] is None                   # standalone-capable (D-133)
        assert j["body"].startswith("ferry")
        _cleanup("journ_a")

    def test_bad_entry_date_is_400():
        u = _user("journ_a")
        r = client.post("/api/v1/journal", headers=_bearer(u),
                        json={"entry_date": "yesterday", "body": "x"})
        assert r.status_code == 400
        _cleanup("journ_a")

    def test_geo_is_optional_and_stored():
        u = _user("journ_a")
        r = client.post("/api/v1/journal", headers=_bearer(u),
                        json={"body": "sunset", "place_lat": 36.7, "place_lon": -121.9})
        assert r.status_code == 201
        assert r.json()["place_lat"] == 36.7
        assert r.json()["place_accuracy_m"] is None
        _cleanup("journ_a")

    def test_geo_accuracy_is_stored_like_an_expenses():
        """Parity with expenses (YONDER-212 follow-up): the GPS accuracy in
        metres rides along with the coordinates, kept on duplicate, edit-able,
        and never negative."""
        u = _user("journ_a")
        h = _bearer(u)
        r = client.post("/api/v1/journal", headers=h, json={
            "body": "harbour", "place_lat": 36.6, "place_lon": -121.9, "place_accuracy_m": 12.5})
        assert r.status_code == 201 and r.json()["place_accuracy_m"] == 12.5
        eid = r.json()["id"]
        r = client.patch(f"/api/v1/journal/{eid}", headers=h, json={"place_accuracy_m": 30})
        assert r.json()["place_accuracy_m"] == 30
        assert client.post("/api/v1/journal", headers=h, json={
            "body": "x", "place_accuracy_m": -1}).status_code == 400
        _cleanup("journ_a")

    # ------------------------------------------------------------------ isolation (D-134)
    def test_journal_is_own_only():
        a, b = _user("journ_a"), _user("journ_b")
        rid = client.post("/api/v1/journal", headers=_bearer(a),
                          json={"body": "a private thought"}).json()["id"]
        # B cannot list it…
        assert all(e["id"] != rid for e in
                   client.get("/api/v1/journal", headers=_bearer(b)).json())
        # …nor fetch it by id (404, existence must not leak)
        assert client.get(f"/api/v1/journal/{rid}", headers=_bearer(b)).status_code == 404
        # A still sees it
        assert client.get(f"/api/v1/journal/{rid}", headers=_bearer(a)).status_code == 200
        _cleanup("journ_a", "journ_b")

    def test_cannot_delete_another_users_entry():
        a, b = _user("journ_a"), _user("journ_b")
        rid = client.post("/api/v1/journal", headers=_bearer(a),
                          json={"body": "mine"}).json()["id"]
        assert client.delete(f"/api/v1/journal/{rid}", headers=_bearer(b)).status_code == 404
        assert client.get(f"/api/v1/journal/{rid}", headers=_bearer(a)).status_code == 200
        _cleanup("journ_a", "journ_b")

    # ------------------------------------------------------------------ attach / edit
    def test_edit_body_without_attach_keeps_trip_null():
        u = _user("journ_a")
        rid = client.post("/api/v1/journal", headers=_bearer(u),
                          json={"body": "draft"}).json()["id"]
        r = client.patch(f"/api/v1/journal/{rid}", headers=_bearer(u),
                         json={"body": "edited", "trip_id": "should-be-ignored"})
        assert r.status_code == 200
        assert r.json()["body"] == "edited"
        assert r.json()["trip_id"] is None   # no attach_trip flag → link untouched
        _cleanup("journ_a")

    def test_attach_to_own_trip():
        u = _user("journ_a")
        t = _trip("Journal Trip A", u.id)
        rid = client.post("/api/v1/journal", headers=_bearer(u),
                          json={"body": "on the road"}).json()["id"]
        r = client.patch(f"/api/v1/journal/{rid}", headers=_bearer(u),
                         json={"attach_trip": True, "trip_id": t.id})
        assert r.status_code == 200
        assert r.json()["trip_id"] == t.id
        # list scoped to the trip finds it
        assert any(e["id"] == rid for e in
                   client.get(f"/api/v1/journal?trip_id={t.id}", headers=_bearer(u)).json())
        _cleanup("journ_a")

    def test_cannot_attach_to_invisible_trip():
        a, b = _user("journ_a"), _user("journ_b")
        t_b = _trip("B private trip", b.id)
        rid = client.post("/api/v1/journal", headers=_bearer(a),
                          json={"body": "x"}).json()["id"]
        r = client.patch(f"/api/v1/journal/{rid}", headers=_bearer(a),
                         json={"attach_trip": True, "trip_id": t_b.id})
        assert r.status_code == 404   # B's trip is not visible to A
        _cleanup("journ_a", "journ_b")

    # ------------------------------------------------------------------ duplicate (D-133)
    def test_duplicate_forks_independent_copy_for_another_trip():
        u = _user("journ_a")
        t1, t2 = _trip("Trip One", u.id), _trip("Trip Two", u.id)
        rid = client.post("/api/v1/journal", headers=_bearer(u),
                          json={"body": "shared memory", "attach_trip": True}).json()["id"]
        client.patch(f"/api/v1/journal/{rid}", headers=_bearer(u),
                     json={"attach_trip": True, "trip_id": t1.id})
        dup = client.post(f"/api/v1/journal/{rid}/duplicate", headers=_bearer(u),
                          json={"trip_id": t2.id}).json()
        assert dup["id"] != rid
        assert dup["trip_id"] == t2.id
        assert dup["duplicated_from"] == rid
        assert dup["body"] == "shared memory"
        # editing the copy leaves the original untouched
        client.patch(f"/api/v1/journal/{dup['id']}", headers=_bearer(u),
                     json={"body": "changed on the copy"})
        orig = client.get(f"/api/v1/journal/{rid}", headers=_bearer(u)).json()
        assert orig["body"] == "shared memory"
        assert orig["trip_id"] == t1.id
        _cleanup("journ_a")

    def test_delete_removes_entry():
        u = _user("journ_a")
        rid = client.post("/api/v1/journal", headers=_bearer(u),
                          json={"body": "temp"}).json()["id"]
        assert client.delete(f"/api/v1/journal/{rid}", headers=_bearer(u)).status_code == 204
        assert client.get(f"/api/v1/journal/{rid}", headers=_bearer(u)).status_code == 404
        _cleanup("journ_a")

    def test_journal_requires_auth():
        assert client.get("/api/v1/journal").status_code in (401, 403)
