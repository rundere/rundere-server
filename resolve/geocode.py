"""Photon geocoding for places the document names but does not locate (D-22:
Photon runs on travel-net, avoiding Google Maps quota).

Airports come from AirTrail's own reference data (`airport_db`); this module
handles everything else — hotels, venues, stations, addresses.

**Verification is not optional here.** Measured during bring-up: querying
"SureStay Plus Casa Grande" returned the SureStay in Humble, Texas — a real
property of the same chain, 1500km from the right one, with a confident-looking
response. Chain names geocode to the wrong branch routinely. So a result is
accepted only when it is consistent with what the document already stated
(city and/or country); otherwise it is discarded and the place stays
un-geocoded, which downstream means review rather than a plausible wrong pin.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lon: float
    country: str | None          # ISO alpha-2
    city: str | None
    name: str | None
    confidence: str              # verified | unverified
    precision: str = "area"      # poi = the actual building; area = its city
    region: str | None = None    # state/province
    address: str | None = None   # street line (housenumber + street)

    @property
    def usable(self) -> bool:
        return self.confidence == "verified"


def _looks_like_address(s: str | None) -> bool:
    """True when a single string is itself a full postal address — a comma to
    separate its parts AND a digit (street number or postal code). Google Maps
    picks and iCal LOCATION values are exactly this: the whole address in one
    field, with no structured city/country to build the tiered queries below.

    A bare venue or chain name ("SureStay Madrid", "Mystery Hotel") has neither
    and must NOT be trusted to a lone-name Photon guess — that is the
    wrong-branch trap this module exists to avoid."""
    s = s or ""
    return "," in s and any(ch.isdigit() for ch in s)


def _norm(s: str | None) -> str:
    """Fold case AND diacritics before comparing: a document says "Zurich"
    while OSM says "Zürich", and a naive comparison rejects the correct hit."""
    import unicodedata
    decomposed = unicodedata.normalize("NFKD", s or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(ch for ch in stripped.casefold() if ch.isalnum())


class PhotonGeocoder:
    def __init__(self, base_url: str = "http://photon:2322", timeout: int = 10):
        self.base = (base_url or "").rstrip("/")
        self.timeout = timeout

    def _query(self, q: str, limit: int = 5) -> list[dict]:
        url = f"{self.base}/api?{urllib.parse.urlencode({'q': q, 'limit': limit})}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return []
        return data.get("features") or []

    def geocode_place(self, place: dict) -> GeocodeResult | None:
        """Locate a Place, or return None rather than guess.

        The query is built from what the document actually said, most specific
        first, so the chain-branch ambiguity is resolved by real context rather
        than by Photon's ranking.
        """
        name = place.get("name") or place.get("raw")
        if not name:
            return None
        stated_city = place.get("city")
        stated_region = place.get("region")
        stated_country = place.get("country")
        address = place.get("address")

        # Two tiers, most specific first.
        #
        # POI tier: ADDRESS-first, comma-separated, exactly as a postal address
        # is written. Booking confirmations almost always print the street
        # address — you need it to get there — so this is usually available and
        # is by far the most reliable input. Measured: "311 Ocotillo Ln, Glendale,
        # US" resolves to the correct building, while jamming the property name
        # and address into one string ("Cactus Ridge Suites 311 Ocotillo Ln") returns
        # nothing at all, and a bare chain name matches the WRONG branch.
        #
        # Area tier: the city, qualified by region and country. The fallback
        # when no address was printed. Good enough for what consumes this — the
        # 50km home-radius test, the destination country for collection naming
        # (D-23), and an approximate AdventureLog Location. Region matters:
        # "Glendale, US" alone resolves to California, but the booking was in
        # Glendale, Arizona.
        poi_queries = [q for q in (
            ", ".join(x for x in (address, stated_city, stated_region,
                                  stated_country) if x) if address else None,
            ", ".join(x for x in (address, stated_city, stated_country) if x)
            if address else None,
            # Named landmarks/venues without a street address (stations,
            # theatres) are often in OSM under their own name.
            ", ".join(x for x in (name, stated_city, stated_country) if x)
            if stated_city else None,
        ) if q]
        area_queries = [q for q in (
            ", ".join(x for x in (stated_city, stated_region, stated_country) if x),
            ", ".join(x for x in (stated_city, stated_country) if x),
        ) if q and stated_city]

        # Formatted-address tier: the whole location as one string, but ONLY when
        # it reads as a real postal address (see _looks_like_address). A Google
        # Maps / iCal LOCATION lands here — there is nothing structured to build
        # the tiers above from, yet the string itself is specific. Tried last so
        # structured document data always wins when present.
        addr_queries = [name] if _looks_like_address(name) else []

        for tier, queries in (("poi", poi_queries), ("area", area_queries),
                              ("addr", addr_queries)):
            for q in queries:
                for feature in self._query(q):
                    props = feature.get("properties") or {}
                    coords = (feature.get("geometry") or {}).get("coordinates") or []
                    if len(coords) != 2:
                        continue
                    lon, lat = coords[0], coords[1]
                    got_city = (props.get("city") or props.get("locality")
                                or props.get("county") or props.get("name"))
                    got_country = props.get("countrycode")

                    # Reject anything contradicting the document. This is the
                    # guard that catches "same chain, wrong branch, wrong state".
                    if stated_country and got_country and \
                            _norm(stated_country) != _norm(got_country):
                        continue
                    if stated_city and got_city and \
                            _norm(stated_city) not in _norm(got_city) and \
                            _norm(got_city) not in _norm(stated_city):
                        continue

                    # Precision describes the RESULT, not which query matched:
                    # a street-level hit is a building, anything else is the
                    # surrounding area. Overstating this would let a city
                    # centroid masquerade as a hotel's front door.
                    street_level = bool(props.get("housenumber") or props.get("street"))
                    # A full formatted address that resolves to a specific
                    # BUILDING is self-verifying — there is no document city to
                    # compare, but a street-level hit for the exact address the
                    # user pasted is not the wrong-branch case. A non-street hit
                    # from that tier (a park centroid, a fuzzy match) stays
                    # unverified and is dropped.
                    verified = bool((stated_city and got_city)
                                    or (stated_country and got_country)
                                    or (tier == "addr" and street_level))
                    street = props.get("street")
                    housenumber = props.get("housenumber")
                    addr_line = (f"{housenumber} {street}".strip() if street
                                 and housenumber else (street or None))
                    return GeocodeResult(
                        lat=float(lat), lon=float(lon),
                        country=(got_country or None), city=(got_city or None),
                        name=props.get("name"),
                        confidence="verified" if verified else "unverified",
                        precision="poi" if street_level else "area",
                        region=(props.get("state") or None), address=addr_line)
        return None


    def geocode_freeform(self, text: str) -> GeocodeResult | None:
        """Locate a free-text address or place name the operator typed in the
        home editor. Unlike geocode_place(), there is nothing to verify
        against — the human IS the verification — so the top hit is returned
        with whatever locality/country Photon reports. Returns None only when
        Photon finds nothing at all."""
        text = (text or "").strip()
        if not text:
            return None
        for feature in self._query(text, limit=1):
            props = feature.get("properties") or {}
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coords) != 2:
                continue
            lon, lat = coords[0], coords[1]
            city = (props.get("city") or props.get("locality")
                    or props.get("county") or props.get("name"))
            street_level = bool(props.get("housenumber") or props.get("street"))
            return GeocodeResult(
                lat=float(lat), lon=float(lon),
                country=(props.get("countrycode") or None), city=city,
                name=props.get("name"), confidence="verified",
                precision="poi" if street_level else "area")
        return None


_TZF = None


def tz_from_coords(lat, lon) -> str | None:
    """IANA zone for a coordinate (offline, via timezonefinder), or None.

    Lets a geocoded place the document never zoned — a train/bus station, a
    coordinate-only stop, a hotel — recover its timezone so a real local time
    survives commit instead of degrading to a date-only midnight stub. Flights
    get their zone from the airport's IATA tz (committing._airport_tz); this is
    the equivalent for everything else.

    Lazy-imported and fault-tolerant: if timezonefinder is unavailable or the
    lookup fails, returns None — identical to the prior behaviour, so a place
    that can't be zoned degrades exactly as it did before, never worse."""
    if lat is None or lon is None:
        return None
    try:
        global _TZF
        if _TZF is None:
            from timezonefinder import TimezoneFinder
            _TZF = TimezoneFinder()
        return _TZF.timezone_at(lat=float(lat), lng=float(lon))
    except Exception:
        return None


def apply_geocode(place: dict, result: GeocodeResult | None,
                  *, accept_unverified: bool = False) -> dict:
    """Merge a geocode into a Place. Document-stated values always win; the
    geocoder only fills blanks, and records that it did (geocode_source)."""
    if result is None:
        return place
    if result.confidence != "verified" and not accept_unverified:
        return place
    out = dict(place)
    if out.get("lat") is None or out.get("lon") is None:
        out["lat"], out["lon"] = result.lat, result.lon
        out["geocode_source"] = "photon"
    if not out.get("country") and result.country:
        out["country"] = result.country.upper()
    if not out.get("city") and result.city:
        out["city"] = result.city
    if not out.get("region") and result.region:
        out["region"] = result.region
    if not out.get("address") and result.address:
        out["address"] = result.address
    # A zone the document never stated, recovered from the resolved coordinates,
    # so a non-airport place (station, venue) keeps its real local time at commit
    # (RULE-04 / D-06). Document/reference tz always wins; this only fills a blank.
    if not out.get("tz") and out.get("lat") is not None and out.get("lon") is not None:
        tz = tz_from_coords(out["lat"], out["lon"])
        if tz:
            out["tz"] = tz
    return out
