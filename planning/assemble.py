"""The 5.0 list builder (SPEC-5 §6.7, D-230, D-231).

Pure and deterministic, like the engine it sits on: the trip's context, the
catalogue, and ONE person's inventory and templates in; their packing lines out.
A list draws only from the person's inventory (D-231):

- templates the trip suggests (or the person picked) add all their items;
- the engine's needs (essentials, active modifiers, the destination's cultural
  adds) resolve to an owned item, or become a "Suggested — not in your
  inventory" line, shown and never added silently (D-83);
- every must-have is added;
- one line per owned item: the largest quantity, the reasons merged, capped by
  how many the person owns, with a warning when that's short.

Archetype and modifier detection stays (engine.py), but only to suggest
template types (D-230). The modest / strict filtering went with cultural
strictness (D-229), so nothing is omitted here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from planning import catalog
from planning.context import PlanningContext
from planning.engine import (PlanItem, _active_modifiers, _quantities, _reason,
                             _score_archetypes)


@dataclass
class Assembly:
    lines: list[PlanItem] = field(default_factory=list)          # owned, draft
    suggestions: list[PlanItem] = field(default_factory=list)    # not owned
    templates: list[dict] = field(default_factory=list)          # applied: {id, label, suggested}
    archetypes: list[dict] = field(default_factory=list)         # detected, for the snapshot
    modifiers: list[str] = field(default_factory=list)
    suggested_types: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)


def variant_for(profile: dict) -> str:
    """The clothing style answered in the questionnaire (Q1, D-229) names the
    catalogue variant for suggestion labels."""
    style = ((profile or {}).get("questionnaire") or {}).get("style")
    return style if style in ("masc", "fem") else "neutral"


def _formulas() -> dict[str, str]:
    """item id → the quantity formula the engine uses for it anywhere, so a
    template line without its own quantity still packs a trip's worth of
    socks rather than one pair."""
    out: dict[str, str] = {}
    entries = list(catalog.essentials()) + list(catalog.international_essentials())
    for a in catalog.archetypes():
        entries += a.get("base_items") or []
    for es in catalog.modifiers().values():
        entries += es
    for e in entries:
        if "formula" in e:
            out.setdefault(e["item"], e["formula"])
    return out


def suggested_types(ctx: PlanningContext, archetype_ids: set[str],
                    modifiers: set[str]) -> set[str]:
    """Template types the trip's signals suggest (templates.json suggest_when).
    `keyword` is matched per template, not per type."""
    out = set()
    for t in catalog.template_types():
        w = t.get("suggest_when") or {}
        if (archetype_ids & set(w.get("archetypes") or [])
                or modifiers & set(w.get("modifiers") or [])
                or (w.get("culture_modest") and (ctx.culture or {}).get("modest"))
                or (w.get("long_flight") and ctx.long_flight)):
            out.add(t["key"])
    return out


def _resolve(need_id: str, items: dict, inventory: list[dict]) -> dict | None:
    """The person's own item for one of the engine's needs: by type_key, then by
    name (the catalogue label or a variant of it). Not by tag: catalogue tags
    describe rather than identify (socks and underwear are both
    "launderable", a beanie and gloves both "warm"), so a tag match would pack
    the wrong thing (SPEC-5 §6.7 amended)."""
    for row in inventory:
        if row["type_key"] == need_id:
            return row
    it = items.get(need_id) or {}
    names = {n.lower() for n in [it.get("label"), *(it.get("variants") or {}).values()] if n}
    for row in inventory:
        if row["label"].strip().lower() in names:
            return row
    return None


def _fits_style(it: dict, variant: str) -> bool:
    """A suggestion follows the clothing style answered in the questionnaire,
    as seeding does (D-229): an item limited to other styles isn't offered.
    Generic / mixed sees everything."""
    styles = it.get("styles") or []
    return variant == "neutral" or not styles or variant in styles


def assemble(ctx: PlanningContext, items: dict, *, inventory: list[dict],
             templates: list[dict], chosen: list[str] | None = None) -> Assembly:
    """Build one person's list. `inventory` rows are dicts with id, type_key,
    label, category, tags, quantity, must_have; `templates` have id, name,
    type_key, keyword and lines [{inventory_item_id, quantity}]. `chosen` is the
    person's explicit set of template ids for this trip (None = what the trip
    suggests)."""
    res = Assembly()
    prof = ctx.profile or {}
    variant = variant_for(prof)
    res.archetypes = _score_archetypes(ctx)
    res.modifiers = _active_modifiers(ctx)
    types = suggested_types(ctx, {a["id"] for a in res.archetypes}, set(res.modifiers))
    res.suggested_types = sorted(types)
    q, res.tasks, notes = _quantities(ctx)
    formulas = _formulas()
    owned = {r["id"]: r for r in inventory}

    # Which templates apply: the person's explicit set, else the suggested ones.
    def suggested(t: dict) -> bool:
        if t["type_key"] == "custom":
            kw = (t.get("keyword") or "").strip().lower()
            return bool(kw) and kw in ctx.text
        return t["type_key"] in types

    if chosen is not None:
        picked = [t for t in templates if t["id"] in set(chosen)]
    else:
        picked = [t for t in templates if suggested(t)]
    res.templates = [{"id": t["id"], "label": t["name"], "suggested": suggested(t)}
                     for t in picked]

    acc: dict[str, dict] = {}          # inventory id -> {qty, reasons, wanted}

    def add(row: dict, qty: int, reason: str) -> None:
        cur = acc.setdefault(row["id"], {"qty": 0, "reasons": []})
        cur["qty"] = max(cur["qty"], qty)
        if reason and reason not in cur["reasons"]:
            cur["reasons"].append(reason)

    def default_qty(row: dict) -> int:
        f = formulas.get(row["type_key"] or "")
        return q[f] if f else int(row.get("quantity") or 1)

    # Templates add all their items (§6.7.3).
    for t in picked:
        for line in t.get("lines") or []:
            row = owned.get(line["inventory_item_id"])
            if row is not None:
                add(row, line.get("quantity") or default_qty(row),
                    f"from your {t['name']} template")

    # Needs resolve to owned items, else they're suggestions (§6.7.4).
    needs: dict[str, dict] = {}
    for e in list(catalog.essentials()) + (
            list(catalog.international_essentials()) if ctx.international else []):
        needs.setdefault(e["item"], {"entry": e, "source": "essential", "trigger": None})
    mods = catalog.modifiers()
    for name in res.modifiers:
        for e in mods.get(name, []):
            needs.setdefault(e["item"], {"entry": e, "source": "modifier", "trigger": name})
    for iid in (ctx.culture or {}).get("add_items", []):
        needs.setdefault(iid, {"entry": {"item": iid, "qty": 1}, "source": "cultural",
                               "trigger": None})
    for iid, n in needs.items():
        e = n["entry"]
        qty = q[e["formula"]] if "formula" in e else int(e.get("qty", 1))
        why = _reason(iid, {"source": n["source"], "trigger": n["trigger"], "qty": qty},
                      ctx, q)
        row = _resolve(iid, items, inventory)
        if row is not None:
            add(row, qty, why)
        elif iid in items and _fits_style(items[iid], variant):
            it = items[iid]
            res.suggestions.append(PlanItem(
                item_id=iid, label=catalog.label_for(iid, variant) if it else iid,
                category=it.get("category", "activity"), quantity=qty,
                bag=it.get("bag", "checked"), reason=why, source="suggested",
                status="suggested"))

    # Must-haves go on every list (§6.7.5).
    for row in inventory:
        if row.get("must_have"):
            add(row, default_qty(row) if row["type_key"] in formulas else 1,
                "your must-have")

    # Quantities are capped by what the person owns (§6.7.7).
    one_bag = prof.get("packing_style") == "one_bag"
    for iid, info in acc.items():
        row = owned[iid]
        have = max(1, int(row.get("quantity") or 1))
        qty = info["qty"]
        if qty > have:
            res.warnings.append(f"{qty} × {row['label']} needed, you have {have}.")
            qty = have
        it = items.get(row["type_key"] or "") or {}
        res.lines.append(PlanItem(
            item_id=iid, label=row["label"], category=row["category"], quantity=qty,
            bag="carry_on" if one_bag else it.get("bag", "checked"),
            reason="; ".join(info["reasons"]), source="inventory",
            inventory_item_id=iid))
    res.lines.sort(key=lambda i: (i.category, i.label.lower()))
    res.suggestions.sort(key=lambda i: (i.category, i.label.lower()))

    if not ctx.has_weather:
        res.warnings.insert(0, "No weather yet — run Discovery for weather-aware "
                               "packing (layers, rain gear).")
    res.warnings += notes
    if one_bag and len(res.lines) > 28:
        res.warnings.append(f"{len(res.lines)} items for one-bag travel — consider "
                            f"trimming 'just in case' extras.")
    for n in (ctx.culture or {}).get("notes", []):
        res.warnings.append(f"{n['country']}: {n['note']}")
    return res
