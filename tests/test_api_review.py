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
                            TripActivity, TripTraveler, User)
    from app import scope
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
        s.query(TripActivity).filter(TripActivity.trip_id.in_(tids)).delete(synchronize_session=False)
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

    @pytest.fixture()
    def admins_write(monkeypatch):
        """The job-scoping tests below are about WHOSE items are logged, not who
        may use the server-wide targets (YONDER-191, tested separately)."""
        monkeypatch.setattr(scope, "external_writes_allowed", lambda user: True)

    def test_commit_dry_run_only_covers_callers_items(w, admins_write):
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

    def test_commit_execute_is_a_job_scoped_to_caller(w, admins_write):
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
        ran_as = []

        def fake_run(s, progress=None, user=None):
            ran_as.append(user.id if user else None)
            return {"extracted": 0, "needs_review": 0}, {}, None
        monkeypatch.setattr(routes, "_run_extract_resolve", fake_run)
        h = H(w["alice"])
        r = client.post("/api/v1/review/process", headers=h)
        assert r.status_code == 202 and r.json()["kind"] == "process"
        assert _wait(r.json()["id"], h)["toast"] == "No new captures were waiting."
        assert ran_as == [w["alice"]]                   # scoped to the caller (YONDER-113)
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

    def test_process_only_touches_the_callers_captures(w, monkeypatch):
        """YONDER-113: Alice's run extracts only her own (and open) batches and
        resolves only her own (and open) items — never Bob's."""
        from app import processing, resolving, scope
        from app.models import IngestBatch
        from app.review.routes import _run_extract_resolve
        with SessionLocal() as s:
            for uid in (w["alice"], w["bob"]):
                s.add(IngestBatch(user_id=uid, status="received", payload={"subject": "RV57"},
                                  artifact_sha256=f"rv57-{uid}", api_key_id="k"))
            s.get(TravelItem, w["bob_item"]).status = {"extract": "ok", "resolve": "pending"}
            s.get(TravelItem, w["flight"]).status = {"extract": "ok", "resolve": "pending"}
            s.commit()
        batches, items = [], []
        monkeypatch.setattr(processing, "process_batch",
                            lambda s, b, llm: batches.append(b.user_id) or "extracted")
        monkeypatch.setattr(resolving, "resolve_item",
                            lambda s, item, *a, **k: items.append(item.owner_id) or "ok")
        try:
            with SessionLocal() as s:
                alice = scope.snapshot(s.get(User, w["alice"]))
                _, _, error = _run_extract_resolve(s, user=alice)
            assert error is None
            assert batches == [w["alice"]] and items == [w["alice"]]
        finally:
            with SessionLocal() as s:
                s.query(IngestBatch).filter(IngestBatch.artifact_sha256.like("rv57-%")).delete(
                    synchronize_session=False)
                s.commit()

    def test_reprocess_never_touches_someone_elses_captures(w, monkeypatch):
        """YONDER-139: Alice's Reprocess resets and rebuilds only her own
        captures. Bob's unlogged (approved!) item and his batch survive."""
        from app import scope
        from app.models import IngestBatch
        from app.review import routes
        with SessionLocal() as s:
            ba = IngestBatch(user_id=w["alice"], status="extracted", payload={"subject": "a"},
                             artifact_sha256="rv139-a", api_key_id="k")
            bb = IngestBatch(user_id=w["bob"], status="extracted", payload={"subject": "b"},
                             artifact_sha256="rv139-b", api_key_id="k")
            s.add_all([ba, bb])
            s.flush()
            s.get(TravelItem, w["flight"]).batch_id = ba.id
            s.get(TravelItem, w["bob_item"]).batch_id = bb.id
            s.commit()
            ba_id, bb_id = ba.id, bb.id
        with SessionLocal() as s:
            alice = s.get(User, w["alice"])
            s.expunge(alice)
        monkeypatch.setattr(scope, "user_of", lambda request: alice)
        ran = {}

        def fake_run(s, progress=None, user=None):
            ran["user"] = user.id if user else None
            return {"extracted": 0, "needs_review": 0}, {}, None
        monkeypatch.setattr(routes, "_run_extract_resolve", fake_run)
        started = []
        real_start = jobs.start
        monkeypatch.setattr(jobs, "start", lambda kind, work, owner=None: started.append(
            real_start(kind, work, owner=owner)) or started[-1])
        try:
            assert client.post("/review/reprocess?bucket=attention").status_code == 200
            for _ in range(250):                             # the job runs on a thread
                j = jobs.get(started[0])
                if j and j["finished"]:
                    break
                time.sleep(0.02)
            assert j["finished"] and not j.get("toast_error"), j
            assert ran["user"] == w["alice"]
            with SessionLocal() as s:
                assert s.get(TravelItem, w["flight"]) is None          # hers: rebuilt
                assert s.get(IngestBatch, ba_id).status == "received"
                assert s.get(TravelItem, w["bob_item"]) is not None    # his: untouched
                assert s.get(IngestBatch, bb_id).status == "extracted"
        finally:
            with SessionLocal() as s:
                s.query(IngestBatch).filter(IngestBatch.artifact_sha256.like("rv139-%")).delete(
                    synchronize_session=False)
                s.commit()

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


    # ------------------------------------------------------------------ YONDER-191
    def test_non_admin_commit_writes_runder_only(w, monkeypatch):
        """YONDER-191: a non-admin's commit still lands on their Run·dere trip
        (first-party, no AirTrail/AdventureLog needed); the external targets are
        skipped — never attempted, even with server-wide credentials set."""
        monkeypatch.setattr(settings, "adventurelog_api_key", "k")
        monkeypatch.setattr(settings, "airtrail_token", "t")
        with SessionLocal() as s:
            ev = s.get(TravelItem, w["event"])
            ev.canonical_key = "rv57-event"
            ev.resolution = {"outcome": "resolved",
                             "targets": ["adventurelog.note", "yonder.activity"]}
            ev.review = {"required": False, "action_taken": "approved"}
            s.commit()
        h = H(w["alice"])
        r = client.post("/api/v1/review/commit", headers=h, json={})
        assert r.status_code == 200
        res = {x["target"]: x for p in r.json()["summary"]["plans"] for x in p["results"]}
        assert res["adventurelog.note"]["skipped"].startswith("no external connection")
        r = client.post("/api/v1/review/commit", headers=h, json={"dry_run": False})
        assert r.status_code == 202
        j = _wait(r.json()["id"], h)
        assert "skipped (admins only" in (j["toast"] or "") and not j.get("error")
        with SessionLocal() as s:
            acts = s.query(TripActivity).filter(TripActivity.trip_id == w["trip"],
                                                TripActivity.source_item_key == "rv57-event").all()
            assert len(acts) == 1                              # first-party write landed
            attempts = s.query(CommitAttempt).filter(CommitAttempt.item_id == w["event"],
                                                     CommitAttempt.target.like("adventurelog%")).count()
            assert attempts == 0                               # nothing sent to AdventureLog

    def test_commit_allowed_for_admin(w):
        with SessionLocal() as s:
            s.get(User, w["alice"]).is_admin = True
            s.commit()
        r = client.post("/api/v1/review/commit", headers=H(w["alice"]), json={})
        assert r.status_code == 200 and r.json()["dry_run"] is True

    def test_target_clients_withheld_from_non_admin(w, monkeypatch):
        from app.review import routes
        monkeypatch.setattr(settings, "adventurelog_api_key", "k")
        monkeypatch.setattr(settings, "airtrail_token", "t")
        with SessionLocal() as s:
            alice, admin = s.get(User, w["alice"]), s.get(User, w["bob"])
            admin.is_admin = True
            assert routes._target_clients(alice) == (None, None)
            assert all(c is not None for c in routes._target_clients(admin))
            assert all(c is not None for c in routes._target_clients(None))   # auth off / CLI

    def test_web_external_routes_refuse_non_admin(w, monkeypatch):
        """Same gate on the web side: commit, create-collection and the sync
        page/push/pull never touch the server-wide targets for a non-admin."""
        from app.trips import routes as trip_routes
        monkeypatch.setattr(settings, "auth_required", True)     # web routes resolve a user
        monkeypatch.setattr(settings, "adventurelog_api_key", "k")
        monkeypatch.setattr(settings, "airtrail_token", "t")
        monkeypatch.setattr(trip_routes, "_adventurelog",
                            lambda: pytest.fail("AdventureLog client built for a non-admin"))
        with SessionLocal() as s:
            tok = authn.issue_access_token(s.get(User, w["alice"]))
            s.get(Trip, w["trip"]).adventurelog_collection_id = "col-1"
            s.commit()
        ck = {settings.auth_cookie_name: tok}
        t = w["trip"]
        assert "limited to admins" in client.post(f"/trips/{t}/adventurelog/push", cookies=ck).text
        assert "limited to admins" in client.post(f"/trips/{t}/adventurelog/pull", cookies=ck).text
        assert client.get(f"/trips/{t}/adventurelog", cookies=ck).status_code == 403
        with SessionLocal() as s:
            s.get(Trip, t).adventurelog_collection_id = None
            s.commit()
        assert "limited to admins" in client.post(f"/trips/{t}/push-adventurelog", cookies=ck).text
