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
    ExtractionError, build_directions_payload)

# Apple's dirflg -> the shared canonical travel mode. Absent => driving (Apple's
# default), i.e. no stated mode. Any other value (e.g. `c` cycling) is a present
# but unsupported signal -> "unknown", which conflicts with every selection.
_DIRFLG = {"d": "car", "w": "walk", "r": "transit"}


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
    """One ground_transport payload from an Apple Maps directions URL. Parses the
    Apple-specific query shape, then hands off to the shared builder so the
    payload matches every other directions provider (D-89)."""
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

    # Travel mode signal from the link's dirflg (D-91 revised): absent => no
    # stated mode; an unrecognized value => a present, unsupported signal.
    dirflg = (_first(q, "dirflg") or "").lower() or None
    url_signal = None if dirflg is None else _DIRFLG.get(dirflg, "unknown")

    # Apple carries no waypoints (D-87): origin + destination only.
    return build_directions_payload(
        [saddr, daddr], capture_context, provider="apple_maps", source_url=url,
        url_mode_signal=url_signal)
