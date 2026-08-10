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

from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.models import Trip, TripActivity
from app.trips import types as activity_types

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
        **_push_state(trip),
    }


def _adventurelog():
    """The AdventureLog client, or None when the deployment hasn't configured
    one — same seam as review's _target_clients."""
    if not settings.adventurelog_api_key:
        return None
    from adapters.targets.adventurelog import AdventureLogClient
    return AdventureLogClient(settings.adventurelog_url,
                              settings.adventurelog_api_key)


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
        session.delete(trip)
        session.commit()
    return _redirect("/trips")


def _push_msg(text: str) -> HTMLResponse:
    """Inline failure notice swapped next to the Push button (a success answers
    with an HX-Redirect that reloads the detail showing the new link)."""
    return HTMLResponse(f'<span class="bp-push-err">{text}</span>')


@router.post("/{trip_id}/push-adventurelog", response_class=HTMLResponse)
def push_adventurelog(trip_id: str, session: Session = Depends(get_session)):
    """Create a PUBLIC AdventureLog collection from this trip and link it.

    Public by default so the collection can be direct-linked from outside
    AdventureLog (owner requirement). Idempotent: a trip already linked is left
    alone — we never create a second collection for the same trip.
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
    trip.adventurelog_collection_id = str(parsed["id"])
    trip.source = trip.source or "manual"
    session.commit()
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
