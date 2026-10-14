"""Per-user visibility scoping (Yonder 4.0 Phase 2 — SPEC-4 D-119–D-121).

One module, used by the HTMX routes now and the /api/v1 layer in Phase 3, so the
two never disagree on who can see what.

Two ownership roots:
- **Trip** — visible if owned by the user, the user travels on it, or it's
  shared with them via a TripMember row.
- **TravelItem (capture)** — visible if owned by the user.

**Ownerless rows are closed (YONDER-186).** With auth on, a row with owner_id
NULL is visible to admins only — never to every signed-in user. NULL owners are
normal only in the auth-off single-user world; with auth on they're drift (a
code path that forgot the owner, or a pre-fix row), and an admin assigns them.

**Admins (4.1 sharing):** an admin additionally sees — and edits — data owned by
users who SHARE with admins (`User.share_with_admins`, on by default). A user who
opts out is, to an admin, like any other user: visible only where they invite
the admin in (roster/share). This is an in-app policy, not encryption; the
server operator can always read the database.

Every function is a **no-op with auth disabled** (`user is None`, the pre-4.0
single-user world), so wiring these in changes nothing until TX_AUTH_REQUIRED
is on.
"""
from __future__ import annotations

from sqlalchemy import or_, select, true

from app.models import JournalEntry, TravelItem, Trip, TripMember, TripTraveler, User


def user_of(request):
    """The user the auth middleware resolved onto the request (or None)."""
    return getattr(getattr(request, "state", None), "user", None)


def snapshot(user):
    """A detached stand-in carrying just what the filters here read (id,
    is_admin) — for a background job that outlives the request's DB session,
    where touching the real User row would fail."""
    if user is None:
        return None
    from types import SimpleNamespace
    return SimpleNamespace(id=user.id, is_admin=bool(getattr(user, "is_admin", False)))


def unrestricted(user) -> bool:
    """True when scoping is a no-op: auth is off (no user). Admins are NOT
    unrestricted since 4.1 — they see sharing users' data; see is_admin/sharing."""
    return user is None


def is_admin(user) -> bool:
    return bool(getattr(user, "is_admin", False))


def sharing_owner_ids():
    """SQL subquery of the users who share their data with admins."""
    return select(User.id).where(User.share_with_admins.is_(True))


def owner_shares(session, owner_id) -> bool:
    """Does this owner share with admins? (No owner: yes — admins own the
    cleanup of ownerless rows.)"""
    if not owner_id:
        return True
    owner = session.get(User, owner_id)
    return owner is None or bool(owner.share_with_admins)


def _admin_sees(session, user, owner_id) -> bool:
    return is_admin(user) and owner_shares(session, owner_id)


# Trip access is two-tier (D-122):
#   FULL (view + edit): owner, admin, or a TRAVELLER on the trip who is a user
#     (implicit — being on the roster shares the trip in).
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
    traveller-on-roster + explicitly-shared; admins also ownerless + sharing
    users'). `true()` when unrestricted."""
    if unrestricted(user):
        return true()
    cond = Trip.owner_id == user.id
    if is_admin(user):
        cond = or_(cond, Trip.owner_id.is_(None),
                   Trip.owner_id.in_(sharing_owner_ids()))
    extra = _member_trip_ids(session, user) | _roster_trip_ids(session, user)
    return or_(cond, Trip.id.in_(extra)) if extra else cond


def filing_trip_filter(session, user):
    """Trips a user's captures can be FILED into (4.1): their own and ones they
    travel on — never someone else's just because the user is an admin, and not
    view-only shares. Ownerless trips: admins only. `true()` with auth off."""
    if user is None:
        return true()
    cond = Trip.owner_id == user.id
    if is_admin(user):
        cond = or_(cond, Trip.owner_id.is_(None))
    roster = _roster_trip_ids(session, user)
    return or_(cond, Trip.id.in_(roster)) if roster else cond


def visible_trip_ids(session, user) -> set[str] | None:
    """The set of VIEW-visible trip ids, or None meaning "all" (unrestricted) —
    for scoping child tables by trip_id without a join."""
    if unrestricted(user):
        return None
    return {row[0] for row in session.query(Trip.id).filter(trip_filter(session, user)).all()}


def can_view_trip(session, user, trip) -> bool:
    if trip is None:
        return False
    if unrestricted(user):
        return True
    if trip.owner_id is not None and trip.owner_id == user.id:
        return True
    if _admin_sees(session, user, trip.owner_id):
        return True
    return _is_traveller(session, user, trip) or _is_member(session, user, trip)


def can_edit_trip(session, user, trip) -> bool:
    """Full access: owner/traveller, or an admin when the owner shares (or the
    trip has no owner). Explicit (non-traveller) shares are view-only, so NOT
    editors (D-122)."""
    if trip is None:
        return False
    if unrestricted(user):
        return True
    if trip.owner_id is not None and trip.owner_id == user.id:
        return True
    if _admin_sees(session, user, trip.owner_id):
        return True
    return _is_traveller(session, user, trip)


def trip_role(session, user, trip) -> str | None:
    """The caller's tier on a trip (D-199, YONDER-185), for the API's `my_role`
    and the web's view-only page. owner | traveller | admin | viewer, or None
    without access. owner/traveller/admin are exactly the editors
    (`can_edit_trip`); viewer is an explicit share for a non-traveller, which is
    view-only (D-122). Auth off: owner (the single user owns everything)."""
    if trip is None:
        return None
    if unrestricted(user):
        return "owner"
    if trip.owner_id is not None and trip.owner_id == user.id:
        return "owner"
    if _is_traveller(session, user, trip):
        return "traveller"
    if _admin_sees(session, user, trip.owner_id):
        return "admin"
    if _is_member(session, user, trip):
        return "viewer"
    return None


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
    cond = TravelItem.owner_id == user.id
    if is_admin(user):
        cond = or_(cond, TravelItem.owner_id.is_(None),
                   TravelItem.owner_id.in_(sharing_owner_ids()))
    return cond


def batch_filter(user):
    """IngestBatch predicate, same rule as captures (a batch becomes captures)."""
    from app.models import IngestBatch
    if unrestricted(user):
        return true()
    cond = IngestBatch.user_id == user.id
    if is_admin(user):
        cond = or_(cond, IngestBatch.user_id.is_(None),
                   IngestBatch.user_id.in_(sharing_owner_ids()))
    return cond


def can_access_capture(user, item) -> bool:
    if item is None:
        return False
    if unrestricted(user):
        return True
    if item.owner_id is not None and item.owner_id == user.id:
        return True
    if is_admin(user):
        from sqlalchemy.orm import object_session
        session = object_session(item)
        return session is not None and owner_shares(session, item.owner_id)
    return False


def capture_or_none(session, request, item_id):
    """Drop-in for `session.get(TravelItem, item_id)` with an ownership check."""
    item = session.get(TravelItem, item_id)
    if item is None or not can_access_capture(user_of(request), item):
        return None
    return item


def require_capture(session, user, item_id):
    """API-style guard: return the capture or raise 404 (own + admin)."""
    from fastapi import HTTPException
    item = session.get(TravelItem, item_id)
    if item is None or not can_access_capture(user, item):
        raise HTTPException(status_code=404, detail="item not found")
    return item


# --------------------------------------------------------------------------- journal
# A journal is PERSONAL (SPEC-6 D-134): visible to its author and admins only,
# never trip-shared. Unlike trips/captures there is deliberately NO "open"
# (NULL owner) tier for a normal user — a diary must not leak between users.
# The auth-off single-user world (user is None) sees everything; an admin sees
# entries of users who share with admins (4.1), same as every other resource.
def journal_filter(user):
    """A predicate for `session.query(JournalEntry).filter(...)`."""
    if unrestricted(user):
        return true()
    cond = JournalEntry.owner_id == user.id
    if is_admin(user):
        cond = or_(cond, JournalEntry.owner_id.is_(None),
                   JournalEntry.owner_id.in_(sharing_owner_ids()))
    return cond


def can_access_journal(user, entry) -> bool:
    if entry is None:
        return False
    if unrestricted(user):
        return True
    if entry.owner_id == user.id:
        return True
    if is_admin(user):
        from sqlalchemy.orm import object_session
        session = object_session(entry)
        return session is not None and owner_shares(session, entry.owner_id)
    return False


def require_journal(session, user, entry_id):
    """API-style guard: return the entry or raise 404 (own + admin). Existence
    never leaks by id — a stranger's entry 404s exactly like a missing one."""
    from fastapi import HTTPException
    entry = session.get(JournalEntry, entry_id)
    if entry is None or not can_access_journal(user, entry):
        raise HTTPException(status_code=404, detail="journal entry not found")
    return entry
