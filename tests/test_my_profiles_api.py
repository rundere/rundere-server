"""5.1, YONDER-408: the apps view and change your own discovery and packing
profiles (`/api/v1/me/discovery-profile…`, `/api/v1/me/packing-profile`).

Always the caller's own (D-211: no acting for someone over the API); the web
form and the API store exactly the same thing; reading the editor and writing it
back changes nothing. Fictional people; SQLite via conftest.
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
    from app.models import (Bag, Base, DiscoveryProfile, InventoryItem, PackingProfile,
                            PackingTemplate, TravelerIdentity, User)

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

DP = "/api/v1/me/discovery-profile"
PP = "/api/v1/me/packing-profile"
QUESTIONNAIRE = {"style": "neutral", "footwear": "minimal", "care": ["sun"],
                 "tech": ["laptop"], "gear": ["cubes"], "bags": {"daypack": 1}}


@pytest.fixture(autouse=True)
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    yield


@pytest.fixture
def person():
    """People with (or without) a traveller of their own, swept after."""
    made = []

    def make(name, traveller=True):
        with SessionLocal() as s:
            t = None
            if traveller:
                t = TravelerIdentity(display_name=f"MP {name}", kind="guest")
                s.add(t)
                s.flush()
            u = User(username=f"mp-{name}-{uuid.uuid4().hex[:6]}", auth_source="local",
                     traveler_id=t.id if t else None)
            s.add(u)
            s.commit()
            made.append((u.id, t.id if t else None))
            s.refresh(u)
            s.expunge(u)
            return u, {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    yield make
    with SessionLocal() as s:
        uids = [u for u, _ in made]
        tids = [t for _, t in made if t]
        for model in (InventoryItem, PackingTemplate, Bag):
            s.query(model).filter(model.user_id.in_(uids)).delete(synchronize_session=False)
        for model in (PackingProfile, DiscoveryProfile):
            s.query(model).filter(model.traveler_id.in_(tids)).delete(synchronize_session=False)
        s.query(User).filter(User.id.in_(uids)).delete(synchronize_session=False)
        s.query(TravelerIdentity).filter(TravelerIdentity.id.in_(tids)).delete(
            synchronize_session=False)
        s.commit()


def _stored(model, traveler_id):
    with SessionLocal() as s:
        row = s.query(model).filter_by(traveler_id=traveler_id).first()
        return dict(row.values) if row else None


# ------------------------------------------------------------------ discovery profile
def test_the_editor_offers_every_choice_and_a_new_profile_ticks_every_activity(person):
    _, h = person("new")
    r = client.get(f"{DP}/editor", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["exists"] is False
    opts = body["options"]
    assert {o["key"] for o in opts["mobility"]} >= {"no_limits", "wheelchair"}
    assert [b["tier"] for b in opts["budget"]] == [0, 1, 2, 3, 4]
    assert opts["cuisine_groups"] and opts["dietary_custom"] is True
    assert body["values"]["activity"]["interests"] == [o["key"] for o in opts["activities"]]


def test_patch_changes_only_what_is_sent_and_stores_what_the_web_would(person):
    u, h = person("patch")
    r = client.patch(DP, headers=h, json={
        "food": {"dietary_restrictions": ["vegetarian", "mp-no-mushrooms"],
                 "cuisine_likes": ["thai"], "budget_ceiling": 2},
        "practical": {"mobility": "moderate"}, "notes": "mp-likes quiet cafés"})
    assert r.status_code == 200, r.text
    assert r.json()["exists"] is True
    r = client.patch(DP, headers=h, json={"food": {"allergies": "mp-peanuts"}})
    v = _stored(DiscoveryProfile, u.traveler_id)
    assert v["food"]["allergies"] == "mp-peanuts"
    assert v["food"]["dietary_restrictions"] == ["vegetarian", "mp-no-mushrooms"]
    assert v["food"]["cuisine_likes"] == ["thai"] and v["food"]["budget_ceiling"] == 2
    assert v["practical"]["mobility"] == "moderate" and v["notes"] == "mp-likes quiet cafés"

    # The web form, given the same choices, stores exactly the same blob.
    from app.trips.routes import discovery_values
    assert v == discovery_values(
        dietary=["vegetarian", "mp-no-mushrooms"], likes=["thai"], budget=2,
        allergies="mp-peanuts", mobility="moderate", notes="mp-likes quiet cafés",
        activities=v["activity"]["interests"])


def test_reading_the_editor_and_writing_it_back_changes_nothing(person):
    u, h = person("round")
    client.patch(DP, headers=h, json={
        "food": {"cuisine_dislikes": ["thai"], "prefer_local": True},
        "drink": {"interests": ["coffee"]}})
    before = _stored(DiscoveryProfile, u.traveler_id)
    values = client.get(f"{DP}/editor", headers=h).json()["values"]
    assert client.put(DP, headers=h, json=values).status_code == 200
    assert client.patch(DP, headers=h, json=values).status_code == 200
    assert _stored(DiscoveryProfile, u.traveler_id) == before


def test_put_replaces_the_whole_profile(person):
    u, h = person("put")
    client.patch(DP, headers=h, json={"food": {"allergies": "mp-shellfish"},
                                      "notes": "mp-note"})
    client.put(DP, headers=h, json={"food": {"budget_ceiling": 1}})
    v = _stored(DiscoveryProfile, u.traveler_id)
    assert v["food"]["budget_ceiling"] == 1
    assert v["food"]["allergies"] == "" and v["notes"] == ""


@pytest.mark.parametrize("body", [
    {"drink": {"interests": ["mp-not-a-drink"]}},
    {"activity": {"interests": ["mp-not-an-activity"]}},
    {"food": {"cuisine_likes": ["mp-not-a-cuisine"]}},
    {"food": {"cuisine_likes": ["thai"], "cuisine_dislikes": ["thai"]}},
    {"food": {"budget_ceiling": 7}},
    {"practical": {"mobility": "mp-teleport"}},
])
def test_an_unknown_choice_is_refused_and_nothing_is_saved(person, body):
    u, h = person("bad")
    assert client.patch(DP, headers=h, json=body).status_code == 422
    assert _stored(DiscoveryProfile, u.traveler_id) is None


def test_its_always_your_own_and_for_is_ignored(person):
    ana, ha = person("ana")
    ben, hb = person("ben")
    client.patch(DP, headers=hb, json={"food": {"allergies": "mp-ben-allergy"}})
    # Ana names Ben's traveller with ?for=: it's ignored, she writes her own.
    r = client.patch(f"{DP}?for={ben.traveler_id}", headers=ha,
                     json={"food": {"allergies": "mp-ana-allergy"}})
    assert r.status_code == 200
    assert _stored(DiscoveryProfile, ana.traveler_id)["food"]["allergies"] == "mp-ana-allergy"
    assert _stored(DiscoveryProfile, ben.traveler_id)["food"]["allergies"] == "mp-ben-allergy"
    ed = client.get(f"{DP}/editor?for={ben.traveler_id}", headers=ha).json()
    assert ed["values"]["food"]["allergies"] == "mp-ana-allergy"


def test_no_traveller_is_refused_and_writes_nothing(person):
    _, h = person("solo", traveller=False)
    for method, path, body in (("get", f"{DP}/editor", None), ("patch", DP, {"notes": "x"}),
                               ("put", DP, {}), ("get", PP, None),
                               ("put", PP, {"questionnaire": QUESTIONNAIRE}),
                               ("patch", PP, {"packing_style": "minimalist"})):
        r = client.request(method, path, headers=h, json=body)
        assert r.status_code == 409, (method, path, r.status_code)
    with SessionLocal() as s:
        assert s.query(DiscoveryProfile).filter(
            DiscoveryProfile.values["notes"].as_string() == "x").count() == 0


def test_signed_out_is_401():
    for method, path in (("get", f"{DP}/editor"), ("patch", DP), ("get", PP), ("put", PP)):
        assert client.request(method, path, json={}).status_code == 401


# ------------------------------------------------------------------ packing profile
def test_put_saves_the_questionnaire_and_seeds_only_your_inventory(person):
    ana, ha = person("pana")
    ben, hb = person("pben")
    r = client.put(PP, headers=hb, json={"questionnaire": QUESTIONNAIRE})
    ben_inventory = r.json()["counts"]["inventory"]
    r = client.put(PP, headers=ha, json={"questionnaire": QUESTIONNAIRE,
                                         "packing_style": "minimalist"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answered"] and body["inventory_added"] > 0 and body["bags_added"] == 1
    assert body["questionnaire"]["style"] == "neutral"
    assert body["questionnaire"]["gear"] == ["cubes", "daypack"]
    assert body["preferences"]["packing_style"] == "minimalist"
    assert body["preferences"]["laundry_comfort"] == "none"          # the default
    # Ben's inventory is untouched by Ana's save.
    assert client.get(PP, headers=hb).json()["counts"]["inventory"] == ben_inventory
    # Saving again is add-only: nothing new.
    again = client.put(PP, headers=ha, json={"questionnaire": QUESTIONNAIRE}).json()
    assert again["inventory_added"] == 0 and again["bags_added"] == 0
    assert again["preferences"]["packing_style"] == "minimalist"     # kept when left out


def test_the_web_and_the_api_store_the_same_answers(person):
    from app.trips.packing_settings import questionnaire_answers
    u, h = person("same")
    client.put(PP, headers=h, json={"questionnaire": QUESTIONNAIRE})
    stored = _stored(PackingProfile, u.traveler_id)["questionnaire"]
    assert stored == questionnaire_answers(**QUESTIONNAIRE)


def test_patch_changes_preferences_without_touching_the_questionnaire(person):
    u, h = person("prefs")
    client.put(PP, headers=h, json={"questionnaire": QUESTIONNAIRE})
    before = _stored(PackingProfile, u.traveler_id)
    r = client.patch(PP, headers=h, json={"climate_preference": "runs_cold"})
    assert r.status_code == 200
    after = _stored(PackingProfile, u.traveler_id)
    assert after["climate_preference"] == "runs_cold"
    assert after["questionnaire"] == before["questionnaire"]
    assert after["questionnaire_saved_at"] == before["questionnaire_saved_at"]
    # The questionnaire goes with PUT; PATCH refuses it.
    assert client.patch(PP, headers=h, json={"questionnaire": QUESTIONNAIRE}
                        ).status_code == 422


@pytest.mark.parametrize("body", [
    {"questionnaire": {**QUESTIONNAIRE, "style": "mp-nope"}},
    {"questionnaire": {**QUESTIONNAIRE, "tech": ["mp-jetpack"]}},
    {"questionnaire": {**QUESTIONNAIRE, "bags": {"mp-trunk": 1}}},
    {"questionnaire": QUESTIONNAIRE, "packing_style": "mp-chaos"},
])
def test_a_bad_packing_answer_is_refused(person, body):
    u, h = person("pbad")
    assert client.put(PP, headers=h, json=body).status_code == 422
    assert _stored(PackingProfile, u.traveler_id) is None


def test_the_packing_profile_offers_the_questions(person):
    _, h = person("popts")
    body = client.get(PP, headers=h).json()
    assert body["answered"] is False
    keys = [q["key"] for q in body["options"]["questions"]]
    assert keys == ["style", "clothing", "footwear", "care", "gear", "tech"]
    assert [p["key"] for p in body["options"]["preferences"]] == [
        "laundry_comfort", "packing_style", "climate_preference"]
