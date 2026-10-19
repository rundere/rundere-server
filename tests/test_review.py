"""Review queue tests — the edit path especially.

The headline case is a REGRESSION for a silent data-loss bug: editing a field
appeared to work (HTTP 200, item cleared review, rules passed) while the value
never reached the database. Cause: a shallow copy let the edit mutate the
loaded object in place, so SQLAlchemy compared the reassigned attribute against
an already-identical value, decided nothing had changed, and skipped the UPDATE.
Anything that only checks the response would have missed it — so these assert
against the stored row.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, TravelItem
    from app.review.routes import _apply_edit, _fields
    Base.metadata.create_all(engine)
    client = TestClient(app)

NOW = datetime.now(timezone.utc).isoformat()


def test_admin_mounts_including_travellers_regression():
    """Regression (2026-08-04): TravelerIdentityAdmin was listed in the admin
    registration loop but never defined, so mount_admin raised NameError at
    startup. main.py swallowed it ('SQLAdmin failed to mount; API still
    serving') and /admin — the 'Tables & settings' page — rendered as an empty
    shell with no tables or links. Mounting must succeed AND register the
    Travellers view."""
    if not DEPS:
        return
    from fastapi import FastAPI
    from app.admin import mount_admin
    from app.db import engine as eng
    a = FastAPI()
    mount_admin(a, eng)                          # must not raise NameError
    c = TestClient(a)
    assert c.get("/admin/traveler-identity/list").status_code == 200


def test_traveller_kind_is_a_dropdown_not_free_text():
    """`kind` is a closed self|guest vocabulary (D-35); the create form must
    offer it as a <select>, not a free-text box a typo can slip through."""
    if not DEPS:
        return
    from fastapi import FastAPI
    from app.admin import mount_admin
    from app.db import engine as eng
    a = FastAPI()
    mount_admin(a, eng)
    html = TestClient(a).get("/admin/traveler-identity/create").text
    assert 'name="kind"' in html
    # the element bearing name="kind" must be a <select> ... </select> holding
    # both options — not an <input type="text">.
    tag_open = html.rfind("<", 0, html.index('name="kind"'))
    assert html[tag_open:tag_open + 7] == "<select"
    assert ">self" in html and ">guest" in html


def test_jobs_registry_runs_and_reports():
    """The in-memory job registry (D-49): start runs the work on a background
    thread, progress updates the live state, and a finished job carries its
    toast; dismiss forgets it."""
    if not DEPS:
        return
    import time as _t
    from app.review import jobs

    def work(progress):
        progress("phaseX", 1, 2, "item-a")
        return "all good", None

    jid = jobs.start("test", work)
    for _ in range(200):
        if jobs.get(jid)["finished"]:
            break
        _t.sleep(0.02)
    j = jobs.get(jid)
    assert j["finished"] and j["toast"] == "all good" and not j["toast_error"]
    jobs.dismiss(jid)
    assert jobs.get(jid) is None


def test_process_button_starts_a_background_job():
    """Process now runs as a background JOB (D-49): the POST returns a live,
    self-polling job card immediately instead of blocking on the LLM pass."""
    if not DEPS:
        return
    import re as _re
    import time as _t
    r = client.post("/review/process?bucket=attention")
    assert r.status_code == 200
    assert 'class="jobcard"' in r.text
    assert '/review/jobs/' in r.text               # the card polls itself
    m = _re.search(r'/review/jobs/([a-f0-9]+)', r.text)
    assert m
    # drain the (empty) job so its background thread doesn't outlive the test
    for _ in range(200):
        d = client.get(f"/review/jobs/{m.group(1)}")
        if "jobcard done" in d.text or d.text.strip() == "":
            break
        _t.sleep(0.02)


def test_process_toast_wording():
    """Unit-level: the toast summarises a run in plain language."""
    if not DEPS:
        return
    from app.review.routes import _process_toast
    assert _process_toast({"extracted": 0, "needs_review": 0}, {}, None) \
        == "No new captures were waiting."
    assert "2 extracted" in _process_toast(
        {"extracted": 2, "needs_review": 0}, {"needs_review": 2}, None)
    assert _process_toast({}, {}, "boom").startswith("Processing failed")


def test_normalize_local_accepts_colonless_time():
    """Regression (2026-08-04): the review datetime field rejected '0720' and,
    when a value slipped through, stored '2027-11-19T0720' — an invalid datetime
    that every downstream check then rejected. Both forms must normalise to
    HH:MM, a bare date must stay date-only, and junk must be refused."""
    if not DEPS:
        return
    from app.review.routes import _normalize_local
    assert _normalize_local("2027-11-19T0720") == "2027-11-19T07:20"
    assert _normalize_local("2027-11-19 07:20") == "2027-11-19T07:20"
    assert _normalize_local("2027-11-19T7:20") == "2027-11-19T07:20"
    assert _normalize_local("2027-11-19") == "2027-11-19"      # date-only preserved
    assert _normalize_local("garbage") is None
    assert _normalize_local("2027-11-19T25:00") is None        # invalid hour


def test_logged_bucket_detection():
    """A real, fully-written commit moves an item to 'Logged'; a dry-run or a
    partial write does not — so 'Approved' keeps meaning 'awaiting the write'."""
    if not DEPS:
        return
    from app.review.routes import _is_logged

    class Item:
        def __init__(self, resolution, commit):
            self.resolution, self.commit = resolution, commit

    done = Item({"targets": ["adventurelog.note"]},
                {"dry_run": False, "results": [{"target": "adventurelog.note", "ok": True}]})
    dry = Item({"targets": ["adventurelog.note"]},
               {"dry_run": True, "results": [{"target": "adventurelog.note", "ok": True}]})
    partial = Item({"targets": ["adventurelog.note", "airtrail.flight"]},
                   {"dry_run": False, "results": [{"target": "adventurelog.note", "ok": True}]})
    assert _is_logged(done) is True
    assert _is_logged(dry) is False        # a preview never counts as logged
    assert _is_logged(partial) is False    # not every target written


def test_commit_preview_is_dry_run_and_writes_nothing():
    """The 'Log approved…' button previews first (D-44): the response is the
    commit panel, and NOTHING is written — a preview must never touch a target."""
    if not DEPS:
        return
    from app.models import CommitAttempt
    with SessionLocal() as s:
        before = s.query(CommitAttempt).count()
    r = client.post("/review/commit", data={"bucket": "staged"})
    assert r.status_code == 200
    assert 'id="commit-panel"' not in r.text        # it fills the panel, isn't it
    assert 'class="cpanel"' in r.text
    with SessionLocal() as s:
        assert s.query(CommitAttempt).count() == before


def test_passenger_context_draws_from_traveller_profiles():
    """Passengers are chosen from configured Travellers, not typed (owner,
    2026-08-05). A document name matching an identity (even in a different shape)
    pre-selects it; a name matching none is surfaced so the operator creates that
    profile. Non-flights have no passenger control."""
    if not DEPS:
        return
    from app.review.routes import _passenger_context
    from app.models import TravelerIdentity
    with SessionLocal() as s:
        s.query(TravelerIdentity).delete()
        s.add(TravelerIdentity(display_name="Kenji Nakamura", kind="self",
                               aliases=["NAKAMURA/KENJI"]))
        s.add(TravelerIdentity(display_name="Pat Example", kind="guest"))
        s.commit()
        payload = {"item_type": "flight",
                   "travelers": [{"raw_name": "NAKAMURA/KENJI MR"},
                                 {"raw_name": "Unknown Person"}]}
        ctx = _passenger_context(s, payload)
        assert ctx["have_identities"]
        me = next(r for r in ctx["identities"] if r["name"] == "Kenji Nakamura")
        assert me["selected"] and me["kind"] == "self"        # matched a doc name
        assert "Unknown Person" in ctx["unmatched"]           # forces a profile
        assert _passenger_context(s, {"item_type": "lodging"}) is None
        s.query(TravelerIdentity).delete(); s.commit()


def test_write_preview_names_the_trip_it_files_into():
    """Q2 (2026-08-05): a MATCHED or ATTACH item must NAME the trip it files
    into so the operator can verify it's the right one — a bare 'Files into
    existing trip' was unverifiable. A MINTING flight still says 'New trip'."""
    if not DEPS:
        return
    from app.review import summary as sm
    matched = {"collection_action": "matched_exact", "collection_id": "c1",
               "collection_name": "Phoenix, AZ, October 2026",
               "targets": ["adventurelog.lodging"]}
    prev = sm.write_preview({"item_type": "lodging", "lodging": {}}, matched)
    filed = [p for p in prev if "trip" in p["text"].lower()]
    assert filed and "Phoenix, AZ, October 2026" in filed[0]["text"]
    assert "New trip" not in filed[0]["text"]                # match, not a mint

    minting = {"collection_action": "created", "collection_name": "Portugal — Sep 2027",
               "targets": ["adventurelog.collection", "adventurelog.transportation"]}
    prev2 = sm.write_preview({"item_type": "flight", "flight": {}}, minting)
    assert any("New trip" in p["text"] and "Portugal — Sep 2027" in p["text"]
               for p in prev2)

    # an ATTACH leg (created, but not minting here) also names its shared trip
    attach = {"collection_action": "created", "collection_name": "Portugal — Sep 2027",
              "mint_group": "x", "is_minter": False,
              "targets": ["adventurelog.transportation"]}
    prev3 = sm.write_preview({"item_type": "flight", "flight": {}}, attach)
    assert any("Portugal — Sep 2027" in p["text"] and "New trip" not in p["text"]
               for p in prev3)


def test_write_preview_reflects_update_and_cancel():
    """D-57: the preview must show an update as 'Update …' (revised in place) and
    a cancellation as 'Cancel flight …' / 'Mark cancelled …', not as a create."""
    if not DEPS:
        return
    from app.review import summary as sm
    reso = {"collection_action": "matched_exact", "collection_id": "c1",
            "collection_name": "United States — November 2026",
            "targets": ["adventurelog.transportation", "airtrail.flight"]}
    pay = {"item_type": "flight", "flight": {}}

    upd = sm.write_preview(pay, reso, {"action": "update"})
    assert any(w["state"] == "update" for w in upd)
    assert any(w["text"].startswith("Update") for w in upd)
    assert not any("Log flight" in w["text"] for w in upd)        # not a create

    canc = sm.write_preview(pay, reso, {"action": "supersede"})
    texts = " ".join(w["text"] for w in canc)
    assert "Cancel flight" in texts and "Mark cancelled" in texts
    assert any(w["state"] == "cancel" for w in canc)

    # default (no revision) is unchanged — a plain create
    base = sm.write_preview(pay, reso)
    assert any("Log flight" in w["text"] for w in base)
    assert not any(w["state"] in ("update", "cancel") for w in base)


def _flight_payload():
    return {
        "item_type": "flight", "booking_status": "changed",
        "booking_reference": "TEST01",
        "flight": {
            "flight_number": "4826",
            "marketing_carrier": {"name": "Test Air", "iata": "TA"},
            "departure": {"place": {"raw": "AAA", "iata": "AAA"},
                          # the failure this whole flow exists to fix: a date
                          # with no time, which RULE-04 refuses to log
                          "time": {"local": "2023-06-30", "precision": "date",
                                   "origin": "document"}},
            "arrival": {"place": {"raw": "BBB", "iata": "BBB"},
                        "time": {"local": "2023-06-30T15:05",
                                 "precision": "datetime", "origin": "document"}},
        },
    }


def _add(session, payload, resolve_state="needs_review"):
    it = TravelItem(
        item_type=payload["item_type"], schema_version="1.4.0",
        source={"type": "gmail", "ref": "m-review", "received_at": NOW,
                "capture_context": {}},
        extraction={"extractor": "llm", "model": "test", "ran_at": NOW,
                    "confidence": 0.6, "payload": payload},
        review={"required": True, "reasons": ["missing_time"]},
        status={"extract": "ok", "resolve": resolve_state,
                "airtrail": "pending", "adventurelog": {}})
    session.add(it)
    session.commit()
    return it


def _skip_or(fn):
    if not DEPS:
        import pytest
        pytest.skip("fastapi not installed on this host")
    return fn()


# ---------------------------------------------------------------- pure logic

def test_edit_sets_precision_from_the_shape_of_the_input():
    """D-06: a date-only entry must NOT be padded to midnight."""
    def run():
        p = _flight_payload()
        assert _apply_edit(p, "/flight/departure/time", "2023-06-30T06:55", "datetime")
        t = p["flight"]["departure"]["time"]
        assert t == {"local": "2023-06-30T06:55", "precision": "datetime",
                     "origin": "document"}

        p2 = _flight_payload()
        assert _apply_edit(p2, "/flight/departure/time", "2023-07-01", "datetime")
        assert p2["flight"]["departure"]["time"]["precision"] == "date"
    _skip_or(run)


def test_edit_creates_missing_intermediate_objects():
    def run():
        p = {"item_type": "lodging", "booking_status": "confirmed"}
        assert _apply_edit(p, "/lodging/place/city", "Lisbon", "text")
        assert p["lodging"]["place"]["city"] == "Lisbon"
    _skip_or(run)


def test_blank_edit_is_not_a_change():
    def run():
        p = _flight_payload()
        assert _apply_edit(p, "/flight/departure/time", "", "datetime") is False
        assert _apply_edit(p, "/flight/flight_number", "4826", "text") is False
    _skip_or(run)


def test_every_editable_field_has_a_pointer():
    def run():
        for f in _fields(_flight_payload()):
            assert f["path"].startswith("/"), f
            assert f["kind"] in ("text", "datetime", "select"), f
    _skip_or(run)


# ---------------------------------------------------------------- HTTP + DB

def test_saving_an_edit_actually_reaches_the_database():
    """THE regression: response said 200 and the item cleared review, but the
    value never persisted."""
    def run():
        with SessionLocal() as s:
            item = _add(s, _flight_payload())
            item_id = item.id
        r = client.post(f"/review/item/{item_id}/save",
                        data={"f:/flight/departure/time": "2023-06-30T06:55"})
        assert r.status_code == 200
        # the response must carry the detail body AND the out-of-band count
        # bars, so the pane updates in place and the queue numbers refresh
        assert "correct anything wrong" in r.text        # detail body
        assert 'hx-swap-oob="true"' in r.text            # OOB count refresh
        with SessionLocal() as s:
            stored = s.get(TravelItem, item_id)
            t = stored.extraction["payload"]["flight"]["departure"]["time"]
            assert t["local"] == "2023-06-30T06:55", "edit did not persist"
            assert t["precision"] == "datetime"
    _skip_or(run)


def test_edited_fields_become_locks_and_survive_reresolution():
    """RULE-15 depends on locks surviving every later resolver pass."""
    def run():
        with SessionLocal() as s:
            item = _add(s, _flight_payload())
            item_id = item.id
        client.post(f"/review/item/{item_id}/save",
                    data={"f:/flight/departure/time": "2023-06-30T07:10"})
        with SessionLocal() as s:
            stored = s.get(TravelItem, item_id)
            assert "/flight/departure/time" in (stored.revision or {}).get("field_locks", [])
            assert (stored.review or {}).get("action_taken") == "edited"
    _skip_or(run)


def test_unknown_field_names_are_ignored():
    """A submitted pointer that is not an editable field on this item must not
    be written into the payload."""
    def run():
        with SessionLocal() as s:
            item = _add(s, _flight_payload())
            item_id = item.id
        client.post(f"/review/item/{item_id}/save",
                    data={"f:/flight/injected": "nope", "notafield": "x"})
        with SessionLocal() as s:
            stored = s.get(TravelItem, item_id)
            assert "injected" not in stored.extraction["payload"]["flight"]
    _skip_or(run)


def test_queue_renders_each_bucket():
    def run():
        for bucket in ("attention", "waiting", "ready", "staged", "all"):
            r = client.get(f"/review?bucket={bucket}")
            assert r.status_code == 200, bucket
    _skip_or(run)


def test_approve_stages_without_writing_targets():
    """D-31: approving marks ready; it must not produce commit records."""
    def run():
        with SessionLocal() as s:
            payload = _flight_payload()
            payload["flight"]["departure"]["time"] = {
                "local": "2026-01-01T08:00", "precision": "datetime",
                "origin": "document"}
            item = _add(s, payload, resolve_state="ok")
            item.review = {"required": False}
            item.resolution = {"outcome": "resolved",
                               "targets": ["adventurelog.transportation"]}
            s.commit()
            item_id = item.id
        r = client.post(f"/review/item/{item_id}/approve")
        assert r.status_code == 200
        with SessionLocal() as s:
            stored = s.get(TravelItem, item_id)
            assert (stored.review or {}).get("action_taken") == "approved"
            assert not stored.commit          # nothing written to any target
    _skip_or(run)


def test_edited_item_is_ready_to_approve_not_approved():
    """The leak fix: an EDITED (not approved) item must land in 'Ready to
    Approve' (ready), never in 'Approved' (staged) — otherwise the queue claims
    it will log when committable() (approved-only) silently skips it."""
    def run():
        from app.review.routes import _bucket_of
        it = TravelItem(
            item_type="flight", schema_version="1.4.0",
            source={"type": "gmail"},
            extraction={"payload": _flight_payload()},
            resolution={"outcome": "resolved", "targets": ["airtrail.flight"]},
            review={"required": False, "action_taken": "edited"},
            status={"extract": "ok", "resolve": "ok"})
        assert _bucket_of(it) == "ready"
        it.review = {"required": False, "action_taken": "approved"}
        assert _bucket_of(it) == "staged"
    _skip_or(run)


def test_save_and_approve_approves_a_resolved_item():
    """'Save & approve' (and_approve) both saves and approves in one submit."""
    def run():
        with SessionLocal() as s:
            payload = _flight_payload()
            payload["flight"]["departure"]["time"] = {
                "local": "2026-01-01T08:00", "precision": "datetime",
                "origin": "document"}
            item = _add(s, payload, resolve_state="ok")
            item.review = {"required": False}
            item.resolution = {"outcome": "resolved",
                               "targets": ["adventurelog.transportation"]}
            s.commit()
            item_id = item.id
        r = client.post(f"/review/item/{item_id}/save?bucket=ready",
                        data={"and_approve": "1"})
        assert r.status_code == 200
        # The response must retarget the whole queue body so the item leaves
        # "Ready" and its strip badge flips to Approved (feedback: it was staying
        # put with a stale "Ready" pill).
        assert r.headers.get("HX-Retarget") == "#queue-body"
        with SessionLocal() as s:
            stored = s.get(TravelItem, item_id)
            assert (stored.review or {}).get("action_taken") == "approved"
    _skip_or(run)


if __name__ == "__main__":
    if not DEPS:
        print("SKIP: fastapi not installed on this host")
        sys.exit(0)
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as ex:
                failures += 1
                print(f"  FAIL {name}: {str(ex)[:300]}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)


def test_approve_stays_on_current_bucket_with_oob_counts():
    """Regression: approving used to always return the 'attention' list,
    bouncing the operator off whatever queue they were on, and the counts
    only updated on a manual refresh."""
    def run():
        with SessionLocal() as s:
            payload = _flight_payload()
            payload["flight"]["departure"]["time"] = {
                "local": "2026-01-01T08:00", "precision": "datetime",
                "origin": "document"}
            item = _add(s, payload, resolve_state="ok")
            item.review = {"required": False}
            item.resolution = {"outcome": "resolved",
                               "targets": ["adventurelog.transportation"]}
            s.commit()
            item_id = item.id
        r = client.post(f"/review/item/{item_id}/approve?bucket=ready")
        assert r.status_code == 200
        assert 'hx-swap-oob="true"' in r.text            # counts refresh OOB
        assert item_id not in r.text                     # left the ready list
    _skip_or(run)


# ---------------------------------------------------------------- trip pickers

def _ready_flight(session):
    payload = _flight_payload()
    item = _add(session, payload, resolve_state="ok")
    item.review = {"required": False}
    item.resolution = {"outcome": "resolved", "targets": ["airtrail.flight"]}
    session.commit()
    return item


def test_yonder_trip_picker_links_and_unlinks_a_capture():
    """2.1.2: the Run·dere trip picker sets / clears item.trip_id. (Since D-148 it
    also re-resolves — see test_linking_a_trip_settles_a_deferred_capture.)"""
    def run():
        from app.models import Trip
        with SessionLocal() as s:
            trip = Trip(name="Lisbon 2027", start_date="2027-09-01",
                        end_date="2027-09-14")
            s.add(trip); s.commit(); trip_id = trip.id
            item = _ready_flight(s)
            item_id = item.id
        r = client.post(f"/review/item/{item_id}/assign-trip",
                        data={"trip_id": trip_id})
        assert r.status_code == 200
        with SessionLocal() as s:
            it = s.get(TravelItem, item_id)
            assert it.trip_id == trip_id
            assert (it.review or {}).get("trip_pinned") is True
        r = client.post(f"/review/item/{item_id}/assign-trip", data={"trip_id": ""})
        with SessionLocal() as s:
            assert s.get(TravelItem, item_id).trip_id is None   # unlinked
    _skip_or(run)


def _hotel_payload(year):
    """A clean hotel booking in a year no other test uses, so no trip covers it
    until the test makes one (the test DB is shared; an earlier test's trip
    would auto-link it). YONDER-222 was a real one of these."""
    return {
        "item_type": "lodging", "booking_status": "confirmed",
        "booking_reference": "HOTEL222",
        "lodging": {
            "property_name": "Test Hotel YONDER-222",
            "place": {"raw": "1 Test Street, Testville", "city": "Testville",
                      "country": "US", "lat": 40.44, "lon": -79.99,
                      "tz": "America/New_York"},
            "check_in": {"local": f"{year}-05-14T16:00", "precision": "datetime",
                         "origin": "document"},
            "check_out": {"local": f"{year}-05-16T11:00", "precision": "datetime",
                          "origin": "document"},
        },
    }


def _deferred_hotel(session, year):
    """Resolve a hotel with no trip for it: it waits ("Waiting for a trip")."""
    from app.review.routes import _bucket_of, _reresolve
    item = _add(session, _hotel_payload(year), resolve_state="pending")
    item.review = {}
    session.commit()
    _reresolve(session, item)
    session.refresh(item)
    assert (item.status or {}).get("resolve") == "deferred", item.status
    assert _bucket_of(item) == "waiting"
    return item


def _trip_covering_the_hotel(session, year):
    from app.models import Trip
    trip = Trip(name=f"Testville May {year}", start_date=f"{year}-05-14",
                end_date=f"{year}-05-18")
    session.add(trip)
    session.commit()
    return trip


def test_linking_a_trip_settles_a_deferred_capture():
    """YONDER-222: a hotel captured before its trip existed waits for a trip.
    Picking that Run·dere trip in review must re-check the item so it becomes
    approvable at once — the link used to be stored while the item stayed in
    "Waiting for a trip" until its TTL sweep."""
    def run():
        from app.review.routes import _bucket_of
        with SessionLocal() as s:
            item_id = _deferred_hotel(s, 2091).id
            trip_id = _trip_covering_the_hotel(s, 2091).id
        r = client.post(f"/review/item/{item_id}/assign-trip?bucket=waiting",
                        data={"trip_id": trip_id})
        assert r.status_code == 200
        assert 'hx-swap-oob="true"' in r.text            # counts refresh too
        with SessionLocal() as s:
            it = s.get(TravelItem, item_id)
            assert it.trip_id == trip_id
            assert (it.status or {}).get("resolve") == "ok"
            assert "yonder.activity" in (it.resolution or {}).get("targets", [])
            assert _bucket_of(it) == "ready"
    _skip_or(run)


def test_save_and_approve_rechecks_a_stale_deferred_capture():
    """YONDER-222: Save & approve with no edits on an item whose state predates
    its trip link must re-check it and approve, not silently do nothing."""
    def run():
        with SessionLocal() as s:
            item = _deferred_hotel(s, 2092)
            item.trip_id = _trip_covering_the_hotel(s, 2092).id   # linked, never re-checked
            s.commit()
            item_id = item.id
        r = client.post(f"/review/item/{item_id}/save?bucket=waiting",
                        data={"and_approve": "1"})
        assert r.status_code == 200
        assert r.headers.get("HX-Retarget") == "#queue-body"
        with SessionLocal() as s:
            assert (s.get(TravelItem, item_id).review or {}).get("action_taken") \
                == "approved"
    _skip_or(run)


def test_save_and_approve_says_why_when_it_cannot_approve():
    """A refused approval must say so instead of re-rendering silently."""
    def run():
        with SessionLocal() as s:
            item_id = _deferred_hotel(s, 2093).id                 # no trip: can't settle
        r = client.post(f"/review/item/{item_id}/save?bucket=waiting",
                        data={"and_approve": "1"})
        assert r.status_code == 200
        assert "Not approved yet." in r.text
        with SessionLocal() as s:
            assert (s.get(TravelItem, item_id).review or {}).get("action_taken") \
                != "approved"
    _skip_or(run)


def test_linking_a_trip_leaves_an_ignored_capture_ignored():
    """Ignored is terminal: linking a trip records the link, nothing else."""
    def run():
        with SessionLocal() as s:
            item = _deferred_hotel(s, 2094)
            item.review = {**(item.review or {}), "action_taken": "ignored"}
            s.commit()
            item_id = item.id
            trip_id = _trip_covering_the_hotel(s, 2094).id
        client.post(f"/review/item/{item_id}/assign-trip", data={"trip_id": trip_id})
        with SessionLocal() as s:
            it = s.get(TravelItem, item_id)
            assert it.trip_id == trip_id
            assert (it.status or {}).get("resolve") == "deferred"
    _skip_or(run)


def test_creating_the_trip_settles_a_waiting_capture():
    """YONDER-222 follow-up: making the trip a waiting capture needs re-checks
    it — no trip pick needed, no waiting for the TTL sweep."""
    def run():
        from app.review.routes import _bucket_of
        with SessionLocal() as s:
            item_id = _deferred_hotel(s, 2095).id
        r = client.post("/trips", data={"name": "Testville May 2095",
                                         "start": "2095-05-14", "end": "2095-05-18"})
        assert r.status_code == 200
        with SessionLocal() as s:
            it = s.get(TravelItem, item_id)
            assert it.trip_id is not None
            assert _bucket_of(it) == "ready"
    _skip_or(run)


def test_re_dating_a_trip_settles_a_capture_it_now_covers():
    def run():
        from app.models import Trip
        from app.review.routes import _bucket_of
        with SessionLocal() as s:
            item_id = _deferred_hotel(s, 2096).id
            trip = Trip(name="Testville 2096", source="manual",
                        start_date="2096-06-01", end_date="2096-06-05")
            s.add(trip); s.commit(); trip_id = trip.id
        client.post(f"/trips/{trip_id}", data={"name": "Testville 2096",
                                               "start": "2096-05-13", "end": "2096-05-17"})
        with SessionLocal() as s:
            it = s.get(TravelItem, item_id)
            assert it.trip_id == trip_id and _bucket_of(it) == "ready"
    _skip_or(run)


def test_a_new_trip_leaves_pinned_and_approved_captures_alone():
    """A person's own decision is never redone: a capture they unlinked by hand
    (pinned) or already approved isn't re-checked by a new trip."""
    def run():
        with SessionLocal() as s:
            pinned = _deferred_hotel(s, 2097)
            pinned.review = {**(pinned.review or {}), "trip_pinned": True}
            s.commit()
            pinned_id = pinned.id
        client.post("/trips", data={"name": "Testville May 2097",
                                    "start": "2097-05-14", "end": "2097-05-18"})
        with SessionLocal() as s:
            it = s.get(TravelItem, pinned_id)
            assert it.trip_id is None
            assert (it.status or {}).get("resolve") == "deferred"
    _skip_or(run)


def test_someone_elses_trip_never_touches_my_waiting_capture(monkeypatch):
    """Scoping (house rule 2): Ben making a trip over the dates of Ana's waiting
    hotel must not re-check it or link it to his trip."""
    def run():
        import uuid
        from app import authn
        from app.config import settings
        from app.models import Trip, User
        monkeypatch.setattr(settings, "auth_required", True)
        tag = uuid.uuid4().hex[:6]
        with SessionLocal() as s:
            ana = User(username=f"rv-ana-{tag}", auth_source="local")
            ben = User(username=f"rv-ben-{tag}", auth_source="local")
            s.add_all([ana, ben]); s.flush()
            item = _deferred_hotel(s, 2098)
            item.owner_id = ana.id
            s.commit()
            item_id, ben_id = item.id, ben.id
            ben_cookie = {settings.auth_cookie_name: authn.issue_access_token(ben)}
        r = client.post("/trips", cookies=ben_cookie,
                        data={"name": "Ben in Testville 2098",
                              "start": "2098-05-14", "end": "2098-05-18"})
        assert r.status_code == 200
        with SessionLocal() as s:
            assert s.query(Trip).filter_by(owner_id=ben_id).count() == 1
            it = s.get(TravelItem, item_id)
            assert it.trip_id is None
            assert (it.status or {}).get("resolve") == "deferred"
    _skip_or(run)


def test_re_dating_over_the_api_settles_a_waiting_capture():
    """PATCH /api/v1/trips/{id} (and MCP update_trip, which calls it) re-checks
    the owner's waiting captures too when the dates change."""
    def run():
        import uuid
        from app import authn
        from app.models import Trip, User
        from app.review.routes import _bucket_of
        with SessionLocal() as s:
            me = User(username=f"rv-api-{uuid.uuid4().hex[:6]}", auth_source="local")
            s.add(me); s.flush()
            item = _deferred_hotel(s, 2099)
            item.owner_id = me.id
            trip = Trip(name="Testville 2099", source="manual", owner_id=me.id,
                        start_date="2099-01-01", end_date="2099-01-05")
            s.add(trip); s.commit()
            item_id, trip_id = item.id, trip.id
            bearer = {"Authorization": f"Bearer {authn.issue_access_token(me)}"}
        r = client.patch(f"/api/v1/trips/{trip_id}", headers=bearer,
                         json={"start_date": "2099-05-13", "end_date": "2099-05-17"})
        assert r.status_code == 200, r.text
        with SessionLocal() as s:
            it = s.get(TravelItem, item_id)
            assert it.trip_id == trip_id and _bucket_of(it) == "ready"
    _skip_or(run)


def test_the_rundere_trip_comes_first_and_adventurelog_is_folded(monkeypatch):
    """D-192: the Run·dere trip picker leads; AdventureLog (only with a
    connection) sits below it, collapsed and marked optional."""
    def run():
        from app.review import routes
        with SessionLocal() as s:
            item_id = _deferred_hotel(s, 2088).id
            _trip_covering_the_hotel(s, 2088)
        html = client.get(f"/review/item/{item_id}?bucket=waiting").text
        assert '<h3>Trip</h3>' in html and 'class="trip-al"' not in html   # no connection
        assert "AdventureLog" not in html and "AirTrail" not in html      # not mentioned at all
        assert "Not on a trip yet" in html
        monkeypatch.setattr(routes, "_list_trips", lambda session, owner_id: [
            {"id": "c1", "name": "AL Collection", "start_date": "2088-05-01",
             "end_date": "2088-05-30"}])
        html = client.get(f"/review/item/{item_id}?bucket=waiting").text
        assert html.index("<h3>Trip</h3>") < html.index('class="trip-al"')
        assert '<details class="trip-al">' in html                        # folded
        assert "AdventureLog <span class=\"quiet\">(optional)</span>" in html
    _skip_or(run)


def test_yonder_trip_picker_rejects_an_unknown_trip():
    """Never link to a trip that doesn't exist."""
    def run():
        with SessionLocal() as s:
            item_id = _ready_flight(s).id
        r = client.post(f"/review/item/{item_id}/assign-trip",
                        data={"trip_id": "no-such-trip"})
        assert r.status_code == 200
        with SessionLocal() as s:
            assert s.get(TravelItem, item_id).trip_id is None
    _skip_or(run)


def test_deleting_a_yonder_trip_clears_its_capture_links():
    """A soft link with no DB FK (migration 0008): the delete route must null
    dangling links itself, and the capture itself must survive."""
    def run():
        from app.models import Trip
        with SessionLocal() as s:
            trip = Trip(name="Temp", start_date="2027-01-01", end_date="2027-01-05")
            s.add(trip); s.commit(); trip_id = trip.id
            item = _ready_flight(s)
            item.trip_id = trip_id; s.commit(); item_id = item.id
        client.post(f"/trips/{trip_id}/delete")
        with SessionLocal() as s:
            it = s.get(TravelItem, item_id)
            assert it is not None                      # capture survives
            assert it.trip_id is None                  # link cleared
    _skip_or(run)


def test_al_trip_picker_stores_the_pick_as_capture_context(monkeypatch):
    """2.1.2: assigning an AdventureLog trip records it as capture_context.
    collection_id — the resolver's top-of-ladder human choice (D-05) — and
    re-resolves so the item files into that collection."""
    def run():
        from app.review import routes
        monkeypatch.setattr(routes, "_list_trips", lambda session, owner_id: [
            {"id": "col-1", "name": "Trip", "start_date": "", "end_date": ""}])
        with SessionLocal() as s:
            item_id = _add(s, _flight_payload(), resolve_state="needs_review").id
        r = client.post(f"/review/item/{item_id}/assign",
                        data={"collection_id": "col-1"})
        assert r.status_code == 200
        with SessionLocal() as s:
            cc = (s.get(TravelItem, item_id).source or {}).get("capture_context") or {}
            assert cc.get("collection_id") == "col-1"
    _skip_or(run)


def test_al_trip_picker_rejects_an_unknown_collection(monkeypatch):
    """An id that names no known AdventureLog trip is ignored, not trusted."""
    def run():
        from app.review import routes
        monkeypatch.setattr(routes, "_list_trips", lambda session, owner_id: [])
        with SessionLocal() as s:
            item_id = _add(s, _flight_payload(), resolve_state="needs_review").id
        r = client.post(f"/review/item/{item_id}/assign",
                        data={"collection_id": "ghost"})
        assert r.status_code == 200
        with SessionLocal() as s:
            cc = (s.get(TravelItem, item_id).source or {}).get("capture_context") or {}
            assert "collection_id" not in cc
    _skip_or(run)


def test_write_preview_speaks_rundere_first_d192():
    """No trip: one Run·dere line. AdventureLog lines only with a connection;
    AirTrail only when a flight logs there."""
    from app.review.summary import write_preview
    hotel = {"item_type": "lodging", "lodging": {"property_name": "X"}}
    pending = {"outcome": "deferred", "targets": [], "collection_pending": True}
    lines = write_preview(hotel, {**pending, "integrations": []})
    assert [(w["system"], w["text"]) for w in lines] == [("Run·dere", "Not on a trip yet")]
    assert "AirTrail" not in lines[0]["why"]
    lines = write_preview(hotel, {**pending, "integrations": ["adventurelog"]})
    assert ("AdventureLog", "Trip record (optional)") in [(w["system"], w["text"]) for w in lines]
    filed = {"outcome": "resolved", "targets": ["yonder.activity"], "collection_pending": True,
             "integrations": ["adventurelog"]}
    systems = [w["system"] for w in write_preview(hotel, filed)]
    assert systems[0] == "Run·dere" and "Not on a trip yet" not in str(write_preview(hotel, filed))


def test_trip_notes_say_which_trip_they_mean_d192():
    """The collection codes are AdventureLog's: on an item already on a Run·dere
    trip they must not read "No trip covers these dates"."""
    def run():
        from app.review.routes import _view
        base = dict(item_type="lodging", schema_version="1.4.0",
                    extraction={"payload": {"item_type": "lodging"}},
                    status={"extract": "ok", "resolve": "ok"},
                    review={"required": True, "reasons": ["no_collection_match"]})
        al = {"outcome": "resolved", "targets": ["yonder.activity"],
              "collection_pending": True, "integrations": ["adventurelog"]}
        filed = _view(TravelItem(trip_id="t1", resolution=al, **base))
        assert filed["note"].startswith("No AdventureLog collection matches")
        assert "No trip covers" not in filed["note"]
        no_al = _view(TravelItem(trip_id="t1", resolution={**al, "integrations": []}, **base))
        assert "AdventureLog" not in (no_al["note"] or "")
        unfiled = _view(TravelItem(trip_id=None, resolution={
            "outcome": "deferred", "targets": [], "integrations": ["adventurelog"],
            "deferred_until": "2031-01-01"}, **{**base, "status": {"extract": "ok",
                                                                    "resolve": "deferred"}}))
        assert "No Run·dere trip or AdventureLog collection" in unfiled["note"]
    _skip_or(run)


def test_log_approved_opens_in_a_sheet_not_inline():
    """Design board: commit is a sheet. The preview names only the systems a
    write goes to, and Confirm swaps the sheet's own body."""
    def run():
        page = client.get("/review?bucket=staged").text
        assert 'id="modal-root"' in page
        assert 'hx-post="/review/commit"' in page and 'hx-target="#modal-root"' in page
        r = client.post("/review/commit", data={"bucket": "staged"})
        assert r.status_code == 200
        assert 'class="rd-sheet"' in r.text and 'role="dialog"' in r.text
        assert "Log approved items" in r.text and "data-sheet-close" in r.text
    _skip_or(run)


def test_systems_text_names_only_where_writes_go():
    from app.review.routes import _systems_text
    plans = lambda *targets, skipped=(): {"plans": [{"results": (
        [{"target": t} for t in targets] + [{"target": t, "skipped": "x"} for t in skipped])}]}
    assert _systems_text(plans("yonder.activity")) == "your Run·dere trips"
    assert _systems_text(plans("yonder.activity", "airtrail.flight",
                               skipped=("adventurelog.lodging",))) == \
        "your Run·dere trips and AirTrail"
    assert _systems_text(plans("yonder.activity", "airtrail.flight", "adventurelog.lodging")) == \
        "your Run·dere trips, AirTrail and AdventureLog"
