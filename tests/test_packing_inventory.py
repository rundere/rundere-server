"""The 5.0 packing inventory model (SPEC-5 §6.6, D-127, D-229–D-231, YONDER-62).

Inventory items, templates and bags are own-only, like the journal: another
person never sees or changes them, trip sharing doesn't reach them, and an
admin sees only ownerless rows. Also: the soft-link sweeps on delete, the
catalogue's seed columns filled once from items.json, and migration 0050.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest                                               # noqa: E402

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

pytestmark = pytest.mark.skipif(not DEPS, reason="needs the app")

if DEPS:
    from fastapi import HTTPException

    from app import authn, scope
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Bag, Base, InventoryItem, PackingCatalogItem, PackingItem,
                            PackingTemplate, PackingTemplateItem, Trip, User)
    from app.trips import routes as trips_routes
    from planning import catalog, inventory

    Base.metadata.create_all(engine)
    client = TestClient(app)


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    yield


@pytest.fixture
def people():
    """Two people and an admin, with their rows swept afterwards."""
    made = []

    def person(name, admin=False):
        with SessionLocal() as s:
            u = User(username=f"inv-{name}-{uuid.uuid4().hex[:6]}", auth_source="local",
                     is_admin=admin)
            s.add(u)
            s.commit()
            s.refresh(u)
            s.expunge(u)
            made.append(u.id)
            return u

    yield person
    with SessionLocal() as s:
        for model in (InventoryItem, PackingTemplate, Bag, PackingItem):
            s.query(model).filter(model.user_id.in_(made)).delete(synchronize_session=False)
        s.query(Trip).filter(Trip.owner_id.in_(made)).delete(synchronize_session=False)
        s.query(User).filter(User.id.in_(made)).delete(synchronize_session=False)
        s.commit()


def _headers(u):
    return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}


def _item(user_id, label="Rain jacket", **kw):
    with SessionLocal() as s:
        row = InventoryItem(user_id=user_id, label=label, category="clothing", **kw)
        s.add(row)
        s.commit()
        return row.id


# ------------------------------------------------------------------ the scope rules
def test_inventory_is_own_only(auth_on, people):
    me, them, admin = people("me"), people("them"), people("admin", admin=True)
    mine, theirs, ownerless = _item(me.id), _item(them.id), _item(None, "Old poncho")
    with SessionLocal() as s:
        def seen(u):
            ids = {r.id for r in s.query(InventoryItem)
                   .filter(scope.own_filter(InventoryItem, u))}
            return ids & {mine, theirs, ownerless}
        assert seen(me) == {mine}
        assert seen(them) == {theirs}
        assert seen(admin) == {ownerless}          # never anyone else's (D-211)
        assert seen(None) == set()                 # signed out with auth on: nothing
        s.query(InventoryItem).filter(InventoryItem.id == ownerless).delete()
        s.commit()


def test_someone_elses_row_404s_like_a_missing_one(auth_on, people):
    me, them, admin = people("me"), people("them"), people("admin", admin=True)
    theirs = _item(them.id)
    with SessionLocal() as s:
        assert scope.require_own(s, them, InventoryItem, theirs).id == theirs
        for who in (me, admin):
            with pytest.raises(HTTPException) as e:
                scope.require_own(s, who, InventoryItem, theirs)
            assert e.value.status_code == 404
            with pytest.raises(HTTPException) as missing:
                scope.require_own(s, who, InventoryItem, "no-such-id")
            assert missing.value.detail == e.value.detail
        # Support access (D-211) doesn't reach it either.
        assert not scope.can_access_own(SimpleNamespace(id=admin.id, is_admin=True),
                                        s.get(InventoryItem, theirs))


def test_templates_and_bags_use_the_same_rule(auth_on, people):
    me, them = people("me"), people("them")
    with SessionLocal() as s:
        s.add_all([PackingTemplate(user_id=them.id, type_key="beach", name="Their beach kit"),
                   Bag(user_id=them.id, label="Their carry-on", kind="suitcase")])
        s.commit()
        for model in (PackingTemplate, Bag):
            assert s.query(model).filter(scope.own_filter(model, me),
                                         model.user_id == them.id).count() == 0
            assert s.query(model).filter(scope.own_filter(model, them),
                                         model.user_id == them.id).count() == 1


def test_auth_off_is_the_single_user_world():
    ownerless = _item(None, "Single-user towel")
    with SessionLocal() as s:
        assert s.query(InventoryItem).filter(scope.own_filter(InventoryItem, None),
                                             InventoryItem.id == ownerless).count() == 1
        s.query(InventoryItem).filter(InventoryItem.id == ownerless).delete()
        s.commit()


# ------------------------------------------------------------------ bags on a packing list
def test_bag_dropdown_and_choice_are_the_persons_own(auth_on, people):
    me, them = people("me"), people("them")
    with SessionLocal() as s:
        mine = Bag(user_id=me.id, label="Example 30L pack", kind="backpack")
        theirs = Bag(user_id=them.id, label="Example roller", kind="suitcase")
        trip = Trip(name="Inventory test trip", source="manual", owner_id=me.id)
        s.add_all([mine, theirs, trip])
        s.flush()
        line = PackingItem(trip_id=trip.id, user_id=me.id, item_key="clothing:socks",
                           label="Socks", category="clothing", status="draft")
        s.add(line)
        s.commit()
        tid, iid, my_bag, their_bag = trip.id, line.id, mine.id, theirs.id
        assert [o["label"] for o in trips_routes._bag_options(s, me)] == ["Example 30L pack"]
        assert their_bag not in trips_routes._bag_valid_values(s, me)

    h = _headers(me)
    r = client.post(f"/trips/{tid}/pack-item/{iid}/bag", headers=h, data={"bag": their_bag})
    assert r.status_code == 400                     # someone else's bag isn't a choice
    r = client.post(f"/trips/{tid}/pack-item/{iid}/bag", headers=h, data={"bag": my_bag})
    assert r.status_code == 200
    assert "Example 30L pack" in r.text and "Example roller" not in r.text


def test_new_bags_get_unique_ids_not_label_slugs(people):
    me, them = people("me"), people("them")
    with SessionLocal() as s:
        a = Bag(user_id=me.id, label="Carry-on", kind="suitcase")
        b = Bag(user_id=them.id, label="Carry-on", kind="suitcase")
        s.add_all([a, b])
        s.commit()                                  # no primary-key clash
        assert a.id != b.id and "carry_on" not in (a.id, b.id)


def test_bags_have_no_admin_screen_and_ownerless_ones_reach_an_admin(people, auth_on):
    """5.0: bags are edited on the person's Inventory page, so the admin has no
    Bag view; a pre-5.0 ownerless bag shows on an admin's own Inventory instead
    of being stranded, and never on anyone else's."""
    from fastapi import FastAPI

    from app.admin import mount_admin
    me, boss = people("me"), people("boss", admin=True)
    with SessionLocal() as s:
        legacy = Bag(label="Pre-5.0 duffel", kind="backpack")
        s.add(legacy)
        s.commit()
        legacy_id = legacy.id
    a = FastAPI()
    mount_admin(a, engine)
    try:
        assert TestClient(a).get("/admin/bag/list").status_code == 404
        with SessionLocal() as s:
            assert legacy_id in {b.id for b in s.query(Bag).filter(scope.own_filter(Bag, boss))}
            assert s.query(Bag).filter(scope.own_filter(Bag, me)).count() == 0
    finally:
        with SessionLocal() as s:
            s.query(Bag).filter(Bag.id == legacy_id).delete()
            s.commit()


# ------------------------------------------------------------------ deleting
def test_deleting_an_item_sweeps_its_links(people):
    me = people("me")
    with SessionLocal() as s:
        item = InventoryItem(user_id=me.id, label="Sun hat", category="clothing")
        keep = InventoryItem(user_id=me.id, label="Sandals", category="footwear")
        tpl = PackingTemplate(user_id=me.id, type_key="beach", name="Beach kit")
        s.add_all([item, keep, tpl])
        s.flush()
        s.add_all([PackingTemplateItem(template_id=tpl.id, inventory_item_id=item.id),
                   PackingTemplateItem(template_id=tpl.id, inventory_item_id=keep.id),
                   PackingItem(trip_id="t-inv", user_id=me.id, item_key="clothing:sun_hat",
                               label="Sun hat", category="clothing", status="packed",
                               inventory_item_id=item.id)])
        s.commit()
        inventory.delete_inventory_item(s, item)
        s.commit()
        lines = s.query(PackingTemplateItem).filter_by(template_id=tpl.id).all()
        assert [l.inventory_item_id for l in lines] == [keep.id]
        packed = s.query(PackingItem).filter_by(trip_id="t-inv").one()
        assert packed.inventory_item_id is None and packed.status == "packed"

        inventory.delete_template(s, tpl)
        s.commit()
        assert s.query(PackingTemplateItem).filter_by(template_id=tpl.id).count() == 0
        assert s.get(InventoryItem, keep.id) is not None    # the item stays


# ------------------------------------------------------------------ the catalogue's seed columns
def test_seed_columns_are_filled_once_and_owner_edits_kept():
    with SessionLocal() as s:
        s.query(PackingCatalogItem).delete()
        catalog.seed_catalog(s)
        # A row from before 0050 has no seed yet; one the owner cleared has [].
        s.get(PackingCatalogItem, "tshirt").seed = None
        s.get(PackingCatalogItem, "jeans").seed = []
        s.commit()
        catalog.seed_catalog(s)
        assert s.get(PackingCatalogItem, "tshirt").seed == ["basics"]
        assert s.get(PackingCatalogItem, "tshirt").seed_qty == 5
        assert s.get(PackingCatalogItem, "jeans").seed == []     # left alone
        s.get(PackingCatalogItem, "jeans").seed = catalog.seed_items()["jeans"].get("seed", [])
        s.commit()
        answers = {"style": "masc", "footwear": "minimal", "care": ["shaving"]}
        assert catalog.seed_for(answers, catalog.items(s)) == catalog.seed_for(answers)


# ------------------------------------------------------------------ the migration (0050)
def test_migration_adds_the_tables_and_leaves_bags_ownerless(tmp_path):
    import sqlalchemy as sa
    db = tmp_path / "mig.db"
    env = {**os.environ, "TX_DATABASE_URL": f"sqlite:///{db}"}

    def alembic(*args):
        r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-2000:]

    alembic("upgrade", "0049")
    eng = sa.create_engine(f"sqlite:///{db}")
    with eng.begin() as c:
        c.execute(sa.text("INSERT INTO bags (id, label, kind, enabled, created_at, updated_at) "
                          "VALUES ('old_pack', 'Old pack', 'backpack', 1, "
                          "'2026-10-01', '2026-10-01')"))
    alembic("upgrade", "head")
    insp = sa.inspect(eng)
    tables = set(insp.get_table_names())
    assert {"inventory_items", "packing_templates", "packing_template_items"} <= tables
    assert "inventory_item_id" in {c["name"] for c in insp.get_columns("packing_items")}
    assert {"seed", "styles", "seed_qty", "minimal"} <= {
        c["name"] for c in insp.get_columns("packing_catalog_items")}
    with eng.begin() as c:
        assert c.execute(sa.text("SELECT user_id FROM bags WHERE id = 'old_pack'")
                         ).scalar() is None
    alembic("downgrade", "0049")
    insp = sa.inspect(sa.create_engine(f"sqlite:///{db}"))
    assert "inventory_items" not in insp.get_table_names()
    assert "user_id" not in {c["name"] for c in insp.get_columns("bags")}
