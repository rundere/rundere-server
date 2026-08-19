"""Pass 1 — read the trip (SPEC-3.1-planning.md §2, D-73).

Derive everything the engine needs first-party: duration, destinations, climate
(from the Discovery weather findings — no second weather call, D-75), transport,
accommodation, activity tags, cultural norms, and the packing profile. Nothing the
trip already captured is re-entered (D-80): activity tags come from TripActivity
TYPES, sharpened by keywords already present in the captured names/locations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from planning import culture

# TripActivity type → transport mode (for archetype scoring + "international").
_TRANSPORT = {"flight": "flight", "train": "train", "drive": "car",
              "carRental": "car", "cruise": "cruise"}

# TripActivity type → the baseline archetype activity tag it implies.
_TYPE_TAGS = {"tour": "sightseeing", "dining": "dining", "event": "nightlife",
              "cruise": "cruise"}

# Keyword → archetype tag, scanned over the free-text the trip ALREADY holds
# (tour name/location, event/dining name). Not re-entry — using captured data.
_KEYWORDS = {
    "temple": ["temple", "shrine", "mosque", "church", "cathedral", "pilgrim",
               "monastery", "pagoda", "religious", "sanctuary", "basilica"],
    "hiking": ["hike", "hiking", "trek", "trail", "mountain", "summit",
               "national park", "backcountry", "walk"],
    "beach": ["beach", "snorkel", "dive", "surf", "swim", "lagoon", "reef",
              "coast", "island", "sail"],
    "ski": ["ski", "snowboard", "slope", "piste", "alpine", "snow"],
    "business": ["business", "conference", "meeting", "summit", "expo",
                 "corporate", "gala", "keynote"],
}

# Free-text lodging type → the archetype accommodation vocabulary.
_ACCOM = {"hotel": "hotel", "hostel": "hostel", "resort": "resort",
          "bnb": "bnb", "airbnb": "bnb", "b&b": "bnb", "apartment": "apartment",
          "flat": "apartment", "campground": "campground", "camping": "campground",
          "cabin": "cabin", "lodge": "cabin", "motel": "motel",
          "guesthouse": "guesthouse", "ryokan": "ryokan", "villa": "resort"}


@dataclass
class PlanningContext:
    duration_days: int
    countries: list[str] = field(default_factory=list)
    cities: list[dict] = field(default_factory=list)
    transport: set[str] = field(default_factory=set)
    accommodation: set[str] = field(default_factory=set)
    activity_tags: set[str] = field(default_factory=set)
    climate_bands: set[str] = field(default_factory=set)   # cold_weather|layering|lightweight|rain_gear|humid
    temp_high_c: int | None = None
    temp_low_c: int | None = None
    has_weather: bool = False
    international: bool = False
    profile: dict = field(default_factory=dict)
    culture: dict = field(default_factory=dict)

    def snapshot(self) -> dict:
        """A JSON-able copy for PackingPlan.context."""
        return {"duration_days": self.duration_days, "countries": self.countries,
                "cities": self.cities, "transport": sorted(self.transport),
                "accommodation": sorted(self.accommodation),
                "activity_tags": sorted(self.activity_tags),
                "climate_bands": sorted(self.climate_bands),
                "temp_high_c": self.temp_high_c, "temp_low_c": self.temp_low_c,
                "has_weather": self.has_weather, "international": self.international,
                "culture": self.culture}


def _duration(start: str | None, end: str | None) -> int:
    try:
        d = (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days + 1
        return max(d, 1)
    except (TypeError, ValueError):
        return 1


def _climate_bands(high: int | None, low: int | None,
                   rain_days: int) -> set[str]:
    bands: set[str] = set()
    if high is not None:
        if high < 12:
            bands.add("cold_weather")           # genuinely cold days → warm layer
        elif high <= 21:
            bands.add("layering")
        else:
            bands.add("lightweight")
        # Cold nights but mild/warm days (desert, altitude) call for a warm layer,
        # not a parka — a 2°C night after a 20°C day is layering, not cold_weather.
        if low is not None and low < 3 and high >= 12:
            bands.add("layering")
    if rain_days >= 2:
        bands.add("rain_gear")
    return bands


def derive_context(activities, derived_locations, weather_findings, *,
                   start_date: str | None, end_date: str | None,
                   profile: dict, home_countries: set[str]) -> PlanningContext:
    """Assemble the planning context.

    `derived_locations` is a discover.places.DerivedLocations; `weather_findings`
    is the list of DiscoveryFinding rows with topic='weather' (their `data` carries
    high_c/low_c/rain_days). Both are passed in so this stays pure/testable — the
    runner wires the real ones."""
    ctx = PlanningContext(duration_days=_duration(start_date, end_date))
    ctx.countries = list(derived_locations.countries)
    ctx.cities = list(derived_locations.cities)
    ctx.international = any(c.upper() not in home_countries for c in ctx.countries)
    ctx.profile = dict(profile or {})

    for a in activities:
        t = a.type
        if t in _TRANSPORT:
            ctx.transport.add(_TRANSPORT[t])
        if t in _TYPE_TAGS:
            ctx.activity_tags.add(_TYPE_TAGS[t])
        v = a.values or {}
        if t == "lodging":
            raw = (v.get("type") or "").strip().lower()
            if raw in _ACCOM:
                ctx.accommodation.add(_ACCOM[raw])
        # keyword sharpening over already-captured free text
        blob = " ".join(str(v.get(k) or "") for k in
                        ("name", "location", "venue", "route")).lower()
        for tag, words in _KEYWORDS.items():
            if any(w in blob for w in words):
                ctx.activity_tags.add(tag)

    # Climate from the Discovery weather findings (D-75).
    highs, lows, rain = [], [], 0
    for f in weather_findings:
        d = f.data or {}
        if isinstance(d.get("high_c"), (int, float)):
            highs.append(d["high_c"])
        if isinstance(d.get("low_c"), (int, float)):
            lows.append(d["low_c"])
        if isinstance(d.get("rain_days"), (int, float)):
            rain = max(rain, int(d["rain_days"]))
    if highs:
        ctx.has_weather = True
        ctx.temp_high_c = max(highs)
        ctx.temp_low_c = min(lows) if lows else None
        ctx.climate_bands = _climate_bands(ctx.temp_high_c, ctx.temp_low_c, rain)

    ctx.culture = culture.summarise(ctx.countries)
    return ctx
