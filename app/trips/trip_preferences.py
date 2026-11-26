"""Trip preferences (5.1, D-251): one combined set of everyone's discovery
preferences on a trip, as D-96 always meant ("the resolver reads the union of
all travellers' profiles").

Who counts: the roster through `discovery_travelers()`, so travellers with a
login and the no-account travellers whose profile an admin manages (acting.py),
never placeholder guests (D-100).

What it is: constraints merged so the strictest wins, and nothing attributed.
"A traveller has a nut allergy", never whose. Free-text notes for agents are
never merged (they're one person's words); allergies are free text too, so they
are listed as written, without a name.

Who sees it: trip editors only (owner, traveller, admin; `scope.can_edit_trip`).
Viewers and strangers get None, and a viewer's own Discover run keeps using
only their own profile, so their findings can't reveal the set either.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import scope
from app.models import Trip

# Strictest last: the merge keeps the furthest entry in this order (D-251).
_MOBILITY_ORDER = ["no_limits", "moderate", "limited", "wheelchair"]


def _profiles(session: Session, trip_id: str) -> list[dict]:
    """Each counted traveller's profile values (the `self` fallback included,
    via `_discovery_profile_row`). Travellers with no profile add nothing."""
    from app.trips.routes import _discovery_profile_row, discovery_travelers
    out = []
    for t in discovery_travelers(session, trip_id):
        row = _discovery_profile_row(session, t)
        if row is not None and row.values:
            out.append(row.values)
    return out


def _union(values: list[list]) -> list:
    seen: list = []
    for vs in values:
        for v in vs or []:
            if v not in seen:
                seen.append(v)
    return seen


def combined_values(session: Session, trip_id: str) -> dict | None:
    """The merged constraints in DiscoveryProfile.values shape (only the fields
    the merge owns), or None when nobody counted has a profile, so callers
    behave exactly as before 5.1 (an empty roster, auth off)."""
    profiles = _profiles(session, trip_id)
    if not profiles:
        return None
    food = [p.get("food") or {} for p in profiles]
    # Tier 0 is "No limit", so the lowest REAL ceiling wins (D-251).
    budgets = [f.get("budget_ceiling") or 0 for f in food]
    budget = min((b for b in budgets if b), default=0)
    mobility = "no_limits"
    for p in profiles:
        m = (p.get("practical") or {}).get("mobility") or "no_limits"
        if m in _MOBILITY_ORDER and _MOBILITY_ORDER.index(m) > _MOBILITY_ORDER.index(mobility):
            mobility = m
    allergies = _union([[(f.get("allergies") or "").strip()] for f in food])
    return {
        "food": {
            "dietary_restrictions": _union([f.get("dietary_restrictions") for f in food]),
            "cuisine_dislikes": _union([f.get("cuisine_dislikes") for f in food]),
            "allergies": [a for a in allergies if a],
            "budget_ceiling": budget,
        },
        "drink": {"interests": _union([(p.get("drink") or {}).get("interests")
                                       for p in profiles])},
        "activity": {"interests": _union([(p.get("activity") or {}).get("interests")
                                          for p in profiles])},
        "practical": {"mobility": mobility},
    }


def for_discover(own: dict | None, combined: dict | None) -> dict | None:
    """The profile a Discover run uses (D-251): the caller's own, with the
    group's CONSTRAINTS laid over it (diet, cuisines to avoid, allergies,
    budget, mobility). Interests, liked cuisines, "prefer local" and notes stay
    the caller's own: they shape what to add, not what to skip."""
    if combined is None:
        return own
    v = {k: dict(s) if isinstance(s, dict) else s for k, s in (own or {}).items()}
    food = dict(v.get("food") or {})
    c = combined["food"]
    food["dietary_restrictions"] = c["dietary_restrictions"]
    food["cuisine_dislikes"] = c["cuisine_dislikes"]
    # the resolvers read allergies as one string (discover/resolvers/food.py)
    food["allergies"] = "; ".join(c["allergies"])
    food["budget_ceiling"] = c["budget_ceiling"]
    v["food"] = food
    v["practical"] = {**(v.get("practical") or {}),
                      "mobility": combined["practical"]["mobility"]}
    return v


def discover_profile(session: Session, trip: Trip, user, own: dict | None) -> dict | None:
    """What `start_discovery` hands the resolvers: the group's constraints for
    an editor, only their own profile for anyone else (a viewer). Auth off
    (`user` None) is the single-user mode: the owner edits everything."""
    if user is not None and not scope.can_edit_trip(session, user, trip):
        return own
    return for_discover(own, combined_values(session, trip.id))


def public(session: Session, trip: Trip, user) -> dict | None:
    """The combined set for an editor's API/MCP snapshot, labelled like
    `discovery_profile_public`; None for anyone who can't edit the trip."""
    if not scope.can_edit_trip(session, user, trip):
        return None
    c = combined_values(session, trip.id)
    if c is None:
        return None
    from discover import cuisines
    from app.trips.routes import (DISCOVERY_ACTIVITIES, DISCOVERY_BUDGET,
                                  DISCOVERY_DIETARY, DISCOVERY_DRINKS, DISCOVERY_MOBILITY)

    def tagged(keys, labels):
        labels = dict(labels)
        return [{"key": k, "label": labels.get(k, k)} for k in keys]

    budget = c["food"]["budget_ceiling"]
    mob = c["practical"]["mobility"]
    return {
        "food": {
            "dietary_restrictions": tagged(c["food"]["dietary_restrictions"], DISCOVERY_DIETARY),
            "avoid_cuisines": [cuisines.label(k) for k in c["food"]["cuisine_dislikes"]],
            "allergies": c["food"]["allergies"],
            "budget_ceiling": {"tier": budget,
                               "label": dict(DISCOVERY_BUDGET).get(budget, "No limit")},
        },
        "drink": {"interests": tagged(c["drink"]["interests"], DISCOVERY_DRINKS)},
        "activity": {"interests": tagged(c["activity"]["interests"], DISCOVERY_ACTIVITIES)},
        "practical": {"mobility": {"key": mob,
                                   "label": dict(DISCOVERY_MOBILITY).get(mob, mob)}},
        "how_to_apply": (
            "Plan for the whole group. These are everyone's constraints on the "
            "trip, combined, with nobody named: honour every dietary restriction, "
            "allergy and cuisine to avoid, stay within the budget ceiling (the "
            "lowest anyone set) and the mobility shown (the strictest anyone "
            "set). Interests are everyone's together; mix them across the days. "
            "Never guess or say which traveller a constraint belongs to."),
    }
