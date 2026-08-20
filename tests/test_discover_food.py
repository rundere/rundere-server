"""Food & drink resolver (SPEC-3.2 §3.1) — the first LLM resolver.

Fully offline: the SearXNG search and the Ollama synthesis are both stubbed at
the food-module boundary. Covers query construction from the profile, the
search->synth->ground->assemble pipeline, grounding of invented places, the
budget post-filter, body_md rendering, and every degrade-to-None path.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discover.resolvers.food as food_mod
from discover.resolvers.food import FoodResolver
from discover.resolvers.base import ResolverContext
from discover.search import SearchResult
from discover.places import DerivedPlace


def _place(city="Lisbon", country="PT"):
    return DerivedPlace(country=country, city=city, lat=38.7, lon=-9.1)


def _ctx(profile=None):
    return ResolverContext(start_date="2026-10-05", end_date="2026-10-14",
                           today="2026-08-18", discovery_profile=profile)


def _resolver(**kw):
    kw.setdefault("searxng_url", "http://searxng:8080")
    kw.setdefault("ollama_url", "http://ollama:11434")
    return FoodResolver(**kw)


def _stub_search(monkeypatch, results):
    calls = []

    def fake(query, *, base_url, timeout=12):
        calls.append(query)
        return results

    monkeypatch.setattr(food_mod, "searxng_search", fake)
    return calls


def _stub_synth(monkeypatch, payload, capture=None):
    def fake(prompt, schema, *, base_url, system=None, model=None, timeout=600):
        if capture is not None:
            capture["prompt"] = prompt
        return payload

    monkeypatch.setattr(food_mod, "synthesize_json", fake)


# ------------------------------------------------------------- query building

def test_queries_always_include_base_and_street_food():
    qs = _resolver()._queries("Lisbon", "PT", {})
    assert qs[0] == "best local restaurants Lisbon PT"
    assert "street food Lisbon" in qs


def test_queries_add_budget_and_drink_interests():
    profile = {"food": {"budget_ceiling": 1},
               "drink": {"interests": ["craft_beer", "coffee", "not_real"]}}
    qs = _resolver()._queries("Porto", "PT", profile)
    assert "cheap eats Porto" in qs                 # $ ceiling
    assert "best craft beer Porto" in qs
    assert "specialty coffee Porto" in qs
    assert not any("not_real" in q for q in qs)     # unknown interest ignored


def test_queries_fine_dining_at_top_tier():
    qs = _resolver()._queries("Paris", "FR", {"food": {"budget_ceiling": 4}})
    assert "fine dining Paris" in qs
    assert not any("cheap eats" in q for q in qs)


def test_baseline_drink_query_always_present():
    # even with no drink interests, drink queries run so it's never food-only and
    # notable breweries/distilleries surface without opting in
    qs = _resolver()._queries("Lisbon", "PT", {})
    assert "best bars and cafes Lisbon" in qs
    assert "best breweries and distilleries Lisbon" in qs


# ------------------------------------------------------------- full pipeline

def test_resolve_builds_grounded_finding(monkeypatch):
    results = [
        SearchResult("https://a.example/tasca", "Tasca do Bairro", "petiscos"),
        SearchResult("https://b.example/cafe", "Café Central", "pastel de nata"),
    ]
    _stub_search(monkeypatch, results)
    _stub_synth(monkeypatch, {
        "places": [
            {"name": "Tasca do Bairro", "type": "restaurant", "neighborhood": "Alfama",
             "price_tier": 2, "why": "classic petiscos", "local_specialty": True,
             "source_url": "https://a.example/tasca"},
            {"name": "Café Central", "type": "cafe", "neighborhood": None,
             "price_tier": 1, "why": "best nata in town", "local_specialty": True,
             "source_url": "https://b.example/cafe"},
        ],
        "local_dishes": ["pastéis de nata", "bacalhau"],
        "allergy_warnings": ["Café Central pastries contain egg"],
    })
    f = _resolver().resolve(_place(), _ctx({"food": {"budget_ceiling": 0}}))
    assert f is not None
    assert f.topic == "food" and f.scope == "city" and f.place_key == "PT:Lisbon"
    assert f.confidence == "medium"
    assert f.resolver == "food.searxng+ollama"
    assert len(f.data["places"]) == 2
    assert f.data["local_dishes"] == ["pastéis de nata", "bacalhau"]
    # body renders specialties, a type heading, a place, and the allergy block
    assert "Local specialties to try:" in f.body_md
    assert "Restaurants" in f.body_md and "Tasca do Bairro" in f.body_md
    assert "⚠️" in f.body_md and "egg" in f.body_md
    # sources are the grounded places' urls, deduped, titled from the results
    assert {s["url"] for s in f.sources} == {"https://a.example/tasca",
                                             "https://b.example/cafe"}


def test_render_includes_address_and_map_link(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://a.ex/t", "Tasca", "x")])
    _stub_synth(monkeypatch, {
        "places": [{"name": "Tasca do Bairro", "type": "restaurant",
                    "neighborhood": "Alfama", "address": "Rua do Bairro 12",
                    "price_tier": 2, "why": "petiscos", "local_specialty": True,
                    "source_url": "https://a.ex/t"}],
        "local_dishes": [], "allergy_warnings": [],
    })
    f = _resolver().resolve(_place(), _ctx({}))
    assert "Rua do Bairro 12" in f.body_md                        # address shown
    assert "[map ↗](https://www.google.com/maps/search/?api=1&query=" in f.body_md
    assert "Tasca%20do%20Bairro" in f.body_md                     # name in the query


def test_resolve_drops_invented_places(monkeypatch):
    results = [SearchResult("https://real.example/x", "Real Place", "good")]
    _stub_search(monkeypatch, results)
    _stub_synth(monkeypatch, {
        "places": [
            {"name": "Real Place", "type": "restaurant", "price_tier": 2,
             "why": "grounded", "local_specialty": False,
             "source_url": "https://real.example/x"},
            {"name": "Hallucinated", "type": "restaurant", "price_tier": 2,
             "why": "invented", "local_specialty": False,
             "source_url": "https://made-up.example/nope"},
        ],
        "local_dishes": [], "allergy_warnings": [],
    })
    f = _resolver().resolve(_place(), _ctx({}))
    names = [p["name"] for p in f.data["places"]]
    assert names == ["Real Place"]                  # invented URL dropped


def test_resolve_budget_filter_drops_over_tier(monkeypatch):
    results = [SearchResult("https://a.example/1", "A", ""),
               SearchResult("https://b.example/2", "B", "")]
    _stub_search(monkeypatch, results)
    _stub_synth(monkeypatch, {
        "places": [
            {"name": "Cheap", "type": "restaurant", "price_tier": 2, "why": "ok",
             "local_specialty": False, "source_url": "https://a.example/1"},
            {"name": "Fancy", "type": "restaurant", "price_tier": 4, "why": "posh",
             "local_specialty": False, "source_url": "https://b.example/2"},
        ],
        "local_dishes": [], "allergy_warnings": [],
    })
    f = _resolver().resolve(_place(), _ctx({"food": {"budget_ceiling": 2}}))
    assert [p["name"] for p in f.data["places"]] == ["Cheap"]


def test_profile_constraints_reach_the_prompt(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://a.example", "A", "s")])
    cap = {}
    _stub_synth(monkeypatch, {"places": [], "local_dishes": ["x"],
                              "allergy_warnings": []}, capture=cap)
    profile = {"food": {"dietary_restrictions": ["vegetarian"],
                        "cuisine_dislikes": ["sushi"], "allergies": "peanuts",
                        "budget_ceiling": 2}}
    _resolver().resolve(_place(), _ctx(profile))
    p = cap["prompt"]
    assert "vegetarian" in p and "sushi" in p and "peanuts" in p
    assert "$$" in p


# ------------------------------------------------------------- degrade paths

def test_none_without_endpoints():
    assert FoodResolver(searxng_url=None, ollama_url="x").resolve(_place(), _ctx()) is None
    assert FoodResolver(searxng_url="x", ollama_url=None).resolve(_place(), _ctx()) is None


def test_none_without_city(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://a.example", "A", "s")])
    _stub_synth(monkeypatch, {"places": [], "local_dishes": [], "allergy_warnings": []})
    assert _resolver().resolve(DerivedPlace(country="PT", city=None,
                                            lat=None, lon=None), _ctx()) is None


def test_none_when_no_search_results(monkeypatch):
    _stub_search(monkeypatch, [])
    called = {"synth": False}

    def fake_synth(*a, **k):
        called["synth"] = True
        return {}

    monkeypatch.setattr(food_mod, "synthesize_json", fake_synth)
    assert _resolver().resolve(_place(), _ctx()) is None
    assert called["synth"] is False                 # never synthesize with no sources


def test_none_when_synth_fails(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://a.example", "A", "s")])
    _stub_synth(monkeypatch, None)
    assert _resolver().resolve(_place(), _ctx()) is None


def test_none_when_all_hallucinated_and_no_dishes(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://real.example", "R", "s")])
    _stub_synth(monkeypatch, {
        "places": [{"name": "Fake", "type": "bar", "price_tier": 2, "why": "x",
                    "local_specialty": False, "source_url": "https://fake.example"}],
        "local_dishes": [], "allergy_warnings": [],
    })
    assert _resolver().resolve(_place(), _ctx()) is None
