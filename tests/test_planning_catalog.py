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
