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

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import (Bag, DiscoveryExclusion, DiscoveryFinding, InventoryItem,
                        PackingItem, PackingPlan,
                        PackingProfile, PackingTemplate, PackingTemplateItem,
                        TripActivity)
from discover.home_filter import home_country_set, is_home_place
from discover.places import DerivedLocations, derive_locations
from planning import bags as bagfit
from planning import catalog, culture
from planning.assemble import assemble
from planning.context import derive_context
from planning.engine import default_profile

log = logging.getLogger("yonder.planning")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def profile_row(session: Session, traveler_id: int | None, *,
                owner: bool = False) -> PackingProfile | None:
    """ONE person's packing profile row (D-123, 4.1). Their traveller's own row;
    the deployment owner (the `self` traveller, or `owner` = auth off) also
    keeps the pre-4.1 unassigned row. Anyone else without a row of their own
    gets None — never the owner's preferences."""
    from app.models import TravelerIdentity
    if traveler_id is not None:
        own = session.query(PackingProfile).filter_by(traveler_id=traveler_id).first()
        if own is not None:
            return own
        ident = session.get(TravelerIdentity, traveler_id)
        if ident is None or ident.kind != "self":
            return None
    elif not owner:
        return None
    return (session.query(PackingProfile).filter_by(traveler_id=None)
            .order_by(PackingProfile.created_at).first())


def load_profile(session: Session, traveler_id: int | None = None, *,
                 owner: bool = True) -> dict:
    """The packing profile values for one person (see profile_row), over the
    engine defaults. This is the per-traveller lookup D-82 anticipated."""
    row = profile_row(session, traveler_id, owner=owner)
    return {**default_profile(), **(row.values or {})} if row else default_profile()


PROFILE_URL = "/trips/settings/packing-profile"
PROFILE_REQUIRED = ("Set up your packing profile first: answer the questions in "
                    "Settings → Packing profile.")


class PackingProfileRequired(HTTPException):
    """No saved questionnaire, so no inventory to pack from (SPEC-5 §6.8,
    D-231). The API answers 409 with `detail` still a plain string, which
    today's iOS and Android builds decode and show; `code` and `url` are
    extra (app.main's handler adds them)."""
    code = "packing_profile_required"
    url = PROFILE_URL

    def __init__(self) -> None:
        super().__init__(status_code=409, detail=PROFILE_REQUIRED)


def profile_ready(session: Session, traveler_id: int | None, *, owner: bool) -> bool:
    """Has this person saved the packing questionnaire (§6.7.1)?"""
    row = profile_row(session, traveler_id, owner=owner)
    return bool(row and (row.values or {}).get("questionnaire_saved_at"))


def answer_questionnaire(session: Session, *, user_id: str | None,
                         traveler_id: int | None, answers: dict,
                         prefs: dict | None = None) -> None:
    """Save packing questionnaire answers for one person and seed their
    inventory and bags, as the Settings → Packing profile form does
    (SPEC-5 §6.2–6.3). For the demo seed and tests; `answers` must already be
    valid. The caller commits."""
    from datetime import datetime, timezone
    from planning import inventory
    values = {**(prefs or {}), "questionnaire": answers,
              "questionnaire_saved_at": datetime.now(timezone.utc).isoformat()}
    row = profile_row(session, traveler_id, owner=user_id is None)
    if row is None:
        session.add(PackingProfile(traveler_id=traveler_id, values=values))
    else:
        row.values = {**(row.values or {}), **values}
    inventory.seed_inventory(session, user_id, answers, catalog.items(session))
    inventory.seed_bags(session, user_id, answers.get("bags") or {})


def _own(model, user_id: str | None):
    # Auth off (no user): the deployment's ownerless rows, as the packing
    # settings pages keep them.
    return model.user_id.is_(None) if user_id is None else model.user_id == user_id


def _inventory(session: Session, user_id: str | None) -> list[dict]:
    return [{"id": r.id, "type_key": r.type_key, "label": r.label, "category": r.category,
             "tags": r.tags or [], "quantity": r.quantity, "must_have": r.must_have}
            for r in session.query(InventoryItem).filter(
                _own(InventoryItem, user_id), InventoryItem.enabled.is_(True))]


def _templates(session: Session, user_id: str | None) -> list[dict]:
    rows = session.query(PackingTemplate).filter(_own(PackingTemplate, user_id)).all()
    lines: dict[str, list] = {}
    if rows:
        for li in session.query(PackingTemplateItem).filter(
                PackingTemplateItem.template_id.in_([r.id for r in rows])):
            lines.setdefault(li.template_id, []).append(
                {"inventory_item_id": li.inventory_item_id, "quantity": li.quantity})
    return [{"id": r.id, "name": r.name, "type_key": r.type_key, "keyword": r.keyword,
             "lines": lines.get(r.id, [])} for r in rows]


def _upsert_item(session: Session, plan: PackingPlan, trip_id: str, pi,
                 user_id: str | None = None) -> None:
    row = (session.query(PackingItem)
           .filter_by(trip_id=trip_id, user_id=user_id, item_key=pi.item_key).first())
    if row is not None and (row.status in ("packed", "removed") or row.edited):
        return                              # human decision — never clobbered
    if row is None:
        row = PackingItem(trip_id=trip_id, user_id=user_id, item_key=pi.item_key)
        session.add(row)
    row.plan_id = plan.id
    row.label = pi.label
    row.category = pi.category
    row.quantity = pi.quantity
    row.bag = pi.bag
    row.reason = pi.reason
    row.source = pi.source
    row.swap_for = pi.swap_for
    row.inventory_item_id = pi.inventory_item_id
    row.status = pi.status              # draft | suggested (5.0); omitted (4.x)
    row.updated_at = _now()


def _latest_manual_archetypes(session: Session, trip_id: str,
                              user_id: str | None = None) -> list | None:
    """The user's last explicit choice for this trip, to carry forward across a
    plain re-generate so it sticks (per-user, D-123). Since 5.0 it holds
    template ids (the column kept its name; SPEC-5 §6.7.3); a 4.x archetype
    id left in it matches none of the person's templates and drops out."""
    prev = (session.query(PackingPlan)
            .filter(PackingPlan.trip_id == trip_id, PackingPlan.user_id == user_id)
            .order_by(PackingPlan.generated_at.desc()).first())
    return (prev.manual_archetypes if prev else None) or None


def _latest_bag_ids(session: Session, trip_id: str, user_id: str | None) -> list | None:
    prev = (session.query(PackingPlan)
            .filter(PackingPlan.trip_id == trip_id, PackingPlan.user_id == user_id)
            .order_by(PackingPlan.generated_at.desc()).first())
    return (prev.bag_ids if prev else None) or None


def bag_infos(session: Session, user_id: str | None) -> list[bagfit.BagInfo]:
    """The person's own enabled bags, with capacity in litres (D-232)."""
    return [bagfit.BagInfo(id=b.id, label=b.label, kind=b.kind,
                           capacity=bagfit.capacity_l(b.capacity))
            for b in session.query(Bag).filter(_own(Bag, user_id), Bag.enabled.is_(True))
            .order_by(Bag.kind, Bag.label)]


def picked_bags(session: Session, plan: PackingPlan | None,
                user_id: str | None) -> list[bagfit.BagInfo]:
    """The plan's picked bags that are still the person's and enabled, in
    the order picked."""
    mine = {b.id: b for b in bag_infos(session, user_id)}
    return [mine[i] for i in ((plan.bag_ids if plan else None) or []) if i in mine]


def generic_bag(row: PackingItem, items: dict, one_bag: bool,
                inv: dict[str, InventoryItem]) -> str:
    """The engine's generic guess for a line (carry_on | checked), recomputed
    for a line already in a list: its own generic bag if it still has one,
    else its catalogue type (the owned item's, or a 4.x "clothing:jeans" key),
    else checked."""
    if one_bag:
        return "carry_on"
    if row.bag in ("carry_on", "checked"):
        return row.bag
    if row.bag == "backpack":
        return "carry_on"
    own = inv.get(row.inventory_item_id or "")
    key = own.type_key if own else (row.item_key or "").split(":", 1)[-1]
    return (items.get(key or "") or {}).get("bag", "checked")


def reassign_bags(session: Session, plan: PackingPlan, user_id: str | None,
                  traveler_id: int | None = None) -> None:
    """Put the list's lines in the plan's picked bags (SPEC-5 §6.10.4),
    without re-generating. Lines moved by hand, ticked off or removed keep
    their bag (D-63); with nothing picked, lines go back to the generic guess.
    The caller commits."""
    picked = picked_bags(session, plan, user_id)
    items = catalog.items(session)
    one_bag = load_profile(session, traveler_id,
                           owner=user_id is None).get("packing_style") == "one_bag"
    rows = (session.query(PackingItem)
            .filter(PackingItem.trip_id == plan.trip_id, PackingItem.user_id == user_id,
                    PackingItem.status == "draft", PackingItem.edited.is_(False)).all())
    inv = {r.id: r for r in session.query(InventoryItem).filter(_own(InventoryItem, user_id))}
    for row in rows:
        guess = generic_bag(row, items, one_bag, inv)
        row.bag = bagfit.assign(guess, picked) or guess


def fit_lines(session: Session, rows: list[PackingItem],
              user_id: str | None) -> list[bagfit.Line]:
    """The fit's view of a list's draft and packed lines (§6.10.2)."""
    items = catalog.items(session)
    defaults = catalog.volume_defaults()
    inv = {r.id: r for r in session.query(InventoryItem).filter(_own(InventoryItem, user_id))}
    out = []
    for row in rows:
        if row.status not in ("draft", "packed"):
            continue
        own = inv.get(row.inventory_item_id or "")
        out.append(bagfit.Line(
            id=row.id, category=row.category, quantity=row.quantity or 1, bag=row.bag,
            volume=bagfit.volume_for(own.type_key if own else None, row.category, items,
                                     defaults, own.volume_l if own else None)))
    return out


def _packing_places(session: Session, trip, user_id, derived, profiles,
                    home_countries) -> DerivedLocations:
    """The places a list packs for: the ones Discover researches for this
    person (the shared Refine places sheet, D-68). Home itself and anything
    they excluded are left out, so a layover, a wrong pin or the city you
    live in doesn't add its weather or customs."""
    excluded = {e.place_key for e in session.query(DiscoveryExclusion).filter(
        DiscoveryExclusion.trip_id == trip.id, DiscoveryExclusion.user_id == user_id)}
    keep = [p for p in derived.places
            if p.place_key not in excluded
            and not is_home_place(p, profiles, trip.start_date)]
    return DerivedLocations(places=keep, dropped=derived.dropped)


def generate_plan(session: Session, trip, *, resolve_airport, geocoder,
                  home_profiles: list = (), today: str | None = None,
                  manual_archetypes: list | None = None, bag_ids: list | None = None,
                  user_id: str | None = None, traveler_id: int | None = None) -> PackingPlan:
    """Generate and persist ONE person's packing list for `trip`, owned by
    `user_id` (D-123: per-user; NULL = auth off), from their own inventory and
    templates (SPEC-5 §6.7, D-231). Raises PackingProfileRequired until they've
    saved the packing questionnaire.

    `manual_archetypes` is the person's explicit set of template ids for this
    trip. Pass it to force a set; pass None (a plain re-generate) and the last
    choice is carried forward. An explicit empty list goes back to the
    templates the trip suggests. `bag_ids`, the bags picked for the trip
    (D-232), is carried forward the same way."""
    owner = user_id is None
    if not profile_ready(session, traveler_id, owner=owner):
        raise PackingProfileRequired()
    if manual_archetypes is None:
        manual_archetypes = _latest_manual_archetypes(session, trip.id, user_id)
    templates = _templates(session, user_id)
    mine = {t["id"] for t in templates}
    override = [t for t in (manual_archetypes or []) if t in mine] or None
    if bag_ids is None:
        bag_ids = _latest_bag_ids(session, trip.id, user_id)
    plan = PackingPlan(trip_id=trip.id, user_id=user_id, status="running",
                       generated_at=_now(), manual_archetypes=override)
    plan.bag_ids = [b.id for b in picked_bags(
        session, PackingPlan(bag_ids=bag_ids), user_id)] or None
    session.add(plan)
    session.flush()
    try:
        acts = session.query(TripActivity).filter(
            TripActivity.trip_id == trip.id).all()
        derived = derive_locations(acts, resolve_airport=resolve_airport,
                                   geocoder=geocoder)
        # Weather is the same for everyone on the trip, so use this person's own
        # weather findings, else any traveller's (4.1: Discover is per person) —
        # a companion shouldn't have to run Discover before packing.
        wq = session.query(DiscoveryFinding).filter(
            DiscoveryFinding.trip_id == trip.id, DiscoveryFinding.topic == "weather")
        weather = wq.filter(DiscoveryFinding.user_id == user_id).all() or wq.all()
        profiles = list(home_profiles or [])
        home_countries = home_country_set(derived.places, profiles, trip.start_date)
        derived = _packing_places(session, trip, user_id, derived, profiles,
                                  home_countries)
        ctx = derive_context(acts, derived, weather, start_date=trip.start_date,
                             end_date=trip.end_date,
                             profile=load_profile(session, traveler_id, owner=owner),
                             home_countries=home_countries,
                             trip_text=f"{trip.name or ''} {trip.notes or ''}")
        # Customs are a destination's: home's never make the list's notes.
        ctx.culture = culture.summarise([c for c in ctx.countries
                                         if c.upper() not in home_countries])
        result = assemble(ctx, catalog.items(session),
                          inventory=_inventory(session, user_id),
                          templates=templates, chosen=override)

        # What people see is their templates (D-230); the detected archetypes
        # and modifiers stay in the snapshot to explain the suggestions.
        plan.archetypes = result.templates
        plan.context = {**ctx.snapshot(), "archetypes": result.archetypes,
                        "modifiers": result.modifiers,
                        "template_types": result.suggested_types}
        plan.warnings = result.warnings
        plan.tasks = result.tasks

        # The picked bags take the lines (§6.10.4); none picked, the guess stays.
        picked = picked_bags(session, plan, user_id)
        for pi in result.lines:
            pi.bag = bagfit.assign(pi.bag, picked) or pi.bag

        produced: set[str] = set()
        for pi in result.lines + result.suggestions:
            _upsert_item(session, plan, trip.id, pi, user_id)
            produced.add(pi.item_key)
        # prune stale drafts / suggestions / 4.x omissions the latest run didn't
        # produce (item removed, template dropped, activity changed) — keep
        # packed/removed/edited. Scoped to THIS user's rows (D-123).
        for row in (session.query(PackingItem)
                    .filter(PackingItem.trip_id == trip.id,
                            PackingItem.user_id == user_id,
                            PackingItem.status.in_(("draft", "omitted", "suggested")),
                            PackingItem.edited.is_(False)).all()):
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
