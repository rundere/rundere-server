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
    return {
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


def render_timeline_html(session: Session, trip: Trip) -> str:
    tpl = trip_routes.templates.env.get_template("timeline_pdf.html")
    return tpl.render(**timeline_context(session, trip))


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
