"""Home suppression for Discovery (SPEC-3-discovery.md; reuses D-07 home logic).

You don't research home. A trip's derived set includes the origin — for a
Toronto-based traveller a YYZ→GVA trip yields both Toronto and Geneva — and
briefing Canada's currency/plugs/language back to a Canadian is noise.

The suppression is scope-aware, which matters for a domestic leg:

  * COUNTRY-scope topics (currency, plugs, visa, language, safety) are identical
    to home whenever the destination COUNTRY is home, so they are skipped for any
    home-country place — Toronto AND a domestic Vancouver both skip these.
  * CITY-scope topics (weather, events, food) are skipped only for a place that
    actually matches a home profile. Vancouver is in the home country but is not
    home, so its weather/events/food still run.

Pure; leans entirely on resolve/home.py (the same matcher the capture pipeline
uses for is_home), so Discovery and commit never disagree on what home is. Home
profiles are first-party Yonder config — using them here keeps Discovery
first-party (no AdventureLog dependency).
"""
from __future__ import annotations

from discover.places import DerivedPlace
from resolve.home import HomeProfileRef, match_place, profiles_in_force


def is_home_place(place: DerivedPlace, profiles: list[HomeProfileRef],
                  on_date: str | None) -> bool:
    """True when this specific place matches a home profile (airport or radius).

    DerivedPlace carries no IATA, so this is a coordinate/radius match — a home
    residence's lat/lon + radius_km catches the home city; a foreign place never
    matches."""
    if place.lat is None or place.lon is None:
        return False
    profile, _ = match_place({"lat": place.lat, "lon": place.lon}, profiles, on_date)
    return profile is not None


def home_country_set(places: list[DerivedPlace], profiles: list[HomeProfileRef],
                     on_date: str | None) -> set[str]:
    """The ISO countries treated as home. Taken from home profiles that state a
    country, PLUS the country of any derived place that matches a home profile —
    so suppression still works when a residence profile omits its country but its
    coordinates place it (the common case)."""
    active = profiles_in_force(profiles, on_date)
    countries = {p.country.upper() for p in active if p.country}
    for place in places:
        if place.country and is_home_place(place, profiles, on_date):
            countries.add(place.country.upper())
    return countries
