"""Apple Maps directions URL extractor — sibling of the Google one (SPEC-4).

Accepts a single share-link shape:

    https://maps.apple.com/?saddr=<origin>&daddr=<destination>[&dirflg=…][&ll=…]

Origin + destination only — Apple's scheme carries no waypoint syntax (D-87).
Unlike Google, Apple embeds no per-stop coordinate blob, so a named stop resolves
through the ordinary geocoder ladder (SPEC-4 §5). Place/date parsing and the
travel-mode contract are shared with the Google extractor via directions_common
(D-89), so the two providers can't drift.

Out of scope: `maps.apple/p/...` short links (iOS 26 dropped their embedded
coordinates, D-88) — `is_apple_short_link` flags them so the capture surface can
show an inline error instead of a silent needs_review.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

# Shared contract (D-89). ExtractionError is re-exported for the pipeline's
# except clause (`apple_maps_url.ExtractionError`).
from extract.directions_common import (  # noqa: F401
    ExtractionError, _capture_dt, _parse_place, resolve_mode)

# Apple's dirflg -> the shared canonical travel mode. Absent => driving (Apple's
# default), i.e. no stated mode. Any other value (e.g. `c` cycling) is a present
# but unsupported signal -> "unknown", which conflicts with every selection.
_DIRFLG = {"d": "car", "w": "walk", "r": "transit"}
# Placeholder origins Apple emits for "start from current location" — not a
# stable stop (SPEC-4 §4). Compared case-insensitively after unquoting.
_UNSTABLE_ORIGINS = {"current location", "my location", "here"}


def is_apple_short_link(url: str) -> bool:
    """True for a `maps.apple/p/...` (or `maps.apple.com/p/...`) short link — out
    of scope (D-88); the caller surfaces an inline capture error for these."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    if host == "maps.apple":
        return True
    return host == "maps.apple.com" and (parts.path or "").startswith("/p/")


def looks_like_apple_maps_url(url: str) -> bool:
    """True for an Apple Maps directions link this extractor accepts: host
    `maps.apple.com` with BOTH `saddr` and `daddr` present. Short-link hosts are
    rejected (D-88). Used as a per-path validator by the capture surface and to
    route the pipeline (SPEC-4 §2)."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    if host != "maps.apple.com":
        return False
    q = parse_qs(parts.query)
    return bool(q.get("saddr") and q.get("daddr"))


def _first(q: dict, key: str) -> str | None:
    # parse_qs already percent-decodes and turns '+' into space; just trim.
    v = (q.get(key) or [None])[0]
    return v.strip() if v else None


def extract_apple_maps_url(url: str, capture_context: dict | None = None) -> list[dict]:
    """One ground_transport payload from an Apple Maps directions URL."""
    cc = capture_context or {}
    date = cc.get("travel_date")
    if not date:
        raise ExtractionError(
            "apple_maps_url requires capture_context.travel_date (RULE-31): "
            "a directions URL carries no date")

    if is_apple_short_link(url):
        raise ExtractionError(
            "Apple Maps short link (maps.apple/p/…) carries no coordinates "
            "(D-88); paste the full maps.apple.com directions link")

    q = parse_qs(urlsplit(url or "").query)
    saddr = _first(q, "saddr")
    daddr = _first(q, "daddr")
    if not (saddr and daddr):
        raise ExtractionError(
            "not an Apple Maps directions URL, or fewer than 2 stops "
            "(both saddr and daddr are required)")
    if saddr.lower() in _UNSTABLE_ORIGINS:
        raise ExtractionError(
            f"Apple Maps saddr={saddr!r} is a current-location placeholder, not "
            "a stable origin; cannot capture a route from it")

    # Travel mode: the capture selection (default car), cross-checked against the
    # link's dirflg (D-91 revised). Present-and-contradicting => ExtractionError.
    dirflg = (_first(q, "dirflg") or "").lower() or None
    url_signal = None if dirflg is None else _DIRFLG.get(dirflg, "unknown")
    mode = resolve_mode(cc.get("mode"), url_signal)

    dep_dt = _capture_dt(date, cc.get("travel_time"), cc.get("timezone"))
    # A multi-day trip arrives on end_date (date-only); otherwise same day.
    arr_dt = (_capture_dt(cc["end_date"], None, cc.get("timezone"))
              if cc.get("end_date") else dep_dt)

    departure = {"place": _parse_place(saddr), "time": dep_dt}
    arrival = {"place": _parse_place(daddr), "time": arr_dt}

    # round_trip is inferred from matching endpoints (Apple carries no explicit
    # signal). Mirrors the Google extractor's behaviour.
    round_trip = saddr == daddr or None

    notes = cc.get("notes")
    if mode == "other":
        # Transit: the URL has no leg steps to store; mark that it is a transit
        # route so downstream can tell it from a drive (D-92).
        marker = f"Public transit route: {saddr} → {daddr} (via Apple Maps)"
        notes = f"{marker}\n{notes}" if notes else marker

    return [{
        "item_type": "ground_transport",
        "booking_status": "planned",           # a plan, never actuals (D-22)
        "title": cc.get("title") or f"{saddr} → {daddr}",
        "notes": notes,
        "ground_transport": {
            "mode": mode,
            "departure": departure,
            "arrival": arrival,
            "waypoints": [],                   # Apple carries no waypoints (D-87)
            "round_trip": round_trip,
            "route": {"provider": "apple_maps", "source_url": url,
                      "is_estimate": True},    # RULE-27b: intent, not actuals
        },
    }]
