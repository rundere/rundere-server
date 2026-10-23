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

from app.trips import types as activity_types

log = logging.getLogger("yonder.trips.enrich")

_DRIVE_FILLS = ("from", "to", "route", "distance", "endWhen")


def _arrival(when: str | None, minutes: int) -> str | None:
    """Departure + drive time, as a datetime-local string — only when the
    departure has a time (a date-only drive has no arrival clock)."""
    try:
        dep = datetime.strptime((when or "")[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return (dep + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


def prepare_activity(atype: str, values: dict, *, old_geo: dict | None = None,
                     session=None, trip_id: str | None = None,
                     activity_id: str | None = None) -> tuple[dict, dict | None]:
    """Everything a save does before storing an activity: fill a road trip's
    blanks from its map link, then locate it (app/trips/geo.py). One Locator is
    shared, so a directions link is looked up once for both. With a session
    and trip, places the trip already has located are matched first."""
    from app.trips.geo import Locator, locate_activity, trip_places
    known = (trip_places(session, trip_id, exclude_id=activity_id)
             if session is not None and trip_id else None)
    loc = Locator.default(known=known)
    values = enrich_activity_values(atype, values, locator=loc)
    return values, locate_activity(atype, values, old_geo=old_geo, locator=loc)


# Where a place's address goes in each type that has one (D-168's mapping:
# lodging and dining say address, a tour says location, an event says venue).
_WHERE_KEYS = ("address", "location", "venue")


def fill_from_place(atype: str, values: dict, place_values: dict) -> dict:
    """`values` with only its EMPTY fields filled from what a place link knows
    (D-209): name, the address in this type's own field, the Map link. Fields
    this type doesn't have are ignored; nothing typed is ever replaced."""
    keys = {f["key"] for f in activity_types.get_type(atype).fields}
    where = next((place_values[k] for k in _WHERE_KEYS if place_values.get(k)), "")
    offer = {k: v for k, v in place_values.items() if k in keys and k not in _WHERE_KEYS}
    slot = next((k for k in _WHERE_KEYS if k in keys), None)
    if where and slot:
        offer[slot] = where
    out = dict(values)
    for key, val in offer.items():
        if val and not out.get(key):
            out[key] = val
    return out


def _place_fill(atype: str, values: dict) -> dict:
    """A non-road-trip activity's blanks from a PLACE link in its Map field
    (YONDER-318: until D-209 only road trips were filled). Never raises."""
    link = activity_types._maps_link(values or {})
    wants = [k for k in ("name", *_WHERE_KEYS)
             if k in {f["key"] for f in activity_types.get_type(atype).fields}]
    if not link or not wants or all(values.get(k) for k in wants):
        return values
    from app import places as places_svc
    from extract import place_url
    try:
        full = place_url.resolved_url(link)          # a short link, expanded once
        if place_url.parse_place_url(full) is None:  # directions, or not a place
            return values
        _, place_values = places_svc.activity_from_link(full)
    except Exception:                         # noqa: BLE001 - the blanks stay the person's
        log.info("couldn't read a place from an activity's map link", exc_info=True)
        return values
    return fill_from_place(atype, values, place_values)


def enrich_activity_values(atype: str, values: dict, *, locator=None) -> dict:
    """Return `values` with blanks filled from the activity's map link: a road
    trip's from its directions, any other type's from a place link (D-209).
    No map link, or nothing blank → returned unchanged. Never raises: a lookup
    failure just leaves the blanks for the person."""
    if atype != "drive":
        return _place_fill(atype, values)
    link = activity_types._maps_link(values or {})
    if not link or all(values.get(k) for k in _DRIVE_FILLS):
        return values
    from app.trips.geo import Locator
    from resolve import routing
    stops, est = (locator or Locator.default()).directions(link)
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
