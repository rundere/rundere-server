"""SearXNG search helper (SPEC-3.2 §5.1).

Offline: the get_json call inside discover.search is stubbed. Covers result
parsing, the max_results cap, empty-URL rows, and the graceful-empty degrade
paths (no base_url, blank query, unreachable, non-dict body).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discover.search as search_mod
from discover.search import SearchResult, searxng_search


def _fake(results):
    return lambda url, timeout=12, retries=1: {"results": results}


def test_parses_results(monkeypatch):
    monkeypatch.setattr(search_mod, "get_json", _fake([
        {"url": "https://a.example/1", "title": "One", "content": "first snippet"},
        {"url": "https://b.example/2", "title": "Two", "content": "second"},
    ]))
    out = searxng_search("best restaurants lisbon", base_url="http://searxng:8080")
    assert out == [
        SearchResult("https://a.example/1", "One", "first snippet"),
        SearchResult("https://b.example/2", "Two", "second"),
    ]


def test_respects_max_results(monkeypatch):
    rows = [{"url": f"https://x.example/{i}", "title": str(i), "content": ""}
            for i in range(20)]
    monkeypatch.setattr(search_mod, "get_json", _fake(rows))
    out = searxng_search("q", base_url="http://searxng:8080", max_results=3)
    assert len(out) == 3 and out[0].url == "https://x.example/0"


def test_drops_empty_url_rows(monkeypatch):
    monkeypatch.setattr(search_mod, "get_json", _fake([
        {"url": "", "title": "no url", "content": "x"},
        {"title": "missing url key", "content": "y"},
        {"url": "https://ok.example", "title": "kept", "content": "z"},
    ]))
    out = searxng_search("q", base_url="http://searxng:8080")
    assert [r.url for r in out] == ["https://ok.example"]


def test_empty_result_is_retried(monkeypatch):
    # SearXNG can come back 200-but-empty when its engines flake — one empty
    # response is retried, and the second (non-empty) one is used.
    calls = {"n": 0}

    def flaky(url, timeout=12, retries=1):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"results": []}                # transient empty
        return {"results": [{"url": "https://ok.example", "title": "t", "content": "c"}]}

    monkeypatch.setattr(search_mod, "get_json", flaky)
    out = searxng_search("q", base_url="http://searxng:8080", retries=1)
    assert calls["n"] == 2                          # retried once
    assert [r.url for r in out] == ["https://ok.example"]


def test_persistently_empty_gives_up(monkeypatch):
    calls = {"n": 0}

    def always_empty(url, timeout=12, retries=1):
        calls["n"] += 1
        return {"results": []}

    monkeypatch.setattr(search_mod, "get_json", always_empty)
    out = searxng_search("q", base_url="http://searxng:8080", retries=1)
    assert out == [] and calls["n"] == 2            # retried once, then gave up


def test_builds_json_query(monkeypatch):
    seen = {}

    def spy(url, timeout=12, retries=1):
        seen["url"] = url
        return {"results": []}

    monkeypatch.setattr(search_mod, "get_json", spy)
    searxng_search("café & bars", base_url="http://searxng:8080/",
                   categories="general", language="en")
    assert seen["url"].startswith("http://searxng:8080/search?")   # trailing / trimmed
    assert "format=json" in seen["url"]
    assert "caf" in seen["url"] and "%26" in seen["url"]            # query url-encoded


def test_empty_on_no_base_url(monkeypatch):
    # must not even call get_json when there's no endpoint
    monkeypatch.setattr(search_mod, "get_json",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert searxng_search("q", base_url=None) == []
    assert searxng_search("q", base_url="") == []


def test_empty_on_blank_query(monkeypatch):
    monkeypatch.setattr(search_mod, "get_json",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert searxng_search("   ", base_url="http://searxng:8080") == []


def test_empty_on_unreachable(monkeypatch):
    monkeypatch.setattr(search_mod, "get_json", lambda url, timeout=12, retries=1: None)
    assert searxng_search("q", base_url="http://searxng:8080") == []


def test_empty_on_non_dict_body(monkeypatch):
    monkeypatch.setattr(search_mod, "get_json", lambda url, timeout=12, retries=1: [1, 2])
    assert searxng_search("q", base_url="http://searxng:8080") == []
