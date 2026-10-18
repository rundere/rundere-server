"""A5 — canonical key generation.

Pure functions, no I/O. Spec: docs/A5-canonical-keys.md.

Every builder returns a KeyResult: one `primary` (what lands in
TravelItem.canonical_key, unique-indexed) plus `alternates` (resolver-internal
match keys, stored in the item_keys table — never in the payload schema).
Two items denote the same real-world thing iff their key sets intersect.

The cross-channel requirement (HANDOFF A5): the airline email, the Google
calendar entry, and a SingleFile capture of the manage-booking page must
collapse to the same flight. Channels disagree about which fields they carry
(the calendar entry often lacks the PNR), so collapse is defined over the key
SET, not the primary alone.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------- text

_ARTICLES = {"the", "a", "an", "le", "la", "les", "el", "los", "das", "der", "die"}
_LODGING_STOP = {"hotel", "hostel", "inn", "resort", "apartment", "apartments",
                 "suites", "bnb", "b", "guesthouse", "lodge", "motel"}
_EVENT_STOP = {"tickets", "ticket", "presents", "live", "tour", "show", "concert",
               "admission", "entry", "event", "performance", "matinee", "evening"}


def normalize_text(s: str) -> str:
    """Casefold, strip diacritics, collapse every punctuation run to one space."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^0-9a-zA-Z]+", " ", s.casefold())
    return " ".join(s.split())


def token_signature(s: str, drop: set[str] | None = None) -> str:
    """Sorted, deduped, hyphen-joined distinctive tokens.

    Sorting makes the signature word-order-insensitive; `drop` removes context
    tokens (city, country, boilerplate). If stripping would leave nothing, the
    undropped token set is kept — a key segment must never be empty.
    """
    tokens = normalize_text(s).split()
    kept = [t for t in tokens if t not in _ARTICLES and t not in (drop or set())]
    if not kept:
        kept = [t for t in tokens if t not in _ARTICLES] or tokens
    return "-".join(sorted(set(kept)))


def _context_tokens(*parts: str | None) -> set[str]:
    out: set[str] = set()
    for p in parts:
        if p:
            out.update(normalize_text(p).split())
    return out


def _code(s: str) -> str:
    """Uppercase alphanumerics only — PNRs, IATA codes, station codes."""
    return re.sub(r"[^0-9A-Z]", "", (s or "").upper())


# ---------------------------------------------------------------- geohash

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash(lat: float, lon: float, precision: int = 5) -> str:
    """Standard geohash. Precision 5 ≈ 4.9 km × 4.9 km cell (OQ-11)."""
    lat_r, lon_r = [-90.0, 90.0], [-180.0, 180.0]
    bits, out, even = 0, [], True
    ch = 0
    while len(out) < precision:
        rng = lon_r if even else lat_r
        val = lon if even else lat
        mid = (rng[0] + rng[1]) / 2
        ch <<= 1
        if val >= mid:
            ch |= 1
            rng[0] = mid
        else:
            rng[1] = mid
        even = not even
        bits += 1
        if bits == 5:
            out.append(_BASE32[ch])
            bits, ch = 0, 0
    return "".join(out)


# ---------------------------------------------------------------- result

@dataclass(frozen=True)
class KeyResult:
    primary: str
    alternates: tuple[str, ...] = ()

    def all_keys(self) -> frozenset[str]:
        return frozenset((self.primary, *self.alternates))


def matches(a: KeyResult, b: KeyResult) -> bool:
    """Same real-world thing iff the key sets intersect."""
    return bool(a.all_keys() & b.all_keys())


def _result(primary: str, *alts: str | None) -> KeyResult:
    seen = [a for a in alts if a and a != primary]
    deduped = tuple(dict.fromkeys(seen))
    return KeyResult(primary=primary, alternates=deduped)


# ---------------------------------------------------------------- flight

def normalize_flight_number(number: str | int, carrier: str | None = None) -> str:
    """'AC 0652' / '0652' / 652 → '652'; optional suffix letter survives ('652A')."""
    s = _code(str(number))
    if carrier:
        c = _code(carrier)
        if c and s.startswith(c) and len(s) > len(c):
            s = s[len(c):]
    # A number may still carry a leading carrier code — e.g. "AC9471" when the
    # supplied carrier was the OPERATING regional ("QK") that does not match the
    # marketing prefix on the number. Strip any leading 1-3 letter code before a
    # run of digits; the flight number itself is the digits (+ optional suffix).
    m = re.fullmatch(r"[A-Z]{0,3}0*([0-9]{1,4})([A-Z]?)", s)
    if not m:
        raise ValueError(f"unparseable flight number: {number!r}")
    return m.group(1) + m.group(2)


def flight_key(carrier: str, number: str | int, departure_date: str,
               origin_iata: str, pnr: str | None = None,
               force_date: bool = False) -> KeyResult:
    """OQ-10.

    PNR form   flight:pnr:{PNR}:{carrier}:{number}:{origin}
               — survives the past-midnight schedule change that breaks a
               date-anchored key. Primary whenever a booking reference exists.
    date form  flight:{carrier}:{number}:{date}:{origin}
               — primary when no PNR (typical of calendar entries); ALWAYS
               present as an alternate so mixed-channel captures collapse.

    force_date: resolver escape hatch for the same-PNR repeated-segment case
    (weekly commuter). RULE-17c flags the intra-batch duplicate; the resolver
    re-keys the duplicates date-anchored.
    """
    c = _code(carrier)
    n = normalize_flight_number(number, carrier)
    o = _code(origin_iata)
    d = str(departure_date)[:10]
    if not (c and n and o and d):
        raise ValueError("flight_key: carrier, number, origin and date are required")
    date_form = f"flight:{c}:{n}:{d}:{o}"
    if pnr and not force_date:
        return _result(f"flight:pnr:{_code(pnr)}:{c}:{n}:{o}", date_form)
    return _result(date_form)


# ---------------------------------------------------------------- lodging

def lodging_key(property_name: str, check_in_date: str,
                city: str | None = None, country: str | None = None,
                confirmation: str | None = None) -> KeyResult:
    """OQ-12.

    conf form  lodging:conf:{CONF}          — primary when a confirmation
               number exists; stable across name variants AND date changes.
    name form  lodging:{name_sig}:{check_in} — primary otherwise; always an
               alternate. name_sig drops articles, lodging boilerplate, and
               the city/country tokens, then sorts — so 'The Hoxton,
               Shoreditch' / 'Hoxton Shoreditch' / 'The Hoxton — Shoreditch
               London' all yield 'hoxton-shoreditch'.
    """
    sig = token_signature(property_name,
                          drop=_LODGING_STOP | _context_tokens(city, country))
    d = str(check_in_date)[:10]
    if not (sig and d):
        raise ValueError("lodging_key: property_name and check_in_date are required")
    name_form = f"lodging:{sig}:{d}"
    if confirmation:
        return _result(f"lodging:conf:{_code(confirmation)}", name_form)
    return _result(name_form)


# ---------------------------------------------------------------- event

def event_key(title: str, start_date: str,
              venue_name: str | None = None, city: str | None = None,
              gcal_event_id: str | None = None) -> KeyResult:
    """OQ-13.

    content form  event:{date}:{title_sig} — primary. title_sig drops ticketing
                  boilerplate plus venue/city tokens, so 'Hamilton — Victoria
                  Palace Theatre' (calendar) and 'Your tickets: Hamilton'
                  (email) both reduce to 'hamilton'.
    gcal form     event:gcal:{id} — alternate when the capture carries the
                  Google Calendar event id; exact across gcal recaptures.
    """
    drop = _EVENT_STOP | _context_tokens(venue_name, city) | {"your", "order",
                                                              "confirmation", "for"}
    sig = token_signature(title, drop=drop)
    d = str(start_date)[:10]
    if not (sig and d):
        raise ValueError("event_key: title and start_date are required")
    return _result(f"event:{d}:{sig}",
                   f"event:gcal:{gcal_event_id}" if gcal_event_id else None)


# ---------------------------------------------------------------- ground

_MODE_PREFIX = {"train": "rail", "bus": "bus", "ferry": "ferry"}
# Modes with no timetable to key on. A rental or transfer is identified by its
# booking reference; there is no service number to anchor to.
_BOOKED_MODE_PREFIX = {"car_rental": "carrental", "rideshare": "rideshare", "taxi": "taxi",
                       "transfer": "transfer", "other": "ground"}


def booked_ground_key(mode: str, confirmation: str,
                      start_date: str | None = None) -> KeyResult:
    """Ground travel identified by its booking reference rather than a
    timetable: car rentals, transfers, and scheduled services whose number the
    document never states (measured: an Amtrak eTicket carrying a PNR but no
    train number). Date rides along as an alternate when known, so a re-issued
    confirmation for the same day still collapses."""
    prefix = _BOOKED_MODE_PREFIX.get(mode) or _MODE_PREFIX.get(mode) or "ground"
    conf = _code(confirmation)
    if not conf:
        raise ValueError("booked_ground_key: confirmation is required")
    primary = f"{prefix}:conf:{conf}"
    alt = f"{prefix}:{conf}:{str(start_date)[:10]}" if start_date else None
    return _result(primary, alt)


def road_key(travel_date: str, origin_lat: float, origin_lon: float,
             dest_lat: float, dest_lon: float) -> KeyResult:
    """OQ-11. Endpoints truncated to geohash-5 (~4.9 km) absorb the
    few-decimals jitter between captures of the same route. The geohash-4
    form (~39 km) rides along as an alternate to absorb cell-boundary
    straddle; the resolver's proximity match is the final backstop.
    Waypoints are deliberately NOT keyed: re-planning a stop is the same drive.
    """
    d = str(travel_date)[:10]
    if not d:
        raise ValueError("road_key: travel_date is required (RULE-31)")
    g5 = geohash(origin_lat, origin_lon, 5), geohash(dest_lat, dest_lon, 5)
    g4 = geohash(origin_lat, origin_lon, 4), geohash(dest_lat, dest_lon, 4)
    return _result(f"road:{d}:{g5[0]}:{g5[1]}", f"road4:{d}:{g4[0]}:{g4[1]}")


def scheduled_ground_key(mode: str, operator: str, service_number: str | int,
                         departure_date: str, origin_code: str,
                         pnr: str | None = None) -> KeyResult:
    """Trains/buses/ferries with a service number mirror flights:
    rail:{operator}:{service}:{date}:{origin}, PNR form first when present.
    """
    prefix = _MODE_PREFIX.get(mode, "ground")
    op, svc, o = _code(operator), _code(str(service_number)), _code(origin_code)
    d = str(departure_date)[:10]
    if not (op and svc and o and d):
        raise ValueError("scheduled_ground_key: operator, service, origin, date required")
    date_form = f"{prefix}:{op}:{svc}:{d}:{o}"
    if pnr:
        return _result(f"{prefix}:pnr:{_code(pnr)}:{op}:{svc}:{o}", date_form)
    return _result(date_form)


# ---------------------------------------------------------------- dispatcher

def keys_for(payload: dict, capture_context: dict | None = None) -> KeyResult | None:
    """Key a schema-shaped Payload (travel-item.schema.json). Returns None when
    the item cannot be keyed (item_type=unknown, or required fields absent) —
    that is a needs_review signal, not an error.
    """
    ctx = capture_context or {}
    t = payload.get("item_type")
    try:
        if t == "flight":
            f = payload["flight"]
            dep = f["departure"]
            # marketing_carrier is a Provider object in the schema; prefer its
            # IATA code, fall back to name, then to the payload-level provider.
            mc = f.get("marketing_carrier") or payload.get("provider") or {}
            carrier = mc.get("iata") or mc.get("name") if isinstance(mc, dict) else mc
            return flight_key(
                carrier=carrier,
                number=f["flight_number"],
                departure_date=dep["time"]["local"],
                origin_iata=dep["place"]["iata"],
                pnr=payload.get("booking_reference"),
            )
        if t == "lodging":
            l = payload["lodging"]
            place = l.get("place") or {}
            return lodging_key(
                property_name=l["property_name"],
                check_in_date=l["check_in"]["local"],
                city=place.get("city"), country=place.get("country"),
                confirmation=payload.get("booking_reference"),
            )
        if t == "event":
            e = payload["event"]
            venue = e.get("venue") or {}
            return event_key(
                title=e["title"], start_date=e["start"]["local"],
                venue_name=venue.get("name"), city=venue.get("city"),
                gcal_event_id=e.get("gcal_event_id"),
            )
        if t == "ground_transport":
            g = payload["ground_transport"]
            mode = g.get("mode")
            ref = payload.get("booking_reference")
            # A booked mode (rental/transfer), or a scheduled one whose service
            # number the document never printed: key on the confirmation.
            if ref and (mode in _BOOKED_MODE_PREFIX
                        or (mode in _MODE_PREFIX and not g.get("service_number"))):
                start = ((g.get("departure") or {}).get("time") or {}).get("local")
                return booked_ground_key(mode, ref, start)
            if mode in _MODE_PREFIX and g.get("service_number"):
                dep = g["departure"]
                place = dep["place"]
                return scheduled_ground_key(
                    mode=mode, operator=(g.get("route") or {}).get("provider")
                    or (payload.get("provider") or {}).get("name", ""),
                    service_number=g["service_number"],
                    departure_date=dep["time"]["local"],
                    origin_code=place.get("iata") or place.get("raw")
                    or place.get("name", ""),
                    pnr=payload.get("booking_reference"),
                )
            # road: date comes from the human (capture_context), per RULE-31/34.
            # The endpoints are departure/arrival — the SAME structure the booked
            # and scheduled paths above use, and what maps_url.py + the committer
            # produce; waypoints are the intermediate stops, not the endpoints.
            date = ctx.get("travel_date") or (
                (g.get("departure") or {}).get("time") or {}).get("local", "")[:10]
            o = (g.get("departure") or {}).get("place") or {}
            dst = (g.get("arrival") or {}).get("place") or {}
            if date and o.get("lat") is not None and dst.get("lat") is not None:
                return road_key(date, o["lat"], o["lon"], dst["lat"], dst["lon"])
            return None
        if t == "note":
            title = payload.get("title") or ctx.get("title")
            date = ctx.get("travel_date")
            if title and date:
                return _result(f"note:{str(date)[:10]}:{token_signature(title)}")
            return None
        return None  # unknown
    except (KeyError, TypeError, ValueError):
        return None


def unkeyable_reason(payload: dict, capture_context: dict | None = None) -> str:
    """Explain, as a review.reasons code, WHY keys_for returned None.

    keys_for stays deliberately boolean-ish (KeyResult | None). This picks the
    ACTIONABLE reason so the queue never labels a perfectly-typed item
    "unknown_type" when its type is obvious and only one field is absent. The
    canonical example (2026-08-04): an Air Canada e-ticket that prints airport
    NAMES but no IATA codes — the extractor is right not to guess codes
    (extract/llm.py), the flight key legitimately needs one, so the fix is a
    human typing the code, not re-labelling the booking.
    """
    t = payload.get("item_type")
    if t == "flight":
        f = payload.get("flight") or {}
        # Only blame the airport code when the flight is otherwise keyable —
        # carrier, number and a departure date are present. If those are also
        # missing, entering a code alone would not clear the block, so fall
        # through to the generic reason.
        mc = f.get("marketing_carrier") or payload.get("provider") or {}
        carrier = (mc.get("iata") or mc.get("name")) if isinstance(mc, dict) else mc
        dep_date = (((f.get("departure") or {}).get("time")) or {}).get("local")
        if carrier and f.get("flight_number") and dep_date:
            for side in ("departure", "arrival"):
                place = ((f.get(side) or {}).get("place")) or {}
                if not place.get("iata"):
                    return "missing_airport_code"
    if t == "ground_transport":
        # A drive (the road path in keys_for) keys on its date plus both
        # endpoints' coordinates. A Google Maps link that names its stops but
        # embeds no coordinates, where the geocoder could not place a stop, is a
        # perfectly-typed drive; the fix is a date or a locatable From/To, never
        # "set the type" (YONDER-121). Route exactly as keys_for does: a taxi read
        # as mode "car" with a stray service number is still a drive (2026-10-12).
        g = payload.get("ground_transport") or {}
        mode, ref, sn = g.get("mode"), payload.get("booking_reference"), g.get("service_number")
        by_ref = bool(ref) and (mode in _BOOKED_MODE_PREFIX
                                or (mode in _MODE_PREFIX and not sn))
        scheduled = mode in _MODE_PREFIX and bool(sn)
        if not by_ref and not scheduled:
            ctx = capture_context or {}
            date = ctx.get("travel_date") or (
                (g.get("departure") or {}).get("time") or {}).get("local", "")[:10]
            if not date:
                return "missing_travel_date"
            for side in ("departure", "arrival"):
                if ((g.get(side) or {}).get("place") or {}).get("lat") is None:
                    return "geocode_failed"
    return "unknown_type"
