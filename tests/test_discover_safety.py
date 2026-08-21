"""Safety & transport resolver (SPEC-3.2 §3.4).

Offline: SearXNG + Ollama stubbed at the module boundary. Covers query
construction, the transport-first pipeline, safety corroboration + verify banner,
grounding of invented advisories, and the degrade paths.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discover.resolvers.safety as safety_mod
from discover.resolvers.safety import SafetyResolver
from discover.resolvers.base import ResolverContext
from discover.search import SearchResult
from discover.places import DerivedPlace


def _place(city="Lisbon", country="PT"):
    return DerivedPlace(country=country, city=city, lat=38.7, lon=-9.1)


def _ctx(today="2026-08-21"):
    return ResolverContext(today=today)


def _resolver(**kw):
    kw.setdefault("searxng_url", "http://searxng:8080")
    kw.setdefault("ollama_url", "http://ollama:11434")
    return SafetyResolver(**kw)


def _stub_search(monkeypatch, results):
    monkeypatch.setattr(safety_mod, "searxng_search",
                        lambda query, *, base_url, timeout=12: results)


def _stub_synth(monkeypatch, payload, capture=None):
    def fake(prompt, schema, *, base_url, system=None, model=None, timeout=600):
        if capture is not None:
            capture["prompt"] = prompt
        return payload
    monkeypatch.setattr(safety_mod, "synthesize_json", fake)


def _payload(adv_urls, transport_urls, ride=("Uber", "Bolt")):
    return {
        "safety": {"summary": "Generally safe; watch pickpockets on tram 28.",
                   "advisories": [{"text": "Pickpockets on tram 28", "source_url": u}
                                  for u in adv_urls],
                   "scams": ["Fake taxi overcharging from the airport"]},
        "transport": {"summary": "Metro + trams cover the centre.",
                      "public_transit": "Metro and trams; pay with a Viva Viagem card",
                      "ride_hailing": list(ride), "apps": ["Citymapper", "Viva Viagem"],
                      "from_airport": "Metro red line to the centre in 20 min",
                      "tips": ["Validate your card on boarding"],
                      "source_urls": list(transport_urls)},
    }


# ------------------------------------------------------------- query building

def test_queries_cover_safety_and_transport():
    qs = _resolver()._queries("Lisbon", "PT", "2026")
    assert "travel safety PT 2026" in qs
    assert "getting around Lisbon public transit" in qs
    assert "ride hailing apps Lisbon" in qs
    assert "Lisbon airport to city center transport" in qs
    assert "scams to avoid Lisbon" in qs


# ------------------------------------------------------------- full pipeline

def test_resolve_builds_transport_and_safety(monkeypatch):
    results = [SearchResult("https://a.gov/pt", "Travel advice", "safe-ish"),
               SearchResult("https://b.org/lx", "Getting around Lisbon", "metro"),
               SearchResult("https://c.com/lx", "Lisbon transit", "trams")]
    _stub_search(monkeypatch, results)
    # 2 distinct advisory domains -> corroborated; transport sourced
    _stub_synth(monkeypatch, _payload(["https://a.gov/pt", "https://b.org/lx"],
                                      ["https://b.org/lx", "https://c.com/lx"]))
    f = _resolver().resolve(_place(), _ctx())
    assert f is not None
    assert f.topic == "safety" and f.place_key == "PT:Lisbon"
    assert f.confidence == "medium" and f.ttl_seconds == 2_592_000
    # transport rendered first, with the concrete specifics
    assert f.body_md.index("### Getting around") < f.body_md.index("### Safety")
    assert "Uber, Bolt" in f.body_md and "Viva Viagem" in f.body_md
    assert "red line" in f.body_md
    # safety corroborated -> no verify banner
    assert "Verify at official source" not in f.body_md
    assert f.data["safety"]["corroborated"] is True


def test_single_source_safety_gets_verify_banner(monkeypatch):
    results = [SearchResult("https://a.gov/pt", "advice", "x"),
               SearchResult("https://b.org/lx", "transit", "y")]
    _stub_search(monkeypatch, results)
    # both advisories from ONE domain -> not corroborated
    _stub_synth(monkeypatch, _payload(["https://a.gov/pt"],
                                      ["https://b.org/lx"]))
    f = _resolver().resolve(_place(), _ctx())
    assert "Verify at official source" in f.body_md
    assert f.data["safety"]["corroborated"] is False


def test_invented_advisory_is_dropped(monkeypatch):
    results = [SearchResult("https://real.gov/x", "real", "a"),
               SearchResult("https://b.org/lx", "transit", "b")]
    _stub_search(monkeypatch, results)
    p = _payload(["https://real.gov/x", "https://hallucinated.example/no"],
                 ["https://b.org/lx"])
    _stub_synth(monkeypatch, p)
    f = _resolver().resolve(_place(), _ctx())
    texts = [a["text"] for a in f.data["safety"]["advisories"]]
    assert len(texts) == 1                              # invented one dropped


def test_transport_without_grounded_source_is_dropped(monkeypatch):
    results = [SearchResult("https://a.gov/pt", "advice", "x")]
    _stub_search(monkeypatch, results)
    # transport cites a url NOT in results -> transport block dropped, safety kept
    _stub_synth(monkeypatch, _payload(["https://a.gov/pt"],
                                      ["https://made-up.example/no"]))
    f = _resolver().resolve(_place(), _ctx())
    assert f.data["transport"] is None
    assert "### Getting around" not in f.body_md
    assert "### Safety" in f.body_md


# ------------------------------------------------------------- degrade paths

def test_none_without_endpoints():
    assert SafetyResolver(searxng_url=None, ollama_url="x").resolve(
        _place(), _ctx()) is None


def test_none_without_city(monkeypatch):
    _stub_search(monkeypatch, [SearchResult("https://a", "A", "s")])
    _stub_synth(monkeypatch, _payload([], []))
    assert _resolver().resolve(
        DerivedPlace(country="PT", city=None, lat=None, lon=None), _ctx()) is None


def test_none_when_no_results(monkeypatch):
    _stub_search(monkeypatch, [])
    assert _resolver().resolve(_place(), _ctx()) is None


def test_none_when_nothing_grounds(monkeypatch):
    results = [SearchResult("https://a.gov/pt", "advice", "x")]
    _stub_search(monkeypatch, results)
    _stub_synth(monkeypatch, _payload(["https://nope.example/1"],
                                      ["https://nope.example/2"]))
    assert _resolver().resolve(_place(), _ctx()) is None
