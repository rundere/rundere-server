"""Google Maps directions URL extractor — fourth in the chain (D-10).

The cheapest extractor in the system: a directions URL carries origin, waypoints
and destination in its path or query. Zero tokens.

A directions URL carries NO date (RULE-31/34): every DateTimeLocal here comes from
capture_context with origin=capture_context, never from the URL. With no capture
date the item is unbuildable (Movement.time is required) — that is an
ExtractionError for the pipeline to turn into needs_review, not a guess.

Three URL shapes are accepted (D-61): the `/maps/dir/A/B/C` path, the
`?api=1&origin=…&destination=…&waypoints=A|B` share format, and a
`maps.app.goo.gl` / `goo.gl/maps` short link, which is expanded by following its
redirect (allowed Google hosts only — never an arbitrary URL). Place-name
segments are split into city/region so the geocoder can actually locate them
("Toronto, ON" alone does not geocode); coordinate segments become lat/lon
directly.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from urllib.parse import parse_qs, unquote_plus, urlsplit

_DIR_PATH = re.compile(r"/maps/dir/([^@?#]+)")
_COORD = re.compile(r"^\s*(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)\s*$")
_HAS_DIGIT = re.compile(r"\d")
# Google short-link hosts we will follow a redirect for. Restricted so the
# server never fetches an arbitrary operator-supplied URL (SSRF).
_SHORT_HOSTS = {"maps.app.goo.gl", "goo.gl", "g.co"}


class ExtractionError(Exception):
    pass


def looks_like_maps_url(url: str) -> bool:
    """True for a Google Maps directions link in any accepted shape — a
    `maps.app.goo.gl` / `goo.gl/maps` share link, a `/maps/dir/…` path, or a
    `?api=1&origin=…&destination=…` query. Lets the native capture page route a
    pasted Maps link to this extractor instead of the generic web fetch, so no
    n8n form is needed. Conservative on bare short-hosts (requires `/maps`) to
    avoid mis-routing an unrelated goo.gl link."""
    u = (url or "").strip()
    parts = urlsplit(u)
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    if host == "maps.app.goo.gl":
        return True
    if host in _SHORT_HOSTS and "/maps" in path:
        return True
    if _DIR_PATH.search(u):
        return True
    q = parse_qs(parts.query)
    return bool(q.get("origin") and q.get("destination"))


def _capture_dt(date, time, tz) -> dict:
    dt = {"local": f"{date}T{time}" if time else str(date),
          "precision": "datetime" if time else "date",
          "origin": "capture_context"}
    if tz:
        dt["tz"] = tz
    return dt


def _parse_place(segment: str) -> dict:
    """A directions stop -> a Place. A coordinate becomes lat/lon; a
    'City, Region[, Country]' name is split so the resolver's geocoder can locate
    it (its area tier needs a structured city — see D-59); a street address or a
    bare name is left as raw for the geocoder's other tiers."""
    seg = segment.strip()
    place: dict = {"raw": seg}
    if _COORD.match(seg):
        return place            # a bare coordinate; coords are read at resolve
    place["name"] = seg
    parts = [p.strip() for p in seg.split(",") if p.strip()]
    # Only treat it as "City, Region[, Country]" when the FIRST part is not a
    # street line (no house number) — an address keeps its whole string for the
    # D-59 formatted-address tier instead.
    if len(parts) >= 2 and not _HAS_DIGIT.search(parts[0]):
        place["city"] = parts[0]
        place["region"] = parts[1]
    return place


def _expand_short(url: str) -> str:
    """Follow a Google short link to its full directions URL. Only the known
    Google hosts are fetched; anything else is returned untouched (SSRF guard).
    Best-effort: a failed expansion returns the original, and the caller then
    reports 'not a directions URL' rather than crashing."""
    host = (urlsplit(url).hostname or "").lower()
    if host not in _SHORT_HOSTS:
        return url
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "yonder"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.geturl() or url
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return url


def _segments(url: str) -> list[str]:
    """Origin, waypoints, destination in order, from whichever URL shape."""
    m = _DIR_PATH.search(url)
    if m:
        segs = [unquote_plus(s).strip() for s in m.group(1).split("/")]
        return [s for s in segs if s and not s.startswith("data=")]
    # ?api=1&origin=…&destination=…&waypoints=A|B  (waypoints are pipe-separated)
    q = parse_qs(urlsplit(url).query)
    origin = (q.get("origin") or [None])[0]
    dest = (q.get("destination") or [None])[0]
    if origin and dest:
        wps = [w.strip() for w in (q.get("waypoints", [""])[0]).split("|") if w.strip()]
        return [origin, *wps, dest]
    return []


def extract_maps_url(url: str, capture_context: dict | None = None) -> list[dict]:
    """One ground_transport road payload from a Google Maps directions URL."""
    cc = capture_context or {}
    date = cc.get("travel_date")
    if not date:
        raise ExtractionError(
            "maps_url requires capture_context.travel_date (RULE-31): "
            "a directions URL carries no date")

    url = _expand_short(url or "")
    segments = _segments(url)
    if len(segments) < 2:
        raise ExtractionError(
            "not a Google Maps directions URL, or fewer than 2 stops "
            f"(found {len(segments)})")

    dep_dt = _capture_dt(date, cc.get("travel_time"), cc.get("timezone"))
    # A multi-day drive arrives on end_date (date-only); otherwise same day.
    arr_dt = (_capture_dt(cc["end_date"], None, cc.get("timezone"))
              if cc.get("end_date") else dep_dt)

    # Coordinates are NOT set here: the extraction Place schema has no coord
    # fields (coords belong at resolve, like geocoding). The exact per-stop coords
    # Google embeds in the data blob (!1d<lon>!2d<lat>) travel in route.source_url
    # and are applied at resolve by _apply_maps_coords — which also beats the
    # abbreviations + postal codes that make Google's formatted addresses
    # un-geocodable ("100 … Pkwy, … ON Z9Z 9Z9" returns nothing from OSM).
    movements = [{"place": _parse_place(seg),
                  "time": arr_dt if i == len(segments) - 1 else dep_dt}
                 for i, seg in enumerate(segments)]
    origin, dest = movements[0], movements[-1]
    waypoints = movements[1:-1]

    return [{
        "item_type": "ground_transport",
        "booking_status": "planned",           # a plan, never actuals (D-22)
        "title": cc.get("title") or f"Drive: {segments[0]} → {segments[-1]}",
        "notes": cc.get("notes"),
        "ground_transport": {
            "mode": "car",
            "departure": origin,
            "arrival": dest,
            "waypoints": waypoints,
            "round_trip": segments[0] == segments[-1] or None,
            "route": {"provider": "google_maps", "source_url": url,
                      "is_estimate": True},    # RULE-27b: intent, not actuals
        },
    }]
