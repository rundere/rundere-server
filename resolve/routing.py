"""Drive distance and time from a directions link (road trips).

A Google or Apple Maps directions link names its stops (sometimes with exact
coordinates) but never the distance or the time it takes. This module turns a
link into located stops and asks an OSRM server for the driving route, so a
road trip shows "210 km (130 mi) · ~2 h 10 min" without anyone typing it.

Everything here is best-effort: a stop that can't be located, an unreachable
router or an unknown link returns None, and the caller leaves the field for a
person to fill. Nothing here ever raises into a request or a resolve run.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

_MAPS_LATLON = re.compile(r"!1d(-?\d+\.\d+)!2d(-?\d+\.\d+)")
_COORD = re.compile(r"^\s*(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)\s*$")


@dataclass(frozen=True)
class RouteEstimate:
    distance_km: float
    duration_minutes: int
    # The road line itself, Google "polyline6" encoded (precision 1e-6),
    # simplified for display — what a map draws A → B along the roads.
    polyline: str | None = None


def is_maps_link(url: str | None) -> bool:
    """A Google or Apple Maps link (directions, search or place)."""
    u = (url or "").strip().lower()
    if not u.startswith("http"):
        return False
    host = (urlsplit(u).hostname or "")
    return (("google." in host and "/maps" in u) or host in {"maps.app.goo.gl", "g.co"}
            or ("goo.gl" in host and "/maps" in u)
            or host in {"maps.apple.com", "maps.apple"})


def directions_stops(url: str) -> list[dict]:
    """The ordered stops of a Google or Apple Maps directions link, each a Place
    dict ({raw, name, city?, region?, lat?, lon?}), or [] when the link isn't
    a directions link. Short Google links are expanded (Google hosts only)."""
    from extract.apple_maps_url import looks_like_apple_maps_url
    from extract.directions_common import _parse_place
    from extract.maps_url import _expand_short, _segments

    url = (url or "").strip()
    if looks_like_apple_maps_url(url):
        q = parse_qs(urlsplit(url).query)
        segments = [q["saddr"][0].strip(), q["daddr"][0].strip()]
        embedded: list[tuple[str, str]] = []
    else:
        url = _expand_short(url)
        segments = _segments(url)
        embedded = _MAPS_LATLON.findall(url)            # (lon, lat) in route order
    if len(segments) < 2:
        return []
    stops = [_parse_place(s) for s in segments]
    for p in stops:
        m = _COORD.match(p.get("raw") or "")
        if m:
            p["lat"], p["lon"] = float(m.group(1)), float(m.group(2))
    if len(embedded) == len(stops):
        for p, (lon, lat) in zip(stops, embedded):
            if p.get("lat") is None:
                p["lat"], p["lon"] = float(lat), float(lon)
    return stops


def locate(place: dict, geocoder) -> dict:
    """Fill a stop's lat/lon: first the verified tiers (geocode_place), then —
    because a person chose this stop in their own maps app — the top free-text
    hit for its name. Returns the place unchanged when nothing is found."""
    if place.get("lat") is not None or geocoder is None:
        return place
    from resolve.geocode import apply_geocode
    try:
        out = apply_geocode(place, geocoder.geocode_place(place))
        if out.get("lat") is None:
            out = apply_geocode(out, geocoder.geocode_freeform(place.get("raw") or ""),
                                accept_unverified=True)
        return out
    except Exception:
        return place


def route(stops: list[tuple[float, float]], *, base_url: str,
          timeout: int = 8) -> RouteEstimate | None:
    """Driving distance/time through (lat, lon) stops, in order, from an OSRM
    server. None on any failure or when routing is switched off."""
    base = (base_url or "").rstrip("/")
    if not base or len(stops) < 2:
        return None
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in stops)
    url = f"{base}/route/v1/driving/{coords}?overview=simplified&geometries=polyline6"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "yonder"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    routes = data.get("routes") or []
    if data.get("code") != "Ok" or not routes:
        return None
    r = routes[0]
    geometry = r.get("geometry")
    return RouteEstimate(distance_km=round(float(r["distance"]) / 1000, 1),
                         duration_minutes=int(round(float(r["duration"]) / 60)),
                         polyline=geometry if isinstance(geometry, str) else None)


def estimate_for_places(places: list[dict], *, base_url: str) -> RouteEstimate | None:
    """Route through already-located places; None if any stop lacks coords."""
    pts = [(p.get("lat"), p.get("lon")) for p in places if isinstance(p, dict)]
    if len(pts) < 2 or any(la is None or lo is None for la, lo in pts):
        return None
    return route([(float(la), float(lo)) for la, lo in pts], base_url=base_url)


def estimate_for_link(url: str, *, geocoder, base_url: str
                      ) -> tuple[list[dict], RouteEstimate | None]:
    """(located stops, estimate) for a directions link. The stops come back even
    when routing fails, so a caller can still name the route."""
    stops = [locate(p, geocoder) for p in directions_stops(url)]
    return stops, (estimate_for_places(stops, base_url=base_url) if stops else None)


def format_distance(est: RouteEstimate) -> str:
    """'210 km (130 mi) · ~2 h 10 min' — both units, since trips cross borders."""
    km = est.distance_km
    mi = km / 1.609344
    dist = f"{km:.0f} km ({mi:.0f} mi)" if km >= 10 else f"{km:.1f} km ({mi:.1f} mi)"
    h, m = divmod(est.duration_minutes, 60)
    t = f"{h} h {m} min" if h else f"{m} min"
    return f"{dist} · ~{t}"


def stop_label(place: dict | None) -> str | None:
    """Short name for one stop in a route title: the place's own name (its first
    comma part), not its city — 'Heritage Square', not 'Phoenix'."""
    if not isinstance(place, dict):
        return None
    name = (place.get("name") or place.get("raw") or "").strip()
    if _COORD.match(name):
        return place.get("city") or name
    first = name.split(",")[0].strip()
    return first or place.get("city") or None


def stop_address(place: dict | None) -> str | None:
    """The fullest description of a stop for a From/To field: its street address
    with city when known, else the text the link or document gave."""
    if not isinstance(place, dict):
        return None
    addr = (place.get("address") or "").strip()
    raw = (place.get("raw") or place.get("name") or "").strip()
    if addr:
        city = place.get("city")
        if city and city.lower() not in addr.lower():
            addr = f"{addr}, {city}"
        name = stop_label(place)
        if name and name.lower() not in addr.lower() and not any(c.isdigit() for c in name):
            return f"{name}, {addr}"
        return addr
    return raw or None


def decode_polyline(encoded: str, precision: int = 6) -> list[tuple[float, float]]:
    """(lat, lon) points from an encoded polyline (polyline6 by default)."""
    points, lat, lon, i, factor = [], 0, 0, 0, 10 ** precision
    while i < len(encoded or ""):
        for axis in (0, 1):
            shift = result = 0
            while True:
                b = ord(encoded[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        points.append((lat / factor, lon / factor))
    return points


_PIN = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")        # Google place pin
_AT = re.compile(r"/@(-?\d+\.\d+),(-?\d+\.\d+)")             # Google viewport
_QUERY_COORD_KEYS = ("q", "query", "ll", "sll", "coordinate", "destination")


def link_point(url: str | None) -> tuple[float, float] | None:
    """The (lat, lon) a Google/Apple Maps PLACE link points at, or None. The
    place pin (!3d…!4d…) beats the viewport centre (/@lat,lon); Apple and
    query-style links carry it as ll= / q= / coordinate=. A Google short link is
    expanded first (Google hosts only). Directions links are not places."""
    url = (url or "").strip()
    if not is_maps_link(url):
        return None
    from extract.maps_url import _expand_short
    url = _expand_short(url)
    if "/maps/dir/" in url or ("saddr=" in url and "daddr=" in url):
        return None
    for rx in (_PIN, _AT):
        m = rx.search(url)
        if m:
            return float(m.group(1)), float(m.group(2))
    q = parse_qs(urlsplit(url).query)
    for key in _QUERY_COORD_KEYS:
        m = _COORD.match((q.get(key) or [""])[0])
        if m:
            return float(m.group(1)), float(m.group(2))
    return None
