"""5.1, YONDER-408 part 2: your inventory, bags and packing templates from the
apps (`/api/v1/me/inventory`, `/me/bags`, `/me/packing-templates`).

They belong to the account (SPEC-5 §6.6): every route is the caller's own, and
someone else's row 404s exactly like a missing one and is never changed.
Fictional people; SQLite via conftest.
"""
import sys
import uuid
from pathlib import Path

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
    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Bag, Base, InventoryItem, PackingTemplate, PackingTemplateItem,
                            User)

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

INV, BAGS, TPL = "/api/v1/me/inventory", "/api/v1/me/bags", "/api/v1/me/packing-templates"


@pytest.fixture(autouse=True)
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    yield


@pytest.fixture
def person():
    """Accounts with no traveller: the inventory is the account's, not a traveller's."""
    made = []

    def make(name):
        with SessionLocal() as s:
            u = User(username=f"mi-{name}-{uuid.uuid4().hex[:6]}", auth_source="local")
            s.add(u)
            s.commit()
            made.append(u.id)
            s.refresh(u)
            s.expunge(u)
            return u, {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    yield make
    with SessionLocal() as s:
        tpls = [t for (t,) in s.query(PackingTemplate.id).filter(PackingTemplate.user_id.in_(made))]
        s.query(PackingTemplateItem).filter(PackingTemplateItem.template_id.in_(tpls)).delete(
            synchronize_session=False)
        for model in (InventoryItem, PackingTemplate, Bag):
            s.query(model).filter(model.user_id.in_(made)).delete(synchronize_session=False)
        s.query(User).filter(User.id.in_(made)).delete(synchronize_session=False)
        s.commit()


def _add(h, label="MI travel pillow", category="comfort", quantity=1):
    r = client.post(INV, headers=h, json={"category": category, "label": label,
                                          "quantity": quantity})
    assert r.status_code == 201, r.text
    return r.json()


def _category(h):
    return client.get(INV, headers=h).json()["options"]["categories"][0]["key"]


# ------------------------------------------------------------------ inventory
def test_the_inventory_lists_every_category_and_its_choices(person):
    _, h = person("list")
    body = client.get(INV, headers=h).json()
    assert body["total"] == 0 and body["bags"] == []
    keys = [c["key"] for c in body["categories"]]
    assert keys == [c["key"] for c in body["options"]["categories"]]
    assert all(c["items"] == [] for c in body["categories"])
    assert {k["key"] for k in body["options"]["bag_kinds"]} == {"backpack", "suitcase",
                                                                "personal_item"}


def test_add_edit_and_delete_an_item(person):
    _, h = person("crud")
    cat = _category(h)
    item = _add(h, category=cat, quantity=2)
    assert item["quantity"] == 2 and item["volume_estimate_l"] > 0
    r = client.patch(f"{INV}/{item['id']}", headers=h,
                     json={"must_have": True, "volume_l": 1.5})
    assert r.status_code == 200
    assert r.json()["must_have"] and r.json()["volume_l"] == 1.5
    assert r.json()["label"] == "MI travel pillow" and r.json()["quantity"] == 2   # kept
    r = client.patch(f"{INV}/{item['id']}", headers=h, json={"volume_l": None})
    assert r.json()["volume_l"] is None
    assert client.delete(f"{INV}/{item['id']}", headers=h).status_code == 204
    assert client.get(INV, headers=h).json()["total"] == 0


def test_a_bad_item_is_refused(person):
    _, h = person("bad")
    assert client.post(INV, headers=h, json={"category": "mi-nope", "label": "x"}
                       ).status_code == 422
    item = _add(h, category=_category(h))
    for body in ({"quantity": 0}, {"volume_l": 99}, {"category": "comfort"}):
        assert client.patch(f"{INV}/{item['id']}", headers=h, json=body).status_code == 422


# ------------------------------------------------------------------ bags
def test_add_change_and_retire_a_bag(person):
    _, h = person("bag")
    r = client.post(BAGS, headers=h, json={"label": "MI blue pack", "kind": "backpack",
                                           "capacity": "30L"})
    assert r.status_code == 201 and r.json()["capacity_l"] == 30
    bag = r.json()["id"]
    r = client.patch(f"{BAGS}/{bag}", headers=h, json={"capacity": "22in carry-on"})
    assert r.json()["capacity"] == "22in carry-on" and r.json()["capacity_l"] is None
    assert r.json()["label"] == "MI blue pack"
    assert client.patch(f"{BAGS}/{bag}", headers=h, json={"kind": "mi-trunk"}
                        ).status_code == 422
    assert client.delete(f"{BAGS}/{bag}", headers=h).status_code == 204
    assert client.get(BAGS, headers=h).json() == []
    with SessionLocal() as s:                        # retired, not deleted
        assert s.get(Bag, bag).enabled is False


# ------------------------------------------------------------------ templates
def test_create_edit_and_delete_a_template(person):
    _, h = person("tpl")
    cat = _category(h)
    a, b = _add(h, "MI sun hat", cat), _add(h, "MI flip-flops", cat)
    free = {t["key"]: t["label"] for t in client.get(TPL, headers=h).json()["free_types"]}
    assert "beach" in free and "custom" in free
    r = client.post(TPL, headers=h, json={
        "type_key": "beach", "items": [{"inventory_item_id": a["id"], "quantity": 2}]})
    assert r.status_code == 201, r.text
    tpl = r.json()
    assert tpl["name"] == free["beach"]                     # named after its type
    assert tpl["items"] == [{"inventory_item_id": a["id"], "quantity": 2}]
    # One per type, except custom.
    assert client.post(TPL, headers=h, json={"type_key": "beach"}).status_code == 409
    assert "beach" not in {t["key"] for t in client.get(TPL, headers=h).json()["free_types"]}
    r = client.patch(f"{TPL}/{tpl['id']}", headers=h, json={"name": "MI lazy days"})
    assert r.json()["name"] == "MI lazy days" and len(r.json()["items"]) == 1   # lines kept
    r = client.patch(f"{TPL}/{tpl['id']}", headers=h,
                     json={"items": [{"inventory_item_id": b["id"]}]})
    assert r.json()["items"] == [{"inventory_item_id": b["id"], "quantity": None}]
    assert client.delete(f"{TPL}/{tpl['id']}", headers=h).status_code == 204
    assert client.get(INV, headers=h).json()["total"] == 2               # items stay


def test_a_custom_template_needs_a_keyword(person):
    _, h = person("custom")
    assert client.post(TPL, headers=h, json={"type_key": "custom"}).status_code == 422
    r = client.post(TPL, headers=h, json={"type_key": "custom", "keyword": "mi-climbing"})
    assert r.status_code == 201 and r.json()["keyword"] == "mi-climbing"
    # Custom isn't one-per-type.
    assert client.post(TPL, headers=h, json={"type_key": "custom", "keyword": "mi-yoga"}
                       ).status_code == 201
    assert client.post(TPL, headers=h, json={"type_key": "mi-nope"}).status_code == 422


def test_suggest_only_proposes(person):
    _, h = person("suggest")
    r = client.get(f"{TPL}/suggest?type=beach", headers=h)
    assert r.status_code == 200 and r.json()["type_key"] == "beach"
    assert client.get(TPL, headers=h).json()["templates"] == []
    assert client.get(f"{TPL}/suggest?type=mi-nope", headers=h).status_code == 422


# ------------------------------------------------------------------ scoping
def test_someone_elses_rows_404_and_never_change(person):
    _, ha = person("ana")
    _, hb = person("ben")
    item = _add(hb, "MI ben's camera", _category(hb))
    bag = client.post(BAGS, headers=hb, json={"label": "MI ben's bag"}).json()
    tpl = client.post(TPL, headers=hb, json={"type_key": "hiking"}).json()
    for method, path, body in (
            ("patch", f"{INV}/{item['id']}", {"label": "mine now"}),
            ("delete", f"{INV}/{item['id']}", None),
            ("patch", f"{BAGS}/{bag['id']}", {"label": "mine now"}),
            ("delete", f"{BAGS}/{bag['id']}", None),
            ("get", f"{TPL}/{tpl['id']}", None),
            ("patch", f"{TPL}/{tpl['id']}", {"name": "mine now"}),
            ("delete", f"{TPL}/{tpl['id']}", None)):
        r = client.request(method, path, headers=ha, json=body)
        assert r.status_code == 404, (method, path, r.status_code)
    # Ana can't put Ben's item in her own template either.
    r = client.post(TPL, headers=ha, json={"type_key": "hiking",
                                           "items": [{"inventory_item_id": item["id"]}]})
    assert r.status_code == 422
    # Nothing of Ben's shows to Ana, and Ben's rows are unchanged.
    assert client.get(INV, headers=ha).json()["total"] == 0
    assert client.get(BAGS, headers=ha).json() == []
    assert client.get(TPL, headers=ha).json()["templates"] == []
    assert client.get(INV, headers=hb).json()["total"] == 1
    assert client.get(BAGS, headers=hb).json()[0]["label"] == "MI ben's bag"
    assert client.get(f"{TPL}/{tpl['id']}", headers=hb).json()["name"] != "mine now"


def test_signed_out_is_401():
    for method, path in (("get", INV), ("post", INV), ("get", BAGS), ("get", TPL)):
        assert client.request(method, path, json={}).status_code == 401
