"""The 5.0 packing profile pages (SPEC-5 §6.1–6.5, D-229, D-230, YONDER-62).

The questionnaire seeds the inventory and bags, add-only; the inventory and
template pages edit them; and every one of those routes is the signed-in
person's own: someone else gets a 404 and changes nothing.
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
    from app.models import (Bag, Base, InventoryItem, PackingProfile, PackingTemplate,
                            PackingTemplateItem, TravelerIdentity, User)
    from app.trips.routes import PACK_CAT_LABELS
    from planning import catalog

    Base.metadata.create_all(engine)
    client = TestClient(app)

BASE = "/trips/settings/packing-profile"
ANSWERS = {"style": "masc", "footwear": "minimal", "care": ["shaving"],
           "gear": ["cubes"], "bags:daypack": "2", "bags:checked": "1", "tech": ["laptop"]}


@pytest.fixture(autouse=True)
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    yield


@pytest.fixture
def person():
    """People with a traveller each (the profile is the traveller's), swept after."""
    made = []

    def make(name, admin=False, account=True):
        with SessionLocal() as s:
            t = TravelerIdentity(display_name=f"PS {name}", kind="guest")
            s.add(t)
            s.flush()
            u = None
            if account:
                u = User(username=f"ps-{name}-{uuid.uuid4().hex[:6]}", auth_source="local",
                         traveler_id=t.id, is_admin=admin)
                s.add(u)
            s.commit()
            made.append((u.id if u else None, t.id))
            if u is None:
                return t.id
            s.refresh(u)
            s.expunge(u)
            return u, {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    yield make
    with SessionLocal() as s:
        uids = [u for u, _ in made if u]
        tpls = [t for (t,) in s.query(PackingTemplate.id).filter(PackingTemplate.user_id.in_(uids))]
        s.query(PackingTemplateItem).filter(PackingTemplateItem.template_id.in_(tpls)).delete(
            synchronize_session=False)
        for model in (InventoryItem, PackingTemplate, Bag):
            s.query(model).filter(model.user_id.in_(uids)).delete(synchronize_session=False)
        s.query(PackingProfile).filter(PackingProfile.traveler_id.in_([t for _, t in made])
                                       ).delete(synchronize_session=False)
        s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
        s.query(TravelerIdentity).filter(TravelerIdentity.id.in_([t for _, t in made])
                                         ).delete(synchronize_session=False)
        s.commit()


def _inventory(uid):
    with SessionLocal() as s:
        return {r.type_key or r.label: r for r in s.query(InventoryItem).filter_by(user_id=uid)}


# ------------------------------------------------------------------ the questionnaire
def test_the_page_asks_the_six_questions_and_three_preferences(person):
    _, h = person("page")
    page = client.get(BASE, headers=h).text
    for name in ("style", "clothing", "footwear", "care", "bags:daypack", "gear", "tech",
                 "laundry_comfort", "packing_style", "climate_preference"):
        assert f'name="{name}"' in page, name
    # The 4.x fields are gone (SPEC-5 §6.4).
    for gone in ("bag_mode", "bag_weight_kg", "style_constraints", "must_have_items",
                 "cultural_strictness", "clothing_variant"):
        assert f'name="{gone}"' not in page, gone
    assert "Which clothing style should your list use?" in page
    assert "a style, not who you are" in page
    assert "Answer these once to start your inventory" in page
    assert "Save and build my inventory" in page


def test_once_answered_the_page_leads_with_inventory_and_templates(person):
    _, h = person("hub")
    client.post(BASE, headers=h, data=ANSWERS)
    page = client.get(BASE, headers=h).text
    assert page.index('class="yn-pack-hub"') < page.index("How you pack") \
        < page.index('<details class="yn-profile-section yn-requestions">') \
        < page.index("Which clothing style")
    assert "Answer these once" not in page and "Save and build my inventory" not in page


def test_inventory_and_templates_have_a_way_back(person):
    _, h = person("back")
    for path in ("/inventory", "/templates", "/templates/new?type=beach"):
        page = client.get(BASE + path, headers=h).text
        assert 'class="yn-page-actions"' in page, path
    for path in ("/inventory", "/templates"):
        page = client.get(BASE + path, headers=h).text
        assert f'<a class="btn btn-primary" href="{BASE}">Done</a>' in page, path


def test_saving_seeds_the_inventory_and_bags(person):
    u, h = person("seed")
    r = client.post(BASE, headers=h, data=ANSWERS, follow_redirects=False)
    # What the answers did is the inventory: the save goes there (303), not
    # back to a page of questions that looks the same.
    assert r.status_code == 303 and "/inventory?added=" in r.headers["location"]
    r = client.get(r.headers["location"], headers=h)
    assert "Your inventory is ready ✓" in r.text
    with SessionLocal() as s:
        v = s.query(PackingProfile).filter_by(traveler_id=u.traveler_id).one().values
    assert v["questionnaire"] == {"style": "masc", "footwear": "minimal",
                                  "clothing": [], "care": ["shaving"], "tech": ["laptop"],
                                  "bags": {"checked": 1, "daypack": 2},
                                  "gear": ["checked", "cubes", "daypack"]}
    assert v["questionnaire_saved_at"] and v["packing_style"] == "balanced"
    want = catalog.seed_for(v["questionnaire"])
    have = _inventory(u.id)
    assert set(have) == set(want)
    assert all(have[k].quantity == q for k, q in want.items())
    assert f"Your answers added {len(want)} items and 3 bags." in r.text
    # Bags are their own section, above what goes in them.
    assert r.text.index('id="bags"') < r.text.index("What goes in them") \
        < r.text.index('id="cat-')
    assert "3 bags, and what you pack in them" in client.get(BASE, headers=h).text
    with SessionLocal() as s:
        bags = {b.label: b.kind for b in s.query(Bag).filter_by(user_id=u.id)}
    assert bags == {"Daypack": "backpack", "Daypack 2": "backpack",
                    "Checked suitcase": "suitcase"}


def test_saving_again_only_adds(person):
    u, h = person("again")
    client.post(BASE, headers=h, data=ANSWERS)
    with SessionLocal() as s:
        row = s.query(InventoryItem).filter_by(user_id=u.id, type_key="tshirt").one()
        row.label, row.quantity = "Favourite tee", 2
        s.commit()
    before = _inventory(u.id)
    r = client.post(BASE, headers=h, data={**ANSWERS, "clothing": ["swim"]})
    assert "Your answers added" in r.text and "and 3 bags" not in r.text
    after = _inventory(u.id)
    assert set(before) < set(after)
    assert after["tshirt"].label == "Favourite tee" and after["tshirt"].quantity == 2
    assert "swimwear" in after
    with SessionLocal() as s:
        assert s.query(Bag).filter_by(user_id=u.id).count() == 3      # no duplicates
    # Saving with nothing new to add stays on the profile and says so.
    r = client.post(BASE, headers=h, data={**ANSWERS, "clothing": ["swim"]},
                    follow_redirects=False)
    assert r.status_code == 200 and "didn't add anything new" in r.text


def test_a_style_and_footwear_answer_are_required(person):
    u, h = person("required")
    r = client.post(BASE, headers=h, data={"clothing": ["swim"]})
    assert "Pick a clothing style and a footwear answer." in r.text
    with SessionLocal() as s:
        assert s.query(PackingProfile).filter_by(traveler_id=u.traveler_id).count() == 0
    assert _inventory(u.id) == {}


def test_unknown_answers_are_dropped(person):
    u, h = person("junk")
    client.post(BASE, headers=h, data={"style": "neutral", "footwear": "varied",
                                       "clothing": ["swim", "bogus"], "tech": ["phone"],
                                       "bags:daypack": "lots", "packing_style": "chaotic"})
    with SessionLocal() as s:
        v = s.query(PackingProfile).filter_by(traveler_id=u.traveler_id).one().values
    assert v["questionnaire"]["clothing"] == ["swim"]
    assert v["questionnaire"]["tech"] == []                 # always on, not stored
    assert v["questionnaire"]["bags"] == {}
    assert v["packing_style"] == "balanced"


def test_acting_for_someone_saves_their_answers_but_seeds_nothing(person):
    admin, h = person("admin", admin=True)
    kid = person("kid", account=False)
    client.post(f"{BASE}?for={kid}", headers=h, data=ANSWERS)
    with SessionLocal() as s:
        assert s.query(PackingProfile).filter_by(traveler_id=kid).count() == 1
        assert s.query(InventoryItem).filter_by(user_id=admin.id).count() == 0
        assert s.query(Bag).filter_by(user_id=admin.id).count() == 0


# ------------------------------------------------------------------ the inventory
def test_every_category_shows_even_when_empty(person):
    _, h = person("empty")
    page = client.get(f"{BASE}/inventory", headers=h).text
    for key, label in PACK_CAT_LABELS.items():
        assert f'id="cat-{key}"' in page and label in page
    assert "packing questionnaire" in page


def test_adding_by_hand_matches_the_catalogue(person):
    u, h = person("add")
    label = catalog.seed_items()["waterproof_jacket"]["label"]
    r = client.post(f"{BASE}/inventory/add", headers=h, follow_redirects=False,
                    data={"category": "clothing", "label": label.upper(), "quantity": "2"})
    assert r.status_code == 303 and r.headers["location"].endswith("/inventory#cat-clothing")
    client.post(f"{BASE}/inventory/add", headers=h,
                data={"category": "activity", "label": "Kite"})
    have = _inventory(u.id)
    assert have["waterproof_jacket"].quantity == 2
    assert have["Kite"].type_key is None
    assert client.post(f"{BASE}/inventory/add", headers=h,
                       data={"category": "nope", "label": "x"}).status_code == 400


def test_editing_and_deleting_an_item(person):
    u, h = person("edit")
    client.post(f"{BASE}/inventory/add", headers=h, data={"category": "tech", "label": "Kindle"})
    iid = _inventory(u.id)["Kindle"].id
    client.post(f"{BASE}/inventory/{iid}", headers=h,
                data={"label": "E-reader", "quantity": "1", "must_have": "1"})
    row = _inventory(u.id)["E-reader"]
    assert row.must_have is True
    page = client.get(f"{BASE}/inventory", headers=h).text
    assert 'value="E-reader"' in page
    client.post(f"{BASE}/inventory/{iid}", headers=h, data={"label": "E-reader", "quantity": "3"})
    assert _inventory(u.id)["E-reader"].must_have is False      # unticked
    client.post(f"{BASE}/inventory/{iid}/delete", headers=h)
    assert _inventory(u.id) == {}


def test_bags_are_added_renamed_and_retired(person):
    u, h = person("bags")
    client.post(f"{BASE}/bags/add", headers=h,
                data={"label": "Example 40L", "kind": "backpack", "capacity": "40L"})
    with SessionLocal() as s:
        bid = s.query(Bag).filter_by(user_id=u.id).one().id
    client.post(f"{BASE}/bags/{bid}", headers=h,
                data={"label": "Example 45L", "kind": "backpack", "capacity": ""})
    client.post(f"{BASE}/bags/{bid}/remove", headers=h)
    with SessionLocal() as s:
        b = s.get(Bag, bid)
        assert (b.label, b.capacity, b.enabled) == ("Example 45L", None, False)
    assert "Example 45L" not in client.get(f"{BASE}/inventory", headers=h).text


def test_rows_save_as_you_go(person):
    """The inventory rows have no Save button (only a noscript fallback): htmx
    posts a row when it changes and gets a small status back, not the page,
    so editing one row can't throw away edits in another."""
    u, h = person("asyougo")
    client.post(f"{BASE}/inventory/add", headers=h,
                data={"category": "gear", "label": "Example headlamp", "quantity": "1"})
    client.post(f"{BASE}/bags/add", headers=h, data={"label": "Example tote", "kind": "backpack"})
    iid = next(iter(_inventory(u.id).values())).id
    with SessionLocal() as s:
        bid = s.query(Bag).filter_by(user_id=u.id).one().id
    page = client.get(f"{BASE}/inventory", headers=h).text
    assert 'hx-trigger="change, submit"' in page
    assert page.count(">Save</button>") == page.count("<noscript><button")
    # Add, Delete and Remove swap only their own section, so the page doesn't
    # reload and jump to the section's anchor; the add fields keep focus.
    assert 'hx-target="#cat-gear" hx-select="#cat-gear"' in page
    assert 'hx-target="#bags" hx-select="#bags"' in page
    assert 'id="add-gear"' in page and 'id="add-bag"' in page and 'id="inv-total"' in page
    hx = {**h, "HX-Request": "true"}
    r = client.post(f"{BASE}/inventory/{iid}", headers=hx, follow_redirects=False,
                    data={"label": "Example headlamp", "quantity": "2", "must_have": "1"})
    assert r.status_code == 200 and "Saved ✓" in r.text and "<html" not in r.text
    r = client.post(f"{BASE}/bags/{bid}", headers=hx, follow_redirects=False,
                    data={"label": "", "kind": "backpack", "capacity": "20L"})
    assert "kept the name Example tote" in r.text
    with SessionLocal() as s:
        it, b = s.get(InventoryItem, iid), s.get(Bag, bid)
        assert (it.quantity, it.must_have) == (2, True)
        assert (b.label, b.capacity) == ("Example tote", "20L")
    # Scripts off: the same post goes back to the page.
    r = client.post(f"{BASE}/bags/{bid}", headers=h, follow_redirects=False,
                    data={"label": "Example tote", "kind": "backpack"})
    assert r.status_code == 303 and r.headers["location"].endswith("/inventory#bags")


# ------------------------------------------------------------------ templates
def test_a_new_template_pre_ticks_and_saves_what_is_ticked(person):
    u, h = person("tpl")
    client.post(BASE, headers=h, data={**ANSWERS, "clothing": ["swim"], "care": ["sun"]})
    have = _inventory(u.id)
    page = client.get(f"{BASE}/templates/new?type=beach", headers=h).text
    assert f'name="item" value="{have["swimwear"].id}" checked' in page
    assert f'name="item" value="{have["laptop"].id}" >' in page          # not beach
    with SessionLocal() as s:
        assert s.query(PackingTemplate).filter_by(user_id=u.id).count() == 0  # only proposed
    r = client.post(f"{BASE}/templates", headers=h, follow_redirects=False, data={
        "type": "beach", "name": "My beach bag", "item": [have["swimwear"].id],
        f"qty:{have['swimwear'].id}": "2", "add_label": "Example dive mask",
        "add_category": "activity"})
    assert r.status_code == 303
    with SessionLocal() as s:
        tpl = s.query(PackingTemplate).filter_by(user_id=u.id).one()
        lines = {l.inventory_item_id: l.quantity for l in
                 s.query(PackingTemplateItem).filter_by(template_id=tpl.id)}
    snorkel = _inventory(u.id)["Example dive mask"]           # joined the inventory
    assert lines == {have["swimwear"].id: 2, snorkel.id: None}
    # Saving goes back to the list and names what was saved; editing too.
    assert r.headers["location"].endswith(f"/templates?saved={tpl.id}")
    listing = client.get(r.headers["location"], headers=h).text
    assert "Saved “My beach bag” ✓" in listing and "· 2 items" in listing
    assert "built-in method" not in listing
    r = client.post(f"{BASE}/templates/{tpl.id}", headers=h, follow_redirects=False,
                    data={"name": "My beach bag", "item": [have["swimwear"].id, snorkel.id]})
    assert r.status_code == 303 and r.headers["location"].endswith(f"/templates?saved={tpl.id}")
    # Someone else's id names nothing.
    _, h2 = person("tpl-other")
    assert "Saved “" not in client.get(f"{BASE}/templates?saved={tpl.id}", headers=h2).text
    # One beach template each: it's no longer offered, and a second is refused.
    assert 'value="beach"' not in client.get(f"{BASE}/templates", headers=h).text
    client.post(f"{BASE}/templates", headers=h, data={"type": "beach", "name": "Again"})
    with SessionLocal() as s:
        assert s.query(PackingTemplate).filter_by(user_id=u.id).count() == 1
    client.post(f"{BASE}/templates/{tpl.id}/delete", headers=h)
    with SessionLocal() as s:
        assert s.query(PackingTemplate).filter_by(user_id=u.id).count() == 0
        assert s.query(PackingTemplateItem).filter_by(template_id=tpl.id).count() == 0
    assert "Example dive mask" in _inventory(u.id)            # the inventory stays


def test_the_keyword_field_belongs_to_add_your_own(person):
    """The new-template form's Keyword is only for "Add your own"; trips.js
    shows it (and makes it required) for that type alone."""
    u, h = person("kw")
    page = client.get(f"{BASE}/templates", headers=h).text
    assert '<option value="custom">Add your own</option>' in page
    assert 'data-show-for-type="custom"' in page and "Your own keyword" not in page
    js = (ROOT / "app/trips/static/trips.js").read_text()
    assert "#tpl-type" in js and "showForType" in js


def test_custom_templates_need_a_keyword_and_can_repeat(person):
    u, h = person("custom")
    r = client.post(f"{BASE}/templates", headers=h, data={"type": "custom", "name": "Work"})
    assert r.status_code == 400 and "needs a keyword" in r.text
    for kw in ("conference", "offsite"):
        client.post(f"{BASE}/templates", headers=h,
                    data={"type": "custom", "name": kw.title(), "keyword": kw})
    with SessionLocal() as s:
        assert sorted(t.keyword for t in s.query(PackingTemplate).filter_by(user_id=u.id)) \
            == ["conference", "offsite"]


# ------------------------------------------------------------------ nobody else's
def test_someone_else_cannot_see_or_change_any_of_it(person):
    me, mine = person("owner")
    _, theirs = person("other")
    client.post(BASE, headers=mine, data=ANSWERS)
    item = next(iter(_inventory(me.id).values()))
    with SessionLocal() as s:
        bag = s.query(Bag).filter_by(user_id=me.id).first()
    client.post(f"{BASE}/templates", headers=mine,
                data={"type": "hiking", "name": "Owner hikes", "item": [item.id]})
    with SessionLocal() as s:
        tpl = s.query(PackingTemplate).filter_by(user_id=me.id).one()

    assert f"/inventory/{item.id}" not in client.get(f"{BASE}/inventory", headers=theirs).text
    assert "Owner hikes" not in client.get(f"{BASE}/templates", headers=theirs).text
    for method, path, data in (
            ("post", f"{BASE}/inventory/{item.id}", {"label": "Taken", "quantity": "9"}),
            ("post", f"{BASE}/inventory/{item.id}/delete", {}),
            ("post", f"{BASE}/bags/{bag.id}", {"label": "Taken", "kind": "backpack"}),
            ("post", f"{BASE}/bags/{bag.id}/remove", {}),
            ("get", f"{BASE}/templates/{tpl.id}", None),
            ("post", f"{BASE}/templates/{tpl.id}", {"name": "Taken"}),
            ("post", f"{BASE}/templates/{tpl.id}/delete", {})):
        r = getattr(client, method)(path, headers=theirs, **({"data": data} if data else {}))
        assert r.status_code == 404, path
    with SessionLocal() as s:
        assert s.get(InventoryItem, item.id).label == item.label
        assert s.get(Bag, bag.id).enabled is True and s.get(Bag, bag.id).label == bag.label
        assert s.get(PackingTemplate, tpl.id).name == "Owner hikes"
    # Ticking someone else's item id in your own template is ignored.
    client.post(f"{BASE}/templates", headers=theirs,
                data={"type": "hiking", "name": "Mine", "item": [item.id]})
    with SessionLocal() as s:
        other_tpl = s.query(PackingTemplate).filter_by(name="Mine").one()
        assert s.query(PackingTemplateItem).filter_by(template_id=other_tpl.id).count() == 0


def test_signed_out_gets_the_sign_in_page_not_the_inventory():
    r = client.get(f"{BASE}/inventory", follow_redirects=False)
    assert r.status_code in (302, 303, 401)
