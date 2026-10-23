"""Shared building blocks for directions-URL extractors (Google + Apple).

`_parse_place` (City/Region split, D-59) and `_capture_dt` (capture-context date
stamping, RULE-34; no date yet is D-210) are generic over *any* directions URL — nothing in either
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
# US state and Canadian province codes — how Google writes the region at the end
# of a "Place, City, ST" stop. Seeing one last marks the part before it as the
# city, not the first part (YONDER-121: "Heritage Square, Phoenix, AZ").
_SUBDIVISION_CODES = frozenset((
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI "
    "WY PR AB BC MB NB NL NS NT NU ON PE QC SK YT").split())

# The user-facing capture selection -> the ground_transport.mode written to the
# payload. Transit is `other` (a transit *directions* link is a plan, not a
# ticketed train/ferry, and its URL carries no leg steps — D-92); walking is the
# new `walk` mode (D-93). Car is the default when no selection is supplied.
MODE_BY_SELECTION = {"car": "car", "transit": "other", "walk": "walk"}
# The canonical mode a selection or a URL signal denotes, in one vocabulary, so
# the two can be compared. "unknown" is a present-but-unsupported URL signal
# (e.g. cycling) — it matches no selection and therefore always conflicts.
_CANON_BY_SELECTION = {"car": "car", "transit": "transit", "walk": "walk"}

# Human-facing labels used in titles/notes so Google and Apple read identically.
PROVIDER_LABEL = {"google_maps": "Google Maps", "apple_maps": "Apple Maps"}
_TITLE_VERB = {"car": "Drive", "walk": "Walk", "other": "Transit"}
# Origins that are not a stable stop — Apple/Google may emit these when a route
# starts from "current location", i.e. whoever shared it (SPEC-4 §4). Compared
# case-insensitively.
UNSTABLE_ORIGINS = {"current location", "my location", "here"}


def is_unstable_origin(segment: str) -> bool:
    return (segment or "").strip().lower() in UNSTABLE_ORIGINS


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
    if len(parts) >= 3 and parts[-1] in _SUBDIVISION_CODES \
            and not any(_HAS_DIGIT.search(p) for p in parts):
        # "Place, City, ST": the city sits just before the region code.
        place["city"] = parts[-2]
        place["region"] = parts[-1]
    elif len(parts) >= 2 and not _HAS_DIGIT.search(parts[0]):
        place["city"] = parts[0]
        place["region"] = parts[1]
    elif len(parts) >= 2:
        # A street line ("100 Harbour Ridge Pkwy, Maple Grove, ON"): it IS the
        # stop's address, so Review shows it in the From/To address field.
        place["address"] = seg
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


def build_directions_payload(segments: list[str], capture_context: dict | None,
                             *, provider: str, source_url: str,
                             url_mode_signal: str | None) -> list[dict]:
    """The one ground_transport payload builder shared by every directions
    provider (SPEC-4 D-89). Callers parse their own URL shape into ordered
    `segments` (origin, waypoints…, destination) and pass the provider's mode
    signal; everything downstream — date stamping, mode resolution, the unstable-
    origin guard, titles, the transit notes marker, round-trip inference and the
    route block — is identical across providers so the experience can't drift.

    `provider` is the route.provider value ("google_maps" / "apple_maps").
    """
    cc = capture_context or {}
    date = cc.get("travel_date")
    if is_unstable_origin(segments[0]):
        raise ExtractionError(
            f"origin {segments[0]!r} is a current-location placeholder, not a "
            "stable origin; cannot capture a route from it")

    mode = resolve_mode(cc.get("mode"), url_mode_signal)

    if date:
        dep_dt = _capture_dt(date, cc.get("travel_time"), cc.get("timezone"))
        # A multi-day trip arrives on end_date (date-only); otherwise same day.
        arr_dt = (_capture_dt(cc["end_date"], None, cc.get("timezone"))
                  if cc.get("end_date") else dict(dep_dt))      # its own copy
    else:
        # D-210 (superseding RULE-31): a route carries no date, and the person
        # hasn't given one yet. The plan is kept, explicitly undated, and the
        # Inbox asks for a date without blocking it. Never a guessed one (D-06).
        dep_dt, arr_dt = {"precision": "none"}, {"precision": "none"}

    # Coordinates are NOT set here: the extraction Place schema has no coord
    # fields (coords belong at resolve). Where a provider embeds them (Google's
    # data blob) they ride in route.source_url and are applied at resolve.
    movements = [{"place": _parse_place(seg),
                  "time": arr_dt if i == len(segments) - 1 else dep_dt}
                 for i, seg in enumerate(segments)]
    origin, dest = segments[0], segments[-1]

    title = cc.get("title") or f"{_TITLE_VERB[mode]}: {origin} → {dest}"
    notes = cc.get("notes")
    if mode == "other":
        # Transit: the URL has no leg steps to store; mark that it is a transit
        # route so downstream can tell it from a drive (D-92).
        label = PROVIDER_LABEL.get(provider, provider)
        marker = f"Public transit route: {origin} → {dest} (via {label})"
        notes = f"{marker}\n{notes}" if notes else marker

    return [{
        "item_type": "ground_transport",
        "booking_status": "planned",           # a plan, never actuals (D-22)
        "title": title,
        "notes": notes,
        "ground_transport": {
            "mode": mode,
            "departure": movements[0],
            "arrival": movements[-1],
            "waypoints": movements[1:-1],
            "round_trip": origin == dest or None,
            "route": {"provider": provider, "source_url": source_url,
                      "is_estimate": True},    # RULE-27b: intent, not actuals
        },
    }]
