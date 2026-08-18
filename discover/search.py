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

from dataclasses import dataclass

from discover.resolvers.base import get_json, qs


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str


def searxng_search(query: str, *, base_url: str | None, categories: str = "general",
                   language: str = "en", max_results: int = 10,
                   timeout: int = 12, retries: int = 1) -> list[SearchResult]:
    """Query SearXNG, return a flat list of {url, title, snippet}.

    A resolver may issue several queries per place and deduplicate by URL before
    feeding Ollama (§5.2), so this helper does NOT dedup — it returns exactly what
    SearXNG gave, capped at max_results and with empty-URL rows dropped."""
    if not base_url or not query.strip():
        return []
    url = f"{base_url.rstrip('/')}/search?" + qs(
        q=query, format="json", categories=categories, language=language)
    data = get_json(url, timeout=timeout, retries=retries)
    if not isinstance(data, dict):
        return []
    out: list[SearchResult] = []
    for item in (data.get("results") or []):
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
