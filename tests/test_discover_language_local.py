"""Language L3 — regional register overlay (SPEC-3.2 §11.3, step 10).

Offline: SearXNG + Ollama stubbed at the module boundary. Covers query
construction, the greet/express/etiquette pipeline, grounding of invented
phrases, the standard-language contrast hint, and the degrade paths.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discover.resolvers.language_local as ll_mod
from discover.resolvers.language_local import LanguageLocalResolver
from discover.resolvers.base import ResolverContext
from discover.search import SearchResult
from discover.places import DerivedPlace


def _place(city="Zurich", country="CH"):
    return DerivedPlace(country=country, city=city, lat=47.4, lon=8.5)


def _ctx(today="2026-08-22"):
    return ResolverContext(today=today)


def _resolver(**kw):
    kw.setdefault("searxng_url", "http://searxng:8080")
    kw.setdefault("ollama_url", "http://ollama:11434")
    return LanguageLocalResolver(**kw)


def _stub_search(monkeypatch, results):
    monkeypatch.setattr(ll_mod, "searxng_search",
                        lambda query, *, base_url, timeout=12: results)


def _stub_synth(monkeypatch, payload, capture=None):
    def fake(prompt, schema, *, base_url, system=None, model=None, timeout=600):
        if capture is not None:
            capture["prompt"] = prompt
        return payload
    monkeypatch.setattr(ll_mod, "synthesize_json", fake)


def _payload(greet_urls, expr_urls):
    return {
        "register_summary": "Swiss German greets 'Grüezi', not textbook 'Guten Tag'.",
        "greetings": [{"phrase": "Grüezi", "meaning": "Hello (formal)",
                       "pronunciation": "GROO-tsee", "note": "use with strangers",
                       "source_url": u} for u in greet_urls],
        "expressions": [{"phrase": "Merci vilmal", "meaning": "Thanks a lot",
                         "source_url": u} for u in expr_urls],
        "etiquette": ["Shake hands and say Grüezi when entering a small shop."],
    }


# ------------------------------------------------------------- query building

def test_queries_are_city_and_register_focused():
    qs = _resolver()._queries("Zurich", "CH")
    assert "local greetings Zurich" in qs
    assert "how do locals greet each other Zurich CH" in qs
    assert "Zurich dialect phrases" in qs
    assert "local slang Zurich CH" in qs


# ------------------------------------------------------------- full pipeline

def test_resolve_builds_register_overlay(monkeypatch):
    results = [SearchResult("https://a.org/ch", "Swiss greetings", "Grüezi"),
               SearchResult("https://b.com/zh", "Zurich slang", "Merci vilmal")]
    _stub_search(monkeypatch, results)
    cap = {}
    _stub_synth(monkeypatch, _payload(["https://a.org/ch"], ["https://b.com/zh"]), cap)
    f = _resolver().resolve(_place(), _ctx())
    assert f is not None
    assert f.topic == "language_local" and f.scope == "city"
    assert f.place_key == "CH:Zurich"
    assert f.confidence == "flagged" and f.ttl_seconds == 7_776_000
    # register overlay rendered with the distinctive local greeting
    assert "Grüezi" in f.body_md
    assert "### How locals greet" in f.body_md
    assert "Merci vilmal" in f.body_md
    assert "not a phrasebook" in f.body_md          # the flagged banner
    # the prompt carried the standard-language contrast hint (CH -> German/French/…)
    assert "German" in cap["prompt"]


def test_fallback_pass_surfaces_register_when_primary_empty(monkeypatch):
    # The register-specific queries ground nothing (Tangier Darija is elusive by
    # exact name); the broader language-name-led fallback must still surface a
    # greeting instead of the register card vanishing.
    GREET = "https://darija.example/salam"
    _FB = ("common local phrases", "how do people greet", "useful everyday",
           "greetings and everyday", "how to say hello and thank you")

    def fake_search(query, *, base_url, timeout=12):
        if any(w in query for w in _FB):
            return [SearchResult(GREET, "Darija greetings", "Salam / Labas")]
        return [SearchResult("https://x.example/1", "unrelated", "x")]

    monkeypatch.setattr(ll_mod, "searxng_search", fake_search)
    # The model always proposes the greeting; it only GROUNDS on the fallback pass.
    monkeypatch.setattr(ll_mod, "synthesize_json", lambda *a, **k: {
        "register_summary": "", "expressions": [], "etiquette": [],
        "greetings": [{"phrase": "Salam", "meaning": "hi", "source_url": GREET}]})
    f = _resolver().resolve(_place("Tangier", "MA"), _ctx())
    assert f is not None
    assert "Salam" in f.body_md
    assert any(s["url"] == GREET for s in f.sources)


def test_invented_greeting_is_dropped(monkeypatch):
    results = [SearchResult("https://real.org/ch", "real", "Grüezi"),
               SearchResult("https://b.com/zh", "slang", "x")]
    _stub_search(monkeypatch, results)
    p = _payload(["https://real.org/ch", "https://hallucinated.example/no"],
                 ["https://b.com/zh"])
    _stub_synth(monkeypatch, p)
    f = _resolver().resolve(_place(), _ctx())
    assert len(f.data["greetings"]) == 1            # invented one dropped


def test_summary_only_still_produces_finding(monkeypatch):
    # No grounded greeting, but a real register summary is worth showing.
    results = [SearchResult("https://a.org/ch", "ctx", "s")]
    _stub_search(monkeypatch, results)
    _stub_synth(monkeypatch, _payload(["https://nope.example/x"], []))
    f = _resolver().resolve(_place(), _ctx())
    assert f is not None
    assert f.data["greetings"] == []
    assert "Grüezi" in f.body_md                     # from the summary text


# ------------------------------------------------------------- degrade paths

def test_none_without_endpoints():
    assert LanguageLocalResolver(searxng_url=None, ollama_url="x").resolve(
        _place(), _ctx()) is None


def test_none_without_city(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://a", "A", "s")])
    _stub_synth(monkeypatch, _payload([], []))
    assert _resolver().resolve(
        DerivedPlace(country="CH", city=None, lat=None, lon=None), _ctx()) is None


def test_none_when_no_results(monkeypatch):
    _stub_search(monkeypatch, [])
    assert _resolver().resolve(_place(), _ctx()) is None


def test_none_when_empty_and_ungrounded(monkeypatch):
    results = [SearchResult("https://a.org/ch", "ctx", "s")]
    _stub_search(monkeypatch, results)
    # no summary, greeting cites a url not in results -> nothing survives
    payload = {"register_summary": "", "greetings":
               [{"phrase": "X", "meaning": "y", "source_url": "https://no.example/1"}],
               "expressions": [], "etiquette": []}
    _stub_synth(monkeypatch, payload)
    assert _resolver().resolve(_place(), _ctx()) is None
