"""Home matching and suppression (D-07). Pure functions, no I/O.

Home is effective-dated config, never a tracked Location. A 2013 flight is
judged against the home in force *then* — the owner lived near PHL before
2017, near DC 2017-2020, and in Toronto since (seeded via
`python -m app.cli seed-home`).

Suppression is a WRITE decision, not deletion (D-07): a suppressed Place keeps
every field it had — the Transportation record still needs its departure point
— what is suppressed is the AdventureLog *Location* write.

Three flags stay deliberately separate:
  is_home                 matched a profile
  suppressed + reason     the write decision
  distance_from_home_km   the radius measurement
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# IATA *metropolitan* codes: a booking system may emit the city code rather
# than the specific airport. YTO covers Toronto's airports, WAS the DC ones.
# Matching one of these against a home airport in the same group counts as home.
METRO_GROUPS: dict[str, set[str]] = {
    "YTO": {"YYZ", "YTZ", "YHM"},
    "WAS": {"DCA", "IAD", "BWI"},
    "NYC": {"JFK", "LGA", "EWR"},
    "LON": {"LHR", "LGW", "LCY", "STN", "LTN"},
    "CHI": {"ORD", "MDW"},
    "PAR": {"CDG", "ORY"},
}


@dataclass(frozen=True)
class HomeProfileRef:
    """What the matcher needs from a home_profiles row."""
    id: str
    kind: str                       # residence | home_airport | home_region
    label: str
    lat: float | None = None
    lon: float | None = None
    radius_km: float = 50.0
    country: str | None = None
    effective_from: str | None = None   # ISO date, None = open start
    effective_to: str | None = None     # ISO date, None = still current
    iata: str | None = None             # for home_airport profiles


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def profiles_in_force(profiles: list[HomeProfileRef],
                      on_date: str | None) -> list[HomeProfileRef]:
    """Profiles effective on the travel date. With no date, only currently-open
    profiles apply — guessing a historical home is worse than not suppressing."""
    if not on_date:
        return [p for p in profiles if not p.effective_to]
    d = str(on_date)[:10]
    out = []
    for p in profiles:
        if p.effective_from and d < p.effective_from:
            continue
        if p.effective_to and d >= p.effective_to:
            continue
        out.append(p)
    return out


def _airport_matches(place_iata: str, profile_iata: str) -> bool:
    if place_iata == profile_iata:
        return True
    # A metro code (YTO) matches any home airport in its group, and vice versa.
    for metro, members in METRO_GROUPS.items():
        if place_iata == metro and profile_iata in members:
            return True
        if profile_iata == metro and place_iata in members:
            return True
    return False


def match_place(place: dict, profiles: list[HomeProfileRef],
                on_date: str | None) -> tuple[HomeProfileRef | None, float | None]:
    """(matching profile, distance_km). Distance is returned whenever it can be
    computed, matched or not — RULE-27's home-radius test needs the number."""
    active = profiles_in_force(profiles, on_date)
    iata = (place.get("iata") or "").upper()
    lat, lon = place.get("lat"), place.get("lon")

    best_dist: float | None = None
    for p in active:                                  # exact/metro airport first
        if p.kind == "home_airport" and iata and p.iata and _airport_matches(iata, p.iata):
            return p, 0.0
    for p in active:                                  # then radius
        if lat is None or lon is None or p.lat is None or p.lon is None:
            continue
        dist = haversine_km(lat, lon, p.lat, p.lon)
        best_dist = dist if best_dist is None else min(best_dist, dist)
        if dist <= p.radius_km:
            return p, round(dist, 1)
    return None, (round(best_dist, 1) if best_dist is not None else None)


def annotate_place(place: dict, profiles: list[HomeProfileRef],
                   on_date: str | None) -> dict:
    """Return a copy with resolver-owned home fields set. Never mutates input,
    never drops data (D-07: suppression is not deletion)."""
    out = dict(place)
    profile, dist = match_place(place, profiles, on_date)
    if dist is not None:
        out["distance_from_home_km"] = dist
    if profile is None:
        out["is_home"] = False
        return out
    out["is_home"] = True
    out["home_ref"] = profile.id
    out["suppressed"] = True                          # RULE-23
    out["suppression_reason"] = "home" if profile.kind == "home_airport" \
        else "home_radius"                            # RULE-24b needs a reason
    return out


def within_home_radius(places: list[dict]) -> bool:
    """True when EVERY place is home — the movement never left home, so it is
    not a trip (D-07 / RULE-28). An empty list is not 'within home'."""
    return bool(places) and all(p.get("is_home") for p in places)
