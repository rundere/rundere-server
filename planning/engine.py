"""The packing engine — passes 2–8 (SPEC-3.1-planning.md §2, §5.5).

Pure and deterministic: a PlanningContext + the item catalog in, a PlanResult out.
Every included item carries a `reason`; every left-behind item is an `omitted` row
with a reason (and a swap where one exists) — shown, never hidden. The quantity
pass is where the intelligence lives: pack `f(duration, laundry)`, and when that
can't cover the trip on its own, emit an in-trip laundry task rather than packing
one-of-everything-per-day (D-83).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil

from planning import catalog
from planning.context import PlanningContext

# Modest filtering / no-heels swaps: omit → swap_for.
_SWAPS = {"dress_shoes": "smart_flats", "tank_top": "modest_top",
          "shorts": "loose_trousers"}


def default_profile() -> dict:
    return {"packing_style": "balanced", "laundry_comfort": "none",
            "cultural_strictness": "strict", "clothing_variant": "neutral",
            "style_constraints": [], "must_have_items": [],
            "bag": {"mode": "carry_on", "weight_kg": 10}}


@dataclass
class PlanItem:
    item_id: str
    label: str
    category: str
    quantity: int
    bag: str
    reason: str
    source: str
    status: str = "draft"           # draft | omitted
    swap_for: str | None = None

    @property
    def item_key(self) -> str:
        return f"{self.category}:{self.item_id}"


@dataclass
class PlanResult:
    archetypes: list[dict] = field(default_factory=list)
    items: list[PlanItem] = field(default_factory=list)
    omitted: list[PlanItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- pass 2: score

def _climate_ok(archetype_id: str, bands: set[str]) -> bool:
    """Gate archetypes on climate so a warm trip never pulls in ski gear (the
    NM-in-October bug: cold_ski was selected for a 28°C trip, dumping a parka +
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
    one_bag = (prof.get("bag") or {}).get("mode") == "one_bag"

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


def _add(acc: dict, entry: dict, source: str, q: dict,
         trigger: str | None = None) -> None:
    """Merge one {item, qty|formula} entry into the accumulator, keeping the
    larger quantity when an item is contributed twice. The first contributor's
    source + trigger win (the trigger is the modifier name, e.g. "cold_weather",
    used to explain WHY an item is on the list)."""
    iid = entry["item"]
    qty = q[entry["formula"]] if "formula" in entry else int(entry.get("qty", 1))
    cur = acc.get(iid)
    if cur is None:
        acc[iid] = {"qty": qty, "source": source, "trigger": trigger}
    elif qty > cur["qty"]:
        cur["qty"] = qty                    # keep the first source/trigger


# ---------------------------------------------------------------- main

def all_archetypes() -> list[dict]:
    """[{id, label}] for the whole catalog — the picker's option list."""
    return [{"id": a["id"], "label": a["label"]} for a in catalog.archetypes()]


def _forced_archetypes(ids: list[str]) -> list[dict]:
    by_id = {a["id"]: a for a in catalog.archetypes()}
    return [{"id": i, "label": by_id[i]["label"], "score": None}
            for i in ids if i in by_id]


def build_plan(ctx: PlanningContext, items: dict, *,
               force_archetypes: list[str] | None = None) -> PlanResult:
    """Run passes 2–8 and return the plan. `items` is the active catalog
    (catalog.items(session)). `force_archetypes` overrides auto-detection with the
    operator's explicit choice (D-98-style confirmable guess)."""
    res = PlanResult()
    prof = ctx.profile or default_profile()
    variant = prof.get("clothing_variant", "neutral")
    constraints = set(prof.get("style_constraints") or [])
    strict = prof.get("cultural_strictness", "strict") == "strict"

    # pass 2 — archetypes (operator override wins over auto-detection)
    if force_archetypes:
        res.archetypes = _forced_archetypes(force_archetypes)
    else:
        res.archetypes = _score_archetypes(ctx)
    chosen = {a["id"] for a in res.archetypes}

    # pass 5 (compute first — assembly needs the formula values)
    q, tasks, qnotes = _quantities(ctx)
    res.tasks += tasks

    # pass 3 — base assembly (essentials + chosen archetypes' base items)
    acc: dict[str, dict] = {}
    for e in catalog.essentials():
        _add(acc, e, "essential", q)
    if ctx.international:
        for e in catalog.international_essentials():
            _add(acc, e, "essential", q)
    for a in catalog.archetypes():
        if a["id"] in chosen:
            for e in a["base_items"]:
                _add(acc, e, "template", q, trigger=a["id"])

    # pass 4 — modifiers (climate/activity/accommodation/transport)
    mods = catalog.modifiers()
    for name in _active_modifiers(ctx):
        for e in mods.get(name, []):
            _add(acc, e, "modifier", q, trigger=name)

    # pass 4b — cultural adds
    for iid in (ctx.culture or {}).get("add_items", []):
        _add(acc, {"item": iid, "qty": 1}, "cultural", q)

    # pass 6 — profile: must-haves, then constraint-driven omissions
    for must in prof.get("must_have_items") or []:
        if must in items:
            _add(acc, {"item": must, "qty": 1}, "must_have", q)

    modest = bool((ctx.culture or {}).get("modest")) and strict
    modest = modest or "modest_dress" in constraints
    no_heels = "no_heels" in constraints

    omit: dict[str, str] = {}                     # item_id -> reason
    for iid in list(acc):
        it = items.get(iid) or {}
        tags = set(it.get("tags") or [])
        if no_heels and "heel" in tags:
            omit[iid] = "you don't pack heels (profile)"
        elif modest and "revealing" in tags:
            omit[iid] = "modest dress code at your destination(s)"
    for iid, reason in omit.items():
        swap = _SWAPS.get(iid)
        if swap and swap in items:
            _add(acc, {"item": swap, "qty": acc[iid]["qty"]}, "cultural", q)
        res.omitted.append(_mk(iid, acc.pop(iid)["qty"], items, variant,
                               "omitted", reason=reason, swap=swap, source="omitted"))

    # pass 7 — bag assignment + weight
    one_bag = (prof.get("bag") or {}).get("mode") == "one_bag"
    total_g = 0
    for iid, info in acc.items():
        it = items.get(iid) or {}
        bag = "carry_on" if one_bag else it.get("bag", "checked")
        weight = (it.get("weight_g") or 0) * info["qty"]
        total_g += weight
        res.items.append(_mk(iid, info["qty"], items, variant, "draft",
                             bag=bag, source=info["source"],
                             reason=_reason(iid, info, ctx, q)))
    res.items.sort(key=lambda i: (i.category, i.label))

    # pass 8 — warnings
    if not ctx.has_weather:
        res.warnings.append("No weather yet — run Discovery for weather-aware "
                            "packing (layers, rain gear).")
    res.warnings += qnotes
    tol = (prof.get("bag") or {}).get("weight_kg")
    if tol and total_g / 1000 > tol:
        res.warnings.append(f"Estimated {total_g/1000:.1f} kg exceeds your "
                            f"{tol} kg limit — wear the bulkiest items on travel day.")
    if one_bag and len(res.items) > 28:
        res.warnings.append(f"{len(res.items)} items for one-bag travel — consider "
                            f"trimming 'just in case' extras.")
    for n in (ctx.culture or {}).get("notes", []):
        res.warnings.append(f"{n['country']}: {n['note']}")

    return res


def _mk(iid, qty, items, variant, status, *, bag=None, reason="", swap=None,
        source="") -> PlanItem:
    it = items.get(iid) or {}
    return PlanItem(item_id=iid, label=catalog.label_for(iid, variant) if it else iid,
                    category=it.get("category", "activity"), quantity=qty,
                    bag=bag or it.get("bag", "checked"), reason=reason,
                    source=source, status=status, swap_for=swap)


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
