"""Address and category for a Maps place link, and a pin for an address, from
Photon (YONDER-200, D-166, YONDER-339).

Optional enrichment (SPEC-8 D-146): a Google/Apple place URL carries a name and a
pin but no address and no category. When Photon is configured this asks it for
both; with Photon unset, down or unsure it returns nothing and the place keeps
its name and pin.

**Verification is not optional** (the same trap resolve/geocode.py documents:
chain names geocode to the wrong branch with a confident-looking answer). A hit
is accepted only when it is within `NEAR_M` metres of the link's own pin AND its
name matches the link's. Without a pin there is nothing to verify against, so a
name-only place is never looked up. The category comes back as an OSM `key=value`
tag, the form `extract/place_url.category_for` understands.
"""
from __future__ import annotations

import math

from resolve.geocode import _norm

NEAR_M = 250
_MIN_NAME = 4
# Countries that write the house number first ("155 W 51st St").
_NUMBER_FIRST = {"US", "CA", "GB", "AU", "NZ", "IE"}


def _metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371000 * 2 * math.asin(math.sqrt(a))


def _names_match(a: str | None, b: str | None) -> bool:
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    return x == y or (min(len(x), len(y)) >= _MIN_NAME and (x in y or y in x))


def _address(props: dict) -> str | None:
    street, number = props.get("street"), props.get("housenumber")
    if street and number:
        first = (props.get("countrycode") or "").upper() in _NUMBER_FIRST
        line = f"{number} {street}" if first else f"{street} {number}"
    else:
        line = street
    city = props.get("city") or props.get("locality") or props.get("district")
    parts = [x for x in (line, city) if x]
    return ", ".join(parts) or None


def _default_geocoder():
    from app.config import settings
    from resolve.geocode import PhotonGeocoder
    # Short timeout: this runs while a person waits on a share or a paste, and
    # losing the enrichment costs only an address and a category (D-146).
    return PhotonGeocoder(settings.photon_url, timeout=3) if settings.photon_url else None


def enrich(name: str | None, lat: float | None, lon: float | None, *,
           geocoder=None) -> dict:
    """`{address?, category?}` for a named pin, or `{}` when Photon is off, finds
    nothing, or nothing it finds is verified. Never raises."""
    if not name or lat is None or lon is None:
        return {}
    gc = geocoder if geocoder is not None else _default_geocoder()
    if gc is None:
        return {}
    try:
        features = gc._query(name, limit=5, near=(lat, lon))
    except Exception:
        return {}
    best = None
    for f in features or []:
        props = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if len(coords) != 2 or not _names_match(name, props.get("name")):
            continue
        d = _metres(lat, lon, float(coords[1]), float(coords[0]))
        if d <= NEAR_M and (best is None or d < best[0]):
            best = (d, props)
    if best is None:
        return {}
    props = best[1]
    key, val = props.get("osm_key"), props.get("osm_value")
    out = {"address": _address(props),
           "category": f"{key}={val}" if key and val else None}
    return {k: v for k, v in out.items() if v}


# --- A pin from an address (YONDER-339) ----------------------------------------
#
# A Google share link carries a name and an address but no pin, and the person can
# type or correct an address in the place panel. Measured against a real Photon
# (2026-10-29): "Name, street, city, country" finds the building; adding the
# postcode ("…, ON M5V 1X2") moved the top hit to a different building on the same
# street, and the geocoder still called it verified. So the postcode never goes in
# the query, and a hit counts only when its house number is the one asked for, or
# its name is the place's own. A wrong pin is worse than none (SPEC-11 D-215).

import re  # noqa: E402

_POSTCODE = re.compile(
    r"\b[A-Z]\d[A-Z] ?\d[A-Z]\d\b"                    # Canada: M5V 1X2
    r"|\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b"          # UK: SW1A 1AA
    r"|(?<=\b[A-Z]{2} )\d{5}(?:-\d{4})?\b",           # US, after the state: NY 10001
    re.I)
_REGION_ONLY = re.compile(r"^[A-Z]{2,3}$")            # "ON", "NY", "NSW"
_HOUSE_FIRST = re.compile(r"^(\d+[a-z]?)\b", re.I)    # "300 Richmond St W"
_HOUSE_LAST = re.compile(r"\b(\d+[a-z]?)$", re.I)     # "Rua Augusta 24"
_AREAS = {"country", "state", "county", "city", "district", "locality", "postcode"}


def _parts(address: str | None) -> list[str]:
    """The address split at its commas, with postcodes and bare region codes
    dropped: "300 Richmond St W, Toronto, ON M5V 1X2" → [street, "Toronto"]."""
    out = []
    for seg in (address or "").split(","):
        seg = _POSTCODE.sub("", seg).strip(" -")
        if seg and not _REGION_ONLY.match(seg):
            out.append(seg)
    return out


def _street(parts: list[str]) -> tuple[str | None, str | None]:
    """(street line, house number) — the first part with a digit in it."""
    for seg in parts:
        if any(ch.isdigit() for ch in seg):
            m = _HOUSE_FIRST.match(seg) or _HOUSE_LAST.search(seg)
            return seg, (m.group(1).lower() if m else None)
    return None, None


def _same_street(line: str | None, got: str | None) -> bool:
    """"300 Richmond St W" and "Richmond Street West" are one street: the first
    word of the street name, past the house number, appears in Photon's."""
    words = [w for w in re.split(r"[\s.]+", line or "") if w and not w[0].isdigit()]
    return bool(words) and bool(got) and _norm(words[0]) in _norm(got)


def locate(name: str | None, address: str | None, *, locality: str | None = None,
           country_code: str | None = None, geocoder=None) -> dict | None:
    """A verified pin for a place that has none, as `{lat, lon, locality, region,
    country_code, address}`, or None. A hit must be in the stated country and
    city. When the address gives a house number, the hit must have that number on
    that street; with no street, it must bear the place's own name. A street, a
    town or a city centre is never a pin. Never raises."""
    from app.places_geo import country_name
    gc = geocoder if geocoder is not None else _default_geocoder()
    if gc is None:
        return None
    parts = _parts(address)
    street, number = _street(parts)
    city = locality or next((p for p in parts if p != street), None)
    code = (country_code or "").upper()[:2] or None
    # The country by name: "CA" in free text can read as California.
    country = country_name(code) if code else None
    queries = []
    for q in ((name, street, city, country) if street and name else None,
              (street, city, country) if street else None,
              (name, city, country) if name and city else None):
        text = ", ".join(x for x in q or () if x)
        if text and text not in queries:
            queries.append(text)
    for text in queries:
        try:
            features = gc._query(text, limit=5)
        except Exception:
            return None
        for f in features or []:
            props = f.get("properties") or {}
            coords = (f.get("geometry") or {}).get("coordinates") or []
            if len(coords) != 2:
                continue
            got_code = (props.get("countrycode") or "").upper()
            got_city = props.get("city") or props.get("locality") or props.get("district")
            if code and got_code and got_code != code:
                continue
            if city and got_city and _norm(city) not in _norm(got_city) \
                    and _norm(got_city) not in _norm(city):
                continue
            # An area is never a pin. Photon files address points as place=house,
            # so only the other place=* values are areas.
            if props.get("type") in _AREAS or (props.get("osm_key") == "place"
                                               and props.get("osm_value") != "house"):
                continue
            if number:
                # The address is the identity: a chain's other branch has the same
                # name (measured: a second "Illstyl3 Sammies" two km away), so with
                # a house number the name proves nothing.
                if _norm(props.get("housenumber")) != _norm(number) \
                        or not _same_street(street, props.get("street")):
                    continue
            elif not _names_match(name, props.get("name")):
                continue
            return {"lat": float(coords[1]), "lon": float(coords[0]),
                    "locality": got_city, "region": props.get("state"),
                    "country_code": got_code or code, "address": _address(props)}
    return None
