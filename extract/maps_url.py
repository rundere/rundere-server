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

# Shared with the Apple extractor (D-89): place/date parsing and the travel-mode
# contract live in directions_common so the two providers can't drift. ExtractionError
# is re-exported here so existing callers (`maps_url.ExtractionError`) keep working.
from extract.directions_common import (  # noqa: F401
    ExtractionError, build_directions_payload)

_DIR_PATH = re.compile(r"/maps/dir/([^@?#]+)")
# Google short-link hosts we will follow a redirect for. Restricted so the
# server never fetches an arbitrary operator-supplied URL (SSRF).
_SHORT_HOSTS = {"maps.app.goo.gl", "goo.gl", "g.co"}
# Google states its travel mode in the ?api=1 form as travelmode=<x>; map the
# ones we support to the shared canonical vocabulary. driving/absent => car.
_GOOGLE_TRAVELMODE = {"driving": "car", "walking": "walk", "transit": "transit"}


def _google_mode_signal(url: str) -> str | None:
    """The canonical travel mode a Google directions URL states, or None. Only
    the ?api=1 travelmode param is read; a /maps/dir path without it states no
    mode (the data blob's !3e codes are undocumented — treated as absent)."""
    tm = (parse_qs(urlsplit(url).query).get("travelmode") or [None])[0]
    if tm is None:
        return None
    return _GOOGLE_TRAVELMODE.get(tm.strip().lower(), "unknown")


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


def _expand_short(url: str) -> str:
    """Follow a Google short link to its full directions URL. Only the known
    Google hosts are fetched; anything else is returned untouched (SSRF guard).
    Best-effort: a failed expansion returns the original, and the caller then
    reports 'not a directions URL' rather than crashing."""
    host = (urlsplit(url).hostname or "").lower()
    if host not in _SHORT_HOSTS:
        return url
    try:
        # A short link can point anywhere, so its redirects go through the public
        # opener: each hop vetted, never into the LAN (YONDER-293). Nothing is read.
        from app import netpolicy
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "yonder"})
        with netpolicy.public_opener().open(req, timeout=10) as resp:
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
    """One ground_transport road payload from a Google Maps directions URL.
    Parses the Google-specific URL shapes, then hands off to the shared builder
    so the payload matches every other directions provider (D-89)."""
    url = _expand_short(url or "")
    segments = _segments(url)
    if len(segments) < 2:
        raise ExtractionError(
            "not a Google Maps directions URL, or fewer than 2 stops "
            f"(found {len(segments)})")

    # The exact per-stop coords Google embeds in the data blob (!1d<lon>!2d<lat>)
    # ride in route.source_url and are applied at resolve by _apply_maps_coords —
    # which also beats the abbreviations + postal codes that make Google's
    # formatted addresses un-geocodable ("100 … Pkwy, … ON Z9Z 9Z9").
    return build_directions_payload(
        segments, capture_context, provider="google_maps", source_url=url,
        url_mode_signal=_google_mode_signal(url))
