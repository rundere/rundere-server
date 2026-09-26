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


# Where a booking came from: an email subject, a site, or a link. Free text on
# purpose — an email has no URL (owner, 2026-09-26). A map link has its own field.
_SOURCE_HINT = "email subject, booking site, or a link"


def _f(key: str, label: str, kind: str = "text", hint: str = "") -> Field:
    f = {"key": key, "label": label, "type": kind}
    if hint:
        f["hint"] = hint          # input placeholder only, never a label
    return f


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
            _f("mapLink", "Map / directions link", "url",
               hint="Google or Apple Maps link"),
            _f("sourceLink", "Source", hint=_SOURCE_HINT),
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
            _f("mapLink", "Map / directions link", "url",
               hint="Google or Apple Maps link"),
            _f("sourceLink", "Source", hint=_SOURCE_HINT),
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
            _f("from", "From", hint="start address or place"),
            _f("to", "To", hint="destination address or place"),
            _f("distance", "Distance", hint="filled from the map link when left blank"),
            _f("mapLink", "Map / directions link", "url",
               hint="Google or Apple Maps link"),
            _f("sourceLink", "Source", hint=_SOURCE_HINT),
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
            _f("mapLink", "Map / directions link", "url",
               hint="Google or Apple Maps link"),
            _f("sourceLink", "Source", hint=_SOURCE_HINT),
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
            _f("type", "Type", hint="hotel, hostel, resort, bnb, campground, cabin, …"),
            _f("address", "Address"),
            _f("confirmation", "Confirmation #"),
            _f("mapLink", "Map / directions link", "url",
               hint="Google or Apple Maps link"),
            _f("sourceLink", "Source", hint=_SOURCE_HINT),
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
            _f("mapLink", "Map / directions link", "url",
               hint="Google or Apple Maps link"),
            _f("sourceLink", "Source", hint=_SOURCE_HINT),
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
            _f("mapLink", "Map / directions link", "url",
               hint="Google or Apple Maps link"),
            _f("sourceLink", "Source", hint=_SOURCE_HINT),
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
            _f("mapLink", "Map / directions link", "url",
               hint="Google or Apple Maps link"),
            _f("sourceLink", "Source", hint=_SOURCE_HINT),
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
            _f("mapLink", "Map / directions link", "url",
               hint="Google or Apple Maps link"),
            _f("sourceLink", "Source", hint=_SOURCE_HINT),
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
            _f("sourceLink", "Source", hint=_SOURCE_HINT),
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
_LINK_KEYS = {"mapLink", "sourceLink"}

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
    from resolve.routing import is_maps_link
    return is_maps_link(url)


def _maps_link(values: dict) -> str | None:
    """The person's own Google/Apple Maps link for this activity: the Map field,
    or (entries made before it existed) a Maps URL pasted as the Source."""
    for key in ("mapLink", "sourceLink"):
        link = (values.get(key) or "").strip()
        if _is_maps_url(link):
            return link
    return None


def map_query(activity_type: str, values: dict) -> str | None:
    """The place text that locates this activity (what `map_url` searches for),
    so a client can hand it to its own maps app. A road trip uses its route."""
    v = values or {}
    if activity_type == "drive":
        return (v.get("route") or _join(v.get("from", ""), v.get("to", ""), sep=" → ")
                or None)
    fn = _MAP_QUERY.get(activity_type)
    return fn(v) if fn else None


def map_url(activity_type: str, values: dict) -> str | None:
    """A 'find on map' link for one timeline activity, or None when it has no
    locatable field. A Google or Apple Maps link the person saved always wins —
    it points at the exact place or route (owner, 2026-09-26). Otherwise:
    flight → departure airport; lodging/restaurant → the property; event → the
    venue; road trip → a search on its route."""
    v = values or {}
    link = _maps_link(v)
    if link:
        return link
    q = map_query(activity_type, v)
    return _maps_search(q) if q else None


def time_labels(activity_type: str) -> tuple[str | None, str | None]:
    """The type's own labels for its start/end times ("Check-in"/"Check-out",
    "Departure"/"Arrival"); None when the type has no such field."""
    t = get_type(activity_type)
    start, end = t.field("when"), t.field("endWhen")
    return (start["label"] if start else None, end["label"] if end else None)


def detail_fields(activity_type: str, values: dict) -> list[dict]:
    """Labelled, non-empty fields for the expanded row, in declaration order,
    with the long free-text fields pushed to the end. `when` is excluded (it is
    the time chip); a field holding a web link is flagged so the template
    renders it as one (the Source may also be plain text, e.g. an email)."""
    t = get_type(activity_type)
    short, long = [], []
    for f in t.fields:
        key = f["key"]
        if key in _META_KEYS:
            continue
        val = values.get(key)
        if not val:
            continue
        row = {"key": key, "label": f["label"], "value": val, "kind": f["type"],
               "is_link": (key in _LINK_KEYS
                           and str(val).strip().lower().startswith(("http://", "https://")))}
        (long if key in _LONG_KEYS else short).append(row)
    return short + long
