"""Packing engine tests (SPEC-3.1-planning.md §2/§5.5, SPEC-5 §6.7) — offline,
hand-built context against the JSON seed catalog. Covers the archetype scoring
that suggests templates, the climate gates, the laundry intelligence, and the
reasons each line carries.

5.0 lists draw only from the person's inventory, so these run against someone
who owns plenty of everything in the catalogue: every need resolves to an
owned line. tests/test_packing_engine_5.py covers what happens when they don't.
4.x's modest-dress omissions, no-heels swaps, forced archetypes and carry-on
weight limit went with the profile fields that drove them (D-229, SPEC-5 §6.4).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from planning import catalog
from planning.assemble import assemble
from planning.context import PlanningContext
from planning.engine import _score_archetypes, default_profile

_ITEMS = catalog.seed_items()
# Owns plenty of everything, so quantities are the formula's, never the cap.
_ALL = [{"id": iid, "type_key": iid, "label": it["label"], "category": it["category"],
         "tags": it.get("tags", []), "quantity": 99, "must_have": False}
        for iid, it in _ITEMS.items()]


def _ctx(days, **kw):
    base = dict(duration_days=days, activity_tags={"sightseeing"},
                climate_bands={"lightweight"}, temp_high_c=24, has_weather=True,
                profile=default_profile(), culture={})
    base.update(kw)
    return PlanningContext(**base)


def _plan(ctx, inventory=None):
    return assemble(ctx, _ITEMS, inventory=_ALL if inventory is None else inventory,
                    templates=[])


def _qty(res, iid):
    return next((i.quantity for i in res.lines if i.inventory_item_id == iid), None)


def _reason(res, iid):
    return next((i.reason for i in res.lines if i.inventory_item_id == iid), None)


# ---------------------------------------------------------- reasons / climate

def test_modifier_items_explain_their_trigger():
    # A cold-weather trip: the insulated jacket says WHY it's on the list.
    res = _plan(_ctx(5, climate_bands={"cold_weather"}))
    assert _qty(res, "insulated_jacket") is not None
    assert _reason(res, "insulated_jacket") == "cold days expected"


def test_every_line_has_a_nonempty_reason():
    res = _plan(_ctx(6, activity_tags={"hiking"}, climate_bands={"layering"}))
    assert res.lines and all((i.reason or "").strip() for i in res.lines)


def test_cold_nights_mild_days_is_layering_not_a_parka():
    # Arizona in October: ~20°C days, ~2°C nights. Layering, not cold_weather.
    from planning.context import _climate_bands
    assert _climate_bands(20, 2, 0) == {"layering"}
    assert _climate_bands(8, -1, 0) == {"cold_weather"}      # genuinely cold days
    assert _climate_bands(26, 15, 0) == {"lightweight"}


def test_cold_ski_not_suggested_for_a_warm_trip():
    # The Arizona bug: cold_ski was chosen at 28°C, dumping a parka into a beach plan.
    res = _plan(_ctx(10, climate_bands={"lightweight", "rain_gear"}, temp_high_c=28))
    assert "cold_ski" not in {a["id"] for a in res.archetypes}
    assert "cold_ski" not in res.suggested_types
    assert _qty(res, "insulated_jacket") is None            # no parka


def test_cold_ski_suggested_when_actually_cold():
    ctx = _ctx(7, activity_tags={"ski"}, accommodation={"cabin"},
               climate_bands={"cold_weather"}, temp_high_c=-4)
    assert "cold_ski" in {a["id"] for a in _score_archetypes(ctx)}
    assert "cold_ski" in _plan(ctx).suggested_types


def test_needs_say_whether_a_modifier_or_an_essential():
    res = _plan(_ctx(7, activity_tags={"beach"}, accommodation={"resort"}))
    assert _reason(res, "swimwear") == "beach days"
    assert _reason(res, "passport") == "travel essential"


# ------------------------------------------------------------------ scoring

def test_beach_activities_pick_the_beach_archetype():
    res = _plan(_ctx(7, activity_tags={"beach"}, accommodation={"resort"}))
    assert res.archetypes[0]["id"] == "beach"
    assert "beach" in res.suggested_types
    assert _qty(res, "swimwear") is not None


def test_business_and_cultural_can_merge():
    ids = {a["id"] for a in _score_archetypes(
        _ctx(6, activity_tags={"business", "temple", "dining"}))}
    assert "cultural" in ids and "business" in ids


def test_essentials_and_international_always_present():
    res = _plan(_ctx(4, international=True))
    ids = {i.inventory_item_id for i in res.lines}
    assert {"passport", "prescription_meds", "travel_insurance", "universal_adapter"} <= ids


# ------------------------------------------------------------------ the magic

def test_laundry_caps_quantities_and_adds_a_task():
    """14-day trip WITH laundry → pack ~a week + a laundry task, not 14 of each."""
    prof = {**default_profile(), "laundry_comfort": "machine"}
    res = _plan(_ctx(14, profile=prof))
    assert _qty(res, "underwear") <= 9                 # not 14+
    assert _qty(res, "socks") <= 8
    assert any(t["do"] == "laundry" for t in res.tasks)


def test_no_laundry_long_trip_warns_instead():
    prof = {**default_profile(), "laundry_comfort": "none"}
    res = _plan(_ctx(14, profile=prof))
    assert _qty(res, "underwear") >= 14                # packs the whole trip
    assert any("laundry" in w.lower() for w in res.warnings)


def test_short_trip_no_laundry_task():
    res = _plan(_ctx(3))
    assert not any(t["do"] == "laundry" for t in res.tasks)


def test_one_bag_is_a_packing_style_now():
    prof = {**default_profile(), "packing_style": "one_bag"}
    res = _plan(_ctx(5, profile=prof))
    assert res.lines and all(i.bag == "carry_on" for i in res.lines)


# ------------------------------------------------------------------ cultural / profile

def test_a_modest_destination_adds_its_items_and_omits_nothing():
    culture = {"modest": True, "shoes_off": True, "add_items": ["modest_scarf"], "notes": []}
    res = _plan(_ctx(7, activity_tags={"beach"}, culture=culture))
    assert _reason(res, "modest_scarf") == "local dress / custom"
    assert _qty(res, "swimwear") is not None             # no revealing filter (D-229)
    assert "religious_sites" in res.suggested_types


def test_must_have_is_added():
    inv = [{**row, "must_have": row["id"] == "headphones"} for row in _ALL]
    res = _plan(_ctx(4), inventory=inv)
    assert _reason(res, "headphones") == "your must-have"


def test_no_weather_warns():
    res = _plan(_ctx(4, has_weather=False, climate_bands=set(), temp_high_c=None))
    assert any("weather" in w.lower() for w in res.warnings)
