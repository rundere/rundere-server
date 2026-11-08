"""Cuisines as a vocabulary, and the Discovery profile layout (YONDER-328).

The OpenStreetMap cuisine list with Like / Neutral / Don't like; the 0049
conversion of the old free-text dislikes; likes adding food searches; "every
activity ticked" meaning the usual mix; and each person's cuisines staying
their own.
"""
import importlib.util
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest                                               # noqa: E402

from discover import cuisines                               # noqa: E402

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
    from app.models import Base, DiscoveryProfile, TravelerIdentity, User
    from app.trips import routes as trips_routes

    Base.metadata.create_all(engine)
    client = TestClient(app)


def _migration():
    path = ROOT / "alembic" / "versions" / "0049_cuisine_vocabulary.py"
    spec = importlib.util.spec_from_file_location("m0049", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ vocabulary
def test_the_vocabulary_is_keyed_and_unique():
    keys = [c["key"] for g in cuisines.groups() for c in g["cuisines"]]
    assert len(keys) == len(set(keys)) >= 40
    assert cuisines.label("steak_house") == "Steakhouse"
    assert cuisines.label("something typed by hand") == "something typed by hand"


@pytest.mark.parametrize("text, key", [
    ("Sushi", "sushi"), ("  fast food ", "fast_food"), ("BBQ", "barbecue"),
    ("Mexican food", "mexican"), ("steakhouses", "steak_house"), ("spicy things", None),
])
def test_free_text_matches_a_key(text, key):
    assert cuisines.match(text) == key


# ------------------------------------------------------------------ 0049
def test_old_dislikes_become_keys_and_the_rest_moves_to_notes():
    m = _migration()
    out = m.convert({"food": {"cuisine_dislikes": ["Sushi", "fast food", "anything too spicy"],
                              "allergies": "peanuts"},
                     "notes": "Love family-run places."})
    assert out["food"]["cuisine_dislikes"] == ["sushi", "fast_food"]
    assert out["food"]["allergies"] == "peanuts"
    assert out["notes"] == ("Love family-run places.\n"
                            "Cuisines to avoid (from before 5.0): anything too spicy")
    assert m.convert(out) is None                           # safe to re-run


def test_a_profile_with_nothing_to_convert_is_left_alone():
    m = _migration()
    assert m.convert({}) is None
    assert m.convert({"food": {"cuisine_dislikes": ["sushi"]}}) is None


def test_moved_notes_stay_within_the_limit():
    m = _migration()
    out = m.convert({"food": {"cuisine_dislikes": ["x" * 50]}, "notes": "n" * 1990})
    assert len(out["notes"]) == m.NOTES_MAX


# ------------------------------------------------------------------ the resolvers
def test_likes_add_searches_capped_and_reach_the_prompt():
    from discover.places import DerivedPlace
    from discover.resolvers.food import FoodResolver
    from discover.search import SearchResult
    r = FoodResolver(searxng_url="http://searxng.test", llm=None)
    profile = {"food": {"cuisine_likes": ["thai", "peruvian", "tapas", "ramen"],
                        "cuisine_dislikes": ["fast_food"]}}
    qs = r._queries("Testville", None, profile)
    assert "best thai restaurant Testville" in qs and "best tapas restaurant Testville" in qs
    assert not any("ramen" in q for q in qs)                # three at most
    prompt = r._prompt(DerivedPlace(country="CA", city="Testville", lat=0, lon=0), profile,
                       [SearchResult(url="https://example.test/a", title="A", snippet="")])
    assert "Avoid cuisines: Fast food" in prompt
    assert "they like" in prompt and "Thai, Peruvian, Tapas, Ramen" in prompt


def test_prefer_local_food_searches_it_and_puts_it_ahead_of_dislikes():
    from discover.places import DerivedPlace
    from discover.resolvers.food import FoodResolver
    from discover.search import SearchResult
    r = FoodResolver(searxng_url="http://searxng.test", llm=None)
    place = DerivedPlace(country="CA", city="Testville", lat=0, lon=0)
    hits = [SearchResult(url="https://example.test/a", title="A", snippet="")]
    off = {"food": {"cuisine_dislikes": ["seafood"]}}
    on = {"food": {"cuisine_dislikes": ["seafood"], "prefer_local": True}}
    assert not any("traditional" in q for q in r._queries("Testville", "CA", off))
    assert "traditional food Testville CA where locals eat" in r._queries("Testville", "CA", on)
    assert "prefer local food" not in r._prompt(place, off, hits)
    prompt = r._prompt(place, on, hits)
    assert "They prefer local food" in prompt and "even when that cuisine is on the avoid list" in prompt


def test_a_closed_cuisine_dialog_is_hidden():
    # .dialog sets display:flex, which beats the browser's own hiding of a closed
    # <dialog>: without this rule Done left it drawn on the page.
    css = (ROOT / "app" / "trips" / "static" / "trips.css").read_text()
    assert "dialog.dialog:not([open]) { display: none; }" in css


def test_every_activity_ticked_searches_the_usual_mix(monkeypatch):
    from ai.client import Endpoint
    from discover.places import DerivedPlace
    from discover.resolvers import events as events_mod
    from discover.resolvers.base import ResolverContext
    seen = []
    r = events_mod.EventsResolver(searxng_url="http://searxng.test",
                                  llm=Endpoint(provider="ollama", url="http://o.test", model="m"))
    monkeypatch.setattr(r, "_extract", lambda place, s, e, interests, qs: seen.append(interests))
    place = DerivedPlace(country="CA", city="Testville", lat=0, lon=0)
    for interests in (list(events_mod._ACTIVITY_PHRASES), ["museums"]):
        r.resolve(place, ResolverContext(start_date="2031-05-10", end_date="2031-05-14",
                                         today="2031-01-01",
                                         discovery_profile={"activity": {"interests": interests}}))
    assert seen[0] == [] and seen[-1] == ["museums"]


@pytest.mark.skipif(not DEPS, reason="needs the app")
def test_effective_activities():
    every = [k for k, _ in trips_routes.DISCOVERY_ACTIVITIES]
    assert trips_routes.effective_activities(every) == []
    assert trips_routes.effective_activities(["museums", "nope"]) == ["museums"]


# ------------------------------------------------------------------ the page and the API
if DEPS:
    _made: list[tuple[str, int]] = []

    @pytest.fixture(autouse=True)
    def _cleanup():
        """Remove the people a test made: stray traveller identities change what
        other tests' resolver sees."""
        yield
        with SessionLocal() as s:
            for uid, tid in _made:
                s.query(DiscoveryProfile).filter_by(traveler_id=tid).delete()
                s.query(User).filter_by(id=uid).delete()
                s.query(TravelerIdentity).filter_by(id=tid).delete()
            s.commit()
        _made.clear()

    def _person(name):
        with SessionLocal() as s:
            t = TravelerIdentity(display_name=f"CU {name}", kind="guest")
            s.add(t)
            s.flush()
            u = User(username=f"cu-{name}-{uuid.uuid4().hex[:6]}", auth_source="local",
                     traveler_id=t.id)
            s.add(u)
            s.commit()
            _made.append((u.id, t.id))
            return u, {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    @pytest.fixture
    def auth_on(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        yield

    def test_a_new_profile_starts_with_every_activity_ticked_and_no_drinks(auth_on):
        _, h = _person("new")
        page = client.get("/trips/settings/discovery-profile", headers=h).text
        for k, _ in trips_routes.DISCOVERY_ACTIVITIES:
            assert f'name="activity_interests" value="{k}" checked>' in page
        for k, _ in trips_routes.DISCOVERY_DRINKS:
            assert f'name="drink_interests" value="{k}" >' in page   # unticked
        # Activities come first, then drinks, then the food section.
        assert page.index("Activity interests") < page.index("Drink interests") \
            < page.index("Food and drink") < page.index("Budget ceiling")
        assert 'id="cuisine-dialog"' in page and "Choose cuisines" in page

    def test_all_activities_is_still_a_blank_profile_for_agents(auth_on):
        u, h = _person("all")
        every = [k for k, _ in trips_routes.DISCOVERY_ACTIVITIES]
        client.post("/trips/settings/discovery-profile", headers=h,
                    data={"activity_interests": every})
        p = client.get("/api/v1/me/discovery-profile", headers=h).json()
        assert p["activity"]["interests"] == [] and p["activity"]["all_interests"] is True
        assert p["blank"] is True

    def test_cuisines_save_and_show_as_labels_for_their_owner_only(auth_on):
        me, mine = _person("me")
        _, theirs = _person("them")
        r = client.post("/trips/settings/discovery-profile", headers=mine, data={
            "cuisine:thai": "like", "cuisine:sushi": "dislike", "cuisine:italian": "neutral"})
        assert r.status_code == 200
        assert "<b>Likes:</b> Thai" in r.text and "<b>Doesn't like:</b> Sushi" in r.text
        p = client.get("/api/v1/me/discovery-profile", headers=mine).json()
        assert p["food"]["liked_cuisines"] == ["Thai"]
        assert p["food"]["avoid_cuisines"] == ["Sushi"]
        assert p["blank"] is False
        # Someone else sees none of it, and their save doesn't touch mine.
        other = client.get("/api/v1/me/discovery-profile", headers=theirs).json()
        assert other["food"]["liked_cuisines"] == [] and other["food"]["avoid_cuisines"] == []
        assert "<b>Likes:</b> Thai" not in client.get("/trips/settings/discovery-profile",
                                                       headers=theirs).text
        client.post("/trips/settings/discovery-profile", headers=theirs,
                    data={"cuisine:thai": "dislike"})
        with SessionLocal() as s:
            row = s.query(DiscoveryProfile).filter_by(traveler_id=me.traveler_id).one()
            assert row.values["food"]["cuisine_likes"] == ["thai"]

    def test_prefer_local_food_saves_and_reaches_the_api(auth_on):
        _, h = _person("local")
        page = client.get("/trips/settings/discovery-profile", headers=h).text
        assert 'name="prefer_local_food" value="1" >' in page       # unticked to start
        assert page.index("Choose cuisines") < page.index("Prefer local food") \
            < page.index("Allergies")
        r = client.post("/trips/settings/discovery-profile", headers=h,
                        data={"prefer_local_food": "1"})
        assert 'name="prefer_local_food" value="1" checked>' in r.text
        p = client.get("/api/v1/me/discovery-profile", headers=h).json()
        assert p["food"]["prefer_local_food"] is True and p["blank"] is False
        client.post("/trips/settings/discovery-profile", headers=h, data={})
        p = client.get("/api/v1/me/discovery-profile", headers=h).json()
        assert p["food"]["prefer_local_food"] is False and p["blank"] is True
