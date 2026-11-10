"""The packing engine's passes (SPEC-3.1-planning.md §2, §5.5).

Pure and deterministic. Since 5.0 (SPEC-5 §6.7, D-230) the list itself is
built by planning/assemble.py from the person's inventory; this module keeps
what it builds on: archetype scoring (it now only suggests template types),
the active modifiers, the quantity formula, and the reasons a line carries.
The quantity pass is where the intelligence lives: pack `f(duration,
laundry)`, and when that can't cover the trip on its own, emit an in-trip
laundry task rather than packing one-of-everything-per-day (D-83).
"""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from planning import catalog
from planning.context import PlanningContext

def default_profile() -> dict:
    # 5.0 (SPEC-5 §6.4): the 4.x keys (cultural strictness, clothing variant,
    # style constraints, must-haves, bag mode and weight) are no longer read.
    return {"packing_style": "balanced", "laundry_comfort": "none"}


@dataclass
class PlanItem:
    item_id: str
    label: str
    category: str
    quantity: int
    bag: str
    reason: str
    source: str
    status: str = "draft"           # draft | omitted | suggested (5.0)
    swap_for: str | None = None
    inventory_item_id: str | None = None      # 5.0: the owned item this line is

    @property
    def item_key(self) -> str:
        # 5.0 lines are keyed by what they are: an owned item, or a catalogue
        # need the person doesn't own (SPEC-5 §6.7), so a re-generate finds them.
        if self.inventory_item_id:
            return f"inv:{self.inventory_item_id}"
        if self.status == "suggested":
            return f"suggest:{self.item_id}"
        return f"{self.category}:{self.item_id}"


# ---------------------------------------------------------------- pass 2: score

def _climate_ok(archetype_id: str, bands: set[str]) -> bool:
    """Gate archetypes on climate so a warm trip never pulls in ski gear (the
    Arizona-in-October bug: cold_ski was selected for a 28°C trip, dumping a parka +
    beanie + winter boots alongside the beach items). cold_ski needs a genuinely
    cold band; beach is dropped when it's cold. Everything else is climate-neutral."""
    if archetype_id == "cold_ski":
        return "cold_weather" in bands
    if archetype_id == "beach":
        return "cold_weather" not in bands
    return True


def _score_archetypes(ctx: PlanningContext) -> list[dict]:
    scored = []
    for a in catalog.archetypes():
        if not _climate_ok(a["id"], ctx.climate_bands):
            continue                              # climate-incompatible → never chosen
        s = 0
        s += 2 * len(ctx.activity_tags & set(a["activities"]))
        if ctx.accommodation & set(a["accommodation"]):
            s += 1
        if ctx.transport & set(a["transport"]):
            s += 1
        if a["duration"]["min"] <= ctx.duration_days <= a["duration"]["max"]:
            s += 1
        scored.append({"id": a["id"], "label": a["label"], "score": s})
    scored.sort(key=lambda x: x["score"], reverse=True)
    top = [a for a in scored if a["score"] > 0][:2]
    if not top:                                   # nothing matched → sensible default
        top = [next(a for a in catalog.archetypes() if a["id"] == "city_break")]
        top = [{"id": "city_break", "label": top[0]["label"], "score": 0}]
    return top


# ---------------------------------------------------------------- pass 5: quantities

def _quantities(ctx: PlanningContext) -> tuple[dict, list[dict], list[str]]:
    """Formula values + any in-trip laundry task + notes. The laundry intelligence
    (§5.5): with laundry you pack ~a week and wash; without, you pack the trip but
    a very long trip forces a laundry warning."""
    d = ctx.duration_days
    prof = ctx.profile
    launder = prof.get("laundry_comfort", "none") in ("hand_wash", "machine")
    style = prof.get("packing_style", "balanced")
    # 5.0: one bag is a packing style (SPEC-5 §6.4); the 4.x bag mode still reads.
    one_bag = style == "one_bag" or (prof.get("bag") or {}).get("mode") == "one_bag"

    cover = min(d, 6) if launder else min(d, 14)
    q = {
        "tops": min(cover, 6) + 1,
        "bottoms": max(1, min(ceil(cover / 3), 4)),
        "underwear": cover + 2,
        "socks": cover + 1,
        "sleepwear": max(1, min(ceil(d / 5), 3)),
    }
    if style == "minimalist":
        q["tops"] = max(2, q["tops"] - 1)
        q["bottoms"] = max(1, q["bottoms"] - 1)
    elif style == "prepared":
        q["tops"] += 1
    if one_bag:
        q["tops"] = min(q["tops"], 5)

    tasks, notes = [], []
    if launder and d > cover:
        tasks.append({"do": "laundry", "every_days": cover, "around_day": cover,
                      "note": f"{q['tops']} tops packed for {d} days — wash roughly "
                              f"every {cover} days."})
    elif not launder and d > 10:
        notes.append(f"{d} days with no laundry means bulky bags — enabling "
                     f"laundry would cut clothing to about a week's worth.")
    return q, tasks, notes


# ---------------------------------------------------------------- assembly helpers

def _active_modifiers(ctx: PlanningContext) -> list[str]:
    mods: list[str] = []
    mods += [b for b in ("cold_weather", "layering", "lightweight", "rain_gear",
                         "humid") if b in ctx.climate_bands]
    for tag in ("business", "hiking", "beach", "temple", "ski", "watersport"):
        if tag in ctx.activity_tags:
            mods.append(tag)
    if "campground" in ctx.accommodation:
        mods.append("camping")
    if ctx.accommodation & {"hostel"}:
        mods.append("hostel")
    if "car" in ctx.transport:
        mods.append("road")
    return mods


# Why a modifier fired — shown as the item's subtext so a surprising item
# (an insulated jacket, ski gear) explains itself and a mis-trigger is visible.
_MOD_REASONS = {
    "cold_weather": "cold days expected",
    "layering": "cool/variable temps — layers",
    "lightweight": "warm weather",
    "rain_gear": "rain in the forecast",
    "humid": "humid climate",
    "business": "for your business plans",
    "hiking": "hiking on this trip",
    "beach": "beach days",
    "temple": "temple / religious sites",
    "ski": "skiing",
    "watersport": "water sports",
    "camping": "camping",
    "hostel": "hostel stay",
    "road": "road trip",
}


# Why an archetype's base item is on the list — its subtext when nothing more
# specific (laundry math) applies.
_ARCHETYPE_REASONS = {
    "beach": "for a beach trip",
    "cold_ski": "for cold-weather / ski",
    "business": "for business",
    "backpacking": "for backpacking",
    "road_trip": "for a road trip",
    "cultural": "for cultural sites",
    "city_break": "for a city trip",
}


def _reason(iid: str, info: dict, ctx: PlanningContext, q: dict) -> str:
    src = info["source"]
    if src == "essential":
        return "travel essential"
    if src == "cultural":
        return "local dress / custom"
    if src == "must_have":
        return "your must-have"
    if src == "modifier":
        return _MOD_REASONS.get(info.get("trigger"), "trip conditions")
    # template item from an archetype's base kit → explain the laundry math for
    # formula-quantified tops, else name the archetype that pulled it in.
    launder = (ctx.profile.get("laundry_comfort") in ("hand_wash", "machine"))
    if info["qty"] == q.get("tops") and launder and ctx.duration_days > info["qty"]:
        return f"{info['qty']} for {ctx.duration_days} days with laundry mid-trip"
    return _ARCHETYPE_REASONS.get(info.get("trigger"),
                                  f"for your {ctx.duration_days}-day trip")
