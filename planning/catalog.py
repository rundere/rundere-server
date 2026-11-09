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

# 5.0 (SPEC-5 §6.2–6.3, D-229): the questionnaire's answers, and the catalogue
# `seed` tags each one seeds a person's inventory from. Kept apart from the
# descriptive `tags` the engine matches needs on. An item with no `seed` is
# vocabulary only: it can be suggested ("not in your inventory") or added by
# hand, but no answer puts it in an inventory.
QUESTIONNAIRE = {
    "style": ["masc", "fem", "neutral"],
    "clothing": ["basics", "smart", "formal", "outdoor", "swim", "cold", "modest"],
    "footwear": ["minimal", "varied"],
    "care": ["toiletries", "skincare", "hair", "shaving", "makeup", "eyewear",
             "sun", "meds"],
    "gear": ["daypack", "backpack", "carry_on", "checked", "personal_item",
             "cubes", "compression", "toiletry_bag"],
    "tech": ["phone", "laptop", "tablet", "camera", "headphones", "watch",
             "adapter", "car"],
}
# Answers that are always on (the form shows them ticked and fixed).
ALWAYS = {"clothing": "basics", "care": "toiletries", "tech": "phone"}
# Q5 bags become the person's Bag rows, not inventory items (§6.3).
BAG_ANSWERS = {"daypack", "backpack", "carry_on", "checked", "personal_item"}
STYLES = ("masc", "fem", "neutral")


def seed_tags() -> set[str]:
    """Every `seed` tag an item may carry."""
    tags = {"essential"} | set(QUESTIONNAIRE["clothing"])
    for group, prefix in (("care", "care"), ("tech", "tech")):
        tags |= {f"{prefix}:{k}" for k in QUESTIONNAIRE[group]}
    tags |= {f"gear:{k}" for k in QUESTIONNAIRE["gear"] if k not in BAG_ANSWERS}
    return tags


def answer_tags(answers: dict) -> set[str]:
    """The seed tags a set of questionnaire answers selects (always-on included)."""
    tags = {"essential"}
    clothing = set(answers.get("clothing") or []) | {ALWAYS["clothing"]}
    care = set(answers.get("care") or []) | {ALWAYS["care"]}
    tech = set(answers.get("tech") or []) | {ALWAYS["tech"]}
    tags |= clothing
    tags |= {f"care:{k}" for k in care}
    tags |= {f"tech:{k}" for k in tech}
    tags |= {f"gear:{k}" for k in answers.get("gear") or [] if k not in BAG_ANSWERS}
    return tags


def seed_for(answers: dict, catalog: dict | None = None) -> dict[str, int]:
    """`{item_id: quantity}` the questionnaire seeds (SPEC-5 §6.3): every item
    whose `seed` tags meet the answers, kept to the chosen style; footwear
    under "minimal" only where the item is marked `minimal`."""
    catalog = catalog if catalog is not None else seed_items()
    style = answers.get("style") or "neutral"
    minimal = answers.get("footwear") == "minimal"
    want = answer_tags(answers)
    out: dict[str, int] = {}
    for iid, it in catalog.items():
        if not set(it.get("seed") or []) & want:
            continue
        # Generic / mixed is the union: it seeds every style's items.
        if style != "neutral" and style not in (it.get("styles") or STYLES):
            continue
        if minimal and it.get("category") == "footwear" and not it.get("minimal"):
            continue
        out[iid] = int(it.get("seed_qty") or 1)
    return out


def _load(name: str) -> dict:
    with open(_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _items_doc() -> dict:
    return _load("items.json")


@lru_cache(maxsize=1)
def _arch_doc() -> dict:
    return _load("archetypes.json")


@lru_cache(maxsize=1)
def _templates_doc() -> dict:
    return _load("templates.json")


def template_types() -> list[dict]:
    """The packing template types (SPEC-5 §6.5, D-230), in display order."""
    return _templates_doc()["types"]


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
        # 5.0 seed fields (SPEC-5 §6.3).
        for t in it.get("seed") or []:
            if t not in seed_tags():
                problems.append(f"item:{iid}: unknown seed tag '{t}'")
        styles = it.get("styles")
        if styles is not None and (not styles or not set(styles) <= set(STYLES)):
            problems.append(f"item:{iid}: bad styles {styles}")
        q = it.get("seed_qty", 1)
        if not isinstance(q, int) or isinstance(q, bool) or not 1 <= q <= 10:
            problems.append(f"item:{iid}: bad seed_qty {q!r}")
        if "minimal" in it and (it.get("category") != "footwear"
                                or not isinstance(it["minimal"], bool)):
            problems.append(f"item:{iid}: `minimal` is a footwear flag")
        for k in (it.get("variants") or {}):
            if k not in STYLES:
                problems.append(f"item:{iid}: unknown variant '{k}'")

    # Every seed tag an answer can select seeds at least one item.
    used = {t for it in seed_items().values() for t in it.get("seed") or []}
    for t in sorted(seed_tags() - used):
        problems.append(f"seed tag '{t}' seeds nothing")

    # Template types (SPEC-5 §6.5): known archetypes, modifiers, items and tags.
    arch_ids = {a["id"] for a in doc["archetypes"]}
    keys = set()
    for t in template_types():
        k = t.get("key")
        if not k or k in keys or not t.get("label"):
            problems.append(f"template:{k}: missing or duplicate key/label")
        keys.add(k)
        when = t.get("suggest_when") or {}
        for a in when.get("archetypes") or []:
            if a not in arch_ids:
                problems.append(f"template:{k}: unknown archetype '{a}'")
        for m in when.get("modifiers") or []:
            if m not in doc["modifiers"]:
                problems.append(f"template:{k}: unknown modifier '{m}'")
        pre = t.get("pretick") or {}
        for i in pre.get("items") or []:
            if i not in known:
                problems.append(f"template:{k}: unknown item '{i}'")
        for tag in pre.get("seed") or []:
            if tag not in seed_tags():
                problems.append(f"template:{k}: unknown seed tag '{tag}'")

    return problems
