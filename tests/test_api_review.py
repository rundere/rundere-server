"""YONDER-57 — the review pipeline over the JSON API (SPEC-4.2 §3.5).

Queue + to_process, the detail payload (editable fields, who flew / who is it
for, trips you can file into), edits with field locks, approve / unapprove /
ignore, process + commit jobs scoped to the caller, and job visibility.
SQLite via conftest; no targets configured, so nothing reaches AirTrail or
AdventureLog.
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, CommitAttempt, TravelerIdentity, TravelItem, Trip,
                            TripTraveler, User)
    from app.review import jobs

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)
    NOW = "2026-09-26T00:00:00Z"

    def _flight(dep_local="2026-10-01T08:00", precision="datetime", travellers=None):
        return {"item_type": "flight", "booking_status": "confirmed",
                "booking_reference": "RV57",
                "travelers": travellers or [],
                "flight": {"flight_number": "57", "marketing_carrier": {"name": "T", "iata": "TA"},
                           "departure": {"place": {"raw": "YYZ", "iata": "YYZ"},
                                         "time": {"local": dep_local, "precision": precision,
                                                  "origin": "document"}},
                           "arrival": {"place": {"raw": "GVA", "iata": "GVA"},
                                       "time": {"local": "2026-10-01T20:00",
                                                "precision": "datetime", "origin": "document"}}}}

    def _event():
        return {"item_type": "event", "booking_status": "confirmed",
                "event": {"title": "RV57 Concert",
                          "start": {"local": "2026-10-02T19:00", "precision": "datetime",
                                    "origin": "document"}}}

    def _item(s, owner_id, payload, *, resolve="ok", review=None, resolution=None, trip_id=None):
        it = TravelItem(item_type=payload["item_type"], schema_version="1.4.0",
                        owner_id=owner_id, canonical_key=None, trip_id=trip_id,
                        source={"type": "gmail", "ref": "rv57", "received_at": NOW,
                                "capture_context": {}},
                        extraction={"extractor": "llm", "confidence": 0.9, "payload": payload},
                        resolution=resolution if resolution is not None else
                        {"outcome": "resolved", "targets": ["airtrail.flight"]},
                        review=review if review is not None else {"required": False},
                        status={"extract": "ok", "resolve": resolve})
        s.add(it)
        s.flush()
        return it

    @pytest.fixture()
    def w():
        with SessionLocal() as s:
            _teardown(s)
            ta, tb = TravelerIdentity(display_name="RV Alice", kind="guest"), \
                TravelerIdentity(display_name="RV Bob", kind="guest")
            s.add_all([ta, tb]); s.flush()
            alice = User(username="rv-alice", auth_source="local", traveler_id=ta.id)
            bob = User(username="rv-bob", auth_source="local", traveler_id=tb.id)
            s.add_all([alice, bob]); s.flush()
            trip = Trip(name="RV Trip", source="manual", owner_id=alice.id,
                        start_date="2026-09-30", end_date="2026-10-05")
            bobs = Trip(name="RV Bob Private", source="manual", owner_id=bob.id)
            s.add_all([trip, bobs]); s.flush()
            s.add_all([TripTraveler(trip_id=trip.id, traveler_id=ta.id, role="owner"),
                       TripTraveler(trip_id=trip.id, traveler_id=tb.id, role="companion")])
            flight = _item(s, alice.id, _flight())
            unresolved = _item(s, alice.id, _flight(dep_local="2026-10-01", precision="date"),
                               resolve="needs_review",
                               review={"required": True, "reasons": ["missing_time"]})
            event = _item(s, alice.id, _event(), trip_id=trip.id,
                          resolution={"outcome": "resolved", "targets": ["adventurelog.note"]})
            bob_item = _item(s, bob.id, _flight(),
                             review={"required": False, "action_taken": "approved"})
            s.commit()
            ids = {"alice": alice.id, "bob": bob.id, "ta": ta.id, "tb": tb.id,
                   "trip": trip.id, "bobs": bobs.id, "flight": flight.id,
                   "unresolved": unresolved.id, "event": event.id, "bob_item": bob_item.id}
        yield ids
        with SessionLocal() as s:
            _teardown(s)

    def _teardown(s):
        uids = [u.id for u in s.query(User).filter(User.username.like("rv-%")).all()]
        iids = [i.id for i in s.query(TravelItem).filter(TravelItem.owner_id.in_(uids)).all()]
        s.query(CommitAttempt).filter(CommitAttempt.item_id.in_(iids)).delete(synchronize_session=False)
        s.query(TravelItem).filter(TravelItem.id.in_(iids)).delete(synchronize_session=False)
        tids = [t.id for t in s.query(Trip).filter(Trip.name.like("RV %")).all()]
        s.query(TripTraveler).filter(TripTraveler.trip_id.in_(tids)).delete(synchronize_session=False)
        s.query(Trip).filter(Trip.id.in_(tids)).delete(synchronize_session=False)
        s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
        s.query(TravelerIdentity).filter(TravelerIdentity.display_name.like("RV %")).delete(
            synchronize_session=False)
        s.commit()

    def H(uid):
        with SessionLocal() as s:
            return {"Authorization": f"Bearer {authn.issue_access_token(s.get(User, uid))}"}

    # ------------------------------------------------------------------ read
    def test_queue_counts_to_process_and_scoping(w):
        r = client.get("/api/v1/review", headers=H(w["alice"])).json()
        ids = {i["id"] for i in r["items"]}
        assert {w["flight"], w["unresolved"], w["event"]} <= ids and w["bob_item"] not in ids
        assert isinstance(r["to_process"], int) and r["counts"]["all"] >= 3

    def test_detail_payload(w):
        d = client.get(f"/api/v1/review/{w['flight']}", headers=H(w["alice"])).json()
        paths = {f["path"]: f for f in d["fields"]}
        assert "/flight/flight_number" in paths and paths["/flight/departure/time"]["kind"] == "datetime"
        assert paths["/booking_status"]["options"]
        assert d["passengers"] is not None and d["who"] is None
        trips = {t["name"]: t for t in d["trip_options"]}
        assert "RV Trip" in trips and "RV Bob Private" not in trips
        assert trips["RV Trip"]["contains"] is True
        assert d["can_approve"] and not d["can_unapprove"] and d["can_ignore"]
        ev = client.get(f"/api/v1/review/{w['event']}", headers=H(w["alice"])).json()
        assert ev["passengers"] is None
        assert {t["name"] for t in ev["who"]} == {"RV Alice", "RV Bob"}
        un = client.get(f"/api/v1/review/{w['unresolved']}", headers=H(w["alice"])).json()
        assert un["bucket"] == "attention" and not un["can_approve"]
        assert paths["/flight/departure/time"]["missing"] is False
        assert {f["path"]: f for f in un["fields"]}["/flight/departure/time"]["missing"] is True

    def test_other_users_item_is_404_everywhere(w):
        h = H(w["alice"])
        bid = w["bob_item"]
        assert client.get(f"/api/v1/review/{bid}", headers=h).status_code == 404
        assert client.patch(f"/api/v1/review/{bid}", headers=h,
                            json={"fields": {"/booking_reference": "X"}}).status_code == 404
        for action in ("approve", "unapprove", "ignore"):
            assert client.post(f"/api/v1/review/{bid}/{action}", headers=h).status_code == 404
        with SessionLocal() as s:
            assert s.get(TravelItem, bid).review["action_taken"] == "approved"

    # ------------------------------------------------------------------ edits
    def test_edit_fields_become_locks_and_reresolve(w):
        r = client.patch(f"/api/v1/review/{w['unresolved']}", headers=H(w["alice"]),
                         json={"fields": {"/flight/departure/time": "2026-10-01 0805",
                                          "/booking_reference": "NEWREF"}})
        assert r.status_code == 200, r.text
        out = r.json()
        assert set(out["changed"]) == {"/flight/departure/time", "/booking_reference"}
        assert set(out["item"]["locked_fields"]) >= set(out["changed"])
        with SessionLocal() as s:
            it = s.get(TravelItem, w["unresolved"])
            t = it.extraction["payload"]["flight"]["departure"]["time"]
            assert t["local"] == "2026-10-01T08:05" and t["precision"] == "datetime"
            assert it.review["action_taken"] == "edited" and it.review["resolved_by"] == "api"
            assert (it.status or {}).get("resolve") != "pending"      # re-resolved

    def test_edit_validation(w):
        h = H(w["alice"])
        url = f"/api/v1/review/{w['flight']}"
        for body, frag in [({"fields": {"/flight/nope": "x"}}, "not an editable field"),
                           ({"fields": {"/flight/departure/time": "next tuesday"}}, "can't read"),
                           ({"fields": {"/booking_status": "maybe"}}, "must be one of"),
                           ({"passengers": ["999999"]}, "unknown traveller"),
                           ({"traveler_ids": [w["ta"]]}, "passengers")]:
            r = client.patch(url, headers=h, json=body)
            assert r.status_code == 400 and frag in r.json()["detail"], (body, r.text)
        r = client.patch(f"/api/v1/review/{w['event']}", headers=h, json={"passengers": []})
        assert r.status_code == 400
        r = client.patch(f"/api/v1/review/{w['event']}", headers=h,
                         json={"traveler_ids": [999999]})
        assert r.status_code == 400 and "not travellers" in r.json()["detail"]

    def test_passengers_and_who(w):
        h = H(w["alice"])
        r = client.patch(f"/api/v1/review/{w['flight']}", headers=h,
                         json={"passengers": [str(w["ta"]), str(w["tb"])]})
        assert r.status_code == 200 and "/travelers" in r.json()["changed"]
        with SessionLocal() as s:
            names = [t["raw_name"] for t in
                     s.get(TravelItem, w["flight"]).extraction["payload"]["travelers"]]
            assert names == ["RV Alice", "RV Bob"]
        r = client.patch(f"/api/v1/review/{w['event']}", headers=h,
                         json={"traveler_ids": [w["tb"]]})
        assert r.status_code == 200
        who = {t["name"]: t["selected"] for t in r.json()["item"]["who"]}
        assert who == {"RV Alice": False, "RV Bob": True}

    # ------------------------------------------------------------------ actions
    def test_approve_unapprove_ignore(w):
        h = H(w["alice"])
        r = client.post(f"/api/v1/review/{w['flight']}/approve", headers=h).json()
        assert r["ok"] and r["item"]["bucket"] == "staged" and r["item"]["can_unapprove"]
        r = client.post(f"/api/v1/review/{w['flight']}/unapprove", headers=h).json()
        assert r["ok"] and r["item"]["bucket"] != "staged"
        r = client.post(f"/api/v1/review/{w['unresolved']}/approve", headers=h).json()
        assert not r["ok"] and r["message"] and r["item"]["bucket"] == "attention"
        r = client.post(f"/api/v1/review/{w['event']}/ignore", headers=h).json()
        assert r["ok"] and not r["item"]["can_ignore"] and not r["item"]["can_approve"]
        with SessionLocal() as s:
            assert s.get(TravelItem, w["event"]).review["action_taken"] == "ignored"

    def test_save_and_approve(w):
        r = client.patch(f"/api/v1/review/{w['flight']}", headers=H(w["alice"]),
                         json={"fields": {"/booking_reference": "SAVEAPP"}, "approve": True})
        out = r.json()
        assert r.status_code == 200 and out["ok"] and out["item"]["bucket"] == "staged"

    def test_logged_item_cannot_be_unapproved_or_ignored(w):
        with SessionLocal() as s:
            it = s.get(TravelItem, w["flight"])
            it.review = {"required": False, "action_taken": "approved"}
            it.commit = {"dry_run": False, "results": [{"target": "airtrail.flight", "ok": True}]}
            s.commit()
        h = H(w["alice"])
        d = client.get(f"/api/v1/review/{w['flight']}", headers=h).json()
        assert d["bucket"] == "logged" and not d["can_unapprove"] and not d["can_ignore"]
        assert client.post(f"/api/v1/review/{w['flight']}/unapprove", headers=h).json()["ok"] is False
        assert client.post(f"/api/v1/review/{w['flight']}/ignore", headers=h).json()["ok"] is False

    # ------------------------------------------------------------------ commit + jobs
    def _wait(job_id, h):
        for _ in range(250):
            j = client.get(f"/api/v1/jobs/{job_id}", headers=h).json()
            if j["finished"]:
                return j
            time.sleep(0.02)
        raise AssertionError("job never finished")

    def test_commit_dry_run_only_covers_callers_items(w):
        with SessionLocal() as s:
            s.get(TravelItem, w["flight"]).review = {"required": False, "action_taken": "approved"}
            s.commit()
            before = s.query(CommitAttempt).count()
        r = client.post("/api/v1/review/commit", headers=H(w["alice"]), json={})
        assert r.status_code == 200 and r.json()["dry_run"] is True
        plans = r.json()["summary"]["plans"]
        assert r.json()["summary"]["items"] == 1
        assert all(p.get("item_id", w["flight"]) != w["bob_item"] for p in plans)
        with SessionLocal() as s:
            assert s.query(CommitAttempt).count() == before       # nothing written

    def test_commit_execute_is_a_job_scoped_to_caller(w):
        with SessionLocal() as s:
            s.get(TravelItem, w["flight"]).review = {"required": False, "action_taken": "approved"}
            s.commit()
        h = H(w["alice"])
        r = client.post("/api/v1/review/commit", headers=h, json={"dry_run": False})
        assert r.status_code == 202 and r.json()["kind"] == "commit"
        jid = r.json()["id"]
        j = _wait(jid, h)
        assert j["finished"] and "1 item" in (j["toast"] or "")
        assert client.get(f"/api/v1/jobs/{jid}", headers=H(w["bob"])).status_code == 404
        with SessionLocal() as s:                 # bob's approved item was never touched
            assert s.get(TravelItem, w["bob_item"]).commit in (None, {})

    def test_process_job_and_busy_rules(w, monkeypatch):
        from app.review import routes
        monkeypatch.setattr(routes, "_run_extract_resolve",
                            lambda s, progress=None: ({"extracted": 0, "needs_review": 0}, {}, None))
        h = H(w["alice"])
        r = client.post("/api/v1/review/process", headers=h)
        assert r.status_code == 202 and r.json()["kind"] == "process"
        assert _wait(r.json()["id"], h)["toast"] == "No new captures were waiting."
        # someone else's job running → 409 (and none of its details)
        fake = {"id": "abc", "kind": "commit", "finished": False, "owner": w["bob"],
                "current": "Bob's secret flight"}
        monkeypatch.setattr(jobs, "current_running", lambda: dict(fake))
        r = client.post("/api/v1/review/process", headers=h)
        assert r.status_code == 409 and "secret" not in r.text
        # your own running job → you get it back instead of a second one
        fake["owner"] = w["alice"]
        r = client.post("/api/v1/review/process", headers=h)
        assert r.status_code == 202 and r.json()["id"] == "abc"

    def test_web_never_shows_someone_elses_job(w, monkeypatch):
        fake = {"id": "abc", "kind": "commit", "finished": False, "owner": w["bob"],
                "current": "Bob's secret flight", "phase": "logging", "done": 1, "total": 2,
                "started_at": time.monotonic()}
        monkeypatch.setattr(jobs, "current_running", lambda: dict(fake))
        r = client.post("/review/process?bucket=attention")   # auth off: no user
        assert r.status_code == 200 and "secret" not in r.text
        assert "Another person" in r.text

    def test_visible_to():
        assert jobs.visible_to({"owner": None}, None)
        assert not jobs.visible_to({"owner": "u1"}, None)
        assert jobs.visible_to({"owner": "u1"}, type("U", (), {"id": "u1"})())
        assert not jobs.visible_to(None, None)
