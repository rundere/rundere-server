"""Planning reference data (SPEC-3.1-planning.md §4).

Two tiers, deliberately different:

  * ITEM CATALOG — owner-editable DATA. The JSON here is the shipped DEFAULT that
    seeds `packing_catalog_items` at startup (D-84); at runtime the engine reads
    the catalog from the DB (`items(session)`), so the owner manages items in the
    admin UI, not by editing JSON.
  * ARCHETYPES / MODIFIERS / ESSENTIALS — structural logic, still JSON (versioned
    with the code, like a rules registry). They reference catalog item ids.

`validate()` is the integrity gate over the JSON seed — every referenced item
exists, every quantity formula is known — run in a test so a bad edit fails CI.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent / "data"

# Quantity formulas the engine knows how to compute (duration/laundry/style aware).
FORMULAS = {"tops", "bottoms", "underwear", "socks", "sleepwear"}


def _load(name: str) -> dict:
    with open(_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _items_doc() -> dict:
    return _load("items.json")


@lru_cache(maxsize=1)
def _arch_doc() -> dict:
    return _load("archetypes.json")


def seed_items() -> dict:
    """The shipped default catalog (from items.json) used to seed the DB and as
    the validation reference."""
    return _items_doc()["items"]


def items(session=None) -> dict:
    """The active item catalog as `{id: {label, category, bag, weight_g, tags,
    variants}}`. From the DB (owner-editable, enabled only) when a session is
    given; falls back to the JSON seed otherwise (tests / pre-seed)."""
    if session is None:
        return seed_items()
    from app.models import PackingCatalogItem
    rows = session.query(PackingCatalogItem).filter(
        PackingCatalogItem.enabled.is_(True)).all()
    if not rows:
        return seed_items()
    return {r.id: {"label": r.label, "category": r.category, "bag": r.bag,
                   "weight_g": r.weight_g or 0, "tags": r.tags or [],
                   "variants": r.variants or {}} for r in rows}


def item(item_id: str, session=None) -> dict | None:
    return items(session).get(item_id)


def categories() -> list[str]:
    return _items_doc()["_meta"]["categories"]


def archetypes() -> list[dict]:
    return _arch_doc()["archetypes"]


def modifiers() -> dict:
    return _arch_doc()["modifiers"]


def essentials() -> list[dict]:
    return _arch_doc()["essentials"]


def international_essentials() -> list[dict]:
    return _arch_doc()["international_essentials"]


def label_for(item_id: str, variant: str = "neutral", session=None) -> str:
    """The display label for an item, honouring the clothing_variant (masc|fem|
    neutral) when the item declares variant alternates."""
    it = item(item_id, session) or {}
    variants = it.get("variants") or {}
    return variants.get(variant) or variants.get("neutral") or it.get("label", item_id)


def seed_catalog(session) -> None:
    """Insert any catalog item missing from the DB (JSON = defaults, DB = truth) —
    same idempotent seed contract as seed_rule_settings. Owner edits and deletions
    are never overwritten; only genuinely-new item ids are added."""
    from app.models import PackingCatalogItem
    have = {r.id for r in session.query(PackingCatalogItem.id).all()}
    for iid, it in seed_items().items():
        if iid in have:
            continue
        session.add(PackingCatalogItem(
            id=iid, label=it.get("label", iid), category=it["category"],
            bag=it.get("bag", "carry_on"), weight_g=it.get("weight_g", 0),
            tags=it.get("tags", []), variants=it.get("variants", {})))
    session.commit()


def validate() -> list[str]:
    """Integrity problems across the JSON datasets — empty list means clean."""
    problems: list[str] = []
    cats = set(categories())
    known = set(seed_items())

    def check(entry: dict, where: str) -> None:
        iid = entry.get("item")
        if iid not in known:
            problems.append(f"{where}: unknown item '{iid}'")
        formula = entry.get("formula")
        if formula is not None and formula not in FORMULAS:
            problems.append(f"{where}: unknown formula '{formula}'")

    doc = _arch_doc()
    for e in doc["essentials"]:
        check(e, "essentials")
    for e in doc["international_essentials"]:
        check(e, "international_essentials")
    for name, lst in doc["modifiers"].items():
        for e in lst:
            check(e, f"modifier:{name}")
    for a in doc["archetypes"]:
        if not (a.get("id") and a.get("base_items") is not None):
            problems.append(f"archetype:{a.get('id')}: missing id/base_items")
        for e in a.get("base_items", []):
            check(e, f"archetype:{a.get('id')}")

    for iid, it in seed_items().items():
        if it.get("category") not in cats:
            problems.append(f"item:{iid}: bad category '{it.get('category')}'")
        if not it.get("label"):
            problems.append(f"item:{iid}: missing label")
        if it.get("bag") not in ("carry_on", "checked"):
            problems.append(f"item:{iid}: bad bag '{it.get('bag')}'")

    return problems
