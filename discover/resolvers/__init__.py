"""Resolver registry (SPEC-3-discovery.md §3).

`build_deterministic()` returns the static/API tier — plugs, currency, weather —
wired with the deployment's configured endpoints. The searxng+llm tier is added
here behind the same interface in a later slice.
"""
from __future__ import annotations

from discover.resolvers.base import Finding, Resolver, ResolverContext
from discover.resolvers.currency import CurrencyResolver
from discover.resolvers.plugs import PlugsResolver
from discover.resolvers.weather import WeatherResolver

__all__ = ["Finding", "Resolver", "ResolverContext", "build_deterministic",
           "PlugsResolver", "CurrencyResolver", "WeatherResolver"]


def build_deterministic(*, frankfurter_url: str = "https://api.frankfurter.dev/v1",
                        open_meteo_url: str = "https://api.open-meteo.com/v1",
                        open_meteo_archive_url: str = "https://archive-api.open-meteo.com/v1",
                        ) -> list[Resolver]:
    return [
        PlugsResolver(),
        CurrencyResolver(base_url=frankfurter_url),
        WeatherResolver(forecast_url=open_meteo_url, archive_url=open_meteo_archive_url),
    ]
