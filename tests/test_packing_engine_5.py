"""The 5.0 packing list builder (SPEC-5 §6.7–6.8, D-230, D-231, YONDER-62).

A list draws only from the person's inventory: templates the trip suggests add
all their items, the engine's needs resolve to owned items or become
"Suggested — not in your inventory", must-haves always go, quantities are
capped by what's owned. Without a saved questionnaire, generating is a 409.
"""
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest                                               # noqa: E402

from planning import catalog                                # noqa: E402
from planning.assemble import assemble, suggested_types     # noqa: E402
from planning.context import PlanningContext, flight_hours  # noqa: E402

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, Bag, InventoryItem, PackingItem, PackingPlan,
                            PackingProfile, PackingTemplate, PackingTemplateItem,
                            TravelerIdentity, Trip, TripActivity, TripTraveler, User)
    from planning.runner import (PROFILE_REQUIRED, PackingProfileRequired,
                                 answer_questionnaire, generate_plan)
    Base.metadata.create_all(engine)
    client = TestClient(app)


# ------------------------------------------------------------------ pure
def _ctx(**kw) -> PlanningContext:
    c = PlanningContext(duration_days=kw.pop("days", 5))
    c.has_weather = True
    c.profile = kw.pop("profile", {"packing_style": "balanced", "laundry_comfort": "none"})
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _inv(type_key, qty=1, must=False, label=None):
    it = catalog.seed_items().get(type_key) or {}
    return {"id": f"inv-{type_key}", "type_key": type_key,
            "label": label or it.get("label", type_key),
            "category": it.get("category", "activity"), "tags": it.get("tags", []),
            "quantity": qty, "must_have": must}


def test_needs_resolve_to_owned_items_or_are_suggested():
    inv = [_inv("passport"), _inv("socks", qty=10)]
    res = assemble(_ctx(), catalog.seed_items(), inventory=inv, templates=[])
    owned = {li.inventory_item_id: li for li in res.lines}
    assert set(owned) == {"inv-passport", "inv-socks"}
    assert owned["inv-passport"].reason == "travel essential"
    assert owned["inv-socks"].quantity == 6              # 5 days + 1, not capped
    suggested = {s.item_id for s in res.suggestions}
    assert "tickets" in suggested and "passport" not in suggested
    assert all(s.status == "suggested" and s.inventory_item_id is None
               for s in res.suggestions)
    assert {li.item_key for li in res.lines} == {"inv:inv-passport", "inv:inv-socks"}
    assert all(s.item_key == f"suggest:{s.item_id}" for s in res.suggestions)


def test_a_need_matches_an_owned_item_by_name_not_by_tag():
    # Renamed-by-hand "Passport" (no type_key) still counts; a "launderable"
    # pair of underwear is not socks.
    inv = [{**_inv("passport"), "type_key": None, "id": "mine"}, _inv("underwear", qty=9)]
    res = assemble(_ctx(), catalog.seed_items(), inventory=inv, templates=[])
    assert "mine" in {li.inventory_item_id for li in res.lines}
    assert "socks" in {s.item_id for s in res.suggestions}


def test_quantities_are_capped_by_what_you_own_with_a_warning():
    res = assemble(_ctx(days=6), catalog.seed_items(), inventory=[_inv("socks", qty=5)],
                   templates=[])
    socks = next(li for li in res.lines if li.inventory_item_id == "inv-socks")
    assert socks.quantity == 5
    assert "7 × Socks needed, you have 5." in res.warnings


def test_must_haves_always_go():
    inv = [_inv("headlamp", must=True)]
    res = assemble(_ctx(), catalog.seed_items(), inventory=inv, templates=[])
    lamp = next(li for li in res.lines if li.inventory_item_id == "inv-headlamp")
    assert lamp.reason == "your must-have" and lamp.quantity == 1


def test_suggested_templates_add_all_their_items_merged_one_line_each():
    inv = [_inv("hiking_boots"), _inv("trekking_poles"), _inv("water_bottle", qty=3)]
    hike = {"id": "t1", "name": "Example hills", "type_key": "hiking", "keyword": None,
            "lines": [{"inventory_item_id": "inv-hiking_boots", "quantity": None},
                      {"inventory_item_id": "inv-water_bottle", "quantity": 2}]}
    beach = {"id": "t2", "name": "Example coast", "type_key": "beach", "keyword": None,
             "lines": [{"inventory_item_id": "inv-trekking_poles", "quantity": None}]}
    res = assemble(_ctx(activity_tags={"hiking"}), catalog.seed_items(), inventory=inv,
                   templates=[hike, beach])
    assert [t["id"] for t in res.templates] == ["t1"]          # beach isn't suggested
    lines = {li.inventory_item_id: li for li in res.lines}
    assert lines["inv-water_bottle"].quantity == 2
    # Hiking boots: from the template AND the hiking modifier — one line, both reasons.
    boots = lines["inv-hiking_boots"]
    assert "from your Example hills template" in boots.reason and "hiking" in boots.reason
    assert sum(1 for li in res.lines if li.inventory_item_id == "inv-hiking_boots") == 1
    # The person's own pick wins over the suggestion.
    res = assemble(_ctx(activity_tags={"hiking"}), catalog.seed_items(), inventory=inv,
                   templates=[hike, beach], chosen=["t2"])
    assert [t["id"] for t in res.templates] == ["t2"]


def test_template_types_follow_the_trip_signals():
    assert "long_flight" in suggested_types(_ctx(long_flight=True), set(), set())
    assert "long_flight" not in suggested_types(_ctx(), set(), set())
    assert "religious_sites" in suggested_types(_ctx(culture={"modest": True}), set(), set())
    assert {"beach", "swimming"} <= suggested_types(_ctx(), {"beach"}, {"beach"})
    assert "fine_dining" not in suggested_types(_ctx(), {"beach", "business"},
                                                {"beach", "business", "hiking"})


def test_a_custom_template_applies_when_its_keyword_is_in_the_trip():
    inv = [_inv("laptop")]
    conf = {"id": "c1", "name": "Example conference kit", "type_key": "custom",
            "keyword": "Summit", "lines": [{"inventory_item_id": "inv-laptop", "quantity": None}]}
    res = assemble(_ctx(text="example data summit 2031"), catalog.seed_items(),
                   inventory=inv, templates=[conf])
    assert [t["id"] for t in res.templates] == ["c1"]
    res = assemble(_ctx(text="a week by the lake"), catalog.seed_items(),
                   inventory=inv, templates=[conf])
    assert res.templates == []


def test_suggestions_use_the_questionnaire_clothing_style():
    """A suggested item with style variants is labelled in the style answered
    in the questionnaire (Q1, D-229)."""
    prof = {"packing_style": "balanced", "questionnaire": {"style": "masc"}}
    res = assemble(_ctx(profile=prof, activity_tags={"business"}), catalog.seed_items(),
                   inventory=[], templates=[])
    styled = [s for s in res.suggestions
              if (catalog.seed_items()[s.item_id].get("variants") or {}).get("masc")]
    assert styled
    assert all(s.label == catalog.seed_items()[s.item_id]["variants"]["masc"] for s in styled)


def test_flight_hours_from_times_route_or_distance():
    def act(values=None, geo=None, starts_at=None):
        return SimpleNamespace(values=values or {}, geo=geo, starts_at=starts_at)
    assert flight_hours(act({"when": "2031-03-01T10:00:00+00:00",
                             "endWhen": "2031-03-01T18:30:00+00:00"})) == 8.5
    assert flight_hours(act({"when": "2031-03-01T10:00", "endWhen": "2031-03-01T18:30"})) is None
    far = {"points": [{"lat": 43.68, "lon": -79.63}, {"lat": 51.47, "lon": -0.45}]}
    assert flight_hours(act(geo=far)) > 6                 # ~5,700 km
    near = {"points": [{"lat": 43.68, "lon": -79.63}, {"lat": 45.32, "lon": -75.67}]}
    assert flight_hours(act(geo=near)) < 1


# ------------------------------------------------------------------ the app
pytestmark_app = pytest.mark.skipif(not DEPS, reason="needs the app")


def _airports(code):
    t = {"YYZ": ("CA", "Toronto", 43.68, -79.63), "GVA": ("CH", "Geneva", 46.23, 6.11)}
    h = t.get((code or "").upper())
    return None if h is None else SimpleNamespace(
        country=h[0], municipality=h[1], lat=h[2], lon=h[3], icao=None)


class _NoGeo:
    def geocode_place(self, p):
        return None

    def geocode_freeform(self, text):
        return None


ANSWERS = {"style": "neutral", "footwear": "minimal", "clothing": [], "care": [],
           "tech": [], "gear": [], "bags": {}}


@pytest.fixture
def world(monkeypatch):
    """Two signed-in people on one invented trip; neither has answered yet."""
    monkeypatch.setattr(settings, "auth_required", True)
    import app.trips.routes as tr
    monkeypatch.setattr(tr, "_airport_resolver", lambda: _airports)
    monkeypatch.setattr(tr, "_photon", lambda: _NoGeo())
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as s:
        ta = TravelerIdentity(display_name=f"Example Ada {tag}", kind="guest")
        tb = TravelerIdentity(display_name=f"Example Ben {tag}", kind="guest")
        s.add_all([ta, tb]); s.flush()
        a = User(username=f"pk5-ada-{tag}", auth_source="local", traveler_id=ta.id)
        b = User(username=f"pk5-ben-{tag}", auth_source="local", traveler_id=tb.id)
        s.add_all([a, b]); s.flush()
        trip = Trip(name="Example lake week", source="manual", owner_id=a.id,
                    start_date="2031-06-02", end_date="2031-06-07")
        s.add(trip); s.flush()
        s.add_all([TripTraveler(trip_id=trip.id, traveler_id=ta.id, role="owner"),
                   TripTraveler(trip_id=trip.id, traveler_id=tb.id, role="companion"),
                   TripActivity(trip_id=trip.id, type="flight",
                                values={"from": "YYZ", "to": "GVA"})])
        s.commit()
        ids = {"a": a.id, "b": b.id, "ta": ta.id, "tb": tb.id, "trip": trip.id,
               "ada": ta.display_name}
    yield ids
    with SessionLocal() as s:
        uids = [ids["a"], ids["b"]]
        s.query(PackingItem).filter(PackingItem.trip_id == ids["trip"]).delete()
        s.query(PackingPlan).filter(PackingPlan.trip_id == ids["trip"]).delete()
        tpls = [t.id for t in s.query(PackingTemplate).filter(PackingTemplate.user_id.in_(uids))]
        s.query(PackingTemplateItem).filter(PackingTemplateItem.template_id.in_(tpls)).delete(
            synchronize_session=False)
        for m in (PackingTemplate, InventoryItem, Bag):
            s.query(m).filter(m.user_id.in_(uids)).delete(synchronize_session=False)
        s.query(PackingProfile).filter(PackingProfile.traveler_id.in_(
            [ids["ta"], ids["tb"]])).delete(synchronize_session=False)
        s.query(TripActivity).filter(TripActivity.trip_id == ids["trip"]).delete()
        s.query(TripTraveler).filter(TripTraveler.trip_id == ids["trip"]).delete()
        s.query(Trip).filter(Trip.id == ids["trip"]).delete()
        s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
        s.query(TravelerIdentity).filter(TravelerIdentity.id.in_(
            [ids["ta"], ids["tb"]])).delete(synchronize_session=False)
        s.commit()


def _h(uid):
    with SessionLocal() as s:
        return {"Authorization": f"Bearer {authn.issue_access_token(s.get(User, uid))}"}


def _answer(ids, who="a"):
    with SessionLocal() as s:
        answer_questionnaire(s, user_id=ids[who], traveler_id=ids["t" + who], answers=ANSWERS)
        s.commit()


@pytestmark_app
def test_generating_without_the_questionnaire_is_refused(world):
    with SessionLocal() as s:
        with pytest.raises(PackingProfileRequired):
            generate_plan(s, s.get(Trip, world["trip"]), resolve_airport=_airports,
                          geocoder=_NoGeo(), user_id=world["a"], traveler_id=world["ta"])
        assert s.query(PackingPlan).filter_by(trip_id=world["trip"]).count() == 0


@pytestmark_app
def test_the_api_409_keeps_detail_a_string(world, monkeypatch):
    """The iOS and Android apps decode `detail` as a string and show it, so
    today's builds handle the 409 unchanged (SPEC-5 §6.8)."""
    from app import api_v1
    from app.main import _packing_profile_required
    import asyncio
    with SessionLocal() as s:
        with pytest.raises(PackingProfileRequired) as e:
            api_v1.api_generate_packing(world["trip"], user=s.get(User, world["a"]), session=s)
    r = asyncio.run(_packing_profile_required(None, e.value))
    import json
    assert r.status_code == 409
    assert json.loads(r.body) == {
        "detail": "Set up your packing profile first: answer the questions in "
                  "Settings → Packing profile.",
        "code": "packing_profile_required", "url": "/trips/settings/packing-profile"}
    assert PROFILE_REQUIRED == json.loads(r.body)["detail"]


@pytestmark_app
def test_the_trip_page_says_so_and_links_the_questionnaire(world, monkeypatch):
    h = _h(world["a"])
    page = client.get(f"/trips/{world['trip']}", headers=h).text
    assert "Set up your packing profile first" in page
    assert 'href="/trips/settings/packing-profile">Set up packing profile</a>' in page
    assert f'hx-post="/trips/{world["trip"]}/pack"' not in page
    client.post(f"/trips/{world['trip']}/pack", headers=h)            # refused quietly
    with SessionLocal() as s:
        assert s.query(PackingPlan).filter_by(trip_id=world["trip"]).count() == 0


@pytestmark_app
def test_a_list_is_one_persons_and_says_whose(world, monkeypatch):
    _answer(world, "a")
    _answer(world, "b")
    with SessionLocal() as s:                  # Ben owns something Ada doesn't
        s.add(InventoryItem(user_id=world["b"], type_key="headlamp", label="Example lamp",
                            category="tech", quantity=1, must_have=True))
        s.commit()
    h = _h(world["a"])
    client.post(f"/trips/{world['trip']}/pack", headers=h)
    page = client.get(f"/trips/{world['trip']}", headers=h).text
    assert f"Packing list for {world['ada']}" in page
    assert "Example lamp" not in page
    with SessionLocal() as s:
        mine = s.query(InventoryItem.id).filter(InventoryItem.user_id == world["a"])
        rows = s.query(PackingItem).filter_by(trip_id=world["trip"]).all()
        assert rows and all(r.user_id == world["a"] for r in rows)
        assert all(r.inventory_item_id in {i for (i,) in mine}
                   for r in rows if r.inventory_item_id)


@pytestmark_app
def test_add_to_inventory_turns_a_suggestion_into_an_owned_line(world, monkeypatch):
    _answer(world, "a")
    h = _h(world["a"])
    client.post(f"/trips/{world['trip']}/pack", headers=h)
    with SessionLocal() as s:
        sug = s.query(PackingItem).filter_by(trip_id=world["trip"], status="suggested").all()
        assert sug                              # the minimal answers don't cover everything
        target = sug[0]
        sid, key, label = target.id, target.item_key, target.label
    page = client.get(f"/trips/{world['trip']}", headers=h).text
    assert "Suggested — not in your inventory" in page and "Add to inventory" in page
    r = client.post(f"/trips/{world['trip']}/pack-item/{sid}/own", headers=h)
    assert r.status_code == 200
    with SessionLocal() as s:
        line = s.get(PackingItem, sid)
        inv = s.get(InventoryItem, line.inventory_item_id)
        assert line.status == "draft" and line.item_key == f"inv:{inv.id}"
        assert inv.user_id == world["a"] and inv.type_key == key.split(":", 1)[1]
        assert inv.label == label
    client.post(f"/trips/{world['trip']}/pack", headers=h)         # re-generate: still one line
    with SessionLocal() as s:
        assert s.query(PackingItem).filter_by(trip_id=world["trip"],
                                              inventory_item_id=inv.id).count() == 1
        assert s.query(PackingItem).filter_by(trip_id=world["trip"], item_key=key).count() == 0
    # Someone else can't own Ada's suggestion.
    with SessionLocal() as s:
        other = s.query(PackingItem).filter_by(trip_id=world["trip"], status="suggested").first()
    if other is not None:
        h = _h(world["b"])
        r = client.post(f"/trips/{world['trip']}/pack-item/{other.id}/own", headers=h)
        assert r.status_code == 404


@pytestmark_app
def test_the_api_list_leaves_suggestions_out(world):
    _answer(world, "a")
    from app import api_v1
    with SessionLocal() as s:
        out = api_v1.api_generate_packing(world["trip"], user=s.get(User, world["a"]), session=s)
    assert out.items and all(i.status in ("draft", "packed") for i in out.items)


@pytestmark_app
def test_someone_elses_template_cant_be_put_on_my_list(world, monkeypatch):
    _answer(world, "a")
    _answer(world, "b")
    with SessionLocal() as s:
        t = PackingTemplate(user_id=world["b"], type_key="beach", name="Ben's example beach")
        s.add(t); s.commit()
        tid = t.id
    h = _h(world["a"])
    client.post(f"/trips/{world['trip']}/pack", headers=h)
    client.post(f"/trips/{world['trip']}/pack/templates/add", headers=h, data={"template_id": tid})
    with SessionLocal() as s:
        plan = (s.query(PackingPlan).filter_by(trip_id=world["trip"], user_id=world["a"])
                .order_by(PackingPlan.generated_at.desc()).first())
        assert tid not in (plan.manual_archetypes or [])
    assert "Ben&#39;s example beach" not in client.get(f"/trips/{world['trip']}", headers=h).text
