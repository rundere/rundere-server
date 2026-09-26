"""Fill in what a road trip's map link already knows (owner, 2026-09-26).

A person adding a Road trip pastes the Google/Apple Maps directions link they
planned it with. The link names the stops; OSRM knows the distance and time.
So blank From / To / Route / Distance — and the arrival time, when a departure
time is set — are filled from it. Only blanks: anything typed always wins, and
clearing Distance recomputes it on the next save.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.config import settings
from app.trips import types as activity_types

log = logging.getLogger("yonder.trips.enrich")

_DRIVE_FILLS = ("from", "to", "route", "distance", "endWhen")


def _geocoder():
    from resolve.geocode import PhotonGeocoder
    return PhotonGeocoder(settings.photon_url) if settings.photon_url else None


def _arrival(when: str | None, minutes: int) -> str | None:
    """Departure + drive time, as a datetime-local string — only when the
    departure has a time (a date-only drive has no arrival clock)."""
    try:
        dep = datetime.strptime((when or "")[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return (dep + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


def enrich_activity_values(atype: str, values: dict) -> dict:
    """Return `values` with a road trip's blanks filled from its map link.
    Any other type, no map link, or nothing blank → returned unchanged. Never
    raises: a lookup failure just leaves the blanks for the person."""
    if atype != "drive":
        return values
    link = activity_types._maps_link(values or {})
    if not link or all(values.get(k) for k in _DRIVE_FILLS):
        return values
    from resolve import routing
    try:
        stops, est = routing.estimate_for_link(link, geocoder=_geocoder(),
                                               base_url=settings.osrm_url)
    except Exception:
        log.warning("road trip lookup failed for %s", link, exc_info=True)
        return values
    if not stops:
        return values
    out = dict(values)
    first, last = stops[0], stops[-1]
    fills = {
        "from": routing.stop_address(first),
        "to": routing.stop_address(last),
        "route": " → ".join(x for x in (routing.stop_label(first),
                                         routing.stop_label(last)) if x) or None,
    }
    if est is not None:
        fills["distance"] = routing.format_distance(est)
        fills["endWhen"] = _arrival(out.get("when"), est.duration_minutes)
    for key, val in fills.items():
        if val and not out.get(key):
            out[key] = val
    return out
