"""Planning reference-dataset integrity (SPEC-3.1-planning.md §4).

The datasets are the "template database"; these tests are the gate that a bad
edit to items.json / archetypes.json fails CI rather than a runtime plan.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from planning import catalog

try:
    from app.db import SessionLocal, engine
    from app.models import Base, PackingCatalogItem
    Base.metadata.create_all(engine)
    DEPS = True
except ImportError:
    DEPS = False


def test_datasets_are_internally_consistent():
    problems = catalog.validate()
    assert problems == [], "dataset integrity problems:\n" + "\n".join(problems)


def test_seven_archetypes_present():
    ids = {a["id"] for a in catalog.archetypes()}
    assert ids == {"city_break", "beach", "cold_ski", "business",
                   "backpacking", "road_trip", "cultural"}


def test_every_archetype_declares_scoring_triggers():
    for a in catalog.archetypes():
        assert a["activities"] and a["accommodation"] and a["transport"]
        assert a["duration"]["min"] <= a["duration"]["max"]


def test_label_for_honours_clothing_variant():
    # swimwear declares masc/fem variants
    assert catalog.label_for("swimwear", "masc") == "Swim trunks"
    assert catalog.label_for("swimwear", "fem") == "Swimsuit"
    # an item with no variants falls back to its neutral label
    assert catalog.label_for("walking_shoes", "fem") == "Walking shoes"


def test_essentials_include_the_non_negotiables():
    ids = {e["item"] for e in catalog.essentials()}
    assert {"passport", "tickets", "prescription_meds"} <= ids


# ------------------------------------------------------------------ 5.0 seed catalogue
# SPEC-5 §6.2–6.5 (D-127, D-229, D-230): the catalogue is the vocabulary a
# person's inventory is seeded from; nobody packs from it directly.

# The ids 4.x shipped: archetypes, modifiers and owners' catalogue rows point at
# them, so they stay.
SHIPPED = {
    "smart_casual_top", "quick_dry_top", "tshirt", "tank_top", "modest_top",
    "linen_shirt", "jeans", "chinos", "quick_dry_bottoms", "shorts", "long_skirt",
    "loose_trousers", "sundress", "cardigan", "light_jacket", "wool_sweater",
    "waterproof_jacket", "compact_umbrella", "insulated_jacket",
    "thermal_base_layer", "ski_trousers", "blazer", "dress_shirt",
    "business_bottoms", "evening_outfit", "swimwear", "rash_guard", "underwear",
    "socks", "wool_socks", "sleepwear", "sun_hat", "beanie", "gloves",
    "modest_scarf", "headscarf", "walking_shoes", "dress_shoes", "smart_flats",
    "slip_on_loafers", "hiking_boots", "sandals", "winter_boots", "ski_boots",
    "toiletry_bag_tsa", "sunscreen", "after_sun", "moisturizer", "lip_balm",
    "anti_chafe", "laundry_kit", "phone_charger", "portable_battery",
    "universal_adapter", "laptop", "headphones", "headlamp", "car_charger",
    "passport", "tickets", "travel_insurance", "reservations", "business_cards",
    "prescription_meds", "pain_relief", "insect_repellent", "daypack",
    "water_bottle", "trail_snacks", "trekking_poles", "waterproof_pouch",
    "beach_towel", "sunglasses", "ski_goggles", "hand_warmers", "padlock",
    "cooler", "tent", "sleeping_bag",
}


def test_the_shipped_items_are_all_still_there():
    assert SHIPPED <= set(catalog.seed_items())


def test_the_catalogue_is_about_two_hundred_items():
    assert 180 <= len(catalog.seed_items()) <= 230


def test_ids_are_slugs_and_labels_are_unique():
    import re
    seen = {}
    for iid, it in catalog.seed_items().items():
        assert re.fullmatch(r"[a-z][a-z0-9_]{1,40}", iid), iid
        key = (it["category"], it["label"].lower())
        assert key not in seen, f"{iid} repeats {seen.get(key)}"
        seen[key] = iid


def test_every_category_has_items():
    used = {it["category"] for it in catalog.seed_items().values()}
    assert used == set(catalog.categories())


def test_every_answer_seeds_something_and_bags_do_not():
    for group in ("clothing", "care", "tech"):
        for k in catalog.QUESTIONNAIRE[group]:
            assert catalog.seed_for({group: [k]}), (group, k)
    for k in catalog.QUESTIONNAIRE["gear"]:
        if k in catalog.BAG_ANSWERS:
            assert f"gear:{k}" not in catalog.seed_tags()   # Bag rows instead
        else:
            assert catalog.seed_for({"gear": [k]}).keys() - catalog.seed_for({}).keys()


def test_the_always_on_answers_seed_the_basics():
    seeded = catalog.seed_for({})
    assert {"passport", "tshirt", "underwear", "socks", "phone_charger",
            "walking_shoes"} <= seeded.keys()
    assert seeded["underwear"] == 7 and seeded["socks"] == 7
    assert "swimwear" not in seeded and "blazer" not in seeded


def test_the_clothing_style_filters_but_never_asks_gender():
    masc = catalog.seed_for({"style": "masc", "clothing": ["formal"]})
    fem = catalog.seed_for({"style": "fem", "clothing": ["formal"]})
    mixed = catalog.seed_for({"style": "neutral", "clothing": ["formal"]})
    assert "smart_flats" not in masc and "smart_flats" in fem
    assert masc.keys() | fem.keys() <= mixed.keys()      # Generic / mixed = the union
    assert set(catalog.QUESTIONNAIRE["style"]) == {"masc", "fem", "neutral"}


def test_minimal_footwear_keeps_only_the_do_everything_pairs():
    answers = {"clothing": ["outdoor", "swim", "formal", "cold"]}
    varied = catalog.seed_for({**answers, "footwear": "varied"})
    minimal = catalog.seed_for({**answers, "footwear": "minimal"})
    shoes = lambda d: {i for i in d if catalog.seed_items()[i]["category"] == "footwear"}
    assert {"hiking_boots", "dress_shoes", "winter_boots"} <= shoes(varied)
    assert shoes(minimal) == {i for i in shoes(varied) if catalog.seed_items()[i].get("minimal")}
    assert {"walking_shoes", "sandals"} <= shoes(minimal)


def test_template_types_match_the_spec():
    keys = [t["key"] for t in catalog.template_types()]
    assert len(keys) == 21 and keys[-1] == "custom"
    assert {"beach", "hiking", "swimming", "fine_dining", "formal_events",
            "cultural_events", "business", "road_trip", "long_flight",
            "sightseeing", "religious_sites"} <= set(keys)


# ------------------------------------------------------------------ DB catalog

def test_seed_populates_db_and_is_idempotent():
    if not DEPS:
        return
    with SessionLocal() as s:
        s.query(PackingCatalogItem).delete()
        s.commit()
        catalog.seed_catalog(s)
        n1 = s.query(PackingCatalogItem).count()
        assert n1 == len(catalog.seed_items())
        catalog.seed_catalog(s)                     # idempotent — no duplicates
        assert s.query(PackingCatalogItem).count() == n1
        # the engine reads the catalog from the DB when given a session
        loaded = catalog.items(s)
        assert loaded["quick_dry_top"]["category"] == "clothing"


def test_disabled_item_drops_out_of_the_active_catalog():
    if not DEPS:
        return
    with SessionLocal() as s:
        s.query(PackingCatalogItem).delete()
        catalog.seed_catalog(s)
        row = s.get(PackingCatalogItem, "jeans")
        row.enabled = False
        s.commit()
        assert "jeans" not in catalog.items(s)      # retired, not deleted


def test_owner_edit_survives_reseed():
    if not DEPS:
        return
    with SessionLocal() as s:
        s.query(PackingCatalogItem).delete()
        catalog.seed_catalog(s)
        row = s.get(PackingCatalogItem, "jeans")
        row.label = "Owner-renamed jeans"
        s.commit()
    with SessionLocal() as s:
        catalog.seed_catalog(s)                     # a later startup re-seed
        assert s.get(PackingCatalogItem, "jeans").label == "Owner-renamed jeans"


def test_new_catalog_item_auto_slugs_id_from_label():
    # Regression: SQLAdmin drops the PK from the create form, so a new item was
    # inserted with id=NULL -> NotNullViolation. id now auto-derives from label.
    if not DEPS:
        return
    with SessionLocal() as s:
        s.query(PackingCatalogItem).delete()
        s.add(PackingCatalogItem(label="Car Phone Mount", category="tech"))
        s.commit()
        rows = s.query(PackingCatalogItem).all()
        assert len(rows) == 1 and rows[0].id == "car_phone_mount"
        # an explicit id (the seed path) is still honoured
        s.add(PackingCatalogItem(id="quick_dry_top", label="Quick-dry top",
                                 category="clothing"))
        s.commit()
        assert s.get(PackingCatalogItem, "quick_dry_top") is not None
    with SessionLocal() as s:                       # restore the shared catalog
        s.query(PackingCatalogItem).delete()
        catalog.seed_catalog(s)
