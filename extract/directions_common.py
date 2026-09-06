"""Shared building blocks for directions-URL extractors (Google + Apple).

`_parse_place` (City/Region split, D-59) and `_capture_dt` (capture-context date
stamping, RULE-31/34) are generic over *any* directions URL — nothing in either
is provider-specific — so both live here and are imported by `extract/maps_url.py`
(Google) and `extract/apple_maps_url.py` (Apple) rather than copied (D-89).

`resolve_mode` centralises the travel-mode contract (D-91 revised): the mode is a
capture-surface selection (car / transit / walking), cross-checked against the
mode signal the URL itself carries (Apple `dirflg`, Google `travelmode`). A
*present, contradicting* signal is rejected; an *absent* signal is not a conflict.
"""
from __future__ import annotations

import re

_COORD = re.compile(r"^\s*(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)\s*$")
_HAS_DIGIT = re.compile(r"\d")

# The user-facing capture selection -> the ground_transport.mode written to the
# payload. Transit is `other` (a transit *directions* link is a plan, not a
# ticketed train/ferry, and its URL carries no leg steps — D-92); walking is the
# new `walk` mode (D-93). Car is the default when no selection is supplied.
MODE_BY_SELECTION = {"car": "car", "transit": "other", "walk": "walk"}
# The canonical mode a selection or a URL signal denotes, in one vocabulary, so
# the two can be compared. "unknown" is a present-but-unsupported URL signal
# (e.g. cycling) — it matches no selection and therefore always conflicts.
_CANON_BY_SELECTION = {"car": "car", "transit": "transit", "walk": "walk"}


class ExtractionError(Exception):
    pass


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


def resolve_mode(selection: str | None, url_signal: str | None) -> str:
    """Return the ground_transport.mode for a directions capture.

    `selection` is the capture-surface choice ("car"/"transit"/"walk"); it
    defaults to "car" when absent (legacy captures, coordinate-only links).
    `url_signal` is the canonical mode the URL carries ("car"/"transit"/"walk"/
    "unknown"/None) — None means the link states no mode.

    Raises ExtractionError when the URL states a mode that contradicts the
    selection (D-91 revised); an absent signal never conflicts.
    """
    sel = (selection or "car").strip().lower()
    if sel not in _CANON_BY_SELECTION:
        raise ExtractionError(
            f"unsupported travel mode selection {selection!r}; "
            f"expected one of car / transit / walk")
    sel_canon = _CANON_BY_SELECTION[sel]
    if url_signal is not None and url_signal != sel_canon:
        raise ExtractionError(
            f"travel-mode mismatch: capture selected {sel!r} but the link's "
            f"directions mode is {url_signal!r}; not guessing which is right")
    return MODE_BY_SELECTION[sel]
