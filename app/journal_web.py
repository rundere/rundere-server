"""Yonder 4.x — Travel Journal web surface (SPEC-6).

The desk path: a `/journal` page with a compose box and the traveller's own
entries, grouped by the day they are ABOUT (D-132). Online-only by design — the
offline field path is the mobile app's outbox (D-135), not the browser.

Thin over app/journal.py (the shared service) and app/scope.py (own-only, D-134),
so the web UI, the JSON API and MCP never diverge. Every mutation re-renders one
`#journal-panel` partial (compose form + list) via HTMX — the simplest thing that
keeps the list and a fresh empty form in sync after each action.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import journal as journal_svc
from app import scope
from app.db import get_session
from app.models import Trip

router = APIRouter(prefix="/journal")
# Reuse the shared base.html / theme.css / _appheader that live in review/templates
# (same as the root dashboard does), so the journal page inherits the app chrome.
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))


def _visible_trips(session: Session, user) -> list[dict]:
    rows = (session.query(Trip).filter(scope.trip_filter(session, user))
            .order_by(Trip.start_date.desc().nullslast(), Trip.name).all())
    return [{"id": t.id, "name": t.name,
             "dates": (f"{t.start_date or ''}"
                       f"{' → ' + t.end_date if t.end_date else ''}").strip()}
            for t in rows]


def _panel_ctx(request: Request, session: Session) -> dict:
    """Everything `_panel.html` needs: the caller's entries grouped by
    entry_date (newest first) and the trips they can attach to."""
    user = scope.user_of(request)
    entries = journal_svc.list_entries(session, user)
    trips = _visible_trips(session, user)
    names = {t["id"]: t["name"] for t in trips}
    groups: list[dict] = []
    for e in entries:                      # already sorted entry_date desc, captured_at desc
        if not groups or groups[-1]["date"] != e.entry_date:
            groups.append({"date": e.entry_date, "entries": []})
        groups[-1]["entries"].append({
            "id": e.id,
            "body": e.body or "",
            "time": e.captured_at.strftime("%H:%M") if e.captured_at else "",
            "trip_id": e.trip_id,
            "trip_name": names.get(e.trip_id),
            "place": e.place_label or (
                f"{e.place_lat:.3f}, {e.place_lon:.3f}"
                if e.place_lat is not None and e.place_lon is not None else ""),
            "duplicated_from": e.duplicated_from,
            "tags": (e.values or {}).get("tags") or [],
        })
    return {"request": request, "groups": groups, "trips": trips,
            "today": date.today().isoformat(), "entry_count": len(entries)}


def _panel(request: Request, session: Session) -> HTMLResponse:
    return templates.TemplateResponse(request, "_journal_panel.html", _panel_ctx(request, session))


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def journal_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "journal.html", _panel_ctx(request, session))


@router.post("", response_class=HTMLResponse)
@router.post("/", response_class=HTMLResponse)
def journal_create(request: Request, body: str = Form(""), entry_date: str = Form(""),
                   trip_id: str = Form(""), session: Session = Depends(get_session)):
    """Create from the compose box. A blank note is a no-op (the box just
    re-renders) — never persist an empty entry. captured_at is the server's
    now: the browser has no reliable device clock/geo, so precise time+geo is
    the mobile path (D-142)."""
    user = scope.user_of(request)
    if body.strip():
        try:
            journal_svc.create_entry(session, user, entry_date=(entry_date or None),
                                     body=body.strip(), trip_id=(trip_id or None),
                                     source="web")
        except ValueError:
            # A malformed date from a hand-edited field: fall back to today
            # rather than 500 — the note matters more than the exact day.
            journal_svc.create_entry(session, user, body=body.strip(),
                                     trip_id=(trip_id or None), source="web")
    return _panel(request, session)


@router.post("/{entry_id}/attach", response_class=HTMLResponse)
def journal_attach(entry_id: str, request: Request, trip_id: str = Form(""),
                   session: Session = Depends(get_session)):
    """Attach to the chosen trip, or detach when the blank option is picked
    (D-133). 404s from scope (unknown/invisible entry or trip) are swallowed to
    a plain re-render — the panel simply shows the unchanged truth."""
    user = scope.user_of(request)
    try:
        journal_svc.attach_trip(session, user, entry_id, trip_id or None)
    except Exception:  # noqa: BLE001 — HTTPException(404) etc.; re-render current state
        session.rollback()
    return _panel(request, session)


@router.post("/{entry_id}/duplicate", response_class=HTMLResponse)
def journal_duplicate(entry_id: str, request: Request,
                      session: Session = Depends(get_session)):
    """Fork the entry, keeping the original's trip (D-133) — the copy is then
    re-attachable to another trip from its own row."""
    user = scope.user_of(request)
    try:
        journal_svc.duplicate_entry(session, user, entry_id)
    except Exception:  # noqa: BLE001
        session.rollback()
    return _panel(request, session)


@router.post("/{entry_id}/delete", response_class=HTMLResponse)
def journal_delete(entry_id: str, request: Request,
                   session: Session = Depends(get_session)):
    user = scope.user_of(request)
    try:
        journal_svc.delete_entry(session, user, entry_id)
    except Exception:  # noqa: BLE001
        session.rollback()
    return _panel(request, session)
