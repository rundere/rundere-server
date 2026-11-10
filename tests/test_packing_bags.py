"""Bags for a trip, and what fits (SPEC-5 §6.10, D-232).

Capacity is read from what the person typed, in litres only; each line has a
rough volume (their own figure, else the catalogue's, else a category
default); a picked bag's fill is the sum against 85 % of its capacity; with no
bag picked the list suggests the smallest of their bags (or pair) that fits.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest                                               # noqa: E402

from planning import bags as bagfit                         # noqa: E402
from planning import catalog                                # noqa: E402
from planning.bags import BagInfo, Line                     # noqa: E402

from tests.test_packing_engine_5 import (DEPS, _airports, _answer, _h,  # noqa: E402,F401
                                         _NoGeo, pytestmark_app, world)

if DEPS:
    from tests.test_packing_engine_5 import client
    from app.db import SessionLocal
    from app.models import Bag, InventoryItem, PackingItem, PackingPlan, Trip
    from planning.runner import generate_plan


# ------------------------------------------------------------------ pure
@pytest.mark.parametrize("text,litres", [
    ("40L", 40), ("40 l", 40), ("40 litres", 40), ("40 Liters", 40),
    ("35–40L", 40), ("Example pack 28L / 1.2 kg", 28), ("40", 40), ("22,5 L", 22.5),
    ("22in carry-on", None), ('22"', None), ("big", None), ("", None), (None, None),
    ("0L", None),
])
def test_capacity_is_read_in_litres_only(text, litres):
    assert bagfit.capacity_l(text) == litres


def test_volume_is_yours_then_the_catalogues_then_the_categorys():
    items = catalog.seed_items()
    d = catalog.volume_defaults()
    assert bagfit.volume_for("jeans", "clothing", items, d, own=2.2) == 2.2
    assert bagfit.volume_for("jeans", "clothing", items, d) == items["jeans"]["volume_l"]
    assert bagfit.volume_for(None, "footwear", items, d) == d["footwear"]
    assert bagfit.volume_for("not_a_thing", "tech", items, d) == d["tech"]


def test_every_category_has_a_default_and_the_catalogue_validates():
    assert set(catalog.categories()) <= set(catalog.volume_defaults())
    assert catalog.validate() == []


def _ln(i, cat, qty, vol, bag=None):
    return Line(id=str(i), category=cat, quantity=qty, volume=vol, bag=bag)


def test_one_pair_of_shoes_is_worn():
    lines = [_ln(1, "clothing", 5, 0.6), _ln(2, "footwear", 1, 4.0), _ln(3, "footwear", 1, 2.0)]
    assert bagfit.total_l(lines) == 5.0       # 3 + 4 + 2, less the 4 L pair worn


def test_carry_on_things_go_in_the_smallest_picked_bag():
    big = BagInfo("big", "Example suitcase", "suitcase", 60)
    small = BagInfo("small", "Example daypack", "backpack", 20)
    assert bagfit.assign("carry_on", [big, small]) == "small"
    assert bagfit.assign("checked", [big, small]) == "big"
    assert bagfit.assign("carry_on", [big]) == "big"
    assert bagfit.assign("checked", []) is None


def test_a_bag_is_over_past_85_percent():
    bag = BagInfo("b", "Example pack", "backpack", 40)
    lines = [_ln(1, "clothing", 10, 3.3, "b")]          # 33 L of 34 L usable: fits
    assert not bagfit.fit(lines, [bag]).fills[0].over
    lines = [_ln(1, "clothing", 10, 3.5, "b")]          # 35 L > 34 L usable
    rep = bagfit.fit(lines, [bag])
    assert rep.fills[0].over and rep.fills[0].used == 35


def test_lines_in_other_bags_are_counted_apart():
    bag = BagInfo("b", "Example pack", "backpack", 40)
    rep = bagfit.fit([_ln(1, "clothing", 2, 1.0, "b"), _ln(2, "clothing", 3, 1.0, "checked")],
                     [bag])
    assert rep.fills[0].used == 2 and rep.elsewhere == 3


def test_suggests_the_smallest_bag_that_fits_else_a_pair():
    bags = [BagInfo("s", "Example suitcase", "suitcase", 70),
            BagInfo("c", "Example carry-on", "suitcase", 40),
            BagInfo("d", "Example daypack", "backpack", 20),
            BagInfo("u", "Example tote", "personal_item", None)]
    assert [b.id for b in bagfit.suggest(30, bags)] == ["c"]
    assert [b.id for b in bagfit.suggest(15, bags)] == ["d"]
    assert [b.id for b in bagfit.suggest(70, bags)] == ["s", "d"]     # 59.5 + 17
    assert bagfit.suggest(500, bags) is None
    assert bagfit.suggest(10, [BagInfo("x", "Example bag", "backpack", None)]) is None


def test_one_bag_suggests_one_bag_plus_a_personal_item():
    bags = [BagInfo("c", "Example carry-on", "suitcase", 40),
            BagInfo("d", "Example daypack", "backpack", 30),
            BagInfo("p", "Example tote", "personal_item", 15)]
    assert {b.id for b in bagfit.suggest(45, bags)} == {"c", "p"}      # the smaller pair
    assert {b.id for b in bagfit.suggest(50, bags)} == {"c", "d"}
    assert bagfit.suggest(50, bags, one_bag=True) is None              # two main bags: no


# ------------------------------------------------------------------ the app
def _bags(ids, who, *specs):
    out = []
    with SessionLocal() as s:
        for label, kind, cap in specs:
            b = Bag(user_id=ids[who], label=label, kind=kind, capacity=cap)
            s.add(b); s.flush()
            out.append(b.id)
        s.commit()
    return out


def _plan(ids, who="a"):
    with SessionLocal() as s:
        return (s.query(PackingPlan).filter_by(trip_id=ids["trip"], user_id=ids[who])
                .order_by(PackingPlan.generated_at.desc()).first())


@pytestmark_app
def test_picking_bags_puts_the_lines_in_them_and_shows_the_fill(world):
    _answer(world, "a")
    big, small = _bags(world, "a", ("Example suitcase", "suitcase", "60L"),
                       ("Example daypack", "backpack", "20 litres"))
    h = _h(world["a"])
    client.post(f"/trips/{world['trip']}/pack", headers=h)
    page = client.get(f"/trips/{world['trip']}", headers=h).text
    assert "Bags for this trip" in page and "should fit in your" in page    # suggestion
    for b in (big, small):
        r = client.post(f"/trips/{world['trip']}/pack/bags/add", headers=h,
                        data={"bag_id": b})
        assert r.status_code == 200
    assert _plan(world).bag_ids == [big, small]
    with SessionLocal() as s:
        rows = s.query(PackingItem).filter_by(trip_id=world["trip"], status="draft").all()
        assert rows and {r.bag for r in rows} <= {big, small}
        passport = next(r for r in rows if r.label == "Passport")
        assert passport.bag == small                    # a carry-on thing
    assert "of 60 L" in r.text and "of 20 L" in r.text
    # The per-line dropdown offers only the picked bags.
    _bags(world, "a", ("Example tote", "personal_item", "10L"))
    page = client.get(f"/trips/{world['trip']}", headers=h).text
    assert ">Example tote · 10L</option>" not in page.split("+ Add a bag…")[1].split("</select>", 1)[1]


@pytestmark_app
def test_the_pick_is_kept_across_re_generates_and_a_hand_move_sticks(world):
    _answer(world, "a")
    big, small = _bags(world, "a", ("Example suitcase", "suitcase", "60L"),
                       ("Example daypack", "backpack", "20L"))
    h = _h(world["a"])
    client.post(f"/trips/{world['trip']}/pack/bags/add", headers=h, data={"bag_id": big})
    assert _plan(world).bag_ids == [big]               # no list yet: generated with it
    with SessionLocal() as s:
        row = s.query(PackingItem).filter_by(trip_id=world["trip"], status="draft").first()
        rid = row.id
    client.post(f"/trips/{world['trip']}/pack-item/{rid}/bag", headers=h, data={"bag": small})
    client.post(f"/trips/{world['trip']}/pack", headers=h)
    assert _plan(world).bag_ids == [big]
    with SessionLocal() as s:
        assert s.get(PackingItem, rid).bag == small     # moved by hand: kept (D-63)
    client.post(f"/trips/{world['trip']}/pack/bags/remove", headers=h, data={"bag_id": big})
    assert not _plan(world).bag_ids


@pytestmark_app
def test_someone_elses_bag_cant_be_picked(world):
    _answer(world, "a")
    _answer(world, "b")
    (bens,) = _bags(world, "b", ("Ben's example pack", "backpack", "40L"))
    h = _h(world["a"])
    client.post(f"/trips/{world['trip']}/pack", headers=h)
    r = client.post(f"/trips/{world['trip']}/pack/bags/add", headers=h, data={"bag_id": bens})
    assert r.status_code == 200 and not _plan(world).bag_ids
    assert "Ben&#39;s example pack" not in r.text
    with SessionLocal() as s:
        trip = s.get(Trip, world["trip"])
        generate_plan(s, trip, resolve_airport=_airports, geocoder=_NoGeo(),
                      bag_ids=[bens], user_id=world["a"], traveler_id=world["ta"])
    assert not _plan(world).bag_ids


@pytestmark_app
def test_no_capacities_says_to_add_them(world):
    _answer(world, "a")
    _bags(world, "a", ("Example pack", "backpack", "22in"))
    h = _h(world["a"])
    client.post(f"/trips/{world['trip']}/pack", headers=h)
    page = client.get(f"/trips/{world['trip']}", headers=h).text
    assert "capacity in litres" in page


@pytestmark_app
def test_an_item_size_and_a_bag_capacity_save_on_the_inventory_page(world):
    _answer(world, "a")
    (bag,) = _bags(world, "a", ("Example pack", "backpack", ""))
    with SessionLocal() as s:
        it = InventoryItem(user_id=world["a"], type_key="jeans", label="Example jeans",
                           category="clothing", quantity=1)
        s.add(it); s.commit()
        iid = it.id
    h = {**_h(world["a"]), "hx-request": "true"}
    base = "/trips/settings/packing-profile"
    r = client.post(f"{base}/inventory/{iid}", headers=h,
                    data={"label": "Example jeans", "quantity": "1", "volume_l": "2.5"})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.get(InventoryItem, iid).volume_l == 2.5
    client.post(f"{base}/inventory/{iid}", headers=h,
                data={"label": "Example jeans", "quantity": "1", "volume_l": ""})
    with SessionLocal() as s:
        assert s.get(InventoryItem, iid).volume_l is None      # back to the estimate
    r = client.post(f"{base}/bags/{bag}", headers=h,
                    data={"label": "Example pack", "kind": "backpack", "capacity": "38L"})
    assert "38 L" in r.text
    r = client.post(f"{base}/bags/{bag}", headers=h,
                    data={"label": "Example pack", "kind": "backpack", "capacity": "22in"})
    assert "not in litres" in r.text
    # Someone else can't set Ada's item size.
    r = client.post(f"{base}/inventory/{iid}", headers={**_h(world["b"]), "hx-request": "true"},
                    data={"label": "x", "quantity": "1", "volume_l": "9"})
    assert r.status_code == 404
    with SessionLocal() as s:
        assert s.get(InventoryItem, iid).volume_l is None


def test_the_guess_for_a_line_already_in_a_list():
    """Re-assigning without re-generating recomputes the engine's guess: the
    line's own generic bag, else its catalogue type, a 4.x key included."""
    from types import SimpleNamespace as NS
    from planning.runner import generic_bag
    items = catalog.seed_items()
    row = NS(bag="a-bag-id", inventory_item_id=None, item_key="documents:passport")
    assert generic_bag(row, items, False, {}) == "carry_on"
    assert generic_bag(NS(bag="checked", inventory_item_id=None, item_key="x"),
                       items, False, {}) == "checked"
    assert generic_bag(NS(bag="a-bag-id", inventory_item_id=None, item_key="custom:ab12"),
                       items, False, {}) == "checked"
    assert generic_bag(row, items, True, {}) == "carry_on"         # one_bag


def test_a_long_run_of_digits_is_quick_and_unknown():
    import time
    t = time.monotonic()
    assert bagfit.capacity_l("0" * 50_000 + "x") is None
    assert time.monotonic() - t < 0.5
