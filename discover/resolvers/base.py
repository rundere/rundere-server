"""Resolver interface + shared helpers (SPEC-3-discovery.md §3).

A resolver turns one derived place into one Finding for one topic. Resolvers are
tiered: this module and its deterministic siblings (plugs/weather/currency) never
touch an LLM; the searxng+llm resolvers land in a later slice behind the same
interface. Kept dependency-injected and stdlib-only (urllib, like resolve/geocode)
so the deterministic tier tests offline with stubbed HTTP.
"""
from __future__ import annotations

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
    # 3.6: the editable language datasets (discover/langdata), loaded from the DB
    # once per run and injected here. None → the resolver falls back to the JSON
    # seed (tests / standalone), so resolvers never need a DB session themselves.
    phrasebook: dict | None = None         # {lang: {key: {t, p}}} — every phrase
    regional_map: dict | None = None       # {country: {region_lower: [lang]}}
    generated_languages: frozenset = frozenset()   # langs whose phrases are generated-only


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


# get_json / qs moved to the leaf module discover.http (to break the
# search ↔ resolvers import cycle); re-exported here for the resolvers and any
# caller that still imports them from discover.resolvers.base.
from discover.http import get_json, qs  # noqa: E402,F401
