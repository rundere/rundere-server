"""Data-licence attribution shown wherever a finding's data is displayed.

Open-Meteo's data is CC BY 4.0 and asks for "Weather data by Open-Meteo.com"
linking https://open-meteo.com/ wherever it's shown (THIRD_PARTY_NOTICES.md).
The weather card on the web, the trip's API snapshot and MCP's get_trip all
read it from here, so the wording lives in one place.

Keyed off the finding's resolver, never the configured URL: a self-hosted
TX_OPEN_METEO_URL mirror still serves Open-Meteo's data, so it keeps the
attribution. An edited weather finding keeps its resolver and so its
attribution too.
"""
from __future__ import annotations

OPEN_METEO = {"text": "Weather data by Open-Meteo.com", "url": "https://open-meteo.com/",
              "license": "CC BY 4.0"}

_BY_RESOLVER = {"weather.openmeteo": OPEN_METEO}


def for_finding(resolver: str | None) -> dict | None:
    """The attribution a finding made by `resolver` must carry, or None."""
    a = _BY_RESOLVER.get(resolver or "")
    return dict(a) if a else None
