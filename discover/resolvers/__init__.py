"""Resolver registry (SPEC-3-discovery.md §3, SPEC-3.2 §10).

`build_deterministic()` returns the static/API tier — plugs, currency, weather,
language — wired with the deployment's configured endpoints. `build_llm()`
returns the searxng+ollama tier behind the same interface; it runs in the
background job (a later slice), not the synchronous deterministic pass.
"""
from __future__ import annotations

from discover.resolvers.base import Finding, Resolver, ResolverContext
from discover.resolvers.currency import CurrencyResolver
from discover.resolvers.events import EventsResolver
from discover.resolvers.food import FoodResolver
from discover.resolvers.language import LanguageResolver
from discover.resolvers.plugs import PlugsResolver
from discover.resolvers.safety import SafetyResolver
from discover.resolvers.weather import WeatherResolver

__all__ = ["Finding", "Resolver", "ResolverContext", "build_deterministic",
           "build_llm", "PlugsResolver", "CurrencyResolver", "WeatherResolver",
           "LanguageResolver", "FoodResolver", "EventsResolver", "SafetyResolver"]


def build_deterministic(*, frankfurter_url: str = "https://api.frankfurter.dev/v1",
                        open_meteo_url: str = "https://api.open-meteo.com/v1",
                        open_meteo_archive_url: str = "https://archive-api.open-meteo.com/v1",
                        ) -> list[Resolver]:
    return [
        PlugsResolver(),
        CurrencyResolver(base_url=frankfurter_url),
        WeatherResolver(forecast_url=open_meteo_url, archive_url=open_meteo_archive_url),
        LanguageResolver(),
    ]


def build_llm(*, searxng_url: str, ollama_url: str,
              model: str = "qwen3:14b", timeout: int = 600) -> list[Resolver]:
    """The LLM tier (§10). More resolvers (events, visa, safety) land here behind
    the same interface as they ship."""
    return [
        FoodResolver(searxng_url=searxng_url, ollama_url=ollama_url,
                     model=model, timeout=timeout),
        EventsResolver(searxng_url=searxng_url, ollama_url=ollama_url,
                       model=model, timeout=timeout),
        SafetyResolver(searxng_url=searxng_url, ollama_url=ollama_url,
                       model=model, timeout=timeout),
    ]
