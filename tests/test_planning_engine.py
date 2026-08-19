"""Packing engine tests (SPEC-3.1-planning.md §2/§5.5) — offline, hand-built
context against the JSON seed catalog. Covers the archetype scoring, the laundry
intelligence, cultural filtering, and profile constraints.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from planning import catalog
from planning.context import PlanningContext
from planning.engine import build_plan, default_profile

_ITEMS = catalog.seed_items()


def _ctx(days, **kw):
    base = dict(duration_days=days, activity_tags={"sightseeing"},
                climate_bands={"lightweight"}, temp_high_c=24, has_weather=True,
                profile=default_profile(), culture={})
    base.update(kw)
    return PlanningContext(**base)


def _qty(res, iid):
    return next((i.quantity for i in res.items if i.item_id == iid), None)


def _reason(res, iid):
    return next((i.reason for i in res.items + res.omitted if i.item_id == iid), None)


# ---------------------------------------------------------- reasons / climate

def test_modifier_items_explain_their_trigger():
    # A cold-weather trip: the insulated jacket says WHY it's on the list.
    res = build_plan(_ctx(5, climate_bands={"cold_weather"}), _ITEMS)
    assert _qty(res, "insulated_jacket") is not None
    assert _reason(res, "insulated_jacket") == "cold days expected"


def test_every_item_has_a_nonempty_reason():
    res = build_plan(_ctx(6, activity_tags={"hiking"}, climate_bands={"layering"}),
                     _ITEMS)
    assert res.items and all((i.reason or "").strip() for i in res.items)


def test_cold_nights_mild_days_is_layering_not_a_parka():
    # Arizona in October: ~20°C days, ~2°C nights. Layering, not cold_weather.
    from planning.context import _climate_bands
    assert _climate_bands(20, 2, 0) == {"layering"}
    assert _climate_bands(8, -1, 0) == {"cold_weather"}      # genuinely cold days
    assert _climate_bands(26, 15, 0) == {"lightweight"}


# ------------------------------------------------------------------ scoring

def test_beach_activities_pick_the_beach_archetype():
    res = build_plan(_ctx(7, activity_tags={"beach"}, accommodation={"resort"}), _ITEMS)
    assert res.archetypes[0]["id"] == "beach"
    assert any(i.item_id == "swimwear" for i in res.items)


def test_business_and_cultural_can_merge():
    res = build_plan(_ctx(6, activity_tags={"business", "temple", "dining"}), _ITEMS)
    ids = {a["id"] for a in res.archetypes}
    assert "cultural" in ids and "business" in ids


def test_essentials_and_international_always_present():
    res = build_plan(_ctx(4, international=True), _ITEMS)
    ids = {i.item_id for i in res.items}
    assert {"passport", "prescription_meds", "travel_insurance", "universal_adapter"} <= ids


# ------------------------------------------------------------------ the magic

def test_laundry_caps_quantities_and_adds_a_task():
    """14-day trip WITH laundry → pack ~a week + a laundry task, not 14 of each."""
    prof = {**default_profile(), "laundry_comfort": "machine"}
    res = build_plan(_ctx(14, profile=prof), _ITEMS)
    assert _qty(res, "underwear") <= 9                 # not 14+
    assert _qty(res, "socks") <= 8
    # the archetype's "tops" formula item (quick_dry_top for a long sightseeing
    # trip) is capped, not one-per-day
    assert _qty(res, "quick_dry_top") is not None and _qty(res, "quick_dry_top") <= 7
    assert any(t["do"] == "laundry" for t in res.tasks)


def test_no_laundry_long_trip_warns_instead():
    prof = {**default_profile(), "laundry_comfort": "none"}
    res = build_plan(_ctx(14, profile=prof), _ITEMS)
    assert _qty(res, "underwear") >= 14                # packs the whole trip
    assert any("laundry" in w.lower() for w in res.warnings)


def test_short_trip_no_laundry_task():
    res = build_plan(_ctx(3), _ITEMS)
    assert not any(t["do"] == "laundry" for t in res.tasks)


# ------------------------------------------------------------------ cultural / profile

def test_modest_destination_omits_revealing_with_swap():
    culture = {"modest": True, "shoes_off": True, "add_items": ["modest_scarf"], "notes": []}
    res = build_plan(_ctx(7, activity_tags={"beach"}, culture=culture,
                          profile={**default_profile(), "cultural_strictness": "strict"}),
                     _ITEMS)
    omitted = {i.item_id for i in res.omitted}
    assert "shorts" in omitted                          # revealing, dropped
    kept = {i.item_id for i in res.items}
    assert "loose_trousers" in kept                     # swapped in
    assert "modest_scarf" in kept                       # cultural add
    assert any(i.swap_for == "loose_trousers" for i in res.omitted if i.item_id == "shorts")


def test_no_heels_swaps_dress_shoes_for_flats():
    res = build_plan(_ctx(5, activity_tags={"business", "dining"},
                          profile={**default_profile(), "style_constraints": ["no_heels"]}),
                     _ITEMS)
    assert "dress_shoes" in {i.item_id for i in res.omitted}
    assert "smart_flats" in {i.item_id for i in res.items}


def test_must_have_is_force_added():
    prof = {**default_profile(), "must_have_items": ["headphones"]}
    res = build_plan(_ctx(4, profile=prof), _ITEMS)
    hp = next((i for i in res.items if i.item_id == "headphones"), None)
    assert hp is not None and hp.source == "must_have"


def test_no_weather_warns():
    res = build_plan(_ctx(4, has_weather=False, climate_bands=set(), temp_high_c=None), _ITEMS)
    assert any("weather" in w.lower() for w in res.warnings)
