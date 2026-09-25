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

from app.models import JournalEntry, TravelItem, Trip, TripMember, TripTraveler


def user_of(request):
    """The user the auth middleware resolved onto the request (or None)."""
    return getattr(getattr(request, "state", None), "user", None)


def unrestricted(user) -> bool:
    """True when scoping is a no-op: no authenticated user, or an admin."""
    return user is None or bool(getattr(user, "is_admin", False))


# Trip access is two-tier (D-122):
#   FULL (view + edit): owner, admin, or a TRAVELLER on the trip who is a user
#     (implicit — being on the roster shares the trip in), or an open trip.
#   VIEW-ONLY (read):  an explicit TripMember share for a NON-traveller.
_READ_METHODS = ("GET", "HEAD", "OPTIONS")


def _member_trip_ids(session, user) -> set[str]:
    """Trips explicitly shared with the user (view-only)."""
    if user is None:
        return set()
    return {row[0] for row in
            session.query(TripMember.trip_id).filter_by(user_id=user.id).all()}


def _roster_trip_ids(session, user) -> set[str]:
    """Trips the user is a TRAVELLER on (implicit full access)."""
    if user is None or getattr(user, "traveler_id", None) is None:
        return set()
    return {row[0] for row in
            session.query(TripTraveler.trip_id).filter_by(traveler_id=user.traveler_id).all()}


def _is_traveller(session, user, trip) -> bool:
    if user is None or getattr(user, "traveler_id", None) is None:
        return False
    return session.query(TripTraveler).filter_by(
        trip_id=trip.id, traveler_id=user.traveler_id).count() > 0


def _is_member(session, user, trip) -> bool:
    return session.query(TripMember).filter_by(trip_id=trip.id, user_id=user.id).count() > 0


# --------------------------------------------------------------------------- trips
def trip_filter(session, user):
    """A predicate for `session.query(Trip).filter(...)` — VIEW visibility (own +
    open + traveller-on-roster + explicitly-shared). `true()` when unrestricted."""
    if unrestricted(user):
        return true()
    cond = or_(Trip.owner_id.is_(None), Trip.owner_id == user.id)
    extra = _member_trip_ids(session, user) | _roster_trip_ids(session, user)
    return or_(cond, Trip.id.in_(extra)) if extra else cond


def visible_trip_ids(session, user) -> set[str] | None:
    """The set of VIEW-visible trip ids, or None meaning "all" (unrestricted) —
    for scoping child tables by trip_id without a join."""
    if unrestricted(user):
        return None
    ids = {row[0] for row in
           session.query(Trip.id).filter(
               or_(Trip.owner_id.is_(None), Trip.owner_id == user.id)).all()}
    ids |= _member_trip_ids(session, user) | _roster_trip_ids(session, user)
    return ids


def can_view_trip(session, user, trip) -> bool:
    if trip is None:
        return False
    if unrestricted(user):
        return True
    if trip.owner_id is None or trip.owner_id == user.id:
        return True
    return _is_traveller(session, user, trip) or _is_member(session, user, trip)


def can_edit_trip(session, user, trip) -> bool:
    """Full access: owner/admin/open/traveller. Explicit (non-traveller) shares
    are view-only, so they are NOT editors (D-122)."""
    if trip is None:
        return False
    if unrestricted(user):
        return True
    if trip.owner_id is None or trip.owner_id == user.id:
        return True
    return _is_traveller(session, user, trip)


# Back-compat alias — "access" means view.
can_access_trip = can_view_trip


def trip_or_none(session, request, trip_id):
    """Drop-in for `session.get(Trip, trip_id)` that enforces access by HTTP
    method: a READ (GET/HEAD) needs view access; a WRITE (POST/DELETE/…) needs
    edit access. No view access → None (each route's `if trip is None` 404 path
    handles it, so existence never leaks). View-but-not-edit on a write → 403."""
    trip = session.get(Trip, trip_id)
    if trip is None:
        return None
    user = user_of(request)
    if not can_view_trip(session, user, trip):
        return None
    method = getattr(request, "method", "GET").upper()
    if method not in _READ_METHODS and not can_edit_trip(session, user, trip):
        from fastapi import HTTPException
        raise HTTPException(status_code=403,
                            detail="you have view-only access to this trip")
    return trip


def require_trip(session, user, trip_id, *, edit: bool = False):
    """API-style guard: return the trip or raise 404 (never 403 for existence —
    a trip's existence must not leak by id-guessing). `edit=True` additionally
    requires edit access, raising 403 for a view-only share."""
    from fastapi import HTTPException
    trip = session.get(Trip, trip_id)
    if trip is None or not can_view_trip(session, user, trip):
        raise HTTPException(status_code=404, detail="trip not found")
    if edit and not can_edit_trip(session, user, trip):
        raise HTTPException(status_code=403, detail="view-only access")
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


def require_capture(session, user, item_id):
    """API-style guard: return the capture or raise 404 (own + open + admin)."""
    from fastapi import HTTPException
    item = session.get(TravelItem, item_id)
    if item is None or not can_access_capture(user, item):
        raise HTTPException(status_code=404, detail="item not found")
    return item


# --------------------------------------------------------------------------- journal
# A journal is PERSONAL (SPEC-6 D-134): visible to its author and admins only,
# never trip-shared. Unlike trips/captures there is deliberately NO "open"
# (NULL owner) tier for a normal user — a diary must not leak between users.
# Admins and the auth-off single-user world (user is None) still see everything,
# same as every other resource.
def journal_filter(user):
    """A predicate for `session.query(JournalEntry).filter(...)`."""
    if unrestricted(user):
        return true()
    return JournalEntry.owner_id == user.id


def can_access_journal(user, entry) -> bool:
    if entry is None:
        return False
    if unrestricted(user):
        return True
    return entry.owner_id == user.id


def require_journal(session, user, entry_id):
    """API-style guard: return the entry or raise 404 (own + admin). Existence
    never leaks by id — a stranger's entry 404s exactly like a missing one."""
    from fastapi import HTTPException
    entry = session.get(JournalEntry, entry_id)
    if entry is None or not can_access_journal(user, entry):
        raise HTTPException(status_code=404, detail="journal entry not found")
    return entry
