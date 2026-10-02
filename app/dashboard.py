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


def _next_up(session: Session, trips: list[Trip], today: str, user=None) -> dict | None:
    """The design's Next-up card (one per screen): the soonest flight on a visible
    trip that hasn't left yet, with mono airport codes; with no flight, the featured
    trip itself. `overline` is the Runway-coloured line ("NEXT UP · FLIGHT · IN 12
    DAYS"). Only real 3-letter codes are shown as codes.

    The flight is the VIEWER'S: an activity tagged to other travellers on a shared trip
    is never their "next up" (app/trips/who.py: no tags = everyone, so an untagged flight
    counts for all). A signed-in user not linked to a traveller identity sees only the
    untagged ones. `user=None` (auth off) filters nothing."""
    import re

    from app.models import TripActivity
    featured = _next_trip(trips, today)
    ids = [t.id for t in trips if t.start_date or t.end_date]
    flights = []
    if ids:
        for a in (session.query(TripActivity).filter(
                TripActivity.trip_id.in_(ids), TripActivity.type == "flight").all()):
            day = (a.starts_at or "")[:10]
            if day and day >= today:
                flights.append(a)
    if user is not None and flights:
        from app.trips import who
        tags = who.activity_map(session, [a.id for a in flights])
        me = getattr(user, "traveler_id", None)
        flights = [a for a in flights if not tags.get(a.id) or (me is not None and me in tags[a.id])]
    flights.sort(key=lambda a: a.starts_at or "")
    code = re.compile(r"^[A-Z]{3}$")
    for a in flights:
        v = a.values or {}
        frm, to = str(v.get("from") or "").strip().upper(), str(v.get("to") or "").strip().upper()
        if code.match(frm) and code.match(to):
            days = (date.fromisoformat(a.starts_at[:10]) - date.fromisoformat(today)).days
            when = "TODAY" if days == 0 else f"IN {days} DAY{'S' if days != 1 else ''}"
            trip = next((t for t in trips if t.id == a.trip_id), None)
            return {"kind": "flight", "overline": f"NEXT UP · FLIGHT · {when}",
                    "from": frm, "to": to, "flight_number": v.get("flight_number") or "",
                    "departs": a.starts_at.replace("T", " ")[:16], "trip_id": a.trip_id,
                    "trip_name": trip.name if trip else ""}
    if featured is None:
        return None
    if featured["in_progress"]:
        overline = "NOW · TRIP IN PROGRESS"
    else:
        d = featured["days_until"]
        overline = f"NEXT UP · TRIP · IN {d} DAY{'S' if d != 1 else ''}"
    return {"kind": "trip", "overline": overline, "trip_id": featured["id"],
            "trip_name": featured["name"], "start": featured["start"], "end": featured["end"]}


def _greeting(user, now=None) -> str:
    """"Good evening, ada" — the Home header's opening line. Server-local time; the
    page is a daily glance, not a clock, so no client-side hour lookup."""
    from datetime import datetime
    hour = (now or datetime.now()).hour
    part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    name = getattr(user, "username", "") or ""
    return f"Good {part}, {name}" if name else f"Good {part}"


def _queue_preview(session: Session, user=None, limit: int = 4) -> list[dict]:
    """The Home review-queue panel: what needs you first, then what is ready to approve.
    Same views as /review (scoped to the caller), so the panel and the queue agree."""
    from app.review.routes import _all_views
    views = _all_views(session, user)
    order = {"attention": 0, "ready": 1}
    rows = sorted((v for v in views if v["bucket"] in order), key=lambda v: order[v["bucket"]])
    return [{"id": v["id"], "bucket": v["bucket"], "kind": v["kind"], "headline": v["headline"],
             "subline": v["subline"]} for v in rows[:limit]]


def _trip_cards(session: Session, trips: list[Trip], today: str, limit: int = 4, user=None) -> list[dict]:
    """Upcoming-trips grid: trips under way first, then the soonest upcoming ones, with
    the same display fields as the Trips list cards (codes, when, item count)."""
    from app.models import TripActivity
    from app.trips.routes import _trip_display, packing_progress
    rank = {"inprogress": 0, "upcoming": 1}
    live = sorted((t for t in trips if t.start_date and _trip_status(t, today) in rank),
                  key=lambda t: (rank[_trip_status(t, today)], t.start_date))[:limit]
    acts: dict[str, list] = {t.id: [] for t in live}
    if live:
        for a in session.query(TripActivity).filter(TripActivity.trip_id.in_(list(acts))).all():
            acts[a.trip_id].append(a)
    pack = packing_progress(session, [t.id for t in live], user)
    return [_trip_display(t, sorted(acts[t.id], key=lambda a: a.starts_at or ""), today, pack.get(t.id))
            for t in live]


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
        "next_up": _next_up(session, trips, today, user),
        "greeting": _greeting(user),
        "queue": _queue_preview(session, user),
        "trip_cards": _trip_cards(session, trips, today, user=user),
        "counts": _pipeline_counts(session, user),
        "capture": _capture_activity(session, today, user),
        "trip_count": len(trips),
        "upcoming_count": upcoming_count,
        "version": settings.app_version,
    })
