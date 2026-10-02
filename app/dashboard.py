"""Root dashboard (3.8.1) — the service's front door.

Before this, the app answered only under /review, /capture, /trips and /admin —
the bare domain (`yonder.home.example`) had no handler for `/` and 404'd. This
gives the root a server-rendered landing page: the three daily surfaces
(Capture, Review, Trips), the trip coming up next, and a live snapshot of what's
moving through the capture pipeline. Pipeline counts are read through the review
queue's own bucket logic (`app.review.routes`) so the dashboard and the queue
can never disagree on what "3 need you" means.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import scope
from app.config import settings
from app.db import get_session
from app.models import IngestBatch, Trip

log = logging.getLogger("yonder.dashboard")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))
from app.icons import install as _install_icons  # noqa: E402
_install_icons(templates)


def _trip_status(t: Trip, today: str) -> str:
    if t.end_date and t.end_date < today:
        return "past"
    if t.start_date and t.start_date > today:
        return "upcoming"
    return "inprogress"


def _next_trip(trips: list[Trip], today: str) -> dict | None:
    """The trip to feature: one already under way, else the soonest upcoming
    one. A trip with no start date can never be "next" — nothing to count down
    to — so it's excluded rather than shown with a blank date."""
    dated = [t for t in trips if t.start_date]
    current = [t for t in dated if _trip_status(t, today) == "inprogress"]
    upcoming = sorted((t for t in dated if _trip_status(t, today) == "upcoming"),
                      key=lambda t: t.start_date)
    t = (current or upcoming or [None])[0]
    if t is None:
        return None
    in_progress = t in current
    days_until = None if in_progress else (
        date.fromisoformat(t.start_date) - date.fromisoformat(today)).days
    return {"id": t.id, "name": t.name, "start": t.start_date, "end": t.end_date,
            "in_progress": in_progress, "days_until": days_until}


def _pipeline_counts(session: Session, user=None) -> dict:
    # Reuse the review queue's own bucket logic — a fresh reimplementation here
    # would inevitably drift from what "Needs you: 3" means on /review. Scoped to
    # the user's own captures (Phase 2) so the two surfaces still agree.
    from app.review.routes import _all_views, _counts
    return _counts(_all_views(session, user))


def _capture_activity(session: Session, today: str, user=None) -> dict:
    q = session.query(IngestBatch)
    if not scope.unrestricted(user):
        q = q.filter(scope.batch_filter(user))              # own + open (+ sharing, admin)
    rows = q.all()
    return {
        "today": sum(1 for b in rows
                     if b.received_at and b.received_at.date().isoformat() == today),
        "awaiting": sum(1 for b in rows if b.status == "received"),
        "needs_review": sum(1 for b in rows if b.status == "needs_review"),
        "total": len(rows),
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    today = date.today().isoformat()
    user = scope.user_of(request)
    trips = session.query(Trip).filter(scope.trip_filter(session, user)).all()
    upcoming_count = sum(1 for t in trips
                         if t.start_date and _trip_status(t, today) == "upcoming")
    return templates.TemplateResponse(request, "dashboard.html", {
        "next_trip": _next_trip(trips, today),
        "counts": _pipeline_counts(session, user),
        "capture": _capture_activity(session, today, user),
        "trip_count": len(trips),
        "upcoming_count": upcoming_count,
        "version": settings.app_version,
    })
