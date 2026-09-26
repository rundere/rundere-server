"""Timeline PDF export — a shareable, print-friendly itinerary for people who
aren't on Yonder yet. Rendered server-side (HTML template → WeasyPrint), so the
web trip page and the iOS app hand out the identical document.

The content is the trip page's own timeline (`timeline_day_groups`), so the PDF
never drifts from what the owner sees. Internal-only data is left out: source
document links (they point at this server) and the edit affordances.
"""
from __future__ import annotations

import re
from datetime import date

from sqlalchemy.orm import Session

from app.models import Trip
from app.trips import routes as trip_routes

# Fields that only make sense inside Yonder.
_OMIT_KEYS = {"sourceLink", "mapLink"}


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
            if a["type"] in ("drive", "carRental", "train", "cruise") and len(
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
    trip_map = (maps.render_map(ground_markers, ground_lines, width=680, height=400)
                if ground_markers else None)
    flight_map = (maps.render_map(flight_markers, flight_lines, width=680, height=260)
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


def timeline_context(session: Session, trip: Trip) -> dict:
    groups = trip_routes.timeline_day_groups(session, trip)
    days = []
    for g in groups:
        days.append({
            "heading": _day_heading(g["day"], g["label"]) if g["day"] else g["label"],
            "activities": [{**a, "rows": _start_row(a) + _field_rows(
                               a["fields"], {a["title"], a["subtitle"]})}
                           for a in g["activities"]],
        })
    travellers = trip_routes._travelers_context(session, trip.id)["travelers"]
    today = date.today().isoformat()
    try:
        map_ctx = _add_maps(days)
    except Exception:                    # a map problem never costs the PDF
        import logging
        logging.getLogger("yonder.trips.pdf").warning("trip maps failed", exc_info=True)
        map_ctx = {"trip_map": None, "flight_map": None, "map_attribution": "", "legend": []}
    return {
        **map_ctx,
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


def render_timeline_html(session: Session, trip: Trip) -> str:
    tpl = trip_routes.templates.env.get_template("timeline_pdf.html")
    return tpl.render(legend_colour=_legend_colour, **timeline_context(session, trip))


def render_timeline_pdf(session: Session, trip: Trip) -> bytes:
    from weasyprint import HTML          # heavy import; only on export

    return HTML(string=render_timeline_html(session, trip)).write_pdf()


def pdf_filename(trip: Trip) -> str:
    """'Lisbon, Spring 2026' -> 'Lisbon-Spring-2026-timeline.pdf'."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", trip.name or "trip").strip("-") or "trip"
    return f"{slug}-timeline.pdf"


def pdf_response(session: Session, trip: Trip, download: bool = False):
    """The PDF as an HTTP response. Inline by default so a browser previews it
    (and can save/share from there); `download=True` forces a save."""
    from fastapi.responses import Response

    disposition = "attachment" if download else "inline"
    return Response(
        render_timeline_pdf(session, trip), media_type="application/pdf",
        headers={"Content-Disposition":
                 f'{disposition}; filename="{pdf_filename(trip)}"',
                 "Cache-Control": "no-store"})
