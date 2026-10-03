"""Address and category for a Maps place link, from Photon (YONDER-200, D-166).

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
