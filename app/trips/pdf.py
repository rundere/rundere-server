"""Timeline PDF export — a shareable, print-friendly itinerary for people who
aren't on Yonder yet. Rendered server-side (HTML template → WeasyPrint), so the
web trip page and the iOS app hand out the identical document.

The content is the trip page's own timeline (`timeline_day_groups`), so the PDF
never drifts from what the owner sees. Internal-only data is left out: source
document links (they point at this server) and the edit affordances.

What else goes in is the downloader's choice (`Sections`, SPEC-8 §13, D-233):
times, addresses, maps and booking details by default, and optionally their own
packing list and Discover briefing. Nothing is looked up to make it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.models import Trip
from app.trips import routes as trip_routes

# Fields that only make sense inside Yonder.
_OMIT_KEYS = {"sourceLink", "mapLink"}

# What each section flag covers (D-233). Address fields are street-level
# places; a drive's or taxi's ends are addresses too, a flight's or train's
# are airports and stations, which are the itinerary.
_ADDRESS_KEYS = {"address", "location", "pickupLocation", "dropoffLocation"}
_STREET_ENDS = {"drive", "taxi"}
# The itinerary itself, always printed. Any other field (confirmation codes,
# seats, notes, a field added later) counts as a booking detail, so a new
# field is left out of a "no details" handout rather than leaked into it.
_ITINERARY_KEYS = {"airline", "flightNumber", "from", "to", "operator", "trainNumber",
                   "route", "company", "name", "type", "duration", "provider", "venue",
                   "vessel", "departurePort", "arrivalPort", "title", "text"}
# Discover topics that make a pre-trip briefing; events, food and saved
# research are planning material, not something to carry.
_BRIEFING_TOPICS = ("weather", "currency", "plugs", "visa", "safety", "language",
                    "language_regional", "language_local")
# Point roles for found addresses on two-ended trips worth a found address:
# the station, airport or port, not a street (those are typed).
_FOUND_ENDS = {"flight", "train", "cruise"}


@dataclass(frozen=True)
class Sections:
    """What goes in the PDF. The defaults are the PDF before D-233, so a link
    or an app build that passes no flags gets exactly what it got before."""
    times: bool = True
    addresses: bool = True
    maps: bool = True
    details: bool = True
    packing: bool = False
    discover: bool = False


_TRUE = {"1", "true", "on", "yes"}


def sections_from_query(params) -> Sections:
    """Sections from the trip page's Export form or a link. The form sends
    `pick=1` with only the ticked boxes, so there an absent flag is off; a
    link without `pick` keeps each default."""
    explicit = "pick" in params
    vals = {}
    for name, default in vars(Sections()).items():
        raw = params.get(name)
        vals[name] = (default if not explicit else False) if raw is None \
            else raw.strip().lower() in _TRUE
    return Sections(**vals)


def _kind(atype: str, key: str, kind: str | None) -> str:
    if kind == "datetime-local":
        return "time"
    if key in _ADDRESS_KEYS or (key in ("from", "to") and atype in _STREET_ENDS):
        return "address"
    return "itinerary" if key in _ITINERARY_KEYS else "detail"


def _wanted(atype: str, f: dict, sec: Sections) -> bool:
    k = _kind(atype, f.get("key") or "", f.get("kind"))
    return {"time": sec.times, "address": sec.addresses,
            "detail": sec.details}.get(k, True)


def _field_rows(fields: list[dict], shown: set[str] = frozenset()) -> list[dict]:
    """Labelled rows for one activity, minus internal links and any value
    already printed as its title/subtitle (property name, address, …)."""
    rows = []
    for f in fields:
        if f.get("key") in _OMIT_KEYS or f.get("is_link") or f["value"] in shown:
            continue
        value = f["value"]
        if f.get("kind") == "datetime-local":
            value = trip_routes.fmt_datetime(value) or value
        rows.append({"label": f["label"], "value": value,
                     "long": f.get("kind") == "textarea"})
    return rows


def _start_row(a: dict) -> list[dict]:
    """Pair the start with the end ("Check-in"/"Check-out") when both exist;
    a lone start is already the time column."""
    if a.get("has_end") and a.get("start_display"):
        return [{"label": a["start_label"], "value": a["start_display"], "long": False}]
    return []


def _day_heading(day: str, fallback: str) -> str:
    """'2026-04-10' -> 'Friday, Apr 10, 2026'."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return fallback
    return f"{d.strftime('%A')}, {trip_routes.fmt_date(day)}"


def _data_uri(img) -> str | None:
    if img is None:
        return None
    import base64
    return f"data:{img.mime};base64,{base64.b64encode(img.data).decode('ascii')}"


def _points(a: dict) -> dict[str, list[dict]]:
    by_role: dict[str, list[dict]] = {}
    for p in ((a.get("geo") or {}).get("points") or []):
        by_role.setdefault(p["role"], []).append(p)
    return by_role


def _route_line(a: dict) -> tuple[list[tuple[float, float]], bool]:
    """A drive's road line (decoded), or a straight dashed stand-in through its
    stops when no router answered."""
    from resolve.routing import decode_polyline
    geo = a.get("geo") or {}
    enc = (geo.get("route") or {}).get("polyline")
    if enc:
        try:
            return decode_polyline(enc, 6), False
        except Exception:
            pass
    pts = [(p["lat"], p["lon"]) for p in geo.get("points") or []]
    return pts, True


def _airport_label(p: dict) -> str:
    code = (p.get("label") or "").strip().upper()
    return code if code.isalpha() and len(code) <= 4 else ""


def _add_maps(days: list[dict]) -> dict:
    """Number every located place in timeline order and draw the maps: a
    'Getting there' map for flights, a 'Trip map' for everything on the
    ground, and an A→B map on each road trip. Returns the overview context;
    each activity dict gains `map_no` and (road trips) `route_map`."""
    from app.trips import maps
    ground_markers, ground_lines = [], []
    flight_markers, flight_lines, seen_airports = [], [], set()
    legend: dict[str, str] = {}
    n = 0
    for day in days:
        for a in day["activities"]:
            pts = _points(a)
            if not pts:
                continue
            colour = maps.KIND_COLOURS.get(a["type"], maps.DEFAULT_COLOUR)
            if a["type"] == "flight":
                if "from" in pts and "to" in pts:
                    f, t = pts["from"][0], pts["to"][0]
                    flight_lines.append(maps.Line(
                        maps.great_circle((f["lat"], f["lon"]), (t["lat"], t["lon"])),
                        colour))
                    for p in (f, t):
                        key = (round(p["lat"], 3), round(p["lon"], 3))
                        if key not in seen_airports:
                            seen_airports.add(key)
                            flight_markers.append(maps.Marker(
                                p["lat"], p["lon"], _airport_label(p), colour))
                continue
            n += 1
            a["map_no"] = n
            legend.setdefault(a["type_label"], colour)
            anchor = (pts.get("place") or pts.get("from") or next(iter(pts.values())))[0]
            if a["type"] in ("drive", "taxi", "carRental", "train", "cruise") and len(
                    (a.get("geo") or {}).get("points") or []) >= 2:
                line, dashed = (_route_line(a) if a["type"] == "drive" else
                                ([(p["lat"], p["lon"]) for p in a["geo"]["points"]], True))
                ground_lines.append(maps.Line(line, colour, dashed=dashed))
                end = (pts.get("to") or [None])[0]
                if end:
                    ground_markers.append(maps.Marker(end["lat"], end["lon"], "", colour))
                if a["type"] == "drive":
                    a["route_map"] = _data_uri(_drive_map(a, colour))
            ground_markers.append(maps.Marker(anchor["lat"], anchor["lon"], str(n), colour))
    # Sized to share page 1 with the header and the first day of the timeline.
    trip_map = (maps.render_map(ground_markers, ground_lines, width=680, height=330)
                if ground_markers else None)
    flight_map = (maps.render_map(flight_markers, flight_lines, width=680, height=420)
                  if flight_lines else None)
    return {
        "trip_map": _data_uri(trip_map), "flight_map": _data_uri(flight_map),
        "map_attribution": next((m.attribution for m in (trip_map, flight_map)
                                 if m is not None and m.attribution), ""),
        "legend": [{"label": k, "colour": v} for k, v in legend.items()],
    }


def _drive_map(a: dict, colour: str):
    """A road trip's own map: its route from A to B (stops lettered in order)."""
    from app.trips import maps
    line, dashed = _route_line(a)
    pts = (a.get("geo") or {}).get("points") or []
    markers = [maps.Marker(p["lat"], p["lon"], chr(ord("A") + i) if i < 26 else "", colour,
                           caption=p.get("label") or "")
               for i, p in enumerate(pts)]
    return maps.render_map(markers, [maps.Line(line, colour, dashed=dashed)],
                           width=560, height=190)


def _found_rows(a: dict) -> list[dict]:
    """Addresses Run·dere already holds for an item's map pins (D-233): the
    street address Photon gave when it was located, or an airport's name and
    city from the bundled table. Only where nothing typed says the same."""
    typed = {f["key"] for f in a["fields"] if f.get("key") in _ADDRESS_KEYS}
    rows = []
    for p in (a.get("geo") or {}).get("points") or []:
        addr = (p.get("address") or "").strip()
        if not addr:
            continue
        role = p.get("role")
        if role == "place" and not typed:
            label = "Address (from the map)"
        elif role in ("from", "to") and a["type"] in _FOUND_ENDS:
            label = ("From" if role == "from" else "To") + " address"
            if p.get("source") != "airport":
                label += " (from the map)"
        else:
            continue
        rows.append({"label": label, "value": addr, "long": False})
    return rows


def _activity(a: dict, sec: Sections) -> dict:
    """One timeline item as the PDF prints it, with what `sec` leaves out
    gone: fields, the subtitle built from them, the time and the map link."""
    t = trip_routes.activity_types.get_type(a["type"])
    fields = [f for f in a["fields"] if _wanted(a["type"], f, sec)]
    subtitle = a["subtitle"]
    if len(fields) != len(a["fields"]):
        subtitle = t.subtitle({f["key"]: f["value"] for f in fields})
    out = {**a, "fields": fields, "subtitle": subtitle}
    if not sec.times:
        out["time_label"] = ""
    if not sec.addresses:
        out["map_url"] = None
    out["rows"] = ((_start_row(out) if sec.times else [])
                   + _field_rows(fields, {out["title"], subtitle})
                   + (_found_rows(out) if sec.addresses else []))
    return out


def _packing(session: Session, trip: Trip, user) -> list[dict] | None:
    """The downloader's own packing list, by bag (D-123): packed and to-pack
    lines, without "Suggested" or "Not needed". None when they have none."""
    from app.models import PackingItem
    uid = user.id if user is not None else None
    items = (session.query(PackingItem)
             .filter(PackingItem.trip_id == trip.id, PackingItem.user_id == uid,
                     PackingItem.status.in_(("draft", "packed"))).all())
    if not items:
        return None
    labels = trip_routes._bag_label_lookup(session, user)
    bags: dict[str, list] = {}
    for i in sorted(items, key=lambda i: (i.category or "", (i.label or "").lower())):
        bags.setdefault(labels.get(i.bag or "", "") or "No bag yet", []).append(
            {"label": i.label, "quantity": i.quantity or 1, "packed": i.status == "packed"})
    return [{"bag": k, "items": v} for k, v in bags.items()]


def _place(key: str | None) -> str:
    """A finding's place key as words: "FR:Nice" -> "Nice, FR"."""
    country, _, city = (key or "").partition(":")
    return f"{city}, {country}" if city else country


def _briefing(session: Session, trip: Trip, user) -> list[dict] | None:
    """The downloader's own Discover briefing (4.1, per person): one section
    per briefing topic, rejected findings left out."""
    from app.models import DiscoveryFinding
    uid = user.id if user is not None else None
    found = (session.query(DiscoveryFinding)
             .filter(DiscoveryFinding.trip_id == trip.id, DiscoveryFinding.user_id == uid,
                     DiscoveryFinding.topic.in_(_BRIEFING_TOPICS),
                     DiscoveryFinding.status != "rejected").all())
    out = []
    for topic in _BRIEFING_TOPICS:
        fs = sorted((f for f in found if f.topic == topic and (f.body_md or "").strip()),
                    key=lambda f: f.place_key or "")
        if fs:
            out.append({"label": trip_routes.TOPIC_LABELS.get(topic, topic),
                        "findings": [{"place": _place(f.place_key), "multi": len(fs) > 1,
                                      "body": trip_routes._render_md(f.body_md)}
                                     for f in fs]})
    return out or None


def timeline_context(session: Session, trip: Trip, sections: Sections | None = None,
                     user=None) -> dict:
    sec = sections or Sections()
    groups = trip_routes.timeline_day_groups(session, trip)
    days = []
    for g in groups:
        days.append({
            "heading": _day_heading(g["day"], g["label"]) if g["day"] else g["label"],
            "activities": [_activity(a, sec) for a in g["activities"]],
        })
    travellers = trip_routes._travelers_context(session, trip.id)["travelers"]
    today = date.today().isoformat()
    map_ctx = {"trip_map": None, "flight_map": None, "map_attribution": "", "legend": []}
    if sec.maps:
        try:
            map_ctx = _add_maps(days)
        except Exception:                # a map problem never costs the PDF
            import logging
            logging.getLogger("yonder.trips.pdf").warning("trip maps failed", exc_info=True)
    # Packing and Discover are personal: a viewer (D-199) gets neither.
    from app import scope
    personal = scope.can_edit_trip(session, user, trip)
    return {
        **map_ctx,
        "packing": _packing(session, trip, user) if sec.packing and personal else None,
        "briefing": _briefing(session, trip, user) if sec.discover and personal else None,
        "trip": {
            "name": trip.name,
            "dates": " – ".join(x for x in (trip_routes.fmt_date(trip.start_date),
                                            trip_routes.fmt_date(trip.end_date)) if x),
            "notes": (trip.notes or "").strip(),
        },
        "travellers": [t["name"] for t in travellers],
        "days": days,
        "generated": trip_routes.fmt_date(today),
    }


def _legend_colour(a: dict) -> str:
    from app.trips.maps import DEFAULT_COLOUR, KIND_COLOURS
    return KIND_COLOURS.get(a.get("type"), DEFAULT_COLOUR)


def render_timeline_html(session: Session, trip: Trip, sections: Sections | None = None,
                         user=None) -> str:
    tpl = trip_routes.templates.env.get_template("timeline_pdf.html")
    return tpl.render(legend_colour=_legend_colour,
                      **timeline_context(session, trip, sections, user))


def render_timeline_pdf(session: Session, trip: Trip, sections: Sections | None = None,
                        user=None) -> bytes:
    from weasyprint import HTML          # heavy import; only on export

    return HTML(string=render_timeline_html(session, trip, sections, user)).write_pdf()


def pdf_filename(trip: Trip) -> str:
    """'Lisbon, Spring 2026' -> 'Lisbon-Spring-2026-timeline.pdf'."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", trip.name or "trip").strip("-") or "trip"
    return f"{slug}-timeline.pdf"


def pdf_response(session: Session, trip: Trip, download: bool = False,
                 sections: Sections | None = None, user=None):
    """The PDF as an HTTP response. Inline by default so a browser previews it
    (and can save/share from there); `download=True` forces a save."""
    from fastapi.responses import Response

    disposition = "attachment" if download else "inline"
    return Response(
        render_timeline_pdf(session, trip, sections, user), media_type="application/pdf",
        headers={"Content-Disposition":
                 f'{disposition}; filename="{pdf_filename(trip)}"',
                 "Cache-Control": "no-store"})
