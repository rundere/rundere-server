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

**Admins have no standing access to people's data (D-211).** To an admin, other
people's trips, captures and journals are like anyone else's: visible only where
the person invites them in (roster/share). The one exception is **support
access**, which the person grants (`User.share_with_admins`, until
`support_until`): while it's live an admin may VIEW the person's trips and Inbox
captures and batches, read-only, and never their journal. Admins edit only
ownerless rows, which nobody else can clean up. This is an in-app policy, not
encryption; the server operator can always read the database.

Every function is a **no-op with auth disabled** (`user is None`, the pre-4.0
single-user world), so wiring these in changes nothing until TX_AUTH_REQUIRED
is on. **With auth on, no user sees nothing** (YONDER-293): a route that skips
the sign-in gate (`/capture`, …) and asks on behalf of nobody gets an empty
answer, never everyone's data. It used to be read as "unrestricted" there.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import false, or_, select, true

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
    """True when scoping is a no-op: auth is off and nobody is signed in. With
    auth on, no user is NOT unrestricted: it sees nothing (fail closed,
    YONDER-293). Admins are NOT unrestricted since 4.1 — they see sharing
    users' data; see is_admin/sharing."""
    from app.config import settings
    return user is None and not settings.auth_required


def is_admin(user) -> bool:
    return bool(getattr(user, "is_admin", False))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def support_live(owner) -> bool:
    """Has this person granted support access that hasn't ended (D-211)?"""
    if owner is None or not owner.share_with_admins:
        return False
    until = owner.support_until
    if until is None:
        return True
    if until.tzinfo is None:                       # SQLite hands back naive UTC
        until = until.replace(tzinfo=timezone.utc)
    return until > _utcnow()


def sharing_owner_ids():
    """SQL subquery of the users whose support access is live (D-211)."""
    now = _utcnow()
    return select(User.id).where(User.share_with_admins.is_(True),
                                 or_(User.support_until.is_(None), User.support_until > now))


def owner_shares(session, owner_id) -> bool:
    """May an admin VIEW this owner's data? Only under live support access
    (D-211). No owner: yes — admins own the cleanup of ownerless rows. An owner
    id with no account behind it is treated as ownerless."""
    if not owner_id:
        return True
    owner = session.get(User, owner_id)
    return owner is None or support_live(owner)


def _admin_sees(session, user, owner_id) -> bool:
    """VIEW only: an admin reading an ownerless row, or one whose owner granted
    support access. Never grounds for an edit (D-211)."""
    return is_admin(user) and owner_shares(session, owner_id)


def _admin_edits(user, owner_id) -> bool:
    """The only rows an admin edits for being an admin: ownerless ones."""
    return is_admin(user) and not owner_id


def note_support_view(user, owner_id) -> None:
    """Log the first time each day an admin opens something of `owner_id`'s under
    support access (D-211), so the person can see who looked. The caller has
    decided the access is BY support access (not the admin's own row, a roster
    place or an explicit share). Written in its own session, because a read
    request never commits its own. Never raises."""
    if not owner_id or user is None or not is_admin(user):
        return
    try:
        from app.db import SessionLocal
        from app.models import AccountEvent
        start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        with SessionLocal() as s:
            seen = (s.query(AccountEvent.id)
                    .filter(AccountEvent.user_id == owner_id, AccountEvent.actor_id == user.id,
                            AccountEvent.action == "support_viewed", AccountEvent.at >= start)
                    .first())
            if seen is None:
                s.add(AccountEvent(user_id=owner_id, actor_id=user.id,
                                   action="support_viewed"))
                s.commit()
    except Exception:                              # logging must never block a read
        import logging
        logging.getLogger(__name__).exception("could not log a support view")


def _trip_by_support(session, user, trip) -> bool:
    """Is support access the only reason `user` sees this trip?"""
    if user is None or not is_admin(user) or not trip.owner_id or trip.owner_id == user.id:
        return False
    return not (_is_traveller(session, user, trip) or _is_member(session, user, trip))


# Trip access is two-tier (D-122):
#   FULL (view + edit): owner, or a TRAVELLER on the trip who is a user
#     (implicit — being on the roster shares the trip in); admins only for an
#     ownerless trip.
#   VIEW-ONLY (read):  an explicit TripMember share for a NON-traveller, or an
#     admin under the owner's support access (D-211).
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
    traveller-on-roster + explicitly-shared; admins also ownerless trips and those
    of people with live support access, D-211). `true()` when unrestricted."""
    if unrestricted(user):
        return true()
    if user is None:
        return false()
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
    if unrestricted(user):
        return true()
    if user is None:
        return false()
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
    if user is None:
        return False
    if trip.owner_id is not None and trip.owner_id == user.id:
        return True
    if _admin_sees(session, user, trip.owner_id):         # ownerless, or support access
        return True
    return _is_traveller(session, user, trip) or _is_member(session, user, trip)


def can_edit_trip(session, user, trip) -> bool:
    """Full access: owner/traveller, or an admin for an ownerless trip. Explicit
    (non-traveller) shares and support access are view-only, so NOT editors
    (D-122, D-211). A demo server edits nothing (D-198): every edit check, and
    the API's `can_edit`, follows from this one answer."""
    if trip is None:
        return False
    from app import demo
    if demo.read_only():
        return False
    if unrestricted(user):
        return True
    if user is None:
        return False
    if trip.owner_id is not None and trip.owner_id == user.id:
        return True
    if _admin_edits(user, trip.owner_id):                 # ownerless only (D-211)
        return True
    return _is_traveller(session, user, trip)


def trip_role(session, user, trip) -> str | None:
    """The caller's tier on a trip (D-199, YONDER-185), for the API's `my_role`
    and the web's view-only page. owner | traveller | admin | viewer, or None
    without access. owner/traveller/admin are exactly the editors
    (`can_edit_trip`; admin = an ownerless trip); viewer is an explicit share for
    a non-traveller or an admin under support access, both view-only (D-122,
    D-211). Auth off: owner (the single user owns everything)."""
    if trip is None:
        return None
    if unrestricted(user):
        return "owner"
    if user is None:
        return None
    if trip.owner_id is not None and trip.owner_id == user.id:
        return "owner"
    if _is_traveller(session, user, trip):
        return "traveller"
    if _admin_edits(user, trip.owner_id):
        return "admin"
    if _is_member(session, user, trip) or _admin_sees(session, user, trip.owner_id):
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
    if _trip_by_support(session, user, trip):
        note_support_view(user, trip.owner_id)
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
    if _trip_by_support(session, user, trip):
        note_support_view(user, trip.owner_id)
    return trip


# --------------------------------------------------------------------------- captures
def capture_filter(user):
    """A predicate for `session.query(TravelItem).filter(...)`."""
    if unrestricted(user):
        return true()
    if user is None:
        return false()
    cond = TravelItem.owner_id == user.id
    if is_admin(user):
        cond = or_(cond, TravelItem.owner_id.is_(None),
                   TravelItem.owner_id.in_(sharing_owner_ids()))
    return cond


def capture_edit_filter(user):
    """The captures a person may CHANGE in bulk (Process, Reprocess, Commit,
    filing): their own, and for an admin the ownerless ones. Never someone
    else's under support access, which is view-only (D-211)."""
    if unrestricted(user):
        return true()
    if user is None:
        return false()
    cond = TravelItem.owner_id == user.id
    if is_admin(user):
        cond = or_(cond, TravelItem.owner_id.is_(None))
    return cond


def batch_edit_filter(user):
    """IngestBatch counterpart of `capture_edit_filter`."""
    from app.models import IngestBatch
    if unrestricted(user):
        return true()
    if user is None:
        return false()
    cond = IngestBatch.user_id == user.id
    if is_admin(user):
        cond = or_(cond, IngestBatch.user_id.is_(None))
    return cond


def batch_filter(user):
    """IngestBatch predicate, same rule as captures (a batch becomes captures)."""
    from app.models import IngestBatch
    if unrestricted(user):
        return true()
    if user is None:
        return false()
    cond = IngestBatch.user_id == user.id
    if is_admin(user):
        cond = or_(cond, IngestBatch.user_id.is_(None),
                   IngestBatch.user_id.in_(sharing_owner_ids()))
    return cond


def can_access_capture(user, item) -> bool:
    """VIEW access to a capture: its owner, or an admin for an ownerless one or
    under the owner's support access (D-211). See `can_edit_capture` for writes."""
    if item is None:
        return False
    if unrestricted(user):
        return True
    if user is None:
        return False
    if item.owner_id is not None and item.owner_id == user.id:
        return True
    if is_admin(user):
        from sqlalchemy.orm import object_session
        session = object_session(item)
        return session is not None and owner_shares(session, item.owner_id)
    return False


def can_edit_capture(user, item) -> bool:
    """Write access to a capture (edit, approve, file, delete, reprocess): its
    owner, or an admin for an ownerless one. Support access is view-only (D-211);
    a demo server edits nothing (D-198)."""
    if item is None:
        return False
    from app import demo
    if demo.read_only():
        return False
    if unrestricted(user):
        return True
    if user is None:
        return False
    if item.owner_id is not None and item.owner_id == user.id:
        return True
    return _admin_edits(user, item.owner_id)


def require_capture_edit(user, item) -> None:
    """Raise 403 when the caller may see `item` but not change it (D-211)."""
    if not can_edit_capture(user, item):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="you have view-only access to this capture")


def capture_or_none(session, request, item_id):
    """Drop-in for `session.get(TravelItem, item_id)` with an ownership check,
    by HTTP method like `trip_or_none`: a read needs view access, a write needs
    edit access (403 for view-only support access, D-211)."""
    item = session.get(TravelItem, item_id)
    user = user_of(request)
    if item is None or not can_access_capture(user, item):
        return None
    method = getattr(request, "method", "GET").upper()
    if method not in _READ_METHODS:
        require_capture_edit(user, item)
    if item.owner_id and user is not None and item.owner_id != user.id:
        note_support_view(user, item.owner_id)          # only support access gets here
    return item


def require_capture(session, user, item_id, *, edit: bool = False):
    """API-style guard: return the capture or raise 404 when the caller can't see
    it. `edit=True` also requires write access, raising 403 for view-only
    support access (D-211)."""
    from fastapi import HTTPException
    item = session.get(TravelItem, item_id)
    if item is None or not can_access_capture(user, item):
        raise HTTPException(status_code=404, detail="item not found")
    if edit:
        require_capture_edit(user, item)
    if item.owner_id and user is not None and item.owner_id != user.id:
        note_support_view(user, item.owner_id)
    return item


# --------------------------------------------------------------------------- journal
# A journal is PERSONAL (SPEC-6 D-134): visible to its author only, never
# trip-shared, and never to an admin, not even under support access (D-211).
# Unlike trips/captures there is deliberately NO "open" (NULL owner) tier for a
# normal user — a diary must not leak between users. An admin sees ownerless
# entries only, to clean them up. The auth-off single-user world (user is None)
# sees everything.
def journal_filter(user):
    """A predicate for `session.query(JournalEntry).filter(...)`."""
    if unrestricted(user):
        return true()
    if user is None:
        return false()
    cond = JournalEntry.owner_id == user.id
    if is_admin(user):
        cond = or_(cond, JournalEntry.owner_id.is_(None))
    return cond


def can_access_journal(user, entry) -> bool:
    if entry is None:
        return False
    if unrestricted(user):
        return True
    if user is None:
        return False
    if entry.owner_id == user.id:
        return True
    return _admin_edits(user, entry.owner_id)          # ownerless only (D-211)


def require_journal(session, user, entry_id):
    """API-style guard: return the entry or raise 404 (own + admin). Existence
    never leaks by id — a stranger's entry 404s exactly like a missing one."""
    from fastapi import HTTPException
    entry = session.get(JournalEntry, entry_id)
    if entry is None or not can_access_journal(user, entry):
        raise HTTPException(status_code=404, detail="journal entry not found")
    return entry
