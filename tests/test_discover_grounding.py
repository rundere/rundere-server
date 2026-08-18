"""LLM grounding checks (SPEC-3.2 §4, D-66/D-90).

Pure functions, no I/O. Covers source-grounding (drop invented URLs, tolerate
trivial reformatting), corroboration (distinct-domain counting), and the verify
banner.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from discover.grounding import (Corroboration, corroborate, domain_of,
                                ground_items, normalize_url, verify_banner)


# ------------------------------------------------------------- normalize_url

def test_normalize_url_canonicalises():
    assert normalize_url("HTTPS://Example.com/Path/") == "https://example.com/Path"
    assert normalize_url("https://example.com") == "https://example.com"
    assert normalize_url("https://example.com/a?b=1#frag") == "https://example.com/a?b=1"
    assert normalize_url("not a url") == ""
    assert normalize_url("") == ""


def test_domain_of_strips_www_and_port():
    assert domain_of("https://www.nytimes.com/x") == "nytimes.com"
    assert domain_of("http://gov.uk:8080/a") == "gov.uk"
    assert domain_of("https://user@host.example/p") == "host.example"
    assert domain_of("garbage") == ""


# ------------------------------------------------------------- ground_items

def test_ground_drops_invented_urls():
    items = [
        {"name": "Real", "source_url": "https://a.example/1"},
        {"name": "Invented", "source_url": "https://hallucinated.example/x"},
        {"name": "NoUrl", "source_url": ""},
    ]
    allowed = ["https://a.example/1", "https://b.example/2"]
    res = ground_items(items, allowed)
    assert [i["name"] for i in res.kept] == ["Real"]
    assert [i["name"] for i in res.dropped] == ["Invented", "NoUrl"]


def test_ground_tolerates_trivial_reformatting():
    # LLM echoes the URL with a trailing slash + uppercased host — still grounded
    items = [{"name": "R", "source_url": "https://A.example/path/"}]
    res = ground_items(items, ["https://a.example/path"])
    assert [i["name"] for i in res.kept] == ["R"] and not res.dropped


def test_ground_custom_url_key():
    items = [{"url": "https://a.example"}]
    res = ground_items(items, ["https://a.example"], url_key="url")
    assert res.kept and not res.dropped


def test_ground_empty_inputs():
    assert ground_items([], ["https://a.example"]).kept == []
    r = ground_items([{"source_url": "https://a.example"}], [])
    assert r.kept == [] and len(r.dropped) == 1


# ------------------------------------------------------------- corroborate

def test_corroborate_counts_distinct_domains():
    c = corroborate(["https://a.example/1", "https://www.a.example/2",
                     "https://b.example/x"])
    assert c.corroborated is True                     # a.example + b.example = 2
    assert c.domains == ["a.example", "b.example"]    # www stripped, deduped


def test_corroborate_single_domain_fails():
    c = corroborate(["https://a.example/1", "https://a.example/2"])
    assert c.corroborated is False and c.domains == ["a.example"]


def test_corroborate_empty():
    c = corroborate([])
    assert c.corroborated is False and c.domains == []


def test_corroborate_custom_threshold():
    c = corroborate(["https://a.example", "https://b.example", "https://c.example"],
                    min_domains=3)
    assert c.corroborated is True


# ------------------------------------------------------------- verify_banner

def test_verify_banner_only_when_uncorroborated():
    assert verify_banner(Corroboration(True, ["a.example", "b.example"])) == ""
    assert "Verify at official source" in verify_banner(
        Corroboration(False, ["a.example"]))
