"""Durable, background Discovery orchestration (SPEC-3.2 §6).

The deterministic tier is fast, but the LLM tier is minutes-to-hours and must
survive a restart — so a Discovery run is a DURABLE `DiscoveryRun` row, not the
in-memory job registry (app/review/jobs.py). This module runs BOTH tiers on a
single background worker (§6.1): the request returns immediately with a
self-polling panel, and findings stream in as each resolver completes.

State machine, all on the row:
  * launch() creates the row (status=running) and submits the worker.
  * the worker writes phase/done/total as it goes and finalizes status=done.
  * a run orphaned by a restart is caught by running_run(): the row still says
    `running` but its id is not in this process's _LIVE set, so the next poll
    marks it `failed` (re-run to recover).

One worker (single-threaded executor) + a per-trip guard in the route means one
run per trip at a time; a second trip queues behind the first.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from app.db import SessionLocal
from app.models import DiscoveryRun, Trip, TripActivity
from discover.places import derive_locations
from discover.resolvers import ResolverContext
from discover.runner import _plan_tasks, _prune_stale_drafts, _upsert

log = logging.getLogger("yonder.discovery.jobs")

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tx-discovery")
_LOCK = threading.Lock()
_LIVE: set[str] = set()          # run ids executing in THIS process


def _now() -> datetime:
    return datetime.now(timezone.utc)


def running_run(session, trip_id: str) -> DiscoveryRun | None:
    """The trip's live run, or None. A row that says `running` but whose id is
    not in _LIVE was orphaned by a restart — mark it failed and return None."""
    run = (session.query(DiscoveryRun)
           .filter(DiscoveryRun.trip_id == trip_id)
           .order_by(DiscoveryRun.started_at.desc()).first())
    if run is None or run.status != "running":
        return None
    with _LOCK:
        live = run.id in _LIVE
    if live:
        return run
    run.status = "failed"
    run.error = "interrupted — the app restarted mid-run; re-run to recover"
    run.finished_at = _now()
    session.commit()
    return None


def launch(trip_id: str, *, resolve_airport, geocoder, det_resolvers,
           llm_resolvers, home_profiles, excluded, discovery_profile,
           nationality, base_currencies, today) -> str:
    """Create the run row and submit the worker. Returns the new run id."""
    with SessionLocal() as s:
        run = DiscoveryRun(trip_id=trip_id, status="running", phase="starting",
                           started_at=_now())
        s.add(run)
        s.commit()
        run_id = run.id
    with _LOCK:
        _LIVE.add(run_id)
    _EXECUTOR.submit(
        _execute, run_id, trip_id, resolve_airport=resolve_airport,
        geocoder=geocoder, det_resolvers=det_resolvers, llm_resolvers=llm_resolvers,
        home_profiles=home_profiles, excluded=excluded,
        discovery_profile=discovery_profile, nationality=nationality,
        base_currencies=base_currencies, today=today)
    return run_id


def _execute(run_id: str, trip_id: str, *, resolve_airport, geocoder,
             det_resolvers, llm_resolvers, home_profiles, excluded,
             discovery_profile, nationality, base_currencies, today) -> None:
    """The worker body. Its OWN session (it runs off-request). Commits after each
    finding so the poll panel streams them in. Directly callable in tests."""
    try:
        with SessionLocal() as s:
            trip = s.get(Trip, trip_id)
            run = s.get(DiscoveryRun, run_id)
            if trip is None or run is None:
                return
            run.phase = "deriving locations"
            s.commit()
            acts = s.query(TripActivity).filter(
                TripActivity.trip_id == trip_id).all()
            derived = derive_locations(acts, resolve_airport=resolve_airport,
                                       geocoder=geocoder)
            if excluded:
                derived.places = [p for p in derived.places
                                  if p.place_key not in excluded]
            homes = list(home_profiles or [])
            det_tasks, home_skipped = _plan_tasks(derived, det_resolvers, homes,
                                                  trip.start_date)
            llm_tasks, _ = _plan_tasks(derived, llm_resolvers, homes, trip.start_date)
            run.total = len(det_tasks) + len(llm_tasks)
            run.params = {"countries": derived.countries, "cities": derived.cities,
                          "dropped": len(derived.dropped),
                          "home_skipped": home_skipped, "excluded": sorted(excluded)}
            s.commit()

            ctx = ResolverContext(
                start_date=trip.start_date, end_date=trip.end_date,
                today=today or date.today().isoformat(),
                base_currencies=tuple(base_currencies),
                discovery_profile=discovery_profile, nationality=nationality)

            produced: set[tuple[str, str]] = set()
            done = 0
            for resolver, place in [*det_tasks, *llm_tasks]:
                run.phase = f"{resolver.topic} · {place.label}"
                run.done = done
                s.commit()
                try:
                    finding = resolver.resolve(place, ctx)
                except Exception:
                    log.exception("discovery: %s failed for %s on trip %s",
                                  resolver.topic, place.label, trip_id)
                    finding = None
                if finding is not None:
                    _upsert(s, run, trip_id, finding)
                    produced.add((finding.topic, finding.place_key))
                done += 1
                run.done = done
                s.commit()                       # stream the finding in

            _prune_stale_drafts(s, trip_id, produced)
            run.done = run.total
            run.status = "done"
            run.phase = "done"
            run.finished_at = _now()
            s.commit()
    except Exception as ex:                      # pragma: no cover - defensive
        log.exception("discovery run %s failed", run_id)
        try:
            with SessionLocal() as s2:
                run = s2.get(DiscoveryRun, run_id)
                if run is not None:
                    run.status = "failed"
                    run.error = str(ex)
                    run.finished_at = _now()
                    s2.commit()
        except Exception:
            pass
    finally:
        with _LOCK:
            _LIVE.discard(run_id)
