"""Trips — Yonder 2.0's in-house trip manager (D-18 lineage: server-rendered
HTMX + Jinja, no build step, nothing extra to deploy).

The design's client-side component is translated here to plain endpoints: the
list and timeline are GET renders; create/edit/delete post and answer with an
`HX-Redirect` back to the page they belong on (a management surface used a few
times a week does not need optimistic partial juggling — a clean reload is
simpler and never leaves a stale half-swapped view). Only the live search /
filter and the modals swap partials.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.models import DiscoveryFinding, DiscoveryRun, Trip, TripActivity
from app.trips import types as activity_types

log = logging.getLogger("yonder.trips")
router = APIRouter(prefix="/trips")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# Let Trips templates {% include %} the shared global header (_appheader.html)
# that lives with the review templates, so all surfaces keep one header.
from jinja2 import ChoiceLoader, FileSystemLoader  # noqa: E402

templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(Path(__file__).parent / "templates")),
    FileSystemLoader(str(Path(__file__).parent.parent / "review" / "templates")),
])

STATUS_LABELS = [("inprogress", "In progress"), ("upcoming", "Upcoming"),
                 ("past", "Past")]
STATUS_FILTERS = [("all", "All"), ("inprogress", "In progress"),
                  ("upcoming", "Upcoming"), ("past", "Past")]

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
           "Oct", "Nov", "Dec"]


# ----------------------------------------------------------------- formatting

def _parse_dt(s: str | None) -> datetime | None:
    """Parse a `datetime-local`/ISO value tolerantly (date-only ok)."""
    if not s:
        return None
    raw = str(s).strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:len(fmt) + 2] if fmt.endswith("S") else raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _fmt_ampm(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{h}:{dt.minute:02d} {ampm}"


def fmt_date(iso: str | None) -> str:
    """'2026-08-15' -> 'Aug 15, 2026'. Accepts a full datetime too."""
    dt = _parse_dt(iso)
    return f"{_MONTHS[dt.month - 1]} {dt.day}, {dt.year}" if dt else ""


def fmt_datetime(iso: str | None) -> str:
    dt = _parse_dt(iso)
    if not dt:
        return ""
    if "T" not in str(iso):                      # date-only, no clock time
        return fmt_date(iso)
    return f"{_MONTHS[dt.month - 1]} {dt.day}, {_fmt_ampm(dt)}"


def fmt_time_only(iso: str | None) -> str:
    dt = _parse_dt(iso)
    if not dt or "T" not in str(iso):
        return ""
    return _fmt_ampm(dt)


def fmt_date_short(iso: str | None) -> str:
    dt = _parse_dt(iso)
    return f"{_MONTHS[dt.month - 1]} {dt.day}" if dt else ""


# ----------------------------------------------------------------- status

def trip_status(t: Trip, today: str) -> str:
    if t.end_date and t.end_date < today:
        return "past"
    if t.start_date and t.start_date > today:
        return "upcoming"
    return "inprogress"


def _trip_display(t: Trip) -> dict:
    notes = (t.notes or "").strip()
    return {
        "id": t.id, "name": t.name,
        "start_display": fmt_date(t.start_date) or "—",
        "end_display": fmt_date(t.end_date) or "—",
        "notes_preview": notes or "—",
    }


def _list_context(session: Session, q: str, status: str) -> dict:
    today = date.today().isoformat()
    trips = session.query(Trip).all()
    ql = q.lower().strip()
    if ql:
        trips = [t for t in trips if ql in (t.name or "").lower()]
    annotated = [(t, trip_status(t, today)) for t in trips]
    if status != "all":
        annotated = [(t, s) for (t, s) in annotated if s == status]

    groups = []
    for key, label in STATUS_LABELS:
        members = [t for (t, s) in annotated if s == key]
        # Upcoming/in-progress ascend by start; past descends (most recent first).
        members.sort(key=lambda t: (t.start_date or ""), reverse=(key == "past"))
        if members:
            groups.append({"label": label,
                           "trips": [_trip_display(t) for t in members]})

    total = session.query(Trip).count()
    return {
        "groups": groups,
        "has_visible": bool(groups),
        "has_any": total > 0,
        "search_no_results": total > 0 and not groups,
        "q": q, "status": status,
        "status_filters": [{"key": k, "label": l, "checked": status == k}
                           for k, l in STATUS_FILTERS],
    }


def _activity_display(a: TripActivity) -> dict:
    t = activity_types.get_type(a.type)
    v = a.values or {}
    when = v.get("when") or ""
    end_when = v.get("endWhen") or ""
    multi_day = bool(when and end_when and when[:10] != end_when[:10])
    return {
        "id": a.id, "type": a.type, "type_label": t.label,
        "title": t.title(v), "subtitle": t.subtitle(v),
        "time_label": fmt_time_only(when),
        "multi_day": multi_day,
        "span_label": ("→ " + fmt_date_short(end_when)) if multi_day else "",
        "fields": activity_types.detail_fields(a.type, v),
    }


def _detail_context(session: Session, trip: Trip) -> dict:
    acts = list(trip_activities(session, trip.id))
    # Sort by the denormalized starts_at; '' (undated) sorts to the end.
    acts.sort(key=lambda a: (a.starts_at or "￿"))
    day_groups: list[dict] = []
    index: dict[str, dict] = {}
    for a in acts:
        key = (a.starts_at or "")[:10] or "_unscheduled"
        grp = index.get(key)
        if grp is None:
            grp = {"label": "Date not set" if key == "_unscheduled"
                   else fmt_date(key), "activities": []}
            index[key] = grp
            day_groups.append(grp)
        grp["activities"].append(_activity_display(a))
    notes = (trip.notes or "").strip()
    return {
        "trip": {
            "id": trip.id, "name": trip.name,
            "start_display": fmt_date(trip.start_date) or "—",
            "end_display": fmt_date(trip.end_date) or "—",
            "notes": notes, "has_notes": bool(notes),
            "adventurelog_collection_id": trip.adventurelog_collection_id,
            "adventurelog_url": _collection_url(trip.adventurelog_collection_id),
        },
        "day_groups": day_groups,
        "has_activities": bool(acts),
        "linked_captures": _linked_captures(session, trip.id),
        **_push_state(trip),
        **_discovery_context(session, trip.id),
    }


def _linked_captures(session: Session, trip_id: str) -> list[dict]:
    """Captured review items linked to this Yonder trip (2.1.2). Read-only here —
    the link is made and unmade in the review queue's trip picker."""
    from app.models import TravelItem
    from app.review import summary as rs
    rows = (session.query(TravelItem)
            .filter(TravelItem.trip_id == trip_id)
            .order_by(TravelItem.created_at).all())
    out = []
    for c in rows:
        payload = (c.extraction or {}).get("payload") or {}
        out.append({"id": c.id, "kind": rs.kind_label(payload),
                    "headline": rs.headline(payload), "subline": rs.subline(payload)})
    return out


def _adventurelog():
    """The AdventureLog client, or None when the deployment hasn't configured
    one — same seam as review's _target_clients."""
    if not settings.adventurelog_api_key:
        return None
    from adapters.targets.adventurelog import AdventureLogClient
    return AdventureLogClient(settings.adventurelog_url,
                              settings.adventurelog_api_key)


def _airtrail():
    """AirTrail client, or None when unconfigured — same seam as _adventurelog."""
    if not settings.airtrail_token:
        return None
    from adapters.targets.airtrail import AirTrailClient
    return AirTrailClient(settings.airtrail_url, settings.airtrail_token)


def _photon():
    from resolve.geocode import PhotonGeocoder
    return PhotonGeocoder(settings.photon_url)


def _airport_resolver():
    """A complete `(code) -> Airport | None` lookup for location derivation
    (discover/places.py). AirTrail's flown-airport reference is checked first
    (free, in-memory), then its complete read-only airport DB (D-50) so a
    never-before-flown destination airport still resolves. Both are optional;
    with neither configured, airport codes simply drop and geocoding carries the
    rest — same graceful degradation as the push path's _resolve_coords."""
    airports: dict = {}
    at = _airtrail()
    if at is not None:
        try:
            airports = at.airport_reference()
        except Exception:
            airports = {}
    db = None
    if settings.airtrail_db_url:
        from adapters.targets.airtrail_db import AirTrailDB
        db = AirTrailDB(settings.airtrail_db_url)

    def resolve(code: str):
        key = (code or "").strip().upper()
        if not key:
            return None
        ap = airports.get(key)
        if ap is None:
            ap = next((a for a in airports.values()
                       if a.icao and a.icao.upper() == key), None)
        if ap is None and db is not None:
            try:
                ap = db.airport(key)
            except Exception:
                ap = None
        return ap

    return resolve


def _collection_url(collection_id: str | None) -> str | None:
    """Browser-facing link to a collection, or None when either the public base
    isn't set or the trip isn't linked."""
    base = (settings.adventurelog_public_url or "").rstrip("/")
    if not (base and collection_id):
        return None
    return f"{base}/collections/{collection_id}"


def _push_state(trip: Trip) -> dict:
    """What the detail header shows for AdventureLog: a link if already pushed,
    a Push button if we can, or a disabled button + reason if we can't yet."""
    linked = bool(trip.adventurelog_collection_id)
    configured = bool(settings.adventurelog_api_key)
    has_dates = bool(trip.start_date and trip.end_date)
    reason = ""
    if not linked:
        if not configured:
            reason = "AdventureLog isn't configured on this server."
        elif not has_dates:
            reason = "Add a start and end date first — AdventureLog needs both."
    return {
        "al_linked": linked,
        "al_show_push": configured and not linked,
        "al_can_push": configured and not linked and has_dates,
        "al_push_reason": reason,
    }


def trip_activities(session: Session, trip_id: str):
    return session.query(TripActivity).filter(
        TripActivity.trip_id == trip_id).all()


def _redirect(url: str) -> HTMLResponse:
    """HTMX client-side navigation — the post's answer is 'go here now'."""
    resp = HTMLResponse("")
    resp.headers["HX-Redirect"] = url
    return resp


# ----------------------------------------------------------------- list / detail

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", status: str = "all",
          session: Session = Depends(get_session)):
    ctx = {"request": request, **_list_context(session, q, status)}
    return templates.TemplateResponse(request, "list.html", ctx)


@router.get("/list", response_class=HTMLResponse)
def list_partial(request: Request, q: str = "", status: str = "all",
                 session: Session = Depends(get_session)):
    """Groups-only fragment for the live search + status filter."""
    ctx = {"request": request, **_list_context(session, q, status)}
    return templates.TemplateResponse(request, "_groups.html", ctx)


@router.get("/close", response_class=HTMLResponse)
def close_modal():
    """Clear the modal slot (HTMX has no built-in 'remove', so swap empty)."""
    return HTMLResponse("")


@router.get("/new", response_class=HTMLResponse)
def new_trip(request: Request, next: str = "/trips"):
    return templates.TemplateResponse(request, "_trip_modal.html", {
        "request": request, "mode": "create", "title": "New trip",
        "action": "/trips", "next": next,
        "form": {"name": "", "start": "", "end": "", "notes": "",
                 "collection_id": ""}, "error": None})


@router.get("/{trip_id}", response_class=HTMLResponse)
def detail(trip_id: str, request: Request,
           session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("<p class='bp-empty'>That trip no longer exists.</p>",
                            status_code=404)
    ctx = {"request": request, **_detail_context(session, trip)}
    return templates.TemplateResponse(request, "detail.html", ctx)


@router.get("/{trip_id}/derived-locations", response_class=HTMLResponse)
def derived_locations(trip_id: str, request: Request,
                      session: Session = Depends(get_session)):
    """The "What Yonder found" viewer (D-68): show the destination set Discovery
    DERIVED from this trip's own activities (D-62) — with per-place provenance
    and the places that couldn't be verified — so the operator sees the
    pipeline's starting point rather than guessing at it. On-demand modal, read
    from the same discover.places output a run would use, so it doubles as a
    pre-flight check."""
    from app.resolving import load_home_profiles
    from discover.home_filter import home_country_set, is_home_place
    from discover.places import derive_locations
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    acts = trip_activities(session, trip_id)
    result = derive_locations(acts, resolve_airport=_airport_resolver(),
                              geocoder=_photon())
    profiles = load_home_profiles(session)
    hc = home_country_set(result.places, profiles, trip.start_date)
    places = [{
        "label": p.label, "city": p.city, "country": p.country,
        "lat": p.lat, "lon": p.lon,
        "sources": [f"{pr['activity_type']} · {pr['field']}"
                    for pr in p.provenance],
        "is_home": is_home_place(p, profiles, trip.start_date),
        "home_country": bool(p.country and p.country.upper() in hc),
    } for p in result.places]
    return templates.TemplateResponse(request, "_derived_locations.html", {
        "request": request, "trip_id": trip_id,
        "trip_name": trip.name,
        "places": places,
        "countries": result.countries,
        "dropped": result.dropped,
        "has_home": any(p["is_home"] or p["home_country"] for p in places),
    })


# --------------------------------------------------------------- discovery (3.0)

TOPIC_ORDER = ["weather", "currency", "plugs", "visa", "safety", "events",
               "food", "language", "packing"]
TOPIC_LABELS = {
    "weather": "Weather", "currency": "Currency", "plugs": "Plugs & voltage",
    "visa": "Visa & vaccination", "safety": "Safety & transport",
    "events": "Local events", "food": "Food & drink", "language": "Language",
    "packing": "Packing",
}
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _render_md(text: str | None) -> Markup:
    """Minimal, safe render of a finding's body: escape first, then a tiny
    subset — **bold**, "- " bullets, line breaks. No markdown dependency; the
    raw markdown is what later crosses to an AdventureLog Note."""
    if not text:
        return Markup("")
    html = str(escape(text))
    html = _MD_BOLD.sub(r"<strong>\1</strong>", html)
    out = []
    for line in html.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("- "):
            out.append("• " + stripped[2:])
        else:
            out.append(line)
    return Markup("<br>".join(out))


def _finding_view(f: DiscoveryFinding) -> dict:
    data = f.data or {}
    return {
        "id": f.id, "topic": f.topic, "place_key": f.place_key,
        "scope": f.scope, "status": f.status, "confidence": f.confidence,
        "body": _render_md(f.body_md), "sources": f.sources or [],
        "is_currency": f.topic == "currency",
        "has_rates": bool(data.get("rates")),
    }


def _run_view(run: DiscoveryRun) -> dict:
    return {"status": run.status, "phase": run.phase, "done": run.done,
            "total": run.total,
            "when": run.finished_at.strftime("%Y-%m-%d %H:%M")
            if run.finished_at else ""}


def _discovery_context(session: Session, trip_id: str) -> dict:
    findings = (session.query(DiscoveryFinding)
                .filter(DiscoveryFinding.trip_id == trip_id).all())
    grouped: dict[str, list] = {}
    for f in findings:
        grouped.setdefault(f.topic, []).append(f)
    topic_groups = []
    for topic in TOPIC_ORDER:
        fs = grouped.get(topic)
        if fs:
            fs.sort(key=lambda x: x.place_key or "")
            topic_groups.append({"topic": topic,
                                 "label": TOPIC_LABELS.get(topic, topic),
                                 "findings": [_finding_view(f) for f in fs]})
    last = (session.query(DiscoveryRun)
            .filter(DiscoveryRun.trip_id == trip_id)
            .order_by(DiscoveryRun.started_at.desc()).first())
    return {"discovery_topics": topic_groups,
            "discovery_has_findings": bool(findings),
            "discovery_last": _run_view(last) if last else None}


def _resolvers():
    from discover.resolvers import build_deterministic
    return build_deterministic(
        frankfurter_url=settings.frankfurter_url,
        open_meteo_url=settings.open_meteo_url,
        open_meteo_archive_url=settings.open_meteo_archive_url)


@router.post("/{trip_id}/discover", response_class=HTMLResponse)
def run_discovery_route(trip_id: str, session: Session = Depends(get_session)):
    """Run a Discovery pass over the trip (deterministic tier) and reload the
    detail so the new findings show. Synchronous for now — the deterministic
    resolvers are fast; the durable background job lands with the LLM tier."""
    from app.resolving import load_home_profiles
    from discover.runner import run_discovery
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    run_discovery(session, trip, resolve_airport=_airport_resolver(),
                  geocoder=_photon(), resolvers=_resolvers(),
                  home_profiles=load_home_profiles(session),
                  base_currencies=settings.base_currency_list)
    return _redirect(f"/trips/{trip_id}")


def _finding_or_404(session: Session, trip_id: str, fid: str) -> DiscoveryFinding | None:
    f = session.get(DiscoveryFinding, fid)
    return f if (f is not None and f.trip_id == trip_id) else None


@router.post("/{trip_id}/finding/{fid}/{action}", response_class=HTMLResponse)
def finding_action(trip_id: str, fid: str, action: str,
                   session: Session = Depends(get_session)):
    f = _finding_or_404(session, trip_id, fid)
    if f is None:
        return HTMLResponse("", status_code=404)
    if action == "approve":
        f.status = "approved"
    elif action == "reject":
        f.status = "rejected"
    elif action == "reset":
        f.status = "draft"
    else:
        return HTMLResponse("", status_code=400)
    session.commit()
    return _redirect(f"/trips/{trip_id}")


@router.get("/{trip_id}/finding/{fid}/edit", response_class=HTMLResponse)
def edit_finding(trip_id: str, fid: str, request: Request,
                 session: Session = Depends(get_session)):
    f = _finding_or_404(session, trip_id, fid)
    if f is None:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_finding_edit.html", {
        "request": request, "trip_id": trip_id, "fid": fid,
        "topic_label": TOPIC_LABELS.get(f.topic, f.topic),
        "place_key": f.place_key, "body": f.body_md or ""})


@router.post("/{trip_id}/finding/{fid}/edit", response_class=HTMLResponse)
def save_finding(trip_id: str, fid: str, body: str = Form(""),
                 session: Session = Depends(get_session)):
    f = _finding_or_404(session, trip_id, fid)
    if f is None:
        return HTMLResponse("", status_code=404)
    f.body_md = body
    f.status = "edited"
    session.commit()
    return _redirect(f"/trips/{trip_id}")


@router.get("/{trip_id}/finding/{fid}/calculator", response_class=HTMLResponse)
def currency_calculator(trip_id: str, fid: str, request: Request,
                        session: Session = Depends(get_session)):
    """The per-trip currency calculator (D-65). Computes client-side from the
    STORED rate snapshot — no live call — pivoting every currency through CAD so
    any pair converts, including local↔local on a multi-country trip."""
    f = _finding_or_404(session, trip_id, fid)
    if f is None:
        return HTMLResponse("", status_code=404)
    cur_findings = (session.query(DiscoveryFinding)
                    .filter(DiscoveryFinding.trip_id == trip_id,
                            DiscoveryFinding.topic == "currency").all())
    # rates[code][base] = units of `code` per 1 `base` (from the snapshots).
    rates: dict[str, dict[str, float]] = {}
    as_of = None
    for cf in cur_findings:
        d = cf.data or {}
        as_of = as_of or d.get("date")
        for code, per in (d.get("rates") or {}).items():
            rates[code] = {b.replace("per_", ""): v for b, v in per.items()}
    # Pivot table: units of each currency per 1 CAD.
    per_cad: dict[str, float] = {"CAD": 1.0}
    usd_per_cad = None
    for code, br in rates.items():
        if br.get("CAD"):
            per_cad[code] = br["CAD"]
        if usd_per_cad is None and br.get("CAD") and br.get("USD"):
            usd_per_cad = br["CAD"] / br["USD"]
    if rates.get("USD", {}).get("CAD"):
        usd_per_cad = rates["USD"]["CAD"]
    if usd_per_cad:
        per_cad["USD"] = usd_per_cad
    # Dropdown order: bases first, then destination currencies.
    bases = [b for b in settings.base_currency_list if b in per_cad]
    dests = sorted(c for c in per_cad if c not in bases)
    currencies = bases + dests
    import json as _json
    return templates.TemplateResponse(request, "_calculator.html", {
        "request": request, "trip_id": trip_id,
        "currencies": currencies, "per_cad_json": Markup(_json.dumps(per_cad)),
        "as_of": as_of or "—",
        "default_from": bases[0] if bases else (currencies[0] if currencies else ""),
        "default_to": dests[0] if dests else (currencies[-1] if currencies else ""),
    })


@router.get("/{trip_id}/edit", response_class=HTMLResponse)
def edit_trip(trip_id: str, request: Request, next: str = "",
              session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_trip_modal.html", {
        "request": request, "mode": "edit", "title": "Edit trip",
        "action": f"/trips/{trip.id}", "next": next or f"/trips/{trip.id}",
        "form": {"name": trip.name, "start": trip.start_date or "",
                 "end": trip.end_date or "", "notes": trip.notes or "",
                 "collection_id": trip.adventurelog_collection_id or ""},
        "error": None})


# ----------------------------------------------------------------- trip writes

def _save_trip_fields(trip: Trip, name: str, start: str, end: str, notes: str,
                      collection_id: str = ""):
    trip.name = name.strip()
    trip.start_date = start.strip() or None
    trip.end_date = end.strip() or None
    trip.notes = notes.strip() or None
    trip.adventurelog_collection_id = collection_id.strip() or None


@router.post("", response_class=HTMLResponse)
def create_trip(request: Request, name: str = Form(""), start: str = Form(""),
                end: str = Form(""), notes: str = Form(""),
                adventurelog_collection_id: str = Form(""),
                next: str = Form("/trips"),
                session: Session = Depends(get_session)):
    if not name.strip():
        return templates.TemplateResponse(request, "_trip_modal.html", {
            "request": request, "mode": "create", "title": "New trip",
            "action": "/trips", "next": next,
            "form": {"name": name, "start": start, "end": end, "notes": notes,
                     "collection_id": adventurelog_collection_id},
            "error": "A trip needs a name."})
    trip = Trip(source="manual")
    _save_trip_fields(trip, name, start, end, notes, adventurelog_collection_id)
    session.add(trip)
    session.commit()
    return _redirect("/trips")


@router.post("/{trip_id}", response_class=HTMLResponse)
def update_trip(trip_id: str, request: Request, name: str = Form(""),
                start: str = Form(""), end: str = Form(""), notes: str = Form(""),
                adventurelog_collection_id: str = Form(""),
                next: str = Form(""), session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    dest = next or f"/trips/{trip.id}"
    if not name.strip():
        return templates.TemplateResponse(request, "_trip_modal.html", {
            "request": request, "mode": "edit", "title": "Edit trip",
            "action": f"/trips/{trip.id}", "next": dest,
            "form": {"name": name, "start": start, "end": end, "notes": notes,
                     "collection_id": adventurelog_collection_id},
            "error": "A trip needs a name."})
    _save_trip_fields(trip, name, start, end, notes, adventurelog_collection_id)
    session.commit()
    return _redirect(dest)


@router.post("/{trip_id}/delete", response_class=HTMLResponse)
def delete_trip(trip_id: str, session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is not None:
        # Explicit child delete: SQLite in tests does not enforce ON DELETE
        # CASCADE unless PRAGMA foreign_keys is on, so never rely on the DB to
        # sweep the activities.
        session.query(TripActivity).filter(
            TripActivity.trip_id == trip_id).delete(synchronize_session=False)
        # Clear the soft link from any captured items filed into this trip
        # (2.1.2) — there's no DB FK to null it for us (migration 0008).
        from app.models import TravelItem
        session.query(TravelItem).filter(
            TravelItem.trip_id == trip_id).update({"trip_id": None},
                                                  synchronize_session=False)
        session.delete(trip)
        session.commit()
    return _redirect("/trips")


def _push_msg(text: str) -> HTMLResponse:
    """Inline failure notice swapped next to the Push button (a success answers
    with an HX-Redirect that reloads the detail showing the new link)."""
    return HTMLResponse(f'<span class="bp-push-err">{text}</span>')


# Every Yonder activity type becomes a Transportation, except lodging (its own
# AdventureLog object) and tour/event/dining/note, which fall through to a
# Note — AdventureLog has no generic "activity" object to hold those.
_AL_TRANSPORT_TYPE = {
    "flight": "plane", "train": "train", "drive": "car",
    "carRental": "car", "cruise": "boat",
}


def _al_iso(value: str | None) -> str | None:
    """A stored `when`/`endWhen` value (bare "YYYY-MM-DD" or datetime-local
    "YYYY-MM-DDTHH:MM") as AdventureLog's explicit-UTC ISO string.

    TripActivity carries no per-activity timezone (RULE D-06's naive-time
    guard doesn't apply here — there's no source document to hold a zone), so
    — consistent with the rest of the adapter layer (D-06's Photon-adjacent
    convention: a naive value is always sent as explicit UTC, never guessed
    at) — this treats the stored value as UTC rather than inventing one."""
    if not value:
        return None
    if len(value) == 10:                                 # "YYYY-MM-DD"
        return f"{value}T00:00:00Z"
    if value.endswith("Z"):
        return value
    return f"{value}:00Z" if len(value) == 16 else f"{value}Z"


def _resolve_coords(place: str | None, airports: dict, photon,
                    cache: dict) -> tuple[float | None, float | None]:
    """Best-effort (lat, lon) for a transportation endpoint.

    `place` is usually an IATA/ICAO code (flight/train) but may be free text
    (a cruise port, a car-rental address) — AirTrail's flown-airport reference
    is checked first (free, exact, already loaded), Photon covers everything
    else, including an airport AirTrail has never seen flown (same fallback
    chain as the home-profile airport lookup in app/review/home.py). `cache`
    is the caller's dict, reused across every activity in one push so the same
    airport isn't geocoded twice."""
    if not place:
        return None, None
    key = place.strip().upper()
    if key in cache:
        return cache[key]
    coords = (None, None)
    airport = airports.get(key)
    if airport is None:
        airport = next((a for a in airports.values()
                        if a.icao and a.icao.upper() == key), None)
    if airport is not None:
        coords = (airport.lat, airport.lon)
    else:
        try:
            looks_like_code = key.isalpha() and len(key) in (3, 4)
            result = photon.geocode_freeform(f"{place} airport" if looks_like_code else place)
        except Exception:
            result = None
        if result is not None:
            coords = (result.lat, result.lon)
    cache[key] = coords
    return coords


def _al_body_for_activity(activity: TripActivity, collection_id: str,
                          airports: dict, photon, coord_cache: dict) -> tuple[str, dict]:
    """Map one TripActivity onto the AdventureLog object it becomes.

    Returns (kind, body) where kind is "transportation" | "lodging" | "note" —
    the three create_* methods on AdventureLogClient. `is_public: True`
    matches the collection push_adventurelog always creates as public; every
    child object must agree or AdventureLog's own validation rejects it."""
    v = activity.values or {}
    t = activity_types.get_type(activity.type)
    title = t.title(v) or "Untitled"
    subtitle = t.subtitle(v)
    starts = _al_iso(v.get("when"))
    ends = _al_iso(v.get("endWhen")) or starts
    link = v.get("sourceLink") or None
    detail_lines = [f"Confirmation: {v['confirmation']}"] if v.get("confirmation") else []
    if v.get("notes"):
        detail_lines.append(v["notes"])
    description = "\n\n".join(detail_lines) or None

    if activity.type in _AL_TRANSPORT_TYPE:
        name = f"{title} – {subtitle}" if subtitle else title
        origin = v.get("from") or v.get("pickupLocation") or v.get("departurePort")
        dest = v.get("to") or v.get("dropoffLocation") or v.get("arrivalPort")
        origin_lat, origin_lon = _resolve_coords(origin, airports, photon, coord_cache)
        dest_lat, dest_lon = _resolve_coords(dest, airports, photon, coord_cache)
        body = {
            "type": _AL_TRANSPORT_TYPE[activity.type], "name": name,
            "date": starts, "end_date": ends,
            "from_location": origin, "to_location": dest,
            "start_code": v.get("from"), "end_code": v.get("to"),
            "origin_latitude": origin_lat, "origin_longitude": origin_lon,
            "destination_latitude": dest_lat, "destination_longitude": dest_lon,
            "link": link, "description": description,
            "is_public": True, "collection": collection_id,
        }
        if activity.type == "flight":
            body["flight_number"] = v.get("flightNumber")
        return "transportation", {k: v for k, v in body.items() if v is not None}

    if activity.type == "lodging":
        body = {
            "name": v.get("name") or title, "check_in": starts, "check_out": ends,
            "location": v.get("address"), "reservation_number": v.get("confirmation"),
            "link": link, "description": v.get("notes") or None,
            "is_public": True, "collection": collection_id,
        }
        return "lodging", {k: v for k, v in body.items() if v is not None}

    content = "\n\n".join(p for p in (subtitle, v.get("text"), description) if p) or None
    body = {
        "name": title, "content": content, "links": [link] if link else None,
        "date": (v.get("when") or "")[:10] or None,
        "is_public": True, "collection": collection_id,
    }
    return "note", {k: v for k, v in body.items() if v is not None}


def _push_activities(client, collection_id: str, trip_id: str,
                     activities: list[TripActivity]) -> None:
    """Best-effort: the collection is already created and linked by the time
    this runs, so one bad activity logs and moves on rather than losing the
    ones that would otherwise succeed."""
    airtrail = _airtrail()
    airports = {}
    if airtrail is not None:
        try:
            airports = airtrail.airport_reference()
        except Exception:
            log.warning("push-adventurelog: AirTrail airport reference unavailable "
                       "for trip %s, falling back to Photon for every endpoint", trip_id)
    photon = _photon()
    coord_cache: dict = {}
    create = {"transportation": client.create_transportation,
             "lodging": client.create_lodging, "note": client.create_note}
    failed = 0
    for activity in activities:
        kind, body = _al_body_for_activity(activity, collection_id,
                                           airports, photon, coord_cache)
        try:
            status, parsed, raw = create[kind](body)
            if status >= 400:
                failed += 1
                log.warning("push-adventurelog: %s for trip %s rejected (HTTP %s): %s",
                           kind, trip_id, status, raw[:300])
        except Exception:
            failed += 1
            log.exception("push-adventurelog: %s failed for trip %s", kind, trip_id)
    if failed:
        log.warning("push-adventurelog: %d/%d activities failed for trip %s",
                    failed, len(activities), trip_id)


@router.post("/{trip_id}/push-adventurelog", response_class=HTMLResponse)
def push_adventurelog(trip_id: str, session: Session = Depends(get_session)):
    """Create a PUBLIC AdventureLog collection from this trip, seed it with the
    trip's activities, and link it.

    Public by default so the collection can be direct-linked from outside
    AdventureLog (owner requirement). Idempotent on the collection itself: a
    trip already linked is left alone — we never create a second collection
    for the same trip. The activity push is one-shot at creation time, not an
    ongoing sync — AdventureLog has no server-side dedup on transportations/
    lodging/notes, so re-running this against an already-linked trip would
    duplicate everything rather than reconcile it.
    """
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    if trip.adventurelog_collection_id:
        return _redirect(f"/trips/{trip_id}")           # already linked
    client = _adventurelog()
    if client is None:
        return _push_msg("AdventureLog isn't configured on this server.")
    if not (trip.start_date and trip.end_date):
        return _push_msg("Add a start and end date first — AdventureLog needs both.")
    try:
        status, parsed, raw = client.create_collection(
            trip.name, trip.start_date, trip.end_date, is_public=True)
    except Exception as ex:                              # network/adapter error
        return _push_msg(f"AdventureLog request failed: {ex}")
    if status >= 400 or not isinstance(parsed, dict) or not parsed.get("id"):
        return _push_msg(f"AdventureLog rejected the collection (HTTP {status}).")
    collection_id = str(parsed["id"])
    trip.adventurelog_collection_id = collection_id
    trip.source = trip.source or "manual"
    session.commit()

    activities = (session.query(TripActivity)
                  .filter(TripActivity.trip_id == trip_id)
                  .order_by(TripActivity.starts_at).all())
    if activities:
        _push_activities(client, collection_id, trip_id, activities)
    return _redirect(f"/trips/{trip_id}")


# ----------------------------------------------------------------- activities

def _activity_modal_ctx(request: Request, trip_id: str, *, mode: str,
                        action: str, atype: str, values: dict) -> dict:
    t = activity_types.get_type(atype)
    return {
        "request": request, "mode": mode,
        "title": "New activity" if mode == "create" else "Edit activity",
        "action": action, "trip_id": trip_id,
        "type": t.key, "type_options": activity_types.type_options(),
        "fields": _modal_fields(t, values),
    }


def _modal_fields(t: activity_types.ActivityType, values: dict) -> list[dict]:
    return [{"key": f["key"], "label": f["label"], "type": f["type"],
             "is_textarea": f["type"] == "textarea",
             "value": values.get(f["key"], "")} for f in t.fields]


@router.get("/{trip_id}/activity/new", response_class=HTMLResponse)
def new_activity(trip_id: str, request: Request,
                 type: str = activity_types.DEFAULT_TYPE):
    return templates.TemplateResponse(request, "_activity_modal.html",
        _activity_modal_ctx(request, trip_id, mode="create",
                            action=f"/trips/{trip_id}/activity",
                            atype=type, values={}))


@router.get("/{trip_id}/activity/fields", response_class=HTMLResponse)
def activity_fields(trip_id: str, request: Request,
                    type: str = activity_types.DEFAULT_TYPE):
    """Re-render just the dynamic field block when the type dropdown changes.
    Switching type starts from empty inputs (as in the design) — the contract
    for what a flight vs a hotel carries is different, so old values would be
    meaningless under the new type."""
    t = activity_types.get_type(type)
    return templates.TemplateResponse(request, "_activity_fields.html", {
        "request": request, "fields": _modal_fields(t, {})})


@router.get("/{trip_id}/activity/{activity_id}/edit", response_class=HTMLResponse)
def edit_activity(trip_id: str, activity_id: str, request: Request,
                  session: Session = Depends(get_session)):
    a = session.get(TripActivity, activity_id)
    if a is None or a.trip_id != trip_id:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_activity_modal.html",
        _activity_modal_ctx(request, trip_id, mode="edit",
                            action=f"/trips/{trip_id}/activity/{a.id}",
                            atype=a.type, values=a.values or {}))


def _collect_values(t: activity_types.ActivityType, form) -> dict:
    """Pull only this type's declared fields out of the posted form."""
    out = {}
    for f in t.fields:
        raw = (form.get(f["key"]) or "").strip()
        if raw:
            out[f["key"]] = raw
    return out


@router.post("/{trip_id}/activity", response_class=HTMLResponse)
async def create_activity(trip_id: str, request: Request,
                          session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    form = await request.form()
    t = activity_types.get_type(form.get("type") or activity_types.DEFAULT_TYPE)
    values = _collect_values(t, form)
    a = TripActivity(trip_id=trip_id, type=t.key, values=values,
                     starts_at=values.get("when") or "")
    session.add(a)
    session.commit()
    return _redirect(f"/trips/{trip_id}")


@router.post("/{trip_id}/activity/{activity_id}", response_class=HTMLResponse)
async def update_activity(trip_id: str, activity_id: str, request: Request,
                          session: Session = Depends(get_session)):
    a = session.get(TripActivity, activity_id)
    if a is None or a.trip_id != trip_id:
        return HTMLResponse("", status_code=404)
    form = await request.form()
    t = activity_types.get_type(form.get("type") or a.type)
    values = _collect_values(t, form)
    a.type = t.key
    a.values = values
    a.starts_at = values.get("when") or ""
    session.commit()
    return _redirect(f"/trips/{trip_id}")


@router.post("/{trip_id}/activity/{activity_id}/delete", response_class=HTMLResponse)
def delete_activity(trip_id: str, activity_id: str,
                    session: Session = Depends(get_session)):
    a = session.get(TripActivity, activity_id)
    if a is not None and a.trip_id == trip_id:
        session.delete(a)
        session.commit()
    return _redirect(f"/trips/{trip_id}")
