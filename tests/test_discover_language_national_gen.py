"""National-language phrasebook generator (Yonder 3.7).

Fills a country's national language that the vetted set lacks (Central Atlas
Tamazight in Morocco) via the shared LLM generator, flagged + promotable. Offline:
the generation seam (discover.phrasegen) is stubbed.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discover.phrasegen as pg_mod
from discover.resolvers.language_national_gen import NationalPhrasebookResolver
from discover.resolvers.base import ResolverContext
from discover.search import SearchResult
from discover.places import DerivedPlace


def _resolver(**kw):
    kw.setdefault("searxng_url", "http://searxng:8080")
    kw.setdefault("ollama_url", "http://ollama:11434")
    return NationalPhrasebookResolver(**kw)


def _place():
    return DerivedPlace(country="MA", city="Tangier", lat=35.8, lon=-5.8)


def _ctx(phrasebook=None, generated=frozenset()):
    return ResolverContext(today="2026-08-22", phrasebook=phrasebook or {},
                           generated_languages=generated)


def _stub_gen(monkeypatch, book):
    monkeypatch.setattr(pg_mod, "searxng_search", lambda q, *, base_url, timeout=12:
                        [SearchResult("https://amazigh.example/azul", "Tamazight", "azul")])
    monkeypatch.setattr(pg_mod, "synthesize_json", lambda *a, **k: {"phrases": book})


# Arabic + French vetted; Central Atlas Tamazight (tzm) is the gap.
_VETTED = {"ar": {"hello": {"t": "مرحبا", "p": "marhaban"}},
           "fr": {"hello": {"t": "Bonjour", "p": "bawn-ZHOOR"}}}


def test_generates_the_national_gap(monkeypatch):
    _stub_gen(monkeypatch, [{"key": "hello", "text": "ⴰⵣⵓⵍ", "pronunciation": "azul"}])
    f = _resolver().resolve(_place(), _ctx(_VETTED))
    assert f is not None
    assert f.topic == "language" and f.place_key == "MA"     # enriches the national finding
    assert f.confidence == "flagged"                          # generated content
    assert "ⴰⵣⵓⵍ" in f.body_md                                # generated Tamazight hello
    assert "generated — verify" in f.body_md
    # vetted Arabic + widely-spoken French still render alongside
    assert "مرحبا" in f.body_md and "Bonjour" in f.body_md
    assert "tzm" in f.data["generated_phrasebooks"]           # handed to the job to persist


def test_none_when_nothing_to_generate(monkeypatch):
    # Every national language already has a phrasebook → the deterministic finding
    # is complete → this resolver adds nothing.
    def _boom(*a, **k):
        raise AssertionError("must not generate when there are no gaps")
    monkeypatch.setattr(pg_mod, "synthesize_json", _boom)
    full = {**_VETTED, "tzm": {"hello": {"t": "ⴰⵣⵓⵍ", "p": "azul"}}}
    assert _resolver().resolve(_place(), _ctx(full)) is None


def test_none_when_generation_finds_nothing(monkeypatch):
    monkeypatch.setattr(pg_mod, "searxng_search", lambda q, *, base_url, timeout=12: [])
    assert _resolver().resolve(_place(), _ctx(_VETTED)) is None   # tzm gap, but no results


def test_already_generated_is_not_regenerated(monkeypatch):
    # tzm was generated on a prior run (in the DB phrasebook + generated set) — no gap.
    def _boom(*a, **k):
        raise AssertionError("must not regenerate a stored language")
    monkeypatch.setattr(pg_mod, "synthesize_json", _boom)
    pb = {**_VETTED, "tzm": {"hello": {"t": "ⴰⵣⵓⵍ", "p": "azul"}}}
    assert _resolver().resolve(_place(), _ctx(pb, generated=frozenset({"tzm"}))) is None


def test_none_without_endpoints():
    r = NationalPhrasebookResolver(searxng_url=None, ollama_url=None)
    assert r.resolve(_place(), _ctx(_VETTED)) is None
