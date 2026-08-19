"""Resolver interface + shared helpers (SPEC-3-discovery.md §3).

A resolver turns one derived place into one Finding for one topic. Resolvers are
tiered: this module and its deterministic siblings (plugs/weather/currency) never
touch an LLM; the searxng+llm resolvers land in a later slice behind the same
interface. Kept dependency-injected and stdlib-only (urllib, like resolve/geocode)
so the deterministic tier tests offline with stubbed HTTP.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from discover.places import DerivedPlace


@dataclass
class ResolverContext:
    """Everything a resolver needs about the trip beyond the place itself."""
    start_date: str | None = None          # ISO "YYYY-MM-DD"
    end_date: str | None = None
    today: str | None = None               # ISO; injected so tests are deterministic
    base_currencies: tuple[str, ...] = ("CAD", "USD")
    timeout: int = 12
    # 3.2 (SPEC-3.2 §7): per-trip data the LLM resolvers read. Deterministic
    # resolvers ignore them. The SearXNG/Ollama ENDPOINTS are NOT here — they are
    # deployment config threaded through each LLM resolver's constructor (via
    # build_llm), exactly like frankfurter_url/open_meteo_url on the deterministic
    # tier. Only the trip-varying inputs live on the context.
    discovery_profile: dict | None = None  # the DiscoveryProfile.values blob (§2.2)
    nationality: str | None = None         # ISO-2, the `self` traveller's (visa)


@dataclass
class Finding:
    """A resolver's output — the durable DiscoveryFinding, pre-persistence."""
    topic: str
    scope: str                             # "country" | "city"
    place_key: str                         # "CH" | "CH:Geneva"
    body_md: str
    data: dict | None = None
    sources: list[dict] = field(default_factory=list)
    confidence: str = "high"               # high | medium | flagged
    resolver: str = ""
    ttl_seconds: int | None = None


class Resolver(Protocol):
    topic: str
    scope: str                             # "country" | "city"

    def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None:
        ...


def get_json(url: str, timeout: int = 12, retries: int = 1) -> dict | None:
    """GET a JSON body, or None on any failure — a resolver that can't reach its
    source degrades to a flagged/absent finding, never a 500.

    One retry by default: the public rate feeds (Frankfurter behind Cloudflare)
    flap intermittently, and a lone transient shouldn't silently drop a base
    currency or a whole topic."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Yonder-Discovery"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            if attempt >= retries:
                return None
    return None


def qs(**params) -> str:
    """Encode query params, dropping None — keeps resolver URL-building terse."""
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
