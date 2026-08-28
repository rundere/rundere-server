"""Per-user visibility scoping (Yonder 4.0 Phase 2 — SPEC-4 D-119–D-121).

One module, used by the HTMX routes now and the /api/v1 layer in Phase 3, so the
two never disagree on who can see what.

Two ownership roots:
- **Trip** — visible if open (owner_id NULL), owned by the user, shared via a
  TripMember row, or the user is admin.
- **TravelItem (capture)** — visible if open (owner_id NULL) or owned by the user
  (or admin).

Every function is a **no-op when scoping shouldn't apply**: `user is None` (auth
disabled — the pre-4.0 single-user world) or the user is an admin. So wiring
these in changes nothing until TX_AUTH_REQUIRED is on.
"""
from __future__ import annotations

from sqlalchemy import or_, true

from app.models import TravelItem, Trip, TripMember


def user_of(request):
    """The user the auth middleware resolved onto the request (or None)."""
    return getattr(getattr(request, "state", None), "user", None)


def unrestricted(user) -> bool:
    """True when scoping is a no-op: no authenticated user, or an admin."""
    return user is None or bool(getattr(user, "is_admin", False))


def _member_trip_ids(session, user) -> set[str]:
    if user is None:
        return set()
    return {row[0] for row in
            session.query(TripMember.trip_id).filter_by(user_id=user.id).all()}


# --------------------------------------------------------------------------- trips
def trip_filter(session, user):
    """A predicate for `session.query(Trip).filter(...)`. `true()` when unrestricted."""
    if unrestricted(user):
        return true()
    cond = or_(Trip.owner_id.is_(None), Trip.owner_id == user.id)
    member_ids = _member_trip_ids(session, user)
    return or_(cond, Trip.id.in_(member_ids)) if member_ids else cond


def visible_trip_ids(session, user) -> set[str] | None:
    """The set of visible trip ids, or None meaning "all" (unrestricted) — for
    scoping child tables by trip_id without a join."""
    if unrestricted(user):
        return None
    ids = {row[0] for row in
           session.query(Trip.id).filter(
               or_(Trip.owner_id.is_(None), Trip.owner_id == user.id)).all()}
    ids |= _member_trip_ids(session, user)
    return ids


def can_access_trip(session, user, trip) -> bool:
    if trip is None:
        return False
    if unrestricted(user):
        return True
    if trip.owner_id is None or trip.owner_id == user.id:
        return True
    return session.query(TripMember).filter_by(trip_id=trip.id, user_id=user.id).count() > 0


def trip_or_none(session, request, trip_id):
    """Drop-in for `session.get(Trip, trip_id)` that also enforces access:
    returns the trip only if the request's user may see it, else None (so each
    route's existing `if trip is None` 404 path handles both cases)."""
    trip = session.get(Trip, trip_id)
    if trip is None or not can_access_trip(session, user_of(request), trip):
        return None
    return trip


def require_trip(session, user, trip_id):
    """API-style guard: return the trip or raise 404 (never 403 — a trip's
    existence must not leak by id-guessing)."""
    from fastapi import HTTPException
    trip = session.get(Trip, trip_id)
    if trip is None or not can_access_trip(session, user, trip):
        raise HTTPException(status_code=404, detail="trip not found")
    return trip


# --------------------------------------------------------------------------- captures
def capture_filter(user):
    """A predicate for `session.query(TravelItem).filter(...)`."""
    if unrestricted(user):
        return true()
    return or_(TravelItem.owner_id.is_(None), TravelItem.owner_id == user.id)


def can_access_capture(user, item) -> bool:
    if item is None:
        return False
    if unrestricted(user):
        return True
    return item.owner_id is None or item.owner_id == user.id


def capture_or_none(session, request, item_id):
    """Drop-in for `session.get(TravelItem, item_id)` with an ownership check."""
    item = session.get(TravelItem, item_id)
    if item is None or not can_access_capture(user_of(request), item):
        return None
    return item
