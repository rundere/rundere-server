"""Regional-language resolver (SPEC-3.2 §5, region refinement).

Offline: the SearXNG + Ollama generation seam is stubbed at the module boundary.
Covers region→language lookup, national-language subtraction, the vetted vs
generated vs note phrasebook paths, and the degrade cases.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discover.phrasegen as pg_mod
import discover.resolvers.language_regional as lr_mod
from discover.resolvers.language_regional import RegionalLanguageResolver
from discover.resolvers.base import ResolverContext
from discover.search import SearchResult
from discover.places import DerivedPlace


def _ctx():
    return ResolverContext(today="2026-08-22")


def _resolver(**kw):
    kw.setdefault("searxng_url", "http://searxng:8080")
    kw.setdefault("ollama_url", "http://ollama:11434")
    return RegionalLanguageResolver(**kw)


def _place(country="IN", city="Bengaluru", region="Karnataka"):
    return DerivedPlace(country=country, city=city, lat=12.97, lon=77.59, region=region)


def _stub_gen(monkeypatch, book, results=None):
    """Stub the SearXNG+Ollama generation (now in discover.phrasegen) to return a
    fixed phrasebook."""
    results = results or [SearchResult("https://a.org/kn", "Kannada phrases", "namaskara")]
    monkeypatch.setattr(pg_mod, "searxng_search",
                        lambda q, *, base_url, timeout=12: results)
    monkeypatch.setattr(pg_mod, "synthesize_json",
                        lambda *a, **k: {"phrases": book})


# ------------------------------------------------------------- lookup + subtraction

def test_bengaluru_surfaces_kannada_generated(monkeypatch):
    # Kannada isn't in the vetted set → generated, flagged, with sources.
    _stub_gen(monkeypatch, [
        {"key": "hello", "text": "ನಮಸ್ಕಾರ", "pronunciation": "namaskara"},
        {"key": "thanks", "text": "ಧನ್ಯವಾದ", "pronunciation": "dhanyavada"},
    ])
    f = _resolver().resolve(_place(), _ctx())
    assert f is not None
    assert f.topic == "language_regional" and f.place_key == "IN:Bengaluru"
    assert f.confidence == "flagged"                 # generated content
    assert "Kannada" in f.body_md and "ನಮಸ್ಕಾರ" in f.body_md
    assert "Machine-generated" in f.body_md          # honesty banner
    assert any(s["url"] == "https://a.org/kn" for s in f.sources)
    langs = f.data["languages"]
    assert langs[0]["code"] == "kn" and langs[0]["generated"] is True


def test_national_language_is_not_relisted(monkeypatch):
    # Quebec's regional language (fr) is already a CA national language → nothing
    # left to surface → no finding.
    _stub_gen(monkeypatch, [])
    f = _resolver().resolve(_place("CA", "Montreal", "Quebec"), _ctx())
    assert f is None


def test_vetted_regional_language_needs_no_generation(monkeypatch):
    # South Tyrol (IT) → German, which IS vetted. It must render vetted phrases
    # and NOT be flagged, without calling the generator.
    def _boom(*a, **k):
        raise AssertionError("generation must not run for a vetted language")
    monkeypatch.setattr(pg_mod, "synthesize_json", _boom)
    f = _resolver().resolve(_place("IT", "Bolzano", "South Tyrol"), _ctx())
    assert f is not None
    assert f.confidence == "high"                    # vetted → not flagged
    assert "German" in f.body_md and "Danke" in f.body_md
    assert "Machine-generated" not in f.body_md


def test_generation_failure_falls_back_to_note(monkeypatch):
    monkeypatch.setattr(pg_mod, "searxng_search", lambda q, *, base_url, timeout=12: [])
    f = _resolver().resolve(_place(), _ctx())         # Kannada, no search results
    assert f is not None
    assert f.confidence == "flagged"
    assert "isn't available yet" in f.body_md
    assert f.data["languages"][0]["has_phrasebook"] is False


# ------------------------------------------------------------- degrade paths

def test_none_without_region(monkeypatch):
    f = _resolver().resolve(_place(region=None), _ctx())
    assert f is None


def test_none_for_unmapped_region(monkeypatch):
    f = _resolver().resolve(_place("IN", "Lucknow", "Uttar Pradesh"), _ctx())
    assert f is None                                  # UP not in the map (Hindi = national)


def test_vetted_still_works_without_llm_endpoints():
    # No SearXNG/Ollama configured: vetted regional languages still resolve.
    r = RegionalLanguageResolver(searxng_url=None, ollama_url=None)
    f = r.resolve(_place("IT", "Bolzano", "South Tyrol"), _ctx())
    assert f is not None and "Danke" in f.body_md


def test_unmapped_country_returns_none():
    f = _resolver().resolve(_place("JP", "Osaka", "Osaka"), _ctx())
    assert f is None


# ------------------------------------------------------------- ctx-injected DB data

def test_stored_generated_phrasebook_is_reused_not_regenerated(monkeypatch):
    # ctx carries a DB-backed phrasebook with a previously-GENERATED Kannada book
    # (kn in generated_languages). The resolver must reuse it (flagged) and NOT
    # regenerate — nor mark it for persistence again.
    def _boom(*a, **k):
        raise AssertionError("must not regenerate a stored phrasebook")
    monkeypatch.setattr(pg_mod, "synthesize_json", _boom)
    ctx = ResolverContext(
        today="2026-08-22",
        phrasebook={"kn": {"hello": {"t": "ನಮಸ್ಕಾರ", "p": "namaskara"}}},
        regional_map={"IN": {"karnataka": ["kn"]}},
        generated_languages=frozenset({"kn"}))
    f = _resolver().resolve(_place(), ctx)
    assert f is not None and f.confidence == "flagged"
    assert "ನಮಸ್ಕಾರ" in f.body_md
    assert f.data["languages"][0]["generated"] is True
    assert f.data["generated_phrasebooks"] == {}      # nothing fresh to persist


def test_fresh_generation_is_returned_for_persistence(monkeypatch):
    _stub_gen(monkeypatch, [
        {"key": "hello", "text": "ನಮಸ್ಕಾರ", "pronunciation": "namaskara"}])
    ctx = ResolverContext(today="2026-08-22", phrasebook={},
                          regional_map={"IN": {"karnataka": ["kn"]}},
                          generated_languages=frozenset())
    f = _resolver().resolve(_place(), ctx)
    # the freshly-generated book is handed to the job to persist
    assert "kn" in f.data["generated_phrasebooks"]
    assert f.data["generated_phrasebooks"]["kn"]["hello"]["t"] == "ನಮಸ್ಕಾರ"
