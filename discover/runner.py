"""Discovery orchestration (SPEC-3-discovery.md §6).

Derive a trip's locations first-party (discover/places.py), run the enabled
resolvers over them, and persist the results as durable DiscoveryFinding rows
inside a DiscoveryRun (D-63). This slice runs SYNCHRONOUSLY — the deterministic
tier (plugs/currency/weather) is a handful of fast HTTP calls — while still
writing the durable run/finding rows. The background-thread + polling harness
lands with the slow searxng+llm tier, which is where it earns its keep.

Idempotency (D-63/D-71): a finding is keyed by (trip_id, topic, place_key). A
re-run refreshes findings in place under discover/policy.py: time-sensitive
topics (weather, currency, food, …) are refreshed even when approved/edited,
with the old version kept as history; a LOCKED finding (plugs/visa/language,
approved or edited), a rejected one, or one an AI agent wrote is left untouched.
The per-finding re-run is the tool for deliberately overriding one of those.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.models import DiscoveryFinding, DiscoveryRun, TripActivity
from discover import items, policy
from discover.home_filter import home_country_set, is_home_place
from discover.places import DerivedLocations, DerivedPlace, derive_locations
from discover.resolvers import Finding, Resolver, ResolverContext

log = logging.getLogger("yonder.discovery")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _country_reps(derived: DerivedLocations) -> list[DerivedPlace]:
    seen: dict[str, DerivedPlace] = {}
    for p in derived.places:
        if p.country and p.country not in seen:
            seen[p.country] = p
    return list(seen.values())


def _city_reps(derived: DerivedLocations) -> list[DerivedPlace]:
    seen: dict[str, DerivedPlace] = {}
    for p in derived.places:
        k = p.city_key
        if k and k not in seen:
            seen[k] = p
    return list(seen.values())


def _plan_tasks(derived: DerivedLocations, resolvers: list[Resolver],
                profiles: list, on_date: str | None
                ) -> tuple[list[tuple[Resolver, DerivedPlace]], dict]:
    """The (resolver, place) tasks to run, with home places suppressed
    scope-aware (see discover/home_filter). Also returns what was suppressed, for
    transparency on the run and in the derived-locations viewer."""
    countries = _country_reps(derived)
    cities = _city_reps(derived)
    home_countries = home_country_set(derived.places, profiles, on_date)
    home_cities = {p.city_key for p in cities if is_home_place(p, profiles, on_date)}

    out: list[tuple[Resolver, DerivedPlace]] = []
    for r in resolvers:
        if r.scope == "country":
            for place in countries:
                if not (place.country and place.country.upper() in home_countries):
                    out.append((r, place))
        else:
            for place in cities:
                if place.city_key not in home_cities:
                    out.append((r, place))

    skipped = {
        "home_countries": sorted(c for c in home_countries
                                 if any(p.country and p.country.upper() == c
                                        for p in countries)),
        "home_cities": sorted(p.label for p in cities if p.city_key in home_cities),
    }
    return out, skipped


def _upsert(session: Session, run: DiscoveryRun, trip_id: str, f: Finding) -> None:
    # Keyed per person too (4.1): each viewer has their own findings.
    existing = (session.query(DiscoveryFinding)
                .filter_by(trip_id=trip_id, user_id=run.user_id, topic=f.topic,
                           place_key=f.place_key)
                .first())
    # discover/policy.py: a locked (lockable topic + approved/edited), rejected
    # or agent-written finding is left alone; anything else is refreshed —
    # even an approved weather report — with the old version kept as history.
    history: list = []
    if existing is not None:
        if not policy.local_run_may_replace(existing):
            return
        history = policy.history_with(existing, f.body_md)
    if existing is None:
        existing = DiscoveryFinding(trip_id=trip_id, user_id=run.user_id,
                                    topic=f.topic, place_key=f.place_key)
        session.add(existing)
    existing.run_id = run.id
    existing.scope = f.scope
    existing.status = "draft"
    existing.body_md = f.body_md
    existing.data = policy.with_history(f.data, history)
    existing.sources = f.sources
    existing.confidence = f.confidence
    existing.resolver = f.resolver
    existing.ttl_seconds = f.ttl_seconds
    existing.generated_at = _now()
    items.sync_from_finding(session, existing)      # Food/Events rows (YONDER-147)


def _in_scope_keys(derived: DerivedLocations) -> set[str]:
    """Every place_key still in the trip's scope — the country key and the city
    key of each derived place. A finding whose place_key is in here belongs to a
    place that is still on the trip; one that isn't belongs to a place that was
    excluded or removed."""
    keys: set[str] = set()
    for p in derived.places:
        if p.country:
            keys.add(p.country)
        if p.city_key:
            keys.add(p.city_key)
    return keys


def _prune_stale_drafts(session: Session, trip_id: str,
                        in_scope_keys: set[str], user_id: str | None = None) -> None:
    """Delete a DRAFT finding only when its PLACE has left the trip — excluded in
    the viewer, or its activity removed / no longer geocodes.

    A place that is STILL in scope but produced nothing this run (a flaky
    SearXNG/LLM pass — the events/food/safety tier federates live search, which is
    inherently non-deterministic) KEEPS its last-good draft. A finding is only ever
    replaced by a newer result via _upsert, never deleted because one run came back
    empty — otherwise a good result silently vanishes on the next re-run.
    Human-touched findings (approved/edited/rejected) are always kept, and so is
    anything an AI agent wrote (topic "research" or resolver "agent", YONDER-108) —
    no local resolver made it, so no local run can re-make it."""
    for f in (session.query(DiscoveryFinding)
              .filter(DiscoveryFinding.trip_id == trip_id,
                      DiscoveryFinding.user_id == user_id,
                      DiscoveryFinding.topic != "research",
                      DiscoveryFinding.status == "draft").all()):
        if f.place_key not in in_scope_keys and not policy.is_external(f):
            items.delete_for_finding(session, f.id)
            session.delete(f)


def _find_place(derived: DerivedLocations, scope: str,
                place_key: str) -> DerivedPlace | None:
    for p in derived.places:
        if scope == "country" and p.country == place_key:
            return p
        if scope != "country" and p.city_key == place_key:
            return p
    return None


def rerun_finding(session: Session, finding, *, resolve_airport, geocoder,
                  resolvers: list[Resolver],
                  base_currencies: tuple[str, ...] = ("CAD", "USD"),
                  today: str | None = None):
    """Regenerate ONE finding in place — the per-topic re-run (D-71). Unlike a
    whole-trip run this deliberately OVERRIDES a locked, edited or agent-written
    finding (an explicit "give me a fresh one"; the old version goes to history), re-fetching live topics (weather/currency) and, for
    the LLM tier later, carrying a steer note. Returns the row, or None if the
    place no longer derives or the topic has no resolver."""
    from app.models import Trip
    resolver = next((r for r in resolvers if r.topic == finding.topic), None)
    if resolver is None:
        return None
    trip = session.get(Trip, finding.trip_id)
    if trip is None:
        return None
    acts = session.query(TripActivity).filter(
        TripActivity.trip_id == trip.id).all()
    derived = derive_locations(acts, resolve_airport=resolve_airport,
                               geocoder=geocoder)
    place = _find_place(derived, finding.scope, finding.place_key)
    if place is None:
        return None
    from discover import langdata
    ctx = ResolverContext(start_date=trip.start_date, end_date=trip.end_date,
                          today=today or date.today().isoformat(),
                          base_currencies=tuple(base_currencies),
                          phrasebook=langdata.phrasebook(session),
                          regional_map=langdata.regional_map(session),
                          generated_languages=langdata.generated_languages(session))
    fresh = resolver.resolve(place, ctx)
    if fresh is None:
        return None
    history = policy.history_with(finding, fresh.body_md)   # an override keeps the old one
    finding.body_md = fresh.body_md
    finding.data = policy.with_history(fresh.data, history)
    finding.sources = fresh.sources
    finding.confidence = fresh.confidence
    finding.resolver = fresh.resolver
    finding.ttl_seconds = fresh.ttl_seconds
    finding.status = "draft"
    finding.generated_at = _now()
    items.sync_from_finding(session, finding)
    session.commit()
    return finding


def run_discovery(session: Session, trip, *, resolve_airport, geocoder,
                  resolvers: list[Resolver], home_profiles: list = (),
                  excluded: set[str] = frozenset(),
                  base_currencies: tuple[str, ...] = ("CAD", "USD"),
                  today: str | None = None, user_id: str | None = None) -> DiscoveryRun:
    """Execute one Discovery pass over `trip` and persist its findings.

    `home_profiles` (HomeProfileRef list) suppresses research of home — see
    discover/home_filter. `excluded` is the set of DerivedPlace place_keys the
    operator excluded in the viewer. Returns the finished (or failed)
    DiscoveryRun row, already committed."""
    run = DiscoveryRun(trip_id=trip.id, user_id=user_id, status="running",
                       phase="deriving locations", started_at=_now())
    session.add(run)
    session.flush()                 # need run.id for finding.run_id

    try:
        acts = session.query(TripActivity).filter(
            TripActivity.trip_id == trip.id).all()
        derived = derive_locations(acts, resolve_airport=resolve_airport,
                                   geocoder=geocoder)
        # Drop operator-excluded places before anything is planned or counted.
        if excluded:
            derived.places = [p for p in derived.places
                              if p.place_key not in excluded]
        from discover import langdata
        ctx = ResolverContext(
            start_date=trip.start_date, end_date=trip.end_date,
            today=today or date.today().isoformat(),
            base_currencies=tuple(base_currencies),
            phrasebook=langdata.phrasebook(session),
            regional_map=langdata.regional_map(session),
            generated_languages=langdata.generated_languages(session))

        tasks, home_skipped = _plan_tasks(derived, resolvers,
                                          list(home_profiles or []), trip.start_date)
        run.params = {"countries": derived.countries, "cities": derived.cities,
                      "dropped": len(derived.dropped), "home_skipped": home_skipped,
                      "excluded": sorted(excluded)}
        run.total = len(tasks)
        session.flush()

        for i, (resolver, place) in enumerate(tasks):
            run.phase = f"{resolver.topic} · {place.label}"
            run.done = i
            session.flush()
            try:
                finding = resolver.resolve(place, ctx)
            except Exception:
                log.exception("discovery: %s failed for %s on trip %s",
                              resolver.topic, place.label, trip.id)
                finding = None
            if finding is not None:
                _upsert(session, run, trip.id, finding)
                from discover import langdata
                langdata.persist_finding_generated(session, finding.data)
            session.flush()

        _prune_stale_drafts(session, trip.id, _in_scope_keys(derived), run.user_id)
        run.done = len(tasks)
        run.status = "done"
        run.phase = "done"
    except Exception as ex:
        log.exception("discovery run failed for trip %s", trip.id)
        run.status = "failed"
        run.error = str(ex)
    finally:
        run.finished_at = _now()
        session.commit()
    return run
