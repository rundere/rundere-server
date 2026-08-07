"""A6 — collection resolution. Pure decision logic, no I/O.

Spec: docs/A6-collection-resolution.md. The caller (resolver service) loads the
item and the candidate collections from the DB, calls resolve_collection(), and
persists the returned Resolution fields. Vocabulary matches the canonical
schema's Resolution object exactly (collection_action / outcome enums).

MVP ladder (owner decisions 2026-08-03 — see D-24):

  1. capture_context.collection_id present -> user_selected. Skip the rest.
  2. Exact: existing collection whose date range CONTAINS the item's span.
     Exactly one -> matched_exact. Several -> needs_review, never guess.
  3. No match + flight -> mint (created), match_attempted=True.
  4. No match + anything else -> deferred, escalating to needs_review after
     DEFERRED_TTL_DAYS.

There is deliberately NO fuzzy rung and NO hint rung: near-misses are worth a
review tap, not a wrong guess; hint rules are future work (OQ-15).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

DEFERRED_TTL_DAYS = 2   # OQ-05 (owner): "if the collection doesn't exist yet,
                        # I messed up badly in my planning."

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


# ---------------------------------------------------------------- inputs

@dataclass(frozen=True)
class CollectionRef:
    """What the ladder needs to know about an existing collection."""
    id: str
    name: str
    start_date: str          # ISO date
    end_date: str            # ISO date
    human_renamed: bool = False


@dataclass(frozen=True)
class Resolution:
    """Field names/values mirror the schema's Resolution object."""
    collection_action: str                  # user_selected|matched_exact|created|none
    outcome: str                            # resolved|deferred|needs_review
    collection_id: str | None = None
    collection_name: str | None = None      # set on mint
    candidate_collections: tuple[str, ...] = ()
    match_attempted: bool = False
    deferred_until: str | None = None       # ISO date, set when outcome=deferred
    deferred_reason: str | None = None


def _d(v: str | date) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


# ---------------------------------------------------------------- naming (D-23)

def majority_month(start: str | date, end: str | date) -> tuple[int, int]:
    """(year, month) containing the midpoint of the span — equivalent to
    'the month where the majority of the trip happens' and tie-proof."""
    s, e = _d(start), _d(end)
    if e < s:
        raise ValueError("end before start")
    mid = s + (e - s) / 2
    return mid.year, mid.month


def collection_name(country: str, start: str | date, end: str | date) -> str:
    """D-23: '{Primary destination country} — {Month Year}'.
    Aug 29 -> Sep 15 is a September trip, not an August one."""
    if not country:
        raise ValueError("collection_name: country is required")
    y, m = majority_month(start, end)
    return f"{country} — {_MONTHS[m - 1]} {y}"


def primary_destination(legs: list[dict]) -> str | None:
    """Pick the trip's primary destination country from flight legs.

    Each leg: {"arrival_country", "arrival_time", "next_departure_time"|None}.
    The turnaround point — the arrival with the longest gap before the next
    departure — is where the traveller actually stays. The final leg's arrival
    (usually home) never wins unless it is the only leg.
    """
    if not legs:
        return None
    if len(legs) == 1:
        return legs[0].get("arrival_country")
    best, best_gap = None, timedelta(-1)
    for leg in legs:
        nxt = leg.get("next_departure_time")
        if not nxt:
            continue                      # final leg: journey ends, not a stay
        gap = datetime.fromisoformat(str(nxt)) - datetime.fromisoformat(
            str(leg["arrival_time"]))
        if gap > best_gap:
            best, best_gap = leg, gap
    return (best or legs[0]).get("arrival_country")


def recompute_name(coll: CollectionRef, country: str,
                   new_start: str | date, new_end: str | date) -> str | None:
    """Rename-on-range-extension (D-23). Returns the new name, or None when no
    rename should happen — including whenever the human has renamed: a human
    edit is locked and never overwritten (D-13 field_locks / RULE-15 spirit)."""
    if coll.human_renamed:
        return None
    fresh = collection_name(country, new_start, new_end)
    return fresh if fresh != coll.name else None


# ---------------------------------------------------------------- the ladder

def resolve_collection(
    item_type: str,
    item_start: str | date,
    item_end: str | date,
    collections: list[CollectionRef],
    capture_collection_id: str | None = None,
    today: str | date | None = None,
    mint_country: str | None = None,
    mint_start: str | date | None = None,
    mint_end: str | date | None = None,
    can_mint: bool = True,
) -> Resolution:
    """Run the MVP ladder for one item. Pure: pass `today` explicitly.

    mint_country: primary destination country of the minting flight (from
    primary_destination()); required only when a flight may mint.
    """
    s, e = _d(item_start), _d(item_end)
    if e < s:
        raise ValueError("item end before start")

    # 1 — human choice short-circuits everything (D-05, RULE-35b/35c)
    if capture_collection_id:
        return Resolution(collection_action="user_selected", outcome="resolved",
                          collection_id=capture_collection_id)

    # 2 — exact: collection date range contains the item span
    exact = [c for c in collections
             if _d(c.start_date) <= s and e <= _d(c.end_date)]
    if len(exact) == 1:
        # Carry the matched collection's NAME too, not just its id: the review
        # queue's write preview says "Files into existing trip …", and without
        # the name the operator cannot tell WHICH trip it matched (is it the
        # right one?). The name is right here in the CollectionRef.
        return Resolution(collection_action="matched_exact", outcome="resolved",
                          collection_id=exact[0].id,
                          collection_name=exact[0].name or None,
                          match_attempted=True)
    if len(exact) > 1:
        # never guess between overlapping trips (RULE-30 side: no mint either)
        return Resolution(collection_action="none", outcome="needs_review",
                          candidate_collections=tuple(c.id for c in exact),
                          match_attempted=True)

    # 3 — no match + flight -> mint, but ONLY for a closed home-to-home journey
    # (D-34). Flying away from home is half a trip: until a flight brings the
    # traveller back, the end date is unknown, and minting would invent one.
    if item_type == "flight":
        if not can_mint:
            ref = _d(today) if today else s
            return Resolution(
                collection_action="none", outcome="deferred", match_attempted=True,
                deferred_until=(ref + timedelta(days=DEFERRED_TTL_DAYS)).isoformat(),
                deferred_reason="no return flight home yet, so the trip has no "
                                "known end date")
        if not mint_country:
            return Resolution(collection_action="none", outcome="needs_review",
                              match_attempted=True,
                              deferred_reason="mint blocked: no destination country")
        span_start = _d(mint_start) if mint_start else s
        span_end = _d(mint_end) if mint_end else e
        return Resolution(collection_action="created", outcome="resolved",
                          collection_name=collection_name(mint_country,
                                                          span_start, span_end),
                          match_attempted=True)

    # 4 — no match + anything else -> deferred with a short fuse (OQ-05: 2 days)
    ref = _d(today) if today else s
    return Resolution(collection_action="none", outcome="deferred",
                      match_attempted=True,
                      deferred_until=(ref + timedelta(days=DEFERRED_TTL_DAYS))
                      .isoformat(),
                      deferred_reason="no collection contains the item's dates")


def escalate_deferred(res: Resolution, today: str | date) -> Resolution:
    """Deferred-item sweep (the one scheduled job, D-02): past deferred_until,
    a deferred item becomes needs_review so it cannot linger invisibly."""
    if res.outcome != "deferred" or not res.deferred_until:
        return res
    if _d(today) > _d(res.deferred_until):
        return Resolution(collection_action="none", outcome="needs_review",
                          match_attempted=res.match_attempted,
                          deferred_reason=res.deferred_reason)
    return res
