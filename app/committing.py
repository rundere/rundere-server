"""Commit stage — the ONLY place that writes to AirTrail and AdventureLog.

Everything it needs was decided at resolve (`resolution.targets`, the matched or
minted collection, self-vs-guest); the committer makes no policy decisions, it
executes them (D-14/D-22). Principles this module encodes:

- **Approve then commit are separate acts (D-31).** Only items a human APPROVED
  are written; resolve alone never reaches an external system.
- **Verbatim evidence (D-19).** Every real HTTP write becomes a `CommitAttempt`
  row with the request and the raw response — the meaning of an AdventureLog or
  AirTrail error often lives only in its body.
- **Idempotent.** A target already written (recorded in `item.commit`) is
  skipped on a re-run, and the two systems are also probed for an existing
  record (AirTrail `find_duplicate`, AdventureLog location proximity) — because
  neither dedups server-side (A4 Q6) and a double-commit is not reversible.
- **Dry-run is the default.** `commit_pending(dry_run=True)` builds and returns
  the exact request bodies WITHOUT sending them, so a write can always be
  reviewed before it happens. The first live write should be one item, watched.
- **One flight at a time (D-06).** Items commit sequentially; each flight is its
  own item, so AirTrail never receives a batch that it would abort wholesale.
- **D-06 midnight stub is marked, never hidden (A4 Q3).** A date-only time is
  written as a date (AdventureLog coerces to midnight server-side); the commit
  record flags `synthesized_time` so it is never read back as a real 00:00.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import CommitAttempt, TravelItem
from resolve.geocode import tz_from_coords
from resolve.keys import has_undated
from resolve.targets import places_in_payload

log = logging.getLogger("yonder.commit")


# ---------------------------------------------------------------- datetime

def _to_utc_z(dt: dict | None, tz_fallback: str | None = None) -> str | None:
    """A schema DateTimeLocal -> explicit UTC 'Z', or None when it cannot be
    done safely. Naive local time is NEVER emitted: AdventureLog reads a naive
    datetime as UTC (A4), so an unknown zone must fall back to date-only rather
    than silently shifting the wall-clock time.

    `tz_fallback` supplies the zone when the extracted time carries none — the
    document rarely states a zone, and for a NEVER-FLOWN airport the tz is only
    known at commit time from the resolved airport (D-50). Passing the resolved
    airport's tz here is what lets a flight keep its real departure/arrival time
    instead of collapsing to date-only."""
    if not isinstance(dt, dict) or dt.get("precision") != "datetime":
        return None
    local = dt.get("local")
    tz = dt.get("tz") or tz_fallback
    if not local or not tz:
        return None
    try:
        aware = datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(tz))
        return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:
        return None


def _date(dt: dict | None) -> str | None:
    if not isinstance(dt, dict) or not dt.get("local"):
        return None
    return str(dt["local"])[:10]


def _airtrail_date(dt: dict | None) -> str | None:
    """The LOCAL calendar date for AirTrail, as an offset ISO (its parser
    extracts only the Y-M-D; the time part is ignored and supplied separately as
    departureTime/arrivalTime). Uses the local wall-clock date, so a flight that
    crosses midnight in UTC still logs on its real local day."""
    if not isinstance(dt, dict) or not dt.get("local"):
        return None
    return str(dt["local"])[:10] + "T00:00:00Z"


def _airtrail_time(dt: dict | None) -> str | None:
    """The LOCAL HH:MM for AirTrail, only when a real time is known (precision
    datetime). AirTrail merges this with the date in the airport's own tz, so we
    pass local wall-clock time, not UTC. Date-only (D-06) returns None."""
    if not isinstance(dt, dict) or dt.get("precision") != "datetime":
        return None
    local = str(dt.get("local") or "")
    return local[11:16] if "T" in local and len(local) >= 16 else None


def _al_datetime(dt: dict | None,
                 tz_fallback: str | None = None) -> tuple[str | None, bool]:
    """(value_to_send, synthesized_time). A full datetime goes as UTC 'Z'; a
    date-only goes as the bare date, which AdventureLog stores as midnight —
    flagged so the stub is never mistaken for a real 00:00 (A4 Q3 / D-06).
    `tz_fallback` is the resolved airport's zone (see _to_utc_z)."""
    z = _to_utc_z(dt, tz_fallback)
    if z:
        return z, False
    d = _date(dt)
    return (d, True) if d else (None, False)


def _coord(v):
    """Round a latitude/longitude to AdventureLog's precision. Its DecimalField
    caps latitude at 6 decimal places and longitude at 9 total digits, so a
    full-precision geocode (e.g. 34.9849387 / -111.9024807) is rejected 400 on
    every location/lodging/transportation write. Six decimals (~0.11 m) keeps
    longitude within 3 integer + 6 fractional = 9 digits and is far finer than
    any address needs. None passes through untouched."""
    if v is None:
        return None
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return v


# ---------------------------------------------------------------- selection

def _fully_committed(item: TravelItem) -> bool:
    """Every target already written for real — nothing left to do, so the item
    is done and must not be offered for logging again."""
    commit = item.commit or {}
    if commit.get("dry_run", True):
        return False
    targets = set((item.resolution or {}).get("targets") or [])
    done = {r.get("target") for r in (commit.get("results") or []) if r.get("ok")}
    return bool(targets) and targets <= done


def committable(item: TravelItem) -> bool:
    """A human APPROVED it (D-31), resolution left it ok, it has somewhere to
    write, and it is not already fully logged. Ignored/needs_review/deferred
    items are never committed."""
    review = item.review or {}
    status = item.status or {}
    resolution = item.resolution or {}
    return (review.get("action_taken") == "approved"
            and status.get("resolve") == "ok"
            and bool(resolution.get("targets"))
            and not _fully_committed(item))


# Marker on a result for an AirTrail/AdventureLog target the commit did not
# attempt because the capture's owner has no connection of that kind (D-150;
# before YONDER-192: because they weren't an admin, D-156 — that older text is
# still on stored results). It counts as settled for the item's Logged state,
# but is NOT a real write, so `_done_targets` ignores it and a later commit,
# once the owner connects, can still write it.
EXTERNAL_SKIP = "no AirTrail/AdventureLog connection"
# D-210: an item with no date yet goes on Run·dere's own timeline (Date not
# set), but a date-bound record elsewhere waits; a later commit writes it.
NO_DATE_SKIP = "no date yet"
_EXTERNAL_SKIPS = {EXTERNAL_SKIP, NO_DATE_SKIP,
                   "no external connection (admins only until per-user connections)"}


def is_external_skip(row: dict) -> bool:
    return row.get("skipped") in _EXTERNAL_SKIPS


def _done_targets(item: TravelItem) -> set[str]:
    """Targets a prior (non-dry) run already wrote, so a re-run skips them."""
    done = set()
    for r in ((item.commit or {}).get("results") or []):
        if r.get("ok") and not r.get("dry_run") and not is_external_skip(r):
            done.add(r.get("target"))
    return done


# ---------------------------------------------------------------- self id

# ---------------------------------------------------------------- body builders

# Our cabin_class -> AirTrail's SeatClasses enum. AirTrail spells premium
# economy "economy+"; an unmapped/blank value returns None (the field is
# nullable) rather than a string AirTrail's Zod enum would reject.
_AIRTRAIL_SEAT_CLASS = {"economy": "economy", "premium_economy": "economy+",
                        "premium economy": "economy+", "business": "business",
                        "first": "first"}


def _airtrail_seat_class(value) -> str | None:
    if not value:
        return None
    return _AIRTRAIL_SEAT_CLASS.get(str(value).strip().lower())


def _airport_tz(airports, iata) -> str | None:
    """The tz of a resolved airport (flown reference or AirTrail's DB, D-50), or
    None. Lets a builder recover the zone the document omitted."""
    if not airports or not iata:
        return None
    a = airports.get((iata or "").upper())
    return getattr(a, "tz", None)


def _flight_number_combined(payload: dict) -> str:
    f = payload.get("flight") or {}
    mc = f.get("marketing_carrier") or {}
    iata = (mc.get("iata") or "") if isinstance(mc, dict) else ""
    return f"{iata}{f.get('flight_number') or ''}"


_PNR = re.compile(r"[A-Z0-9]{6}")


def airline_record_locator(ref) -> str | None:
    """A flight's booking reference as AirTrail takes it: the 6-character
    airline record locator (the PNR a GDS such as Sabre issues), letters and
    digits. Anything else — a booking site's longer reference — isn't a PNR:
    it stays on Run·dere's own record and isn't sent, because AirTrail rejects
    the whole flight over a custom field that breaks its limit (owner,
    2026-10-12; seen live: "Booking Reference must be at most 6 characters")."""
    v = re.sub(r"[\s-]", "", str(ref or "")).upper()
    return v if _PNR.fullmatch(v) else None


def build_airtrail_flight(payload: dict, *, airports: dict, airlines: dict,
                          self_uid: str | None) -> tuple[dict | None, str | None]:
    """(body, error). error is a human sentence when the flight cannot be
    written safely — an airport or airline whose code we can't name — and the
    committer records it rather than sending a half-resolved flight.

    Everything goes as CODES (D-186): AirTrail resolves airports by ICAO/IATA
    against its own table, so no AirTrail database id is needed. With no
    `self_uid`, the seat is `<USER_ID>`, which AirTrail (3.13) fills in with
    the token's own user — the connection owner."""
    f = payload.get("flight") or {}
    dep = (f.get("departure") or {}).get("place") or {}
    arr = (f.get("arrival") or {}).get("place") or {}
    dep_iata = (dep.get("iata") or "").upper()
    arr_iata = (arr.get("iata") or "").upper()
    frm, to = airports.get(dep_iata), airports.get(arr_iata)
    if not frm or not (frm.icao or frm.iata):
        return None, f"no airport is known for {dep_iata or '?'}"
    if not to or not (to.icao or to.iata):
        return None, f"no airport is known for {arr_iata or '?'}"
    mc = f.get("marketing_carrier") or {}
    airline = airlines.get((mc.get("iata") or "").upper())
    if not airline or not (airline.icao or airline.iata):
        return None, (f"AirTrail has no airline for {mc.get('iata') or '?'} "
                      "(or the token lacks reference_data.read)")
    self_uid = self_uid or "<USER_ID>"

    dep_dt = f.get("departure", {}).get("time")
    arr_dt = f.get("arrival", {}).get("time")
    # AirTrail keeps DATE and TIME in SEPARATE fields (departure + departureTime)
    # and merges them in the AIRPORT's own tz itself, then converts to UTC. So we
    # send the LOCAL calendar date and the LOCAL HH:MM — NOT a combined UTC 'Z'
    # (that has no *Time field, which triggers "Cannot have arrival date without
    # time"). An arrival date is only sent WITH its time, never alone.
    arr_time = _airtrail_time(arr_dt)
    body = {
        "flightNumber": _flight_number_combined(payload),
        "date": _date(dep_dt),
        "datePrecision": "day",                 # precision of the date; times are separate
        "departure": _airtrail_date(dep_dt),
        "departureTime": _airtrail_time(dep_dt),
        "arrival": _airtrail_date(arr_dt) if arr_time else None,
        "arrivalTime": arr_time,
        # /api/flight/save wants CODE STRINGS, not objects (verified against the
        # live schema 2026-08-05 — the earlier object shape was only ever dry-run
        # previewed, never actually written). Airports resolve by
        # getAirportByIcao() ?? getAirportByIata(), so ICAO (with IATA fallback)
        # works; the AIRLINE resolves by getAirlineByIcao() ONLY, so it MUST be
        # the ICAO code (ACA/AIC), never the IATA (AC/AI).
        "from": frm.icao or frm.iata,
        "to": to.icao or to.iata,
        "airline": airline.icao or airline.iata,
        "seats": [{"userId": self_uid,
                   "seat": None, "seatNumber": (f.get("seats") or [None])[0],
                   "seatClass": _airtrail_seat_class(f.get("cabin_class"))}],
        "note": None,
    }
    # AirTrail's /api/flight/save accepts a customFields object (it destructures
    # `customFields = {}` and stores it). The n8n workflow carried the PNR and
    # e-ticket number there; both are extracted, so pass them through — a ticket
    # number in particular has no first-class AirTrail column.
    custom = {}
    pnr = airline_record_locator(payload.get("booking_reference"))
    if pnr:
        custom["booking_reference"] = pnr
    ticket = next((t for t in (f.get("ticket_numbers") or []) if t), None)
    if ticket:
        custom["ticket_number"] = ticket
    if custom:
        body["customFields"] = custom
    return body, None


# AdventureLog's transportation `type` is a CLOSED, coarse enum (plane/train/
# bus/car/boat/bike/walking/other) — it has no "car_rental", "rideshare" etc.
# Map our finer ground modes onto it; the specific mode + operator are preserved
# in the description so nothing is lost (owner, 2026-08-10: a car rental logs as
# "car" with the rental details in the notes, not a rejected write).
_AL_TRANSPORT_TYPE = {"train": "train", "bus": "bus", "ferry": "boat",
                      "car": "car", "car_rental": "car", "rideshare": "car", "taxi": "car",
                      "transfer": "car", "other": "other"}
# Human label for the real mode, led into the description when AdventureLog's
# coarse type would otherwise hide it.
_GROUND_MODE_DESC = {"car_rental": "Car rental", "rideshare": "Taxi", "taxi": "Taxi",
                     "transfer": "Transfer", "ferry": "Ferry"}


def build_transportation(payload: dict, *, collection_id: str | None,
                         airports: "dict | None" = None) -> tuple[dict, bool]:
    f = payload.get("flight") or payload.get("ground_transport") or {}
    dep = (f.get("departure") or {}).get("place") or {}
    arr = (f.get("arrival") or {}).get("place") or {}
    is_flight = payload.get("item_type") == "flight"
    dep_dt = f.get("departure", {}).get("time")
    arr_dt = f.get("arrival", {}).get("time")
    # Same fix as the AirTrail flight: when the document carried no zone, fall
    # back to the resolved airport's tz so the transportation keeps its real
    # time instead of logging as date-only. Prefer any tz already on the place.
    # tz fallback chain: document/reference tz -> airport IATA tz (flights) ->
    # the place's own coordinates (trains/buses/ferries have no IATA, but the
    # station was geocoded). Without the last hop a real train time degrades to
    # date-only at commit, since _to_utc_z refuses to emit a naive local (D-06).
    dep_tz = (dep.get("tz") or _airport_tz(airports, dep.get("iata"))
              or tz_from_coords(dep.get("lat"), dep.get("lon")))
    arr_tz = (arr.get("tz") or _airport_tz(airports, arr.get("iata"))
              or tz_from_coords(arr.get("lat"), arr.get("lon")))
    date_val, dep_synth = _al_datetime(dep_dt, dep_tz)
    end_val, _ = _al_datetime(arr_dt, arr_tz)
    dep_code = (dep.get("iata") or "").upper() or None
    arr_code = (arr.get("iata") or "").upper() or None
    label_from = dep_code or dep.get("city") or dep.get("name") or "?"
    label_to = arr_code or arr.get("city") or arr.get("name") or "?"
    number = _flight_number_combined(payload) if is_flight else (f.get("service_number") or "")
    name = f"{number} – {label_from} → {label_to}".strip(" –")
    # Booking ref + passengers in the description (matching the n8n workflow):
    # AdventureLog has no fields for either, and they are exactly what a person
    # wants when they open the transportation record later.
    ref = payload.get("booking_reference")
    pax = ", ".join(t.get("raw_name", "") for t in (payload.get("travelers") or [])
                    if t.get("raw_name"))
    # Lead the description with the SPECIFIC ground mode + operator, since
    # AdventureLog's coarse type collapses e.g. a car rental to "car". A flight's
    # type ("plane") already says everything, so it gets no lead.
    mode = None if is_flight else (f.get("mode") or "other")
    prov = payload.get("provider") or {}
    prov_name = prov.get("name") if isinstance(prov, dict) else None
    lead = _GROUND_MODE_DESC.get(mode, "")
    if prov_name:
        lead = f"{lead} · {prov_name}" if lead else prov_name
    desc = ". ".join(p for p in (lead, f"Booking Ref: {ref}" if ref else "",
                                 f"Passengers: {pax}" if pax else "") if p) or None
    body = {
        "type": "plane" if is_flight else _AL_TRANSPORT_TYPE.get(mode, "other"),
        "name": name,
        "flight_number": _flight_number_combined(payload) if is_flight else None,
        "date": date_val,
        "end_date": end_val,
        "from_location": dep.get("name") or dep.get("city"),
        "to_location": arr.get("name") or arr.get("city"),
        "start_code": dep_code,
        "end_code": arr_code,
        "origin_latitude": _coord(dep.get("lat")),
        "origin_longitude": _coord(dep.get("lon")),
        "destination_latitude": _coord(arr.get("lat")),
        "destination_longitude": _coord(arr.get("lon")),
        "start_timezone": dep_tz,
        "end_timezone": arr_tz,
        "description": desc,
    }
    if collection_id:
        body["collection"] = collection_id
    return {k: v for k, v in body.items() if v is not None}, dep_synth


# Our schema's lodging_kind -> AdventureLog's lodging type (mostly 1:1; our
# "camping" is AL's "campground"). AL also has resort/villa, which we don't
# extract but can infer from the property name below.
_AL_LODGING_FROM_KIND = {"hotel": "hotel", "hostel": "hostel",
                         "apartment": "apartment", "house": "house",
                         "bnb": "bnb", "camping": "campground", "cabin": "cabin"}
# Name keywords -> AL type, first match wins. Cheap inference from the property
# name we already have, so "…Spa Resort" logs as a resort, not "Other".
_AL_LODGING_KEYWORDS = [
    ("resort", "resort"), ("hostel", "hostel"), ("bed & breakfast", "bnb"),
    ("bed and breakfast", "bnb"), ("b&b", "bnb"), ("villa", "villa"),
    ("cabin", "cabin"), ("campground", "campground"), ("campsite", "campground"),
    ("aparthotel", "apartment"), ("apartment", "apartment"),
]


def _al_lodging_type(payload: dict) -> str:
    """AdventureLog lodging type. Prefer an explicitly extracted lodging_kind,
    then infer from the property name, else default to 'hotel' (never leave it
    to AL's 'Other' default — a booked stay is a hotel unless told otherwise)."""
    lg = payload.get("lodging") or {}
    kind = (lg.get("lodging_kind") or "").lower()
    if kind in _AL_LODGING_FROM_KIND:
        return _AL_LODGING_FROM_KIND[kind]
    name = (lg.get("property_name") or "").lower()
    for kw, t in _AL_LODGING_KEYWORDS:
        if kw in name:
            return t
    return "hotel"


def build_lodging(payload: dict, *, collection_id: str | None) -> dict:
    lg = payload.get("lodging") or {}
    place = lg.get("place") or {}
    ci, _ = _al_datetime(lg.get("check_in"))
    co, _ = _al_datetime(lg.get("check_out"))
    body = {
        "name": lg.get("property_name") or place.get("name") or "Lodging",
        "type": _al_lodging_type(payload),
        "check_in": ci, "check_out": co,
        "reservation_number": payload.get("booking_reference"),
        "latitude": _coord(place.get("lat")), "longitude": _coord(place.get("lon")),
        "location": place.get("address") or place.get("raw"),
    }
    if collection_id:
        body["collection"] = collection_id
    return {k: v for k, v in body.items() if v is not None}


def build_note(payload: dict, *, collection_id: str | None) -> dict:
    ev = payload.get("event") or {}
    title = ev.get("title") or payload.get("title") or "Note"
    date = _date(ev.get("start")) or _date(ev.get("end"))
    body = {"name": title, "content": payload.get("notes") or ev.get("description"),
            "date": date}
    if collection_id:
        body["collection"] = collection_id
    return {k: v for k, v in body.items() if v is not None}


# Our ground_transport.mode -> Yonder activity type (app/trips/types.py). Modes
# with no dedicated Yonder type fall back to "drive" (Road trip), whose free-text
# route field carries the endpoints.
# Uber, Lyft and the like ARE taxis (owner, 2026-10-12, D-193): "rideshare" —
# the older mode for them — files as a Taxi too.
_YONDER_GROUND_TYPE = {"train": "train", "car_rental": "carRental",
                       "ferry": "cruise", "bus": "drive", "car": "drive",
                       "taxi": "taxi", "rideshare": "taxi",
                       "transfer": "drive", "other": "drive"}


def _local(dt: dict | None) -> str | None:
    """The LOCAL datetime string a Yonder activity stores in its `when`/`endWhen`
    fields (datetime-local). Yonder keeps wall-clock local strings, not UTC."""
    if not isinstance(dt, dict):
        return None
    return dt.get("local") or None


def _label(place: dict | None) -> str | None:
    if not isinstance(place, dict):
        return None
    return (place.get("iata") or place.get("city") or place.get("name")
            or place.get("raw") or None)


def _capture_source(session: Session, item: TravelItem) -> str | None:
    """What the timeline's Source field says for a capture: the email's subject
    or the captured page's address. Maps captures carry their link in the Map
    field instead, so they get none here."""
    from app.models import IngestBatch
    batch = session.get(IngestBatch, item.batch_id) if item.batch_id else None
    p = (batch.payload or {}) if batch is not None else {}
    kind = (item.source or {}).get("type")
    if kind == "gmail" and p.get("subject"):
        return f"Email: {p['subject']}"
    if kind == "web":
        return p.get("source_url") or "Captured web page"
    if kind == "gcal":
        return "Google Calendar"
    return None


def build_yonder_activity(payload: dict, *, source: str | None = None) -> tuple[str, dict]:
    """Map a capture payload to a Yonder (type, values) for a TripActivity.

    Yonder is a first-party trip system, so this mirrors the manual add-activity
    form's value keys (app/trips/types.py) rather than an external API shape. The
    activity is filed under the item's linked Yonder trip by the commit stage.
    `source` is the capture's origin for the Source field (see _capture_source)."""
    atype, values = _yonder_activity(payload)
    if source and not values.get("sourceLink"):
        values["sourceLink"] = source
    return atype, values


def _yonder_activity(payload: dict) -> tuple[str, dict]:
    t = payload.get("item_type")
    ref = payload.get("booking_reference") or None

    if t == "flight":
        f = payload.get("flight") or {}
        mc = f.get("marketing_carrier") or {}
        dep, arr = f.get("departure") or {}, f.get("arrival") or {}
        values = {
            "when": _local(dep.get("time")), "endWhen": _local(arr.get("time")),
            "airline": (mc.get("name") or mc.get("iata") or None),
            "flightNumber": _flight_number_combined(payload) or None,
            "from": _label(dep.get("place")), "to": _label(arr.get("place")),
            "confirmation": ref,
        }
        return "flight", {k: v for k, v in values.items() if v}

    if t == "ground_transport":
        g = payload.get("ground_transport") or {}
        prov = payload.get("provider") or {}
        dep, arr = g.get("departure") or {}, g.get("arrival") or {}
        a, b = _label(dep.get("place")), _label(arr.get("place"))
        atype = _YONDER_GROUND_TYPE.get(g.get("mode"), "drive")
        provider = (prov.get("name") if isinstance(prov, dict) else None)
        route = g.get("route") or {}
        base = {"when": _local(dep.get("time")), "endWhen": _local(arr.get("time")),
                "confirmation": ref,
                # the directions link the drive was captured from (owner,
                # 2026-09-26: the activity must keep it — it's the Map button)
                "mapLink": route.get("source_url") or None}
        if atype == "drive":
            from resolve.routing import (RouteEstimate, format_distance,
                                         stop_address, stop_label)
            dp, ap = dep.get("place"), arr.get("place")
            base["route"] = " → ".join(
                x for x in (stop_label(dp), stop_label(ap)) if x) or None
            # The full From/To (a reviewer's edited address wins over the name)
            base["from"], base["to"] = stop_address(dp), stop_address(ap)
            if route.get("distance_km") is not None:
                base["distance"] = format_distance(RouteEstimate(
                    float(route["distance_km"]), int(route.get("duration_minutes") or 0)))
        elif atype == "carRental":
            base.update({"company": provider, "pickupLocation": a, "dropoffLocation": b})
        elif atype == "taxi":
            from resolve.routing import stop_address
            base.update({"company": provider,
                         "from": stop_address(dep.get("place")) or a,
                         "to": stop_address(arr.get("place")) or b})
        elif atype == "cruise":
            base.update({"operator": provider, "departurePort": a, "arrivalPort": b})
        else:  # train
            base.update({"operator": provider, "trainNumber": g.get("service_number"),
                         "from": a, "to": b})
        return atype, {k: v for k, v in base.items() if v}

    if t == "lodging":
        lg = payload.get("lodging") or {}
        place = lg.get("place") or {}
        values = {
            "when": _local(lg.get("check_in")), "endWhen": _local(lg.get("check_out")),
            "name": lg.get("property_name") or _label(place),
            "address": place.get("address") or place.get("raw"),
            "confirmation": ref,
        }
        return "lodging", {k: v for k, v in values.items() if v}

    if t == "event":
        ev = payload.get("event") or {}
        values = {"when": _local(ev.get("start")), "name": ev.get("title") or "Event",
                  "venue": _label(ev.get("venue"))}
        return "event", {k: v for k, v in values.items() if v}

    # Anything else (incl. note): a plain note carrying whatever text we have.
    values = {"name": payload.get("title") or "Note",
              "text": payload.get("notes")}
    return "note", {k: v for k, v in values.items() if v}


# ---------------------------------------------------------------- one item

_ORDER = ["adventurelog.collection", "adventurelog.location",
          "adventurelog.transportation", "adventurelog.lodging",
          "adventurelog.note", "airtrail.flight", "yonder.activity"]

# AdventureLog has no orphan path for these — they REQUIRE a collection (RULE-08).
_NEEDS_COLLECTION = {"adventurelog.transportation", "adventurelog.lodging",
                     "adventurelog.note"}

# Revision handling (D-13/D-57). An `update` PATCHes the record the prior item
# wrote; a `supersede` (cancellation) deletes the AirTrail flight but MARKS the
# AdventureLog record [CANCELLED] rather than deleting it (owner: keep the paper
# trail). Only these targets carry an updatable in-place record; a location or
# collection is not revised (places are stable, the trip is not re-minted).
#
# CAUTION GUARDRAIL (owner, 2026-08-06): only ever revise/delete a record we can
# identify EXACTLY — AdventureLog by the record id stored at create, AirTrail by
# the full flight natural key. When no exact target is found, an update CREATES A
# NEW record and a cancellation SKIPS + flags; we never mutate a vaguely-matched
# record.
_REV_UPDATABLE = {"airtrail.flight", "adventurelog.transportation",
                  "adventurelog.lodging", "adventurelog.note"}
_REV_CANCEL_MARK = {"adventurelog.transportation", "adventurelog.lodging",
                    "adventurelog.note"}
_CANCEL_TAG = "[CANCELLED]"


def _prior_target_records(prior: TravelItem | None) -> dict:
    """{target: record_id} for every target the PRIOR item actually wrote, read
    from its commit envelope. Locations (many per item, place-keyed) are not
    returned — they are never revised. This is what an update PATCHes and a
    supersede deletes/marks."""
    out: dict = {}
    if prior is None:
        return out
    for r in ((prior.commit or {}).get("results") or []):
        target = r.get("target")
        rid = r.get("id")
        if not target or not rid or target == "adventurelog.location":
            continue
        if r.get("ok") and not r.get("dry_run"):
            out.setdefault(target, str(rid))
    return out


def _airtrail_flight_id(airtrail, prior: TravelItem | None,
                        prior_records: dict) -> str | None:
    """The AirTrail flight id to update/delete, or None to fall back to safety.

    AirTrail's /api/flight/save returns only {success} — no record id (verified
    in the server build) — so nothing exact is stored at create. The only handle
    is the flight's NATURAL KEY: carrier + number + calendar date + both
    endpoints. That is an EXACT identity match (the very key AirTrail dedups on),
    NOT a fuzzy one — and we require ALL FIVE parts to be present before trusting
    it (owner: never revise/delete a vaguely-matched record; create new or skip
    instead). A missing part, an ambiguous list, or no match → None, and the
    caller creates a new record (update) or leaves it and flags (supersede)."""
    fid = prior_records.get("airtrail.flight")
    if fid:                                         # exact id, if one ever exists
        return str(fid)
    if airtrail is None or prior is None:
        return None
    f = ((prior.extraction or {}).get("payload") or {}).get("flight") or {}
    mc = f.get("marketing_carrier") or {}
    dep = (f.get("departure") or {}).get("place") or {}
    arr = (f.get("arrival") or {}).get("place") or {}
    key = (mc.get("iata"), f.get("flight_number"),
           _date(f.get("departure", {}).get("time")),
           dep.get("iata"), arr.get("iata"))
    if not all(key):                                # incomplete key → do not guess
        return None
    try:
        dup = airtrail.find_duplicate(
            carrier_iata=key[0], flight_number=key[1], departure_date=key[2],
            origin_iata=key[3], destination_iata=key[4])
    except Exception:
        return None
    return str(dup["id"]) if dup and dup.get("id") else None


def _mark_cancelled(body: dict) -> dict:
    """Prefix the human-facing fields of an AdventureLog body with the cancel tag
    so a kept record reads as cancelled at a glance, without touching dates."""
    out = dict(body)
    name = out.get("name")
    if name and not str(name).startswith(_CANCEL_TAG):
        out["name"] = f"{_CANCEL_TAG} {name}"
    for field in ("description", "content"):
        if field in out and out[field] is not None:
            out[field] = f"{_CANCEL_TAG} {out[field]}"
    return out


class _Resolver:
    """Resolve an airport/airline by code: the AirTrail flown-flight reference
    first (already loaded), then `fallback(code)` — Run·dere's own airport table
    for airports (D-147), AirTrail's airline search API for airlines (D-186).
    A dict-like `.get` so build_airtrail_flight is unchanged; caches misses so
    a code is never looked up twice."""
    def __init__(self, flown: dict, fallback=None):
        self._flown = flown or {}
        self._fallback = fallback
        self._cache: dict = {}

    def get(self, code):
        code = (code or "").upper()
        if not code:
            return None
        if code in self._flown:
            return self._flown[code]
        if code not in self._cache:
            try:
                self._cache[code] = self._fallback(code) if self._fallback else None
            except Exception as ex:             # a lookup failure is a miss, not a crash
                log.warning("reference lookup for %s failed: %s", code, ex)
                self._cache[code] = None
        return self._cache[code]


def commit_item(session: Session, item: TravelItem, adventurelog, airtrail, *,
                identities=None, airports: dict | None = None,
                airlines: dict | None = None, dry_run: bool = True,
                external: bool | frozenset[str] = True,
                self_uid: str | None = None) -> dict:
    """Execute (or, dry, plan) every target for one item. Returns a result
    envelope; also written to `item.commit` and reflected in `item.status`.

    `external` is which external systems may be written: True = whichever
    client is passed, False = none, or a set of "airtrail"/"adventurelog" — the
    capture owner's connections (D-150). A target outside it is skipped, never
    attempted; the first-party Run·dere target always runs. `self_uid` is the
    AirTrail user a flight's seat belongs to: the owner's connection's AirTrail
    user id. None means the connection token's own user, which AirTrail fills
    in (YONDER-322: the Travel profile's id is no longer a fallback)."""
    payload = (item.extraction or {}).get("payload") or {}
    resolution = item.resolution or {}
    targets = [t for t in _ORDER if t in set(resolution.get("targets") or [])]
    airports = airports or {}
    airlines = airlines or {}
    if external is True:
        systems = frozenset(k for k, c in (("airtrail", airtrail),
                                           ("adventurelog", adventurelog)) if c is not None)
    elif external is False:
        systems = frozenset()
    else:
        systems = frozenset(external)
    # Always honour prior real writes — even in a dry run — so the PREVIEW shows
    # an already-written target as skipped rather than promising to create it
    # again (which execution's idempotency would then refuse).
    already = _done_targets(item)

    # Revision (D-13): an `update` PATCHes the record the prior item wrote; a
    # `supersede` cancels it. Both need the prior item's committed record ids.
    revision = item.revision or {}
    action = revision.get("action") or "create"
    prior_item = (session.get(TravelItem, revision.get("prior_item_id"))
                  if revision.get("prior_item_id") else None)
    prior_records = _prior_target_records(prior_item)
    al_cache: dict = {}          # collection detail, fetched once per collection

    # An existing (matched) collection is referenced; a minted one is created
    # here and its new id threads into the child records. An ATTACH leg (journey
    # sharing, D-45+) has no collection_id of its own — it borrows the one its
    # minter sibling created, resolved here.
    collection_id = _resolve_collection_id(session, item)
    if dry_run and not collection_id and resolution.get("mint_group"):
        # Preview only: the minter has not really created the trip, so show the
        # linkage intent rather than an orphan error.
        collection_id = "(trip created by first leg)"
    results: list[dict] = []

    def _record(target, operation, request, resp, *, extra=None):
        status, parsed, raw = (resp if resp else (None, None, ""))
        # A dry-run row is a PLAN, not an outcome: there is no HTTP status, so it
        # must read as "will do", never "failed" (that misled the preview into
        # showing every planned write in red).
        ok = True if dry_run else (status is not None and status < 400)
        row = {"target": target, "operation": operation, "ok": ok,
               "dry_run": dry_run, "status": status}
        if extra:
            row.update(extra)
        if dry_run:
            row["request"] = request
        else:
            new_id = _extract_id(parsed)
            if new_id:
                row["id"] = new_id
            if not ok:
                row["error"] = (raw or "")[:400]
            session.add(CommitAttempt(
                item_id=item.id, target=target, operation=operation,
                request_payload=request, response_status=status,
                response_headers=None, response_body=(raw or "")[:20000]))
        results.append(row)
        return ok, parsed

    def _rev_body(target):
        """Rebuild a target's body from the CURRENT payload — an update sends the
        whole record, the same shape a create would, just via PATCH/upsert."""
        if target == "adventurelog.transportation":
            body, _ = build_transportation(payload, collection_id=collection_id,
                                           airports=airports)
            return body
        if target == "adventurelog.lodging":
            return build_lodging(payload, collection_id=collection_id)
        if target == "adventurelog.note":
            return build_note(payload, collection_id=collection_id)
        return None

    def _do_update(target, rid):
        if target == "airtrail.flight":
            body, err = build_airtrail_flight(payload, airports=airports,
                                              airlines=airlines, self_uid=self_uid)
            if err:
                results.append({"target": target, "operation": "update",
                                "ok": False, "dry_run": dry_run, "error": err})
                return
            resp = None if dry_run else airtrail.update_flight(rid, body)
            _record(target, "update", body, resp, extra={"updated_id": rid})
            return
        body = _rev_body(target)
        updater = {"adventurelog.transportation": getattr(adventurelog, "update_transportation", None),
                   "adventurelog.lodging": getattr(adventurelog, "update_lodging", None),
                   "adventurelog.note": getattr(adventurelog, "update_note", None)}[target]
        resp = None if dry_run else updater(rid, body)
        _record(target, "update", body, resp, extra={"updated_id": rid})

    def _do_cancel_mark(target, rid):
        """Keep the AdventureLog record but mark it [CANCELLED] (owner choice)."""
        body = _mark_cancelled(_rev_body(target) or {})
        updater = {"adventurelog.transportation": getattr(adventurelog, "update_transportation", None),
                   "adventurelog.lodging": getattr(adventurelog, "update_lodging", None),
                   "adventurelog.note": getattr(adventurelog, "update_note", None)}[target]
        resp = None if dry_run else updater(rid, body)
        _record(target, "cancel", body, resp, extra={"cancelled_id": rid})

    for target in targets:
        if target in already:
            results.append({"target": target, "operation": "skip",
                            "ok": True, "skipped": "already committed"})
            continue
        if target != "yonder.activity" and target.split(".", 1)[0] not in systems:
            results.append({"target": target, "operation": "skip",
                            "ok": True, "skipped": EXTERNAL_SKIP})
            continue
        if target != "yonder.activity" and has_undated(payload):
            results.append({"target": target, "operation": "skip",
                            "ok": True, "skipped": NO_DATE_SKIP})
            continue

        # ---- Yonder (first-party, 2.2.0): file the capture onto its linked
        # Yonder trip as a TripActivity. Self-contained — a LOCAL DB upsert keyed
        # on (trip_id, source_item_key=canonical_key), independent of AirTrail /
        # AdventureLog and of the revision id-plumbing below. Re-committing or an
        # updated capture updates the same activity; a cancellation removes it.
        if target == "yonder.activity":
            from app.models import TripActivity
            tid, key = item.trip_id, item.canonical_key
            if not tid and action != "supersede" and resolution.get("mint_yonder_trip"):
                tid = _resolve_or_mint_yonder_trip(session, item, resolution,
                                                   collection_id, dry_run)
            existing = (session.query(TripActivity)
                        .filter(TripActivity.trip_id == tid,
                                TripActivity.source_item_key == key).first()
                        if (tid and key) else None)
            if action == "supersede":
                if not existing:
                    results.append({"target": target, "operation": "cancel",
                                    "ok": True, "skipped": "cancellation — nothing "
                                    "on the Run·dere trip to remove"})
                    continue
                extra = {"deleted_id": existing.id}
                if not dry_run:
                    from app.trips import who
                    who.clear_activity(session, existing.id)
                    from discover import items as discovery_items
                    discovery_items.unlink_activity(session, existing.id)
                    from app import expenses as expenses_svc
                    expenses_svc.unlink_activity(session, existing.id)   # SPEC-10: keep the expense, unlinked
                    session.delete(existing)
                _record(target, "cancel", {"trip_id": tid},
                        None if dry_run else (200, {}, ""), extra=extra)
                continue
            if not tid:
                results.append({"target": target, "operation": "create", "ok": False,
                                "dry_run": dry_run,
                                "error": "no Run·dere trip is linked to this item"})
                continue
            atype, values = build_yonder_activity(
                payload, source=_capture_source(session, item))
            op = "update" if existing else "create"
            if dry_run:
                _record(target, op, {"trip_id": tid, "type": atype, "values": values},
                        None, extra={"yonder_type": atype})
                continue
            # Where it is: the capture's own located places (already verified at
            # resolve), else located from the activity's text like a manual one.
            from app.trips.geo import geo_from_payload, locate_activity
            geo = geo_from_payload(atype, values, payload) or locate_activity(
                atype, values, old_geo=existing.geo if existing else None)
            if existing:
                existing.type, existing.values = atype, values
                existing.geo = geo
                existing.starts_at = values.get("when") or ""
                flag_modified(existing, "values")
                act_id = existing.id
            else:
                act = TripActivity(trip_id=tid, type=atype, values=values, geo=geo,
                                   starts_at=values.get("when") or "",
                                   source_item_key=key)
                session.add(act)
                session.flush()
                act_id = act.id
            _tag_booked_travelers(session, act_id, tid, payload, identities,
                                  keep_existing=existing is not None, item=item)
            _record(target, op, {"trip_id": tid, "type": atype, "values": values},
                    (200, {"id": act_id}, ""), extra={"yonder_type": atype})
            continue

        # ---- revision: revise or cancel an ALREADY-written record before the
        # create path (D-13/D-57). A target with no prior record falls through:
        # an update may introduce a genuinely new sub-record; a supersede adds
        # nothing new and skips it.
        rev_id = prior_records.get(target)
        if action == "update" and target in _REV_UPDATABLE:
            # For a flight, recover the id via the live list if the prior commit
            # never stored one — otherwise the update would fall through and
            # create a duplicate.
            upd_id = (_airtrail_flight_id(airtrail, prior_item, prior_records)
                      if target == "airtrail.flight" else rev_id)
            if upd_id:
                _do_update(target, upd_id)
                continue
        if action == "supersede":
            if target == "airtrail.flight":
                fid = _airtrail_flight_id(airtrail, prior_item, prior_records)
                if fid:
                    resp = None if dry_run else airtrail.delete_flight(fid)
                    _record(target, "cancel", {"id": fid}, resp,
                            extra={"deleted_id": fid})
                    continue
                # No exact flight to delete — never delete a guess. Leave it and
                # flag, so a human can remove it if it really was cancelled.
                results.append({"target": target, "operation": "skip", "ok": True,
                                "skipped": "cancellation — could not locate the "
                                           "exact flight to delete; left in place "
                                           "for manual review"})
                continue
            if target in _REV_CANCEL_MARK and rev_id:
                _do_cancel_mark(target, rev_id)
                continue
            results.append({"target": target, "operation": "skip", "ok": True,
                            "skipped": "cancellation — nothing to write"})
            continue

        # A collection-bound record cannot be written before its trip exists.
        # For an attach leg that means the minter sibling must be logged first.
        if target in _NEEDS_COLLECTION and not collection_id:
            results.append({"target": target, "operation": "create", "ok": False,
                            "dry_run": dry_run,
                            "error": "trip not created yet — log the journey's "
                                     "first flight first"})
            continue

        if target == "adventurelog.collection":
            body = {"name": resolution.get("collection_name"),
                    "start_date": resolution.get("collection_start"),
                    "end_date": resolution.get("collection_end")}
            if dry_run:
                _record(target, "create", body, None)
                # So the preview shows child records linking to this mint,
                # stand in a placeholder id the real run replaces with the id
                # AdventureLog returns.
                collection_id = collection_id or "(new collection)"
            else:
                ok, parsed = _record(target, "create", body,
                                     adventurelog.create_collection(
                                         body["name"], body["start_date"],
                                         body["end_date"]))
                if ok:
                    collection_id = _extract_id(parsed) or collection_id
                    # Persist the real id so ATTACH siblings (committed after
                    # this minter) can borrow it via mint_group.
                    if collection_id:
                        item.resolution = {**(item.resolution or {}),
                                           "collection_id": collection_id}
                        flag_modified(item, "resolution")

        elif target == "adventurelog.location":
            for place in _unsuppressed_places(payload):
                lat, lon = _coord(place.get("lat")), _coord(place.get("lon"))
                body = {"name": place.get("name") or place.get("raw") or "Place",
                        "latitude": lat, "longitude": lon,
                        "location": place.get("address"), "city": place.get("city"),
                        "region": place.get("region"), "country": place.get("country"),
                        "collections": [collection_id] if collection_id else []}
                if dry_run:
                    _record(target, "create", body, None,
                            extra={"place": body["name"]})
                    continue
                existing = _existing_location(adventurelog, place)
                if existing:
                    # The airport already exists (an earlier trip created it).
                    # Locations are M2M with collections, so still link it to
                    # THIS trip — otherwise a later journey through the same
                    # airport would leave it filed only under the first trip.
                    linked = _link_location_to_collection(
                        adventurelog, existing, collection_id)
                    results.append({"target": target, "operation": "skip",
                                    "ok": True, "place": body["name"],
                                    "skipped": ("already in AdventureLog"
                                                + (f" — {linked}" if linked else "")),
                                    "id": existing})
                    continue
                _record(target, "create", body,
                        adventurelog.create_location(
                            name=body["name"], latitude=lat,
                            longitude=lon, address=place.get("address"),
                            city=place.get("city"), region=place.get("region"),
                            country=place.get("country"),
                            collection_id=collection_id),
                        extra={"place": body["name"]})

        elif target == "adventurelog.transportation":
            body, synth = build_transportation(payload, collection_id=collection_id,
                                               airports=airports)
            existing = _dedup_al(adventurelog, target, collection_id, body, al_cache)
            if existing:
                results.append({"target": target, "operation": "skip", "ok": True,
                                "id": existing, "skipped": "already in AdventureLog"})
                continue
            resp = None if dry_run else adventurelog.create_transportation(body)
            _record(target, "create", body, resp,
                    extra={"synthesized_time": synth} if synth else None)

        elif target == "adventurelog.lodging":
            body = build_lodging(payload, collection_id=collection_id)
            existing = _dedup_al(adventurelog, target, collection_id, body, al_cache)
            if existing:
                results.append({"target": target, "operation": "skip", "ok": True,
                                "id": existing, "skipped": "already in AdventureLog"})
                continue
            resp = None if dry_run else adventurelog.create_lodging(body)
            _record(target, "create", body, resp)

        elif target == "adventurelog.note":
            body = build_note(payload, collection_id=collection_id)
            existing = _dedup_al(adventurelog, target, collection_id, body, al_cache)
            if existing:
                results.append({"target": target, "operation": "skip", "ok": True,
                                "id": existing, "skipped": "already in AdventureLog"})
                continue
            resp = None if dry_run else adventurelog.create_note(body)
            _record(target, "create", body, resp)

        elif target == "airtrail.flight":
            body, err = build_airtrail_flight(
                payload, airports=airports, airlines=airlines, self_uid=self_uid)
            if err:
                results.append({"target": target, "operation": "create",
                                "ok": False, "dry_run": dry_run, "error": err})
                continue
            # The duplicate check is a read, so run it for the PREVIEW too — an
            # already-logged flight should show as skip up front, not surprise
            # the operator only at execute.
            if airtrail is not None:
                dup = airtrail.find_duplicate(
                    carrier_iata=(payload["flight"]["marketing_carrier"] or {}).get("iata"),
                    flight_number=payload["flight"]["flight_number"],
                    departure_date=_date(payload["flight"]["departure"]["time"]) or "",
                    # body["from"]/["to"] are now code STRINGS, so read the IATA
                    # straight off the extracted places for the dup lookup.
                    origin_iata=((payload["flight"].get("departure") or {}).get("place") or {}).get("iata"),
                    destination_iata=((payload["flight"].get("arrival") or {}).get("place") or {}).get("iata"))
                if dup:
                    results.append({"target": target, "operation": "skip",
                                    "ok": True, "id": dup.get("id"),
                                    "skipped": "already in AirTrail"})
                    continue
            resp = None if dry_run else airtrail.save_flight(body)
            _record(target, "create", body, resp)

    envelope = {
        "committed_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run, "results": results,
    }
    if not dry_run:
        prior = (item.commit or {}).get("results") or []
        # Keep prior successful writes so a partial re-run reads as cumulative.
        envelope["results"] = _merge_results(prior, results)
        item.commit = envelope
        _apply_status(item, envelope["results"])
        for attr in ("commit", "status"):
            flag_modified(item, attr)
    return envelope


# ---------------------------------------------------------------- helpers

def _extract_id(parsed) -> str | None:
    if isinstance(parsed, dict):
        for k in ("id", "pk", "uuid"):
            if parsed.get(k):
                return str(parsed[k])
    return None


def _resolve_collection_id(session, item) -> str | None:
    """The collection this item's children file into: its own if it has one, or
    the one its journey minter created (looked up by the shared mint_group).
    Returns None when an attach leg's minter has not created the trip yet — the
    caller then refuses the collection-bound writes rather than orphaning them."""
    res = item.resolution or {}
    if res.get("collection_id"):
        return res["collection_id"]
    group = res.get("mint_group")
    if not group:
        return None
    for sib in (session.query(TravelItem)
                .filter(TravelItem.id != item.id).all()):
        sres = sib.resolution or {}
        if sres.get("mint_group") == group and sres.get("collection_id"):
            return str(sres["collection_id"])
    return None


def _resolve_or_mint_yonder_trip(session: Session, item: TravelItem,
                                 resolution: dict, collection_id: str | None,
                                 dry_run: bool) -> str | None:
    """The Yonder trip this item's activity files under, minting one when the
    resolver decided a NEW AdventureLog trip is needed and no EXISTING Yonder
    trip already covers it (owner, 2026-08-12): the two systems track the same
    journey, so whichever leg mints the AdventureLog collection also mints the
    matching Yonder trip, linked to it via adventurelog_collection_id — the
    same pairing "push to AdventureLog" makes in the other direction. An
    attach leg borrows the minter's trip via the shared mint_group, mirroring
    how _resolve_collection_id already does this for the AdventureLog side.
    Returns None when the minter sibling has not created the trip yet (out-of-
    order commit) — the caller then refuses this target for now, same as an
    unresolved AdventureLog collection, and it resolves on the next commit.
    """
    if resolution.get("yonder_trip_id"):
        return resolution["yonder_trip_id"]
    group = resolution.get("mint_group")
    if group:
        for sib in (session.query(TravelItem)
                    .filter(TravelItem.id != item.id).all()):
            sres = sib.resolution or {}
            if sres.get("mint_group") == group and sres.get("yonder_trip_id"):
                tid = str(sres["yonder_trip_id"])
                if not dry_run:
                    # Persist onto the ATTACH leg too, not just the minter --
                    # item.trip_id is read elsewhere (review UI, linked-capture
                    # listings, the auto-link skip check), so every leg of the
                    # journey should show as linked, not just the one that
                    # happened to create the trip.
                    item.trip_id = tid
                return tid
    if not resolution.get("is_minter"):
        return None                    # minter hasn't committed its trip yet
    if dry_run:
        return "(new Run·dere trip)"
    from app.models import Trip
    cid = (collection_id if collection_id
           and not str(collection_id).startswith("(") else None)
    # The minted trip belongs to whoever owns the capture that minted it (the
    # ingesting key's user, D-120) — same as a hand-made trip is owned by its
    # creator. NULL only when the capture itself is open (unscoped key).
    trip = Trip(source="resolver",
               name=resolution.get("collection_name") or "Untitled trip",
               start_date=resolution.get("collection_start"),
               end_date=resolution.get("collection_end"),
               adventurelog_collection_id=cid,
               owner_id=item.owner_id)
    session.add(trip)
    session.flush()
    item.trip_id = trip.id
    item.resolution = {**(item.resolution or {}), "yonder_trip_id": trip.id}
    flag_modified(item, "resolution")
    return trip.id


def _tag_booked_travelers(session: Session, activity_id: str, trip_id: str,
                          payload: dict, identities, *, keep_existing: bool,
                          item: TravelItem | None = None) -> None:
    """4.1: on a multi-traveller trip, tag the activity with who it's for.

    - A flight: the passengers on the booking ("Who flew" in review), matched
      to the trip's travellers. An unnamed booking was already assumed to be
      the capturing user's at resolve.
    - Anything else: the reviewer's "Who is this for" pick, else the traveller
      of the user who captured it.
    Only the trip's own travellers count; the whole roster means everyone. A
    re-commit of an activity someone already tagged by hand keeps their tags."""
    from app.trips import who
    roster = who.roster_ids(session, trip_id)
    if len(roster) < 2:
        return
    if keep_existing and who.activity_map(session, [activity_id]):
        return
    chosen: set = set()
    if payload.get("item_type") == "flight":
        if not identities:
            return
        from resolve.identity import match_identity
        for t in (payload.get("travelers") or []):
            ident, _, _ = match_identity(t.get("raw_name", ""), identities)
            if ident is not None:
                chosen.add(ident.id)
    elif item is not None:
        chosen = set((item.review or {}).get("traveler_ids") or [])
        if not chosen and item.owner_id:
            from app.models import User
            user = session.get(User, item.owner_id)
            if user is not None and user.traveler_id is not None:
                chosen = {user.traveler_id}
    if chosen:
        who.set_travelers(session, activity_id, trip_id, chosen)


def _unsuppressed_places(payload: dict) -> list[dict]:
    return [p for p in places_in_payload(payload) if not p.get("suppressed")]


def _existing_location(adventurelog, place: dict) -> str | None:
    """Find an existing AdventureLog location to reuse. Two calibrations, both
    from live data (2026-08-05):

    - **Airports query by IATA, not name.** Swarm-imported airports are named
      e.g. "Jomo Kenyatta International Airport (NBO)", which our extracted names
      ("…Nairobi", "Bole Intl.") don't match; but the IATA in parens
      does. The proximity filter drops the junk a bare code pulls in ('LAS' also
      matches Spanish 'las …').
    - **Airports need a looser radius (~2 km).** A Swarm check-in sits at a
      terminal while our geocode is the airport reference point — measured 1.0 km
      apart for NBO, 1.15 km for YYZ, so the old 0.5 km MISSED them and would
      have created duplicates. Non-airport places stay tight so two nearby
      properties never merge."""
    iata = (place.get("iata") or "").upper()
    query = iata or place.get("name") or place.get("raw") or ""
    radius = 2.0 if iata else 0.5
    try:
        hits = adventurelog.find_locations_near(
            query, place.get("lat"), place.get("lon"), radius_km=radius)
    except Exception:
        return None
    return str(hits[0]["id"]) if hits and hits[0].get("id") else None


def _collection_ids_of(loc: dict) -> list[str]:
    """The collection ids a location already belongs to, tolerating both id-list
    and object-list serializations."""
    out = []
    for c in (loc.get("collections") or []):
        cid = c.get("id") if isinstance(c, dict) else c
        if cid:
            out.append(str(cid))
    return out


def _link_location_to_collection(adventurelog, location_id: str,
                                 collection_id: str | None) -> str | None:
    """Best-effort: ensure an EXISTING location is also filed under this trip's
    collection (locations are M2M, so an airport reused by a later trip must be
    linked to it, not left on the first trip only). Returns a short note when it
    added the link, None otherwise. Never raises: a link failure must not block
    the rest of the commit — the location still exists, just unlinked here.

    Read-modify-write of the FULL set so it can only ADD; if the current set
    can't be read, it does nothing rather than risk replacing existing links."""
    if not collection_id:
        return None
    try:
        status, loc, _ = adventurelog.get_location(location_id)
        if status >= 400 or not isinstance(loc, dict):
            return None
        current = _collection_ids_of(loc)
        if str(collection_id) in current:
            return None                         # already filed under this trip
        st2, _, _ = adventurelog.set_location_collections(
            location_id, current + [str(collection_id)])
        return "linked to this trip" if st2 < 400 else None
    except Exception:
        return None


def _al_collection_records(adventurelog, collection_id, cache: dict) -> dict:
    """The transportations/lodging/notes already in a collection, fetched once
    per collection per commit run. A minted-collection placeholder (dry preview,
    "(...)" ) has none. Never raises — a read failure just means no dedup."""
    if not collection_id or str(collection_id).startswith("("):
        return {}
    if collection_id not in cache:
        try:
            st, parsed, _ = adventurelog.get_collection(collection_id)
            cache[collection_id] = (parsed if st < 400 and isinstance(parsed, dict)
                                    else {})
        except Exception:
            cache[collection_id] = {}
    return cache[collection_id]


def _match_transportation(records: dict, body: dict) -> str | None:
    """An existing transportation in the collection matching this one: by the
    route codes + mode when coded (flights/coded ground), else by name + date."""
    sc = (body.get("start_code") or "").upper()
    ec = (body.get("end_code") or "").upper()
    for t in (records.get("transportations") or []):
        if sc and ec:
            if (t.get("start_code") or "").upper() == sc and \
               (t.get("end_code") or "").upper() == ec and \
               t.get("type") == body.get("type"):
                return t.get("id")
        elif body.get("name") and t.get("name") == body.get("name") and \
                str(t.get("date") or "")[:10] == str(body.get("date") or "")[:10]:
            return t.get("id")
    return None


def _match_lodging(records: dict, body: dict) -> str | None:
    ref = body.get("reservation_number")
    for l in (records.get("lodging") or []):
        if ref and str(l.get("reservation_number") or "") == str(ref):
            return l.get("id")
    for l in (records.get("lodging") or []):
        if body.get("name") and l.get("name") == body.get("name"):
            return l.get("id")
    return None


def _match_note(records: dict, body: dict) -> str | None:
    for n in (records.get("notes") or []):
        if n.get("name") == body.get("name") and \
                str(n.get("date") or "")[:10] == str(body.get("date") or "")[:10]:
            return n.get("id")
    return None


_AL_DEDUP = {"adventurelog.transportation": _match_transportation,
             "adventurelog.lodging": _match_lodging,
             "adventurelog.note": _match_note}


def _dedup_al(adventurelog, target: str, collection_id, body: dict,
              cache: dict) -> str | None:
    """The id of a record already in the collection matching this body, or None.
    Runs for the dry preview too, so an already-present record shows as skipped
    before the operator confirms — the same way the flight duplicate check does."""
    matcher = _AL_DEDUP.get(target)
    if adventurelog is None or matcher is None or not collection_id:
        return None
    return matcher(_al_collection_records(adventurelog, collection_id, cache), body)


def _merge_results(prior: list[dict], new: list[dict]) -> list[dict]:
    by_target = {r.get("target"): r for r in prior if r.get("ok")}
    for r in new:
        by_target[r.get("target")] = r
    return list(by_target.values())


def _apply_status(item: TravelItem, results: list[dict]) -> None:
    status = dict(item.status or {})
    for system in ("airtrail", "adventurelog"):
        rows = [r for r in results if str(r.get("target", "")).startswith(system)]
        if not rows:
            continue
        if all(r.get("ok") for r in rows):
            status[system] = "committed"
        elif any(r.get("ok") for r in rows):
            status[system] = "partial"
        else:
            status[system] = "failed"
    item.status = status


# ---------------------------------------------------------------- batch

def commit_pending(session: Session, adventurelog=None, airtrail=None, *,
                   identities=None, dry_run: bool = True, limit: int = 0,
                   progress=None, item_filter=None, external: bool = True,
                   clients_for=None) -> dict:
    """Commit every approved, resolved item. Sequential (one flight at a time,
    D-06); per-item commit so one failure never blocks the rest.

    `item_filter` (a TravelItem predicate, e.g. scope.capture_filter(user))
    limits it to the captures that person may log — the web "Log approved"
    button passes the pressing user's, so nobody logs someone else's approved
    items (YONDER-105). None = every item (the operator CLI).

    `clients_for(owner_id) -> integrations.Clients` gives each capture its
    OWNER's AirTrail/AdventureLog clients (D-150): an admin logging a shared
    user's capture writes to that user's accounts, never their own, and an
    owner with no connection gets Run·dere only. Without it, the clients passed
    here are used for every item (the operator CLI and tests); `external=False`
    then skips them all."""
    from app.integrations import Clients
    if clients_for is None:
        fixed = Clients(None, adventurelog=adventurelog if external else None,
                        airtrail=airtrail if external else None)
        clients_for = lambda _owner: fixed      # noqa: E731
    refs: dict[int, tuple] = {}                 # per Clients: (airports, airlines)

    def references(c) -> tuple:
        if id(c) not in refs:
            flown_airports = flown_airlines = {}
            if c.airtrail is not None:
                try:
                    flights = c.airtrail.list_flights()
                    flown_airports = c.airtrail.airport_reference()
                    flown_airlines = c.airtrail.airline_reference(flights)
                except Exception as ex:
                    log.warning("AirTrail reference load failed: %s", ex)
            # Flown reference first; then Run·dere's own airport table and
            # AirTrail's airline search, over the API with this person's token —
            # no AirTrail database (D-186).
            from resolve import airports as airports_mod
            search = getattr(c.airtrail, "search_airline", None)
            refs[id(c)] = (_Resolver(flown_airports, airports_mod.lookup),
                           _Resolver(flown_airlines, search))
        return refs[id(c)]

    q = session.query(TravelItem)
    if item_filter is not None:
        q = q.filter(item_filter)
    items = [i for i in q.order_by(TravelItem.created_at).all() if committable(i)]
    if limit:
        items = items[:limit]

    total = len(items)
    summary = {"items": 0, "writes_ok": 0, "writes_failed": 0,
               "skipped": 0, "dry_run": dry_run, "plans": []}
    for idx, item in enumerate(items):
        if progress:
            progress("logging", idx, total, str(item.canonical_key or item.id)[:70])
        c = clients_for(item.owner_id)
        airports, airlines = references(c)
        env = commit_item(session, item, c.adventurelog, c.airtrail,
                          identities=identities, airports=airports,
                          airlines=airlines, dry_run=dry_run, external=c.external,
                          self_uid=c.airtrail_user_id)
        summary["items"] += 1
        for r in env["results"]:
            if is_external_skip(r):
                summary["external_skipped"] = summary.get("external_skipped", 0) + 1
            elif r.get("skipped"):
                summary["skipped"] += 1
            elif r.get("ok"):
                summary["writes_ok"] += 1
            else:
                summary["writes_failed"] += 1
        summary["plans"].append({"item_id": item.id,
                                 "headline": item.canonical_key,
                                 "results": env["results"]})
        if not dry_run:
            session.commit()
    return summary
