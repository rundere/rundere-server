"""Run·dere 4.7 — Saved places web page (YONDER-200, D-166/D-167).

`/places` lists the caller's own saved places (own-only, via app/places.py) and
files one to a trip — optionally with a date, otherwise undated to be scheduled
later. Thin over the shared service and `api_v1.file_place`, so the page, the
JSON API and MCP never diverge. Every action re-renders the one
`#places-panel` partial.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import api_v1, places as places_svc, scope
from app.db import get_session

router = APIRouter(prefix="/places")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))
from app.icons import install as _install_icons  # noqa: E402
_install_icons(templates)


def _panel_ctx(request: Request, session: Session, notice: dict | None = None) -> dict:
    from app.capture import _filing_trips
    user = scope.user_of(request)
    trips = [{"id": t.id, "name": t.name,
              "dates": " → ".join(x for x in (t.start_date, t.end_date) if x)}
             for t in _filing_trips(session, user)]
    fileable = api_v1.fileable_trips(session, user)
    places = places_svc.list_places(session, user)
    # A hint only: the picker pre-selects it, the person still presses File (D-133).
    suggested = {p.id: places_svc.suggest_trip(session, fileable, p) for p in places}
    return {"request": request, "places": places, "trips": trips,
            "suggested": suggested, "notice": notice}


def _panel(request: Request, session: Session, notice: dict | None = None):
    return templates.TemplateResponse(request, "_places_panel.html",
                                      _panel_ctx(request, session, notice))


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def places_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "places.html", _panel_ctx(request, session))


@router.post("/{place_id}/file", response_class=HTMLResponse)
def places_file(place_id: str, request: Request, trip_id: str = Form(""),
                starts_at: str = Form(""), session: Session = Depends(get_session)):
    user = scope.user_of(request)
    if not trip_id:
        return _panel(request, session, {"ok": False, "message": "Pick a trip to file it to."})
    try:
        a = api_v1.file_place(session, user, place_id, trip_id,
                              api_v1.SavedPlaceFileIn(starts_at=starts_at.strip() or None))
    except HTTPException as exc:
        session.rollback()
        return _panel(request, session, {"ok": False, "message": str(exc.detail)})
    when = f" on {a.starts_at[:10]}" if a.starts_at else " (no date yet)"
    return _panel(request, session, {
        "ok": True, "message": f"Filed “{a.title or 'place'}”{when}.",
        "trip_id": trip_id})


@router.post("/{place_id}/rename", response_class=HTMLResponse)
def places_rename(place_id: str, request: Request, name: str = Form(""),
                  session: Session = Depends(get_session)):
    try:
        places_svc.update_place(session, scope.user_of(request), place_id, name=name)
    except (ValueError, HTTPException) as exc:
        session.rollback()
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _panel(request, session, {"ok": False, "message": msg})
    return _panel(request, session)


@router.post("/{place_id}/delete", response_class=HTMLResponse)
def places_delete(place_id: str, request: Request, session: Session = Depends(get_session)):
    try:
        places_svc.delete_place(session, scope.user_of(request), place_id)
    except HTTPException:
        session.rollback()
    return _panel(request, session)
