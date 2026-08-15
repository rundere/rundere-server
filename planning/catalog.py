"""Load + validate the planning reference datasets (SPEC-3.1-planning.md §4).

The item catalog is the single source of truth; archetypes.json references it.
`validate()` is the integrity gate — every referenced item must exist and every
quantity formula must be known — run in a test so a bad edit to either file fails
CI rather than surfacing as a broken plan at runtime.
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


def items() -> dict:
    return _items_doc()["items"]


def item(item_id: str) -> dict | None:
    return items().get(item_id)


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


def label_for(item_id: str, variant: str = "neutral") -> str:
    """The display label for an item, honouring the clothing_variant (masc|fem|
    neutral) when the item declares variant alternates."""
    it = item(item_id) or {}
    variants = it.get("variants") or {}
    return variants.get(variant) or variants.get("neutral") or it.get("label", item_id)


def validate() -> list[str]:
    """Integrity problems across the datasets — empty list means clean."""
    problems: list[str] = []
    cats = set(categories())
    known = set(items())

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

    for iid, it in items().items():
        if it.get("category") not in cats:
            problems.append(f"item:{iid}: bad category '{it.get('category')}'")
        if not it.get("label"):
            problems.append(f"item:{iid}: missing label")
        if it.get("bag") not in ("carry_on", "checked"):
            problems.append(f"item:{iid}: bad bag '{it.get('bag')}'")

    return problems
