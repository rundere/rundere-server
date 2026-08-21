"""Activity-type registry — the one contract for what each timeline entry
carries (Yonder 2.0). Ported from the Trips design's `types` map.

Each type declares its ordered editable `fields` and pure `title`/`subtitle`
functions. Three consumers share this single source of truth:
  * the activity modal        — renders one input per field,
  * the timeline row          — title + subtitle + the time chip,
  * the expanded row          — the labelled read-only field list.

Storing the entered values as a plain dict (persisted to TripActivity.values)
keeps a new type a data change here, never a schema migration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# Field input kinds understood by the template + form handler.
#   text | textarea | url | datetime-local
Field = dict[str, str]


def _join(*parts: str, sep: str = " ") -> str:
    return sep.join(p for p in parts if p)


@dataclass(frozen=True)
class ActivityType:
    key: str
    label: str
    fields: tuple[Field, ...]
    title: Callable[[dict], str]
    subtitle: Callable[[dict], str]

    def field(self, key: str) -> Field | None:
        return next((f for f in self.fields if f["key"] == key), None)


def _f(key: str, label: str, kind: str = "text") -> Field:
    return {"key": key, "label": label, "type": kind}


# Order here is the order shown in the type dropdown.
_TYPES: tuple[ActivityType, ...] = (
    ActivityType(
        "flight", "Flight",
        (
            _f("when", "Departure", "datetime-local"),
            _f("endWhen", "Arrival", "datetime-local"),
            _f("airline", "Airline"),
            _f("flightNumber", "Flight #"),
            _f("from", "From"),
            _f("to", "To"),
            _f("confirmation", "Confirmation #"),
            _f("sourceLink", "Source document", "url"),
            _f("notes", "Notes", "textarea"),
        ),
        title=lambda v: _join(v.get("airline", ""), v.get("flightNumber", "")) or "Flight",
        subtitle=lambda v: _join(v.get("from", ""), v.get("to", ""), sep=" → "),
    ),
    ActivityType(
        "train", "Train",
        (
            _f("when", "Departure", "datetime-local"),
            _f("endWhen", "Arrival", "datetime-local"),
            _f("operator", "Operator / line"),
            _f("trainNumber", "Train #"),
            _f("from", "From"),
            _f("to", "To"),
            _f("confirmation", "Confirmation #"),
            _f("sourceLink", "Source document", "url"),
            _f("notes", "Notes", "textarea"),
        ),
        title=lambda v: _join(v.get("operator", ""), v.get("trainNumber", "")) or "Train",
        subtitle=lambda v: _join(v.get("from", ""), v.get("to", ""), sep=" → "),
    ),
    ActivityType(
        "drive", "Road trip",
        (
            _f("when", "Depart", "datetime-local"),
            _f("endWhen", "Arrive", "datetime-local"),
            _f("route", "Route"),
            _f("distance", "Distance"),
            _f("sourceLink", "Source document", "url"),
            _f("notes", "Notes", "textarea"),
        ),
        title=lambda v: v.get("route") or "Road trip",
        subtitle=lambda v: v.get("distance", ""),
    ),
    ActivityType(
        "carRental", "Car rental",
        (
            _f("when", "Pickup", "datetime-local"),
            _f("endWhen", "Drop-off", "datetime-local"),
            _f("company", "Rental company"),
            _f("pickupLocation", "Pickup location"),
            _f("dropoffLocation", "Drop-off location"),
            _f("confirmation", "Confirmation #"),
            _f("sourceLink", "Source document", "url"),
            _f("notes", "Notes", "textarea"),
        ),
        title=lambda v: v.get("company") or "Car rental",
        subtitle=lambda v: _join(v.get("pickupLocation", ""), v.get("dropoffLocation", ""), sep=" → "),
    ),
    ActivityType(
        "lodging", "Lodging",
        (
            _f("when", "Check-in", "datetime-local"),
            _f("endWhen", "Check-out", "datetime-local"),
            _f("name", "Property name"),
            # Accommodation kind — feeds the packing archetype (resort→beach,
            # campground/cabin→outdoors, …) and AdventureLog's Lodging.type on
            # push (D-81). Free text; the planning context normalises it.
            _f("type", "Type (hotel, hostel, resort, bnb, campground, cabin, …)"),
            _f("address", "Address"),
            _f("confirmation", "Confirmation #"),
            _f("sourceLink", "Source document", "url"),
            _f("notes", "Notes", "textarea"),
        ),
        title=lambda v: v.get("name") or "Lodging",
        subtitle=lambda v: v.get("address", ""),
    ),
    ActivityType(
        "tour", "Tour / activity",
        (
            _f("when", "Start", "datetime-local"),
            _f("endWhen", "End", "datetime-local"),
            _f("duration", "Duration"),
            _f("name", "Activity name"),
            _f("provider", "Operator"),
            _f("location", "Location"),
            _f("sourceLink", "Source document", "url"),
            _f("notes", "Notes", "textarea"),
        ),
        title=lambda v: v.get("name") or "Tour / activity",
        subtitle=lambda v: v.get("provider", ""),
    ),
    ActivityType(
        "event", "Event",
        (
            _f("when", "Start", "datetime-local"),
            _f("name", "Event name"),
            _f("venue", "Venue"),
            _f("seat", "Seat / section"),
            _f("sourceLink", "Source document", "url"),
            _f("notes", "Notes", "textarea"),
        ),
        title=lambda v: v.get("name") or "Event",
        subtitle=lambda v: v.get("venue", ""),
    ),
    ActivityType(
        "dining", "Restaurant",
        (
            _f("when", "Reservation time", "datetime-local"),
            _f("name", "Restaurant"),
            _f("address", "Address"),
            _f("partySize", "Party size"),
            _f("sourceLink", "Source document", "url"),
            _f("notes", "Notes", "textarea"),
        ),
        title=lambda v: v.get("name") or "Restaurant",
        subtitle=lambda v: v.get("address", ""),
    ),
    ActivityType(
        "cruise", "Cruise / ferry",
        (
            _f("when", "Departure", "datetime-local"),
            _f("endWhen", "Arrival", "datetime-local"),
            _f("operator", "Cruise line"),
            _f("vessel", "Ship"),
            _f("departurePort", "Departure port"),
            _f("arrivalPort", "Arrival port"),
            _f("cabin", "Cabin #"),
            _f("sourceLink", "Source document", "url"),
            _f("notes", "Notes", "textarea"),
        ),
        title=lambda v: v.get("vessel") or v.get("operator") or "Cruise",
        subtitle=lambda v: _join(v.get("departurePort", ""), v.get("arrivalPort", ""), sep=" → "),
    ),
    ActivityType(
        "note", "Note",
        (
            _f("when", "Date (optional)", "datetime-local"),
            _f("title", "Title"),
            _f("sourceLink", "Source document", "url"),
            _f("text", "Details", "textarea"),
        ),
        title=lambda v: v.get("title") or "Note",
        subtitle=lambda v: "",
    ),
)

TRIP_ACTIVITY_TYPES: dict[str, ActivityType] = {t.key: t for t in _TYPES}

# Keys never shown in the expanded read-only field grid: `when` becomes the
# time chip, and the long free-text fields render last, full width.
_META_KEYS = {"when"}
_LONG_KEYS = {"notes", "text"}

DEFAULT_TYPE = "flight"


def get_type(key: str) -> ActivityType:
    """The registered type, falling back to the default rather than raising —
    an unknown type on an old row must still render, not 500 the timeline."""
    return TRIP_ACTIVITY_TYPES.get(key, TRIP_ACTIVITY_TYPES[DEFAULT_TYPE])


def type_options() -> list[dict]:
    return [{"value": t.key, "label": t.label} for t in _TYPES]


# Which value(s) locate each activity type on a map. A road trip is special:
# it prefers the shared Google Maps *directions* URL. Everything else builds a
# Maps SEARCH deeplink from a place string (a plain URL — no API call / quota).
def _maps_search(query: str) -> str:
    import urllib.parse
    return "https://www.google.com/maps/search/?api=1&query=" + \
        urllib.parse.quote(query)


_MAP_QUERY: dict[str, Callable[[dict], str | None]] = {
    "flight": lambda v: (f"{v['from']} airport" if v.get("from") else None),
    "train": lambda v: (f"{v['from']} train station" if v.get("from") else None),
    "carRental": lambda v: v.get("pickupLocation") or None,
    "lodging": lambda v: _join(v.get("name", ""), v.get("address", ""), sep=", ") or None,
    "tour": lambda v: v.get("location") or v.get("name") or None,
    "event": lambda v: _join(v.get("venue", ""), v.get("name", "") if not v.get("venue") else "") or None,
    "dining": lambda v: _join(v.get("name", ""), v.get("address", ""), sep=", ") or None,
    "cruise": lambda v: (f"{v['departurePort']} cruise terminal"
                         if v.get("departurePort") else None),
}


def _is_maps_url(url: str) -> bool:
    u = (url or "").lower()
    return u.startswith("http") and (
        "google." in u and "/maps" in u or "maps.app.goo.gl" in u
        or "goo.gl/maps" in u)


def map_url(activity_type: str, values: dict) -> str | None:
    """A 'find on map' link for one timeline activity, or None when it has no
    locatable field. Flight → departure airport; lodging/restaurant → the
    property; event → the venue; road trip → the shared directions URL if there is
    one, else a search on the route text."""
    v = values or {}
    if activity_type == "drive":
        link = (v.get("sourceLink") or "").strip()
        if _is_maps_url(link):
            return link
        return _maps_search(v["route"]) if v.get("route") else None
    fn = _MAP_QUERY.get(activity_type)
    q = fn(v) if fn else None
    return _maps_search(q) if q else None


def detail_fields(activity_type: str, values: dict) -> list[dict]:
    """Labelled, non-empty fields for the expanded row, in declaration order,
    with the long free-text fields pushed to the end. `when` is excluded (it is
    the time chip); `sourceLink` is flagged so the template renders a link."""
    t = get_type(activity_type)
    short, long = [], []
    for f in t.fields:
        key = f["key"]
        if key in _META_KEYS:
            continue
        val = values.get(key)
        if not val:
            continue
        row = {"label": f["label"], "value": val,
               "is_link": key == "sourceLink"}
        (long if key in _LONG_KEYS else short).append(row)
    return short + long
