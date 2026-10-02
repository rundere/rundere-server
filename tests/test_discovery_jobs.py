"""Durable background Discovery orchestration (SPEC-3.2 §6).

Offline: fake resolvers + a fake airport/geocoder, so the mixed-tier run, the
DiscoveryRun state machine, restart-orphan detection, and the self-polling panel
are all covered without network or real threads (_execute is called directly).
"""
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, DiscoveryFinding, DiscoveryRun, Trip,
                            TripActivity)
    import app.trips.discovery_jobs as dj
    from discover.resolvers.base import Finding
    Base.metadata.create_all(engine)
    _client = TestClient(app)
    DEPS = True
except ImportError:
    DEPS = False


class _FakeDet:
    topic = "plugs"
    scope = "country"

    def resolve(self, place, ctx):
        return Finding(topic=self.topic, scope=self.scope, place_key=place.country,
                       body_md=f"plugs {place.country}", resolver="fake",
                       confidence="high")


class _FakeFood:
    """City-scope LLM stand-in that records the profile it was handed."""
    topic = "food"
    scope = "city"
    seen_profiles: list = []

    def resolve(self, place, ctx):
        _FakeFood.seen_profiles.append(ctx.discovery_profile)
        return Finding(topic=self.topic, scope=self.scope, place_key=place.city_key,
                       body_md=f"food in {place.label}", resolver="food.fake",
                       confidence="medium")


def _airports(code):
    table = {"YYZ": ("CA", "Toronto"), "GVA": ("CH", "Geneva")}
    hit = table.get((code or "").upper())
    return None if hit is None else SimpleNamespace(
        country=hit[0], municipality=hit[1], lat=0.0, lon=0.0, icao=None)


class _NoGeo:
    def geocode_place(self, place):
        return None

    def geocode_freeform(self, text):
        return None


def _fresh_trip():
    with SessionLocal() as s:
        s.query(DiscoveryFinding).delete()
        s.query(DiscoveryRun).delete()
        s.query(TripActivity).delete()
        s.query(Trip).delete()
        s.commit()
        # Always upcoming: a past trip can't be researched (D-157, YONDER-196).
        start = date.today() + timedelta(days=30)
        t = Trip(name="Alpine trip", start_date=start.isoformat(),
                 end_date=(start + timedelta(days=9)).isoformat())
        s.add(t)
        s.commit()
        s.add(TripActivity(trip_id=t.id, type="flight", starts_at=f"{start.isoformat()}T09:00",
                           values={"from": "YYZ", "to": "GVA"}))
        s.commit()
        return t.id


def _new_run(trip_id):
    with SessionLocal() as s:
        run = DiscoveryRun(trip_id=trip_id, status="running", phase="starting")
        s.add(run)
        s.commit()
        return run.id


def _execute(trip_id, run_id, *, det=None, llm=None, profile=None):
    dj._execute(run_id, trip_id, resolve_airport=_airports, geocoder=_NoGeo(),
                det_resolvers=det if det is not None else [_FakeDet()],
                llm_resolvers=llm if llm is not None else [_FakeFood()],
                home_profiles=[], excluded=set(), discovery_profile=profile,
                nationality="CA", base_currencies=("CAD", "USD"), today="2026-08-14")


# ------------------------------------------------------------- mixed-tier run

def test_execute_runs_both_tiers_and_finishes():
    if not DEPS:
        return
    _FakeFood.seen_profiles = []
    tid = _fresh_trip()
    rid = _new_run(tid)
    _execute(tid, rid, profile={"food": {"budget_ceiling": 2}})
    with SessionLocal() as s:
        run = s.get(DiscoveryRun, rid)
        assert run.status == "done" and run.phase == "done"
        # 2 countries × plugs + 2 cities × food = 4
        assert run.total == 4 and run.done == 4
        got = {(f.topic, f.place_key)
               for f in s.query(DiscoveryFinding).filter_by(trip_id=tid).all()}
        assert ("plugs", "CA") in got and ("plugs", "CH") in got
        assert ("food", "CA:Toronto") in got and ("food", "CH:Geneva") in got
    # the profile was threaded through to the LLM resolver
    assert {"food": {"budget_ceiling": 2}} in _FakeFood.seen_profiles


def test_llm_tier_failure_does_not_lose_deterministic_findings():
    if not DEPS:
        return
    class _Boom:
        topic = "food"
        scope = "city"

        def resolve(self, place, ctx):
            raise RuntimeError("ollama down")

    tid = _fresh_trip()
    rid = _new_run(tid)
    _execute(tid, rid, llm=[_Boom()])
    with SessionLocal() as s:
        run = s.get(DiscoveryRun, rid)
        assert run.status == "done"                 # a resolver raising != run failure
        got = {(f.topic, f.place_key)
               for f in s.query(DiscoveryFinding).filter_by(trip_id=tid).all()}
        assert got == {("plugs", "CA"), ("plugs", "CH")}   # det survives, no food
        # the deterministic drafts were NOT pruned by the (empty) llm phase
        assert s.query(DiscoveryFinding).filter_by(trip_id=tid).count() == 2


# ------------------------------------------------------------- orphan detection

def test_running_run_marks_orphan_failed():
    if not DEPS:
        return
    tid = _fresh_trip()
    rid = _new_run(tid)                             # not in _LIVE -> orphaned
    with SessionLocal() as s:
        assert dj.running_run(s, tid) is None
        assert s.get(DiscoveryRun, rid).status == "failed"
        assert "restart" in (s.get(DiscoveryRun, rid).error or "")


def test_running_run_returns_live_run():
    if not DEPS:
        return
    tid = _fresh_trip()
    rid = _new_run(tid)
    dj._LIVE.add(rid)
    try:
        with SessionLocal() as s:
            got = dj.running_run(s, tid)
            assert got is not None and got.id == rid and got.status == "running"
    finally:
        dj._LIVE.discard(rid)


# ------------------------------------------------------------- poll panel

def test_status_panel_shows_progress_while_live():
    if not DEPS:
        return
    tid = _fresh_trip()
    with SessionLocal() as s:
        run = DiscoveryRun(trip_id=tid, status="running", phase="food · Geneva, CH",
                           done=3, total=4)
        s.add(run)
        s.commit()
        rid = run.id
    dj._LIVE.add(rid)
    try:
        html = _client.get(f"/trips/{tid}/discover/status").text
        assert "Researching" in html
        assert "food · Geneva, CH" in html and "3 / 4" in html
        # it keeps polling itself
        assert f"/trips/{tid}/discover/status" in html
    finally:
        dj._LIVE.discard(rid)


def test_status_panel_stops_and_shows_findings_when_done():
    if not DEPS:
        return
    tid = _fresh_trip()
    rid = _new_run(tid)
    _execute(tid, rid)                              # completes -> status done
    html = _client.get(f"/trips/{tid}/discover/status").text
    assert "Food &amp; Drink" in html                             # the section rail
    assert 'hx-trigger="load delay:1500ms' not in html            # polling stopped
    assert "Re-run discovery" in html                             # button back


def test_render_md_handles_headings_and_callouts():
    if not DEPS:
        return
    from app.trips.routes import _render_md
    html = str(_render_md("### Restaurants\n- **Tasca** — good\n> ⚠️ **Warn** this\n> - egg"))
    assert '<span class="yn-fh">Restaurants</span>' in html        # ### heading
    assert "• <strong>Tasca</strong> — good" in html               # bullet + bold
    assert '<span class="yn-fq">⚠️ <strong>Warn</strong> this</span>' in html  # callout
    assert '<span class="yn-fq">• egg</span>' in html              # bulleted callout
    assert "###" not in html and "&gt;" not in html                # no raw markdown


def test_render_md_renders_safe_links():
    if not DEPS:
        return
    from app.trips.routes import _render_md
    html = str(_render_md("- **Tasca** — good [map ↗](https://maps.example/?q=a&b=1)"))
    assert '<a href="https://maps.example/?q=a&amp;b=1" target="_blank"' in html
    assert "rel=\"noopener\">map ↗</a>" in html
    # a javascript: url is NOT linkified (regex only matches http/https)
    raw = str(_render_md("[x](javascript:alert(1))"))
    assert "<a" not in raw and "javascript:alert(1)" in raw


# ---------------------------------------------------------------- YONDER-196 / D-157
def _set_trip_dates(tid, start, end):
    with SessionLocal() as s:
        t = s.get(Trip, tid)
        t.start_date, t.end_date = start, end
        s.commit()


def _launches(monkeypatch):
    calls = []
    monkeypatch.setattr(dj, "launch", lambda *a, **k: calls.append(a))
    return calls


def test_trip_has_ended_rules():
    from types import SimpleNamespace as T
    from discover.runner import trip_has_ended
    assert trip_has_ended(T(start_date="2026-01-01", end_date="2026-01-05"), "2026-10-02")
    assert not trip_has_ended(T(start_date="2026-09-30", end_date="2026-10-02"), "2026-10-02")
    assert not trip_has_ended(T(start_date="2027-01-01", end_date="2027-01-05"), "2026-10-02")
    assert trip_has_ended(T(start_date="2026-09-01", end_date=None), "2026-10-02")   # start stands in
    assert not trip_has_ended(T(start_date=None, end_date=None), "2026-10-02")       # undated = planning


def test_run_is_blocked_for_a_past_trip_but_allowed_for_current_and_future(monkeypatch):
    if not DEPS:
        return
    calls = _launches(monkeypatch)
    tid = _fresh_trip()
    today = date.today()
    fmt = lambda d: d.isoformat()          # noqa: E731
    _set_trip_dates(tid, fmt(today - timedelta(days=20)), fmt(today - timedelta(days=10)))
    r = _client.post(f"/trips/{tid}/discover")
    assert r.status_code == 200 and "This trip has ended" in r.text
    assert f'hx-post="/trips/{tid}/discover"' not in r.text and not calls   # no button; nothing launched
    _set_trip_dates(tid, fmt(today - timedelta(days=2)), fmt(today + timedelta(days=2)))
    assert "This trip has ended" not in _client.post(f"/trips/{tid}/discover").text
    _set_trip_dates(tid, fmt(today + timedelta(days=30)), fmt(today + timedelta(days=40)))
    assert "This trip has ended" not in _client.post(f"/trips/{tid}/discover").text
    assert len(calls) == 2


def test_rerun_is_blocked_for_a_past_trip_and_findings_stay_readable(monkeypatch):
    if not DEPS:
        return
    tid = _fresh_trip()
    rid = _new_run(tid)
    _execute(tid, rid)
    with SessionLocal() as s:
        fid = s.query(DiscoveryFinding).filter(DiscoveryFinding.trip_id == tid).first().id
    ran = []
    monkeypatch.setattr("discover.runner.rerun_finding", lambda *a, **k: ran.append(1))
    today = date.today()
    _set_trip_dates(tid, (today - timedelta(days=20)).isoformat(),
                    (today - timedelta(days=10)).isoformat())
    r = _client.post(f"/trips/{tid}/finding/{fid}/rerun")
    assert r.status_code == 200 and not ran
    section = _client.get(f"/trips/{tid}/discover/status").text
    assert "This trip has ended" in section and "Food &amp; Drink" in section   # still readable
    assert "/rerun" not in section and "Refresh rates" not in section           # no buttons offered
