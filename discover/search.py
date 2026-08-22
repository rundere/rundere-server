"""SearXNG search helper (SPEC-3.2 §5.1).

The LLM discovery tier grounds its synthesis in real web results: each resolver
issues one or more SearXNG queries and feeds the {url, title, snippet} hits to
Ollama as the ONLY source of truth (D-90). This module is that search step.

SearXNG is already in the stack (searxng-core on travel-net). It exposes a JSON
API when settings.yml enables `search.formats: [html, json]`. No API key.

Reuses the get_json / retry pattern from discover/resolvers/base.py — stdlib
HTTP, no new dependency. Returns [] on ANY failure (unreachable, non-JSON,
empty query/url): a resolver that can't reach SearXNG degrades to a
flagged/absent finding, never a 500.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from discover.http import get_json, qs

# Widening gaps (seconds) BEFORE each retry of an empty result. Discovery's LLM
# tier runs in the background job (no wall clock — SPEC-3.2 §6), so waiting out a
# transient SearXNG dip is free; better a slow-but-complete city than a dropped
# one. len == the retry count: 3 retries (up to 4 attempts total).
_EMPTY_RETRY_BACKOFF: tuple[float, ...] = (2.0, 5.0, 12.0)

# Indirection so tests can neutralise the sleep without patching stdlib globally.
_sleep = time.sleep


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str


def searxng_search(query: str, *, base_url: str | None, categories: str = "general",
                   language: str = "en", max_results: int = 10, timeout: int = 12,
                   backoff: tuple[float, ...] = _EMPTY_RETRY_BACKOFF
                   ) -> list[SearchResult]:
    """Query SearXNG, return a flat list of {url, title, snippet}.

    A resolver may issue several queries per place and deduplicate by URL before
    feeding Ollama (§5.2), so this helper does NOT dedup — it returns exactly what
    SearXNG gave, capped at max_results and with empty-URL rows dropped.

    SearXNG federates live engines: a query can come back 200-but-EMPTY when the
    engines it fanned out to all timed out or rate-limited that instant — usually
    transient. So an empty result is RETRIED with a widening gap (`backoff`), up to
    len(backoff) times, to cut the run-to-run flakiness that drops a whole city.
    get_json separately retries a hard HTTP error."""
    if not base_url or not query.strip():
        return []
    url = f"{base_url.rstrip('/')}/search?" + qs(
        q=query, format="json", categories=categories, language=language)
    results = None
    for gap in (*backoff, None):          # one extra pass after the last gap
        data = get_json(url, timeout=timeout, retries=1)
        if not isinstance(data, dict):
            # Unreachable / bad body — SearXNG is down, not flaky. get_json already
            # retried the transient blip; a widening backoff would just burn minutes
            # across a run waiting for a service that isn't coming back. Bail fast.
            return []
        results = data.get("results")
        if results or gap is None:
            break
        _sleep(gap)                       # reachable-but-empty (engines flaked) → retry
    if not results:
        return []
    out: list[SearchResult] = []
    for item in (results or []):
        if not isinstance(item, dict):
            continue
        u = (item.get("url") or "").strip()
        if not u:
            continue
        out.append(SearchResult(
            url=u,
            title=(item.get("title") or "").strip(),
            # SearXNG names the snippet field "content".
            snippet=(item.get("content") or "").strip(),
        ))
        if len(out) >= max_results:
            break
    return out
