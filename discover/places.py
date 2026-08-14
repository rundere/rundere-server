"""First-party location derivation (SPEC-3-discovery.md §1, D-62).

The load-bearing idea of Discovery: Yonder has no "Location" concept and must
NOT read a trip's locations from AdventureLog — that would break first-party
ownership. Instead the set of places to research is a pure function of the trip's
OWN `TripActivity.values`, which already name places (see app/trips/types.py):
flight from/to, lodging city, tour location, cruise ports, and so on.

Resolution reuses the paths the push stage already trusts:
  * airport codes  → an injected airport resolver (AirTrail's flown reference,
    then its complete read-only airport DB), which carries country + municipality;
  * everything else → PhotonGeocoder.geocode_place(), VERIFIED ONLY — a hit that
    contradicts the stated city/country is refused (the wrong-branch guard in
    resolve/geocode.py), so an ambiguous place is DROPPED, never guessed.

A dropped place is not a silent loss: it is returned in `dropped` so the
derived-locations viewer (D-68) can show "couldn't place: <raw>" rather than
pretend the trip has fewer destinations than it does. The real destination set is
anchored by flights and lodging — the reliable, well-structured signals — and
free-text tours/dining/events that lack a stated city simply fall through to that
anchor, since they happen inside a city a flight or hotel already established.

Pure and dependency-injected (an airport resolver callable + a geocoder), so it
tests fully offline with fakes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable


@dataclass(frozen=True)
class PlaceRef:
    """One place-bearing field pulled off an activity, before resolution."""
    activity_id: str
    activity_type: str
    field: str                 # the source field: "to", "city", "venue", ...
    kind: str                  # "airport" | "place"
    raw: str                   # the literal string, for the dropped-list display
    geo: dict = field(default_factory=dict)   # geocode_place input (place kind)
    freeform: str = ""         # user-typed address/place to try if strict geocode
                               # can't verify (manual entry has no structured city)


@dataclass
class DerivedPlace:
    """A resolved destination, with the activity fields that produced it."""
    country: str | None        # ISO alpha-2, upper-cased
    city: str | None
    lat: float | None
    lon: float | None
    region: str | None = None
    provenance: list[dict] = field(default_factory=list)

    @property
    def label(self) -> str:
        return ", ".join(p for p in (self.city, self.country) if p) or "(unnamed)"

    @property
    def country_key(self) -> str | None:
        """Idempotency key for a country-scope finding, e.g. "US"."""
        return self.country

    @property
    def city_key(self) -> str | None:
        """Idempotency key for a city-scope finding, e.g. "US:Glendale"."""
        if self.country and self.city:
            return f"{self.country}:{self.city}"
        return None

    @property
    def place_key(self) -> str | None:
        """Stable identity for the whole place — the handle the exclude/include
        toggle stores. City places key on "CC:City", country-only on "CC"."""
        return self.city_key or self.country


@dataclass
class DerivedLocations:
    places: list[DerivedPlace]
    dropped: list[dict]        # [{raw, activity_type, field}]

    @property
    def countries(self) -> list[str]:
        """Distinct ISO countries, in first-seen order — the country-scope set."""
        seen, out = set(), []
        for p in self.places:
            if p.country and p.country not in seen:
                seen.add(p.country)
                out.append(p.country)
        return out

    @property
    def cities(self) -> list[dict]:
        """Distinct {city, country} — the city-scope set."""
        seen, out = set(), []
        for p in self.places:
            if p.city:
                key = (p.country or "", p.city.casefold())
                if key not in seen:
                    seen.add(key)
                    out.append({"city": p.city, "country": p.country})
        return out


def _iso(country: str | None) -> str | None:
    return country.upper() if country else None


def _looks_like_airport_code(s: str | None) -> bool:
    """An UPPER-CASE 3–4 letter token — an IATA (JFK) or ICAO (EGLL) code.

    Upper-case is required so a title-case city that happens to be four letters
    ("Oslo", "Bern") is treated as a place to geocode, not mis-read as a code
    that would fail the airport lookup and drop."""
    s = (s or "").strip()
    return s.isalpha() and s.isupper() and len(s) in (3, 4)


def _extract(activity) -> list[PlaceRef]:
    """Place-bearing fields for one activity, per the type→fields map in §1.

    `activity` is duck-typed: anything with `.id`, `.type`, and `.values`."""
    t = activity.type
    v = activity.values or {}
    refs: list[PlaceRef] = []

    def add(fname: str, kind: str, raw, geo: dict | None = None,
            freeform: str | None = None) -> None:
        raw = (raw or "").strip()
        if raw:
            refs.append(PlaceRef(activity.id, t, fname, kind, raw, geo or {},
                                 (freeform or "").strip()))

    if t == "flight":
        add("from", "airport", v.get("from"))
        add("to", "airport", v.get("to"))
    elif t == "train":
        # Stations/cities, not airport codes — geocode as a city.
        for f in ("from", "to"):
            val = v.get(f)
            add(f, "place", val, {"name": val, "raw": val, "city": val},
                freeform=val)
    elif t == "carRental":
        for f in ("pickupLocation", "dropoffLocation"):
            val = v.get(f)
            if _looks_like_airport_code(val):
                add(f, "airport", val)
            else:
                add(f, "place", val, {"name": val, "raw": val, "city": val},
                    freeform=val)
    elif t == "lodging":
        raw = v.get("name") or v.get("address") or v.get("city")
        add("lodging", "place", raw, {
            "name": v.get("name"), "raw": v.get("name") or v.get("address"),
            "address": v.get("address"), "city": v.get("city")},
            # freeform ONLY the address (a specific postal string), never a bare
            # property name — a lone chain name is the wrong-branch trap.
            freeform=v.get("address"))
    elif t == "tour":
        loc = v.get("location")
        name = v.get("name")
        add("location", "place", loc or name,
            {"name": name or loc, "raw": name or loc, "city": loc}, freeform=loc)
    elif t == "dining":
        add("address", "place", v.get("name") or v.get("address"),
            {"name": v.get("name"), "raw": v.get("name") or v.get("address"),
             "address": v.get("address")}, freeform=v.get("address"))
    elif t == "event":
        add("venue", "place", v.get("venue"),
            {"name": v.get("venue"), "raw": v.get("venue")}, freeform=v.get("venue"))
    elif t == "cruise":
        for f in ("departurePort", "arrivalPort"):
            val = v.get(f)
            add(f, "place", val, {"name": val, "raw": val, "city": val},
                freeform=val)
    # drive (free-text route) and note carry no reliably-parseable place — skipped.
    return refs


def _resolve(ref: PlaceRef, resolve_airport: Callable, geocoder) -> DerivedPlace | None:
    if ref.kind == "airport":
        ap = resolve_airport(ref.raw)
        if ap is not None and (ap.country or ap.municipality):
            return DerivedPlace(country=_iso(ap.country), city=ap.municipality,
                                lat=ap.lat, lon=ap.lon)
        return None
    res = geocoder.geocode_place(ref.geo)
    if res is not None and res.usable and (res.country or res.city):
        return DerivedPlace(country=_iso(res.country), city=res.city,
                            lat=res.lat, lon=res.lon, region=res.region)
    # Freeform fallback for a user-typed address/place the strict, verified path
    # couldn't confirm — manual entry carries no structured city/country to check
    # against. The human typed it, so (like resolve/geocode.geocode_freeform and
    # the push path) the top hit is trusted. Only address-like fields set
    # ref.freeform, so a bare chain name still can't reach here.
    if ref.freeform:
        try:
            ff = geocoder.geocode_freeform(ref.freeform)
        except Exception:
            ff = None
        if ff is not None and (ff.country or ff.city):
            return DerivedPlace(country=_iso(ff.country), city=ff.city,
                                lat=ff.lat, lon=ff.lon, region=ff.region)
    return None


def derive_locations(activities: Iterable, *, resolve_airport: Callable,
                     geocoder) -> DerivedLocations:
    """Derive a trip's destination set from its activities (D-62).

    resolve_airport: (code:str) -> Airport|None   — a complete/authoritative
                     lookup; see app.trips.routes._airport_resolver.
    geocoder:        an object with geocode_place(place:dict) -> GeocodeResult|None
                     (resolve.geocode.PhotonGeocoder).

    Returns the deduped places (each merging the activity fields that produced
    it) and the list of raw strings that could not be verified.
    """
    merged: dict[tuple, DerivedPlace] = {}
    dropped: list[dict] = []

    for activity in activities:
        for ref in _extract(activity):
            place = _resolve(ref, resolve_airport, geocoder)
            prov = {"activity_id": ref.activity_id,
                    "activity_type": ref.activity_type,
                    "field": ref.field, "raw": ref.raw}
            if place is None:
                dropped.append({"raw": ref.raw,
                                "activity_type": ref.activity_type,
                                "field": ref.field})
                continue
            key = (place.country or "", (place.city or "").casefold())
            existing = merged.get(key)
            if existing is None:
                place.provenance.append(prov)
                merged[key] = place
            else:
                existing.provenance.append(prov)
                # Backfill coords/region if an earlier, coarser hit lacked them.
                if existing.lat is None and place.lat is not None:
                    existing.lat, existing.lon = place.lat, place.lon
                if not existing.region and place.region:
                    existing.region = place.region

    return DerivedLocations(places=list(merged.values()), dropped=dropped)
