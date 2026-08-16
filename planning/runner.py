"""Planning orchestration (SPEC-3.1-planning.md §2/§6).

Derive context → run the engine → persist a PackingPlan + PackingItem rows.
Synchronous and durable, mirroring the Discovery runner. Idempotent on
(trip_id, item_key): a re-generate PRESERVES the human states (packed = ticked
off, removed = you dropped it) and refreshes only draft/omitted rows, then prunes
stale drafts — the same review-survives-regeneration contract as Discovery (D-63).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (DiscoveryFinding, PackingItem, PackingPlan,
                        PackingProfile, TripActivity)
from discover.home_filter import home_country_set
from discover.places import derive_locations
from planning import catalog
from planning.context import derive_context
from planning.engine import build_plan, default_profile

log = logging.getLogger("yonder.planning")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_profile(session: Session) -> dict:
    """The active packing profile's values, or engine defaults. Single-user for
    now (D-82): the first profile row wins; a per-traveller lookup slots in here
    when Yonder gains multi-user."""
    row = session.query(PackingProfile).order_by(PackingProfile.created_at).first()
    return {**default_profile(), **(row.values or {})} if row else default_profile()


def _upsert_item(session: Session, plan: PackingPlan, trip_id: str, pi) -> None:
    row = (session.query(PackingItem)
           .filter_by(trip_id=trip_id, item_key=pi.item_key).first())
    if row is not None and row.status in ("packed", "removed"):
        return                              # human decision — never clobbered
    if row is None:
        row = PackingItem(trip_id=trip_id, item_key=pi.item_key)
        session.add(row)
    row.plan_id = plan.id
    row.label = pi.label
    row.category = pi.category
    row.quantity = pi.quantity
    row.bag = pi.bag
    row.reason = pi.reason
    row.source = pi.source
    row.swap_for = pi.swap_for
    row.status = pi.status              # draft | omitted
    row.updated_at = _now()


def generate_plan(session: Session, trip, *, resolve_airport, geocoder,
                  home_profiles: list = (), today: str | None = None) -> PackingPlan:
    """Generate and persist a Packing Plan for `trip`. Returns the committed row."""
    plan = PackingPlan(trip_id=trip.id, status="running", generated_at=_now())
    session.add(plan)
    session.flush()
    try:
        acts = session.query(TripActivity).filter(
            TripActivity.trip_id == trip.id).all()
        derived = derive_locations(acts, resolve_airport=resolve_airport,
                                   geocoder=geocoder)
        weather = (session.query(DiscoveryFinding)
                   .filter(DiscoveryFinding.trip_id == trip.id,
                           DiscoveryFinding.topic == "weather").all())
        profiles = list(home_profiles or [])
        home_countries = home_country_set(derived.places, profiles, trip.start_date)
        ctx = derive_context(acts, derived, weather, start_date=trip.start_date,
                             end_date=trip.end_date, profile=load_profile(session),
                             home_countries=home_countries)
        result = build_plan(ctx, catalog.items(session))

        plan.archetypes = result.archetypes
        plan.context = ctx.snapshot()
        plan.warnings = result.warnings
        plan.tasks = result.tasks

        produced: set[str] = set()
        for pi in result.items + result.omitted:
            _upsert_item(session, plan, trip.id, pi)
            produced.add(pi.item_key)
        # prune stale drafts/omitted the latest run didn't produce (item removed,
        # place excluded, activity changed) — keep packed/removed.
        for row in (session.query(PackingItem)
                    .filter(PackingItem.trip_id == trip.id,
                            PackingItem.status.in_(("draft", "omitted"))).all()):
            if row.item_key not in produced:
                session.delete(row)

        plan.status = "done"
    except Exception as ex:
        log.exception("packing plan failed for trip %s", trip.id)
        plan.status = "failed"
        plan.error = str(ex)
    finally:
        plan.finished_at = _now()
        session.commit()
    return plan
