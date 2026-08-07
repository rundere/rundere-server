"""schema.org JSON-LD extractor — first in the chain (D-10).

Measured hit rate on the first real corpus: ~0% (D-10 addendum). It stays
first anyway: attempting it costs microseconds, some vendor may yet emit it,
and when present it is the most trustworthy source in the whole system.

Maps FlightReservation / LodgingReservation / EventReservation /
TrainReservation / RentalCarReservation to schema-conformant Payloads.
"""
from __future__ import annotations

import json
import re

from resolve.keys import normalize_flight_number

_LDJSON = re.compile(
    r'(?is)<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>')

_ISO = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2})(?::\d{2})?(?:\.\d+)?([Zz]|[+-]\d{2}:?\d{2})?)?")


def _dt(value: str | None) -> dict | None:
    """schema.org datetime -> DateTimeLocal. The offset (if any) is dropped —
    it is not an IANA zone and DateTimeLocal.local is the wall-clock time at
    the place, which is what these fields carry. Never synthesizes (D-06)."""
    if not value:
        return None
    m = _ISO.match(str(value).strip())
    if not m:
        return None
    date_part, time_part, zone = m.groups()
    if time_part:
        return {"local": f"{date_part}T{time_part}", "precision": "datetime",
                "tz": "Etc/UTC" if zone and zone.upper() == "Z" else None,
                "origin": "document"}
    return {"local": date_part, "precision": "date", "tz": None, "origin": "document"}


def _airport(node: dict | None) -> dict | None:
    if not isinstance(node, dict):
        return None
    iata = node.get("iataCode")
    name = node.get("name")
    raw = name or iata
    if not raw:
        return None
    place = {"raw": str(raw)}
    if isinstance(iata, str) and re.fullmatch(r"[A-Z]{3}", iata):
        place["iata"] = iata
    if name:
        place["name"] = str(name)
    return place


def _provider(node: dict | None, kind: str) -> dict | None:
    if not isinstance(node, dict) or not node.get("name"):
        return None
    p = {"name": str(node["name"]), "kind": kind}
    iata = node.get("iataCode")
    if isinstance(iata, str) and re.fullmatch(r"[A-Z0-9]{2}", iata):
        p["iata"] = iata
    return p


def _travelers(node) -> list[dict]:
    nodes = node if isinstance(node, list) else [node] if node else []
    out = []
    for n in nodes:
        name = n.get("name") if isinstance(n, dict) else n
        if name:
            out.append({"raw_name": str(name)})
    return out


_STATUS = {
    "reservationconfirmed": "confirmed",
    "reservationcancelled": "cancelled",
    "reservationpending": "pending",
}


def _status(node: dict) -> str:
    s = str(node.get("reservationStatus") or "").rsplit("/", 1)[-1].lower()
    return _STATUS.get(s, "confirmed")


_DESIGNATOR = re.compile(r"([A-Z]{2,3})?0*(\d{1,4})([A-Z]?)")


def _flight(res: dict) -> dict | None:
    f = res.get("reservationFor") or {}
    dep_t, arr_t = _dt(f.get("departureTime")), _dt(f.get("arrivalTime"))
    dep_p, arr_p = _airport(f.get("departureAirport")), _airport(f.get("arrivalAirport"))
    number = f.get("flightNumber")
    if not (dep_t and arr_t and dep_p and arr_p and number):
        return None

    # schema.org's `provider` is frequently the OPERATING carrier — a regional
    # like Jazz ("QK") flying an "AC9471" that Air Canada markets — NOT the
    # marketing carrier the flight number names. The canonical key is built on
    # the marketing carrier, so conflating the two (as this handler used to)
    # both crashed normalize_flight_number and, once fixed, would key the flight
    # under the wrong airline and break cross-channel collapse.
    operating = _provider(f.get("provider") or f.get("airline"), "airline")
    m = _DESIGNATOR.fullmatch(str(number).replace(" ", "").upper())
    mkt_code = m.group(1) if (m and m.group(1)) else None
    try:
        number_norm = normalize_flight_number(
            str(number), mkt_code or (operating or {}).get("iata"))
    except ValueError:
        return None

    if mkt_code and operating and operating.get("iata") == mkt_code:
        marketing, operating_out = operating, None       # mainline: one carrier
    elif mkt_code:
        # We know the marketing code from the number but not its display name;
        # Provider.name is required, so fall back to the code itself.
        marketing = {"name": mkt_code, "iata": mkt_code, "kind": "airline"}
        operating_out = operating                        # regional / codeshare
    else:
        marketing, operating_out = operating, None       # number had no prefix

    flight = {
        "marketing_carrier": marketing,
        "flight_number": number_norm,
        "departure": {"place": dep_p, "time": dep_t},
        "arrival": {"place": arr_p, "time": arr_t},
    }
    if operating_out:
        flight["operating_carrier"] = operating_out
    return {
        "item_type": "flight",
        "booking_status": _status(res),
        # Air Canada (and others) carry the PNR in reservationId, not
        # reservationNumber — accept either.
        "booking_reference": (res.get("reservationNumber")
                              or res.get("reservationId") or None),
        "provider": marketing,
        "travelers": _travelers(res.get("underName")),
        "title": None, "notes": None,
        "flight": flight,
    }


def _lodging(res: dict) -> dict | None:
    biz = res.get("reservationFor") or {}
    name = biz.get("name")
    ci = _dt(res.get("checkinTime") or res.get("checkinDate"))
    co = _dt(res.get("checkoutTime") or res.get("checkoutDate"))
    if not (name and ci and co):
        return None
    place = {"raw": str(name), "name": str(name)}
    addr = biz.get("address")
    if isinstance(addr, dict):
        if addr.get("addressLocality"):
            place["city"] = str(addr["addressLocality"])
        cc = addr.get("addressCountry")
        cc = cc.get("name") if isinstance(cc, dict) else cc
        if isinstance(cc, str) and re.fullmatch(r"[A-Z]{2}", cc):
            place["country"] = cc
    return {
        "item_type": "lodging",
        "booking_status": _status(res),
        "booking_reference": res.get("reservationNumber") or None,
        "travelers": _travelers(res.get("underName")),
        "lodging": {"property_name": str(name), "lodging_kind": "hotel",
                    "place": place, "check_in": ci, "check_out": co},
    }


def _event(res: dict) -> dict | None:
    ev = res.get("reservationFor") or {}
    title, start = ev.get("name"), _dt(ev.get("startDate"))
    if not (title and start):
        return None
    venue = None
    loc = ev.get("location")
    if isinstance(loc, dict) and loc.get("name"):
        venue = {"raw": str(loc["name"]), "name": str(loc["name"])}
    detail = {"title": str(title), "start": start, "venue": venue}
    end = _dt(ev.get("endDate"))
    if end:
        detail["end"] = end
    return {
        "item_type": "event",
        "booking_status": _status(res),
        "booking_reference": res.get("reservationNumber") or None,
        "travelers": _travelers(res.get("underName")),
        "event": detail,
    }


def _train(res: dict) -> dict | None:
    trip = res.get("reservationFor") or {}
    dep_t, arr_t = _dt(trip.get("departureTime")), _dt(trip.get("arrivalTime"))
    dep_n, arr_n = trip.get("departureStation"), trip.get("arrivalStation")
    dep_n = dep_n.get("name") if isinstance(dep_n, dict) else dep_n
    arr_n = arr_n.get("name") if isinstance(arr_n, dict) else arr_n
    if not (dep_t and arr_t and dep_n and arr_n):
        return None
    return {
        "item_type": "ground_transport",
        "booking_status": _status(res),
        "booking_reference": res.get("reservationNumber") or None,
        "provider": _provider(trip.get("provider"), "rail"),
        "travelers": _travelers(res.get("underName")),
        "ground_transport": {
            "mode": "train",
            "service_number": str(trip.get("trainNumber")) if trip.get("trainNumber") else None,
            "departure": {"place": {"raw": str(dep_n)}, "time": dep_t},
            "arrival": {"place": {"raw": str(arr_n)}, "time": arr_t},
        },
    }


_HANDLERS = {
    "FlightReservation": _flight,
    "LodgingReservation": _lodging,
    "EventReservation": _event,
    "TrainReservation": _train,
}


def _nodes(doc) -> list[dict]:
    if isinstance(doc, list):
        return [n for item in doc for n in _nodes(item)]
    if isinstance(doc, dict):
        if "@graph" in doc:
            return _nodes(doc["@graph"])
        return [doc]
    return []


def extract_jsonld(html: str) -> list[dict]:
    """Returns schema-conformant Payloads for every reservation found."""
    payloads = []
    for block in _LDJSON.findall(html):
        try:
            doc = json.loads(block.strip())
        except ValueError:
            continue
        for node in _nodes(doc):
            handler = _HANDLERS.get(str(node.get("@type") or ""))
            if handler:
                p = handler(node)
                if p:
                    payloads.append(p)
    return payloads
