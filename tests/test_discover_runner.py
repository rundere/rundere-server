"""Discovery runner tests (SPEC-3-discovery.md §6) — real DB session, offline
resolvers, so run lifecycle + finding persistence + idempotency are covered
without network.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from app.db import SessionLocal, engine
    from app.models import (Base, DiscoveryFinding, DiscoveryRun, Trip,
                            TripActivity)
    from discover.places import DerivedPlace
    from discover.resolvers import PlugsResolver
    from discover.resolvers.base import Finding
    from discover.runner import run_discovery
    Base.metadata.create_all(engine)
    DEPS = True
except ImportError:
    DEPS = False


class _FakeCityResolver:
    """A city-scope resolver that needs no network — stands in for weather."""
    topic = "weather"
    scope = "city"

    def resolve(self, place, ctx):
        return Finding(topic=self.topic, scope=self.scope,
                       place_key=place.city_key or place.country,
                       body_md=f"weather for {place.label}", resolver="fake")


def _airports(code):
    table = {"YYZ": ("CA", "Toronto"), "GVA": ("CH", "Geneva")}
    hit = table.get((code or "").upper())
    if hit is None:
        return None
    return SimpleNamespace(country=hit[0], municipality=hit[1], lat=0.0, lon=0.0,
                           icao=None)


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
        t = Trip(name="Alpine trip", start_date="2026-09-01", end_date="2026-09-10")
        s.add(t)
        s.commit()
        s.add(TripActivity(trip_id=t.id, type="flight", starts_at="2026-09-01T09:00",
                           values={"from": "YYZ", "to": "GVA"}))
        s.commit()
        return t.id


def _run(trip_id, excluded=frozenset()):
    with SessionLocal() as s:
        trip = s.get(Trip, trip_id)
        run = run_discovery(s, trip, resolve_airport=_airports, geocoder=_NoGeo(),
                            resolvers=[PlugsResolver(), _FakeCityResolver()],
                            excluded=excluded, today="2026-08-14")
        return run.id


def test_runner_persists_findings_and_marks_done():
    if not DEPS:
        return
    tid = _fresh_trip()
    _run(tid)
    with SessionLocal() as s:
        run = (s.query(DiscoveryRun).filter_by(trip_id=tid).one())
        assert run.status == "done"
        # 2 countries × plugs + 2 cities × weather = 4 tasks/findings.
        assert run.total == 4
        findings = s.query(DiscoveryFinding).filter_by(trip_id=tid).all()
        assert len(findings) == 4
        plugs = {f.place_key for f in findings if f.topic == "plugs"}
        assert plugs == {"CA", "CH"}
        weather = {f.place_key for f in findings if f.topic == "weather"}
        assert weather == {"CA:Toronto", "CH:Geneva"}
        assert all(f.status == "draft" for f in findings)


def test_rerun_preserves_edited_but_refreshes_drafts():
    if not DEPS:
        return
    tid = _fresh_trip()
    _run(tid)
    with SessionLocal() as s:
        ch = s.query(DiscoveryFinding).filter_by(trip_id=tid, topic="plugs",
                                                 place_key="CH").one()
        ch.status = "edited"
        ch.body_md = "HUMAN EDIT — keep me"
        s.commit()
        edited_id = ch.id

    _run(tid)                       # whole-trip re-run over the same trip

    with SessionLocal() as s:
        ch = s.get(DiscoveryFinding, edited_id)
        assert ch.status == "edited"              # human edit preserved
        assert ch.body_md == "HUMAN EDIT — keep me"
        # a draft finding was regenerated in place (still exactly one row)
        ca = s.query(DiscoveryFinding).filter_by(trip_id=tid, topic="plugs",
                                                 place_key="CA").all()
        assert len(ca) == 1 and ca[0].status == "draft"
        # no duplication overall
        assert s.query(DiscoveryFinding).filter_by(trip_id=tid).count() == 4


def test_excluded_place_is_not_researched():
    if not DEPS:
        return
    tid = _fresh_trip()
    _run(tid, excluded={"CH:Geneva"})
    with SessionLocal() as s:
        got = {(f.topic, f.place_key)
               for f in s.query(DiscoveryFinding).filter_by(trip_id=tid).all()}
        assert not any(pk.startswith("CH") for _, pk in got)   # Geneva/CH excluded
        assert ("plugs", "CA") in got and ("weather", "CA:Toronto") in got
        run = (s.query(DiscoveryRun).filter_by(trip_id=tid)
               .order_by(DiscoveryRun.started_at.desc()).first())
        assert run.params["excluded"] == ["CH:Geneva"]


def test_rerun_prunes_stale_drafts():
    if not DEPS:
        return
    tid = _fresh_trip()
    _run(tid)                                   # 4 findings incl. CH
    with SessionLocal() as s:
        assert s.query(DiscoveryFinding).filter_by(trip_id=tid).count() == 4
    _run(tid, excluded={"CH:Geneva"})           # re-run with Geneva excluded
    with SessionLocal() as s:
        got = {(f.topic, f.place_key)
               for f in s.query(DiscoveryFinding).filter_by(trip_id=tid).all()}
        assert got == {("plugs", "CA"), ("weather", "CA:Toronto")}   # CH drafts pruned


_FLAKY = {"fail": False}


class _FlakyCityResolver:
    """A city-scope resolver that returns a finding when healthy and NOTHING when
    flaky — stands in for the federated events/food/safety tier whose live search
    can come back empty on any given run."""
    topic = "events"
    scope = "city"

    def resolve(self, place, ctx):
        if _FLAKY["fail"]:
            return None
        return Finding(topic=self.topic, scope=self.scope,
                       place_key=place.city_key or place.country,
                       body_md=f"events for {place.label}", resolver="fake")


def _run_resolvers(trip_id, resolvers, excluded=frozenset()):
    with SessionLocal() as s:
        trip = s.get(Trip, trip_id)
        run = run_discovery(s, trip, resolve_airport=_airports, geocoder=_NoGeo(),
                            resolvers=resolvers, excluded=excluded, today="2026-08-14")
        return run.id


def test_flaky_empty_run_keeps_last_good_finding():
    # The regression behind "cities vanish on re-run": a resolver that produced a
    # finding last run but comes back empty this run must NOT lose the finding —
    # the place is still on the trip, so the last-good draft is preserved.
    if not DEPS:
        return
    tid = _fresh_trip()
    r = _FlakyCityResolver()
    _FLAKY["fail"] = False
    try:
        _run_resolvers(tid, [r])                    # both cities get an events finding
        with SessionLocal() as s:
            assert s.query(DiscoveryFinding).filter_by(
                trip_id=tid, topic="events").count() == 2
        _FLAKY["fail"] = True
        _run_resolvers(tid, [r])                    # every resolve returns None now
        with SessionLocal() as s:
            # places still in scope → last-good findings preserved, not pruned
            assert s.query(DiscoveryFinding).filter_by(
                trip_id=tid, topic="events").count() == 2
    finally:
        _FLAKY["fail"] = False


def test_prune_keeps_human_touched_findings():
    if not DEPS:
        return
    tid = _fresh_trip()
    _run(tid)
    with SessionLocal() as s:
        ch = s.query(DiscoveryFinding).filter_by(trip_id=tid, topic="plugs",
                                                 place_key="CH").one()
        ch.status = "edited"
        s.commit()
        cid = ch.id
    _run(tid, excluded={"CH:Geneva"})           # CH no longer produced
    with SessionLocal() as s:
        kept = s.get(DiscoveryFinding, cid)
        assert kept is not None and kept.status == "edited"   # not pruned


def test_rerun_single_finding_overrides_edit():
    if not DEPS:
        return
    from discover.runner import rerun_finding
    tid = _fresh_trip()
    _run(tid)
    with SessionLocal() as s:
        ch = s.query(DiscoveryFinding).filter_by(trip_id=tid, topic="plugs",
                                                 place_key="CH").one()
        ch.status = "edited"
        ch.body_md = "STALE MANUAL TEXT"
        s.commit()
        fid = ch.id
    with SessionLocal() as s:
        f = s.get(DiscoveryFinding, fid)
        rerun_finding(s, f, resolve_airport=_airports, geocoder=_NoGeo(),
                      resolvers=[PlugsResolver(), _FakeCityResolver()],
                      today="2026-08-14")
    with SessionLocal() as s:
        f = s.get(DiscoveryFinding, fid)
        # Explicit single re-run OVERRIDES the edit and regenerates fresh content.
        assert f.status == "draft"
        assert "Type" in f.body_md and f.body_md != "STALE MANUAL TEXT"
