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
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("yonder.routing")

_MAPS_LATLON = re.compile(r"!1d(-?\d+\.\d+)!2d(-?\d+\.\d+)")
_COORD = re.compile(r"^\s*(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)\s*$")


@dataclass(frozen=True)
class RouteEstimate:
    distance_km: float
    duration_minutes: int
    # The road line itself, Google "polyline6" encoded (precision 1e-6),
    # simplified for display — what a map draws A → B along the roads.
    polyline: str | None = None
    provider: str = "osrm"


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


# ---------------------------------------------------------------- routers
#
# Three interchangeable road routers, tried in the configured order
# (TX_ROUTING_PROVIDERS, default "valhalla,ors,osrm"); an unconfigured one is
# skipped and a failure — including "no road data here", which is how a
# self-hosted home-region Valhalla answers for Europe or India — falls through
# to the next. So: self-host for home, a hosted API for everywhere else.
#   valhalla  TX_VALHALLA_URL — the full /route endpoint: self-hosted
#             (http://valhalla:8002/route) or Stadia Maps
#             (https://api.stadiamaps.com/route/v1 + TX_VALHALLA_API_KEY)
#   ors       TX_ORS_API_KEY — openrouteservice.org (worldwide, free key tier)
#   osrm      TX_OSRM_URL — self-hosted OSRM or the public demo server

_UA = {"User-Agent": "rundere (+https://github.com/rundere/rundere-server)"}


def _http_json(url: str, *, body: dict | None = None, headers: dict | None = None,
               timeout: int = 8):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {**_UA, **(headers or {})}
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _route_osrm(stops, *, base_url: str, timeout: int = 8) -> RouteEstimate | None:
    base = (base_url or "").rstrip("/")
    if not base:
        return None
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in stops)
    data = _http_json(f"{base}/route/v1/driving/{coords}"
                      "?overview=simplified&geometries=polyline6", timeout=timeout)
    routes = data.get("routes") or []
    if data.get("code") != "Ok" or not routes:
        return None
    r = routes[0]
    geometry = r.get("geometry")
    return RouteEstimate(distance_km=round(float(r["distance"]) / 1000, 1),
                         duration_minutes=int(round(float(r["duration"]) / 60)),
                         polyline=geometry if isinstance(geometry, str) else None,
                         provider="osrm")


def _route_valhalla(stops, *, url: str, api_key: str = "",
                    timeout: int = 8) -> RouteEstimate | None:
    if not url:
        return None
    if api_key:
        url += ("&" if "?" in url else "?") + "api_key=" + urllib.parse.quote(api_key)
    data = _http_json(url, timeout=timeout, body={
        "locations": [{"lat": lat, "lon": lon} for lat, lon in stops],
        "costing": "auto", "directions_options": {"units": "kilometers"}})
    trip = data.get("trip") or {}
    summary = trip.get("summary") or {}
    if trip.get("status") not in (0, None) or "length" not in summary:
        return None
    # One shape per leg (polyline6); join them into a single line.
    pts: list[tuple[float, float]] = []
    for leg in trip.get("legs") or []:
        leg_pts = decode_polyline(leg.get("shape") or "", 6)
        pts.extend(leg_pts[1:] if pts and leg_pts else leg_pts)
    return RouteEstimate(distance_km=round(float(summary["length"]), 1),
                         duration_minutes=int(round(float(summary.get("time", 0)) / 60)),
                         polyline=encode_polyline(_simplify(pts)) if pts else None,
                         provider="valhalla")


def _route_ors(stops, *, base_url: str, api_key: str,
               timeout: int = 8) -> RouteEstimate | None:
    if not api_key:
        return None
    base = (base_url or "https://api.openrouteservice.org").rstrip("/")
    data = _http_json(f"{base}/v2/directions/driving-car", timeout=timeout,
                      headers={"Authorization": api_key, "Accept": "application/json"},
                      body={"coordinates": [[lon, lat] for lat, lon in stops]})
    routes = data.get("routes") or []
    if not routes:
        return None
    r = routes[0]
    summary = r.get("summary") or {}
    if "distance" not in summary:
        return None
    geometry = r.get("geometry")          # Google polyline, precision 5
    pts = decode_polyline(geometry, 5) if isinstance(geometry, str) else []
    return RouteEstimate(distance_km=round(float(summary["distance"]) / 1000, 1),
                         duration_minutes=int(round(float(summary.get("duration", 0)) / 60)),
                         polyline=encode_polyline(_simplify(pts)) if pts else None,
                         provider="ors")


def configured_routers() -> list[tuple[str, Callable]]:
    """[(name, fn(stops) -> RouteEstimate | None)] in the configured order,
    leaving out any router that isn't set up."""
    from app.config import settings
    table = {
        "valhalla": (settings.valhalla_url, lambda st: _route_valhalla(
            st, url=settings.valhalla_url, api_key=settings.valhalla_api_key)),
        "ors": (settings.ors_api_key, lambda st: _route_ors(
            st, base_url=settings.ors_url, api_key=settings.ors_api_key)),
        "osrm": (settings.osrm_url, lambda st: _route_osrm(st, base_url=settings.osrm_url)),
    }
    order = [n.strip().lower() for n in (settings.routing_providers or "").split(",")]
    return [(n, table[n][1]) for n in order if n in table and table[n][0]]


def route(stops: list[tuple[float, float]], *, base_url: str | None = None,
          timeout: int = 8) -> RouteEstimate | None:
    """Driving distance/time (and the road line) through (lat, lon) stops, in
    order: the first configured router that answers wins. `base_url` pins a
    single OSRM server instead ("" = off). None when every router fails or
    none is set up — callers then simply leave distance blank."""
    if len(stops) < 2:
        return None
    routers = ([("osrm", lambda st: _route_osrm(st, base_url=base_url, timeout=timeout))]
               if base_url is not None else configured_routers())
    for name, fn in routers:
        try:
            est = fn(stops)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                KeyError, TypeError):
            est = None
        if est is not None:
            return est
        log.info("router %s had no route; trying the next", name)
    return None


def estimate_for_places(places: list[dict], *, base_url: str | None = None
                        ) -> RouteEstimate | None:
    """Route through already-located places; None if any stop lacks coords."""
    pts = [(p.get("lat"), p.get("lon")) for p in places if isinstance(p, dict)]
    if len(pts) < 2 or any(la is None or lo is None for la, lo in pts):
        return None
    return route([(float(la), float(lo)) for la, lo in pts], base_url=base_url)


def estimate_for_link(url: str, *, geocoder, base_url: str | None = None
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


def encode_polyline(points: list[tuple[float, float]], precision: int = 6) -> str:
    """Encode (lat, lon) points as a Google polyline (polyline6 by default)."""
    factor, out, plat, plon = 10 ** precision, [], 0, 0
    for lat, lon in points:
        ilat, ilon = int(round(lat * factor)), int(round(lon * factor))
        for delta in (ilat - plat, ilon - plon):
            v = ~(delta << 1) if delta < 0 else delta << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(out)


def _simplify(points: list[tuple[float, float]], max_points: int = 400
              ) -> list[tuple[float, float]]:
    """Thin a long road line for display (keeps both ends): a PDF thumbnail or
    an app map needs a few hundred points, not every road vertex."""
    if len(points) <= max_points:
        return points
    step = (len(points) - 1) / (max_points - 1)
    return [points[round(i * step)] for i in range(max_points)]


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
