"""Events & activities resolver (SPEC-3.2 §3.2, three-tier D-98).

Fully offline: SearXNG search and Ollama synthesis stubbed at the module
boundary. Covers query construction (impact + base + per-interest), the
impacts/notable split, grounding, rendering, and the degrade paths.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discover.resolvers.events as events_mod
from discover.resolvers.events import EventsResolver
from discover.resolvers.base import ResolverContext
from discover.search import SearchResult
from discover.places import DerivedPlace


def _place(city="Seville", country="ES"):
    return DerivedPlace(country=country, city=city, lat=37.4, lon=-5.9)


def _ctx(interests=None, start="2026-04-01"):
    profile = {"activity": {"interests": interests or []}}
    return ResolverContext(start_date=start, end_date="2026-04-08",
                           today="2026-02-01", discovery_profile=profile)


def _resolver(**kw):
    kw.setdefault("searxng_url", "http://searxng:8080")
    kw.setdefault("ollama_url", "http://ollama:11434")
    return EventsResolver(**kw)


def _stub_search(monkeypatch, results):
    monkeypatch.setattr(events_mod, "searxng_search",
                        lambda query, *, base_url, timeout=12: results)


def _stub_synth(monkeypatch, payload, capture=None):
    def fake(prompt, schema, *, base_url, system=None, model=None, timeout=600):
        if capture is not None:
            capture["prompt"] = prompt
        return payload
    monkeypatch.setattr(events_mod, "synthesize_json", fake)


# ------------------------------------------------------------- query building

def test_impact_and_base_queries_always_present():
    qs = _resolver()._queries("Seville", "ES", "April 2026", [])
    assert "holidays Seville April 2026" in qs
    assert "ES public holidays April 2026" in qs
    assert "religious observances ES April 2026" in qs
    assert "top things to do Seville" in qs
    assert "events Seville April 2026" in qs
    assert "festivals Seville April 2026" in qs
    # Events/things-to-do lead, holidays follow — so the round-robin fills the
    # snippet budget with event results before holiday results.
    assert qs.index("top things to do Seville") < qs.index("holidays Seville April 2026")


def test_fallback_pass_surfaces_attractions_when_primary_empty(monkeypatch):
    # A smaller town with no dated events: the primary (events-shaped) pass grounds
    # nothing, so the broader tourism-shaped fallback must still surface the city's
    # attractions instead of the city dropping out entirely.
    ATTR = "https://attractions.example/caverns"
    _FALLBACK = ("attraction", "tourism", "points of interest", "landmarks",
                 "day trips", "best things to do in")

    def fake_search(query, *, base_url, timeout=12):
        if any(w in query for w in _FALLBACK):
            return [SearchResult(ATTR, "Grand Canyon Skywalk", "a famous canyon walk")]
        return [SearchResult("https://cal.example/x", "Holiday calendar", "generic")]

    monkeypatch.setattr(events_mod, "searxng_search", fake_search)
    # The model always proposes the attraction; it only GROUNDS on the fallback
    # pass, where ATTR is actually in the search results.
    monkeypatch.setattr(events_mod, "synthesize_json", lambda *a, **k: {
        "impacts": [],
        "notable": [{"name": "Grand Canyon Skywalk", "why": "iconic", "source_url": ATTR}]})
    f = _resolver().resolve(_place("Glendale", "US"), _ctx())
    assert f is not None
    assert "Grand Canyon Skywalk" in f.body_md
    assert any(s["url"] == ATTR for s in f.sources)


def test_search_round_robin_does_not_starve_events(monkeypatch):
    # The regression: three holiday queries returned 10 hits each and, under the
    # old greedy fill, exhausted the 28-snippet budget before the events query ran
    # — so a signature event (the Balloon Fiesta) never reached Ollama. Round-robin
    # must let the events query contribute regardless of its position.
    def fake_search(query, *, base_url, timeout=12):
        if query.startswith(("holidays", "ES public", "religious")):
            return [SearchResult(f"https://cal.example/{query[:4]}/{i}",
                                 "Holiday calendar", "generic") for i in range(10)]
        if query.startswith("events"):
            return [SearchResult("https://phx.example/fiesta",
                                 "Balloon Fiesta 2026", "Oct 3–11 balloons")]
        return [SearchResult(f"https://ttd.example/{query[:6]}/{i}",
                             "Things to do", "misc") for i in range(3)]
    monkeypatch.setattr(events_mod, "searxng_search", fake_search)
    r = _resolver(max_snippets=28)
    results = r._search(r._queries("Phoenix", "US", "October 2026", []))
    urls = [x.url for x in results]
    assert "https://phx.example/fiesta" in urls          # survived the budget


def test_interest_queries_added_per_checked_interest():
    qs = _resolver()._queries("Seville", "ES", "April 2026",
                              ["live_music", "historical", "bogus"])
    assert "live music Seville April 2026" in qs
    assert "live music near Seville" in qs
    assert "historical sites Seville April 2026" in qs
    assert not any("bogus" in q for q in qs)          # unknown interest ignored


def test_queries_without_dates_have_no_trailing_space():
    qs = _resolver()._queries("Seville", "ES", "", [])
    assert "holidays Seville" in qs                     # no trailing ' '
    assert all(q == q.strip() for q in qs)


# ------------------------------------------------------------- full pipeline

def test_resolve_builds_two_tiers(monkeypatch):
    results = [
        SearchResult("https://a.ex/semana", "Semana Santa", "processions"),
        SearchResult("https://b.ex/flamenco", "Flamenco show", "nightly"),
        SearchResult("https://c.ex/park", "Water park", "family fun"),
    ]
    _stub_search(monkeypatch, results)
    _stub_synth(monkeypatch, {
        "impacts": [{"name": "Semana Santa", "date_range": "Apr 1-8",
                     "effect": "processions close central streets",
                     "source_url": "https://a.ex/semana"}],
        "notable": [
            {"name": "Flamenco show", "date_range": None, "matched_interest": "live_music",
             "why": "nightly tablao", "source_url": "https://b.ex/flamenco"},
            {"name": "Water park", "date_range": None, "matched_interest": None,
             "why": "day trip", "source_url": "https://c.ex/park"},
        ],
    })
    f = _resolver().resolve(_place(), _ctx(["live_music"]))
    assert f is not None
    assert f.topic == "events" and f.place_key == "ES:Seville"
    assert f.confidence == "medium" and f.ttl_seconds == 604_800
    assert len(f.data["impacts"]) == 1 and len(f.data["notable"]) == 2
    assert "other" not in f.data                       # noisy tier dropped
    # impact callout; interest-matched item under its heading; the rest under Notable
    assert "⚠️" in f.body_md and "Semana Santa" in f.body_md
    assert "### Live Music" in f.body_md and "Flamenco show" in f.body_md
    assert "### Notable events & activities" in f.body_md and "Water park" in f.body_md


def test_no_interests_puts_everything_under_notable(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://a.ex/f", "Fiesta", "big")])
    _stub_synth(monkeypatch, {
        "impacts": [],
        "notable": [{"name": "Balloon Fiesta", "date_range": "Oct 3-11",
                     "matched_interest": None, "why": "world's largest",
                     "source_url": "https://a.ex/f"}],
    })
    f = _resolver().resolve(_place(), _ctx([]))        # blank profile
    assert f is not None and "Balloon Fiesta" in f.body_md
    assert "### Notable events & activities" in f.body_md   # no weird "None Stated"


def test_dedupes_repeated_event_names(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://a.ex/1", "A", ""),
                               SearchResult("https://b.ex/2", "B", "")])
    _stub_synth(monkeypatch, {
        "impacts": [],
        "notable": [
            {"name": "Balloon Fiesta", "matched_interest": None, "why": "x",
             "source_url": "https://a.ex/1"},
            {"name": "balloon  fiesta", "matched_interest": None, "why": "dup",
             "source_url": "https://b.ex/2"},
        ],
    })
    f = _resolver().resolve(_place(), _ctx([]))
    assert len(f.data["notable"]) == 1                 # case/space-insensitive dedupe


def test_resolve_drops_invented_items(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://real.ex/x", "Real", "y")])
    _stub_synth(monkeypatch, {
        "impacts": [],
        "notable": [{"name": "Real Fest", "matched_interest": "museums",
                     "why": "grounded", "source_url": "https://real.ex/x"},
                    {"name": "Fake Fest", "matched_interest": "museums",
                     "why": "invented", "source_url": "https://made-up.ex/no"}],
    })
    f = _resolver().resolve(_place(), _ctx(["museums"]))
    assert [s["name"] for s in f.data["notable"]] == ["Real Fest"]


def test_interests_reach_the_prompt(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://a.ex", "A", "s")])
    cap = {}
    _stub_synth(monkeypatch, {"impacts": [{"name": "X", "effect": "y",
                              "source_url": "https://a.ex"}],
                              "notable": []}, capture=cap)
    _resolver().resolve(_place(), _ctx(["cooking_classes", "architecture"]))
    assert "Cooking Classes" in cap["prompt"] and "Architecture" in cap["prompt"]


def test_exact_trip_window_reaches_the_prompt(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://a.ex", "A", "s")])
    cap = {}
    _stub_synth(monkeypatch, {"impacts": [], "notable": []}, capture=cap)
    # trip Oct 2–11 (not just "October") so the model can exclude Oct 12 holidays
    _resolver().resolve(_place(), ResolverContext(
        start_date="2026-10-02", end_date="2026-10-11",
        discovery_profile={"activity": {"interests": []}}))
    assert "October 2, 2026" in cap["prompt"] and "October 11, 2026" in cap["prompt"]
    assert "inclusive" in cap["prompt"].lower()


# ------------------------------------------------------------- degrade paths

def test_none_without_endpoints():
    assert EventsResolver(searxng_url=None, ollama_url="x").resolve(
        _place(), _ctx()) is None


def test_none_without_city(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://a.ex", "A", "s")])
    _stub_synth(monkeypatch, {"impacts": [], "notable": []})
    assert _resolver().resolve(
        DerivedPlace(country="ES", city=None, lat=None, lon=None), _ctx()) is None


def test_none_when_no_results(monkeypatch):
    _stub_search(monkeypatch, [])
    assert _resolver().resolve(_place(), _ctx()) is None


def test_none_when_all_empty(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://a.ex", "A", "s")])
    _stub_synth(monkeypatch, {"impacts": [], "notable": []})
    assert _resolver().resolve(_place(), _ctx()) is None
