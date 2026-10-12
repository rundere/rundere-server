"""Yonder 4.x — Travel Journal service (SPEC-6).

The one place journal create / list / update / delete / duplicate / attach logic
lives, so the JSON API (and a future web compose box, and MCP) never diverge.
Entries are authored, personal, and dated — deliberately outside the
capture→extract→resolve→commit pipeline (D-131). Scoping is own-only (D-134),
enforced via app/scope.py; callers pass the resolved user (None = auth-off
single-user world).

Framework-agnostic where it can be: bad input raises ValueError (the API maps it
to 400); trip/entry access is delegated to app/scope.py, which raises the 404/403
the rest of the API already uses.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app import scope
from app.models import JournalEntry

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UNSET = object()  # "argument not supplied" sentinel, distinct from an explicit None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_entry_date(value: str | None) -> str:
    """`entry_date` is the day the entry is ABOUT (D-132) — defaults to today
    (UTC) when unset, and must otherwise be a bare ISO date."""
    v = (value or "").strip()
    if not v:
        return _now().date().isoformat()
    if not _ISO_DATE.match(v):
        raise ValueError("entry_date must be an ISO date (YYYY-MM-DD)")
    return v


def _ord(iso: str | None) -> int | None:
    try:
        return date.fromisoformat((iso or "")[:10]).toordinal()
    except ValueError:
        return None


def _trip_rank(trip, on_date: str) -> tuple:
    """Sort key for a trip against an entry date (YONDER-188): distance in days
    from the trip's range to the date (0 when it falls inside), then latest
    start, then shortest span, then name/id so the order never wobbles. A trip
    with no start date has no range to compare, so it sorts last. A trip with
    no end date is treated as a single day (its start)."""
    d = _ord(on_date)
    start = _ord(trip.get("start_date"))
    if d is None or start is None:
        return (1, 0, 0, 0, trip.get("name") or "", trip.get("id") or "")
    end = _ord(trip.get("end_date"))
    end = start if end is None or end < start else end
    dist = 0 if start <= d <= end else (start - d if d < start else d - end)
    return (0, dist, -start, end - start, trip.get("name") or "", trip.get("id") or "")


def rank_trips(trips: list[dict], on_date: str | None) -> list[dict]:
    """`trips` (dicts with id/name/start_date/end_date) nearest-to-`on_date`
    first; shared so web, API and MCP can use one rule."""
    on = normalize_entry_date(on_date) if on_date else _now().date().isoformat()
    return sorted(trips, key=lambda t: _trip_rank(t, on))


def default_trip_id(trips: list[dict], on_date: str | None) -> str | None:
    """The trip whose date range covers `on_date` (latest start, then shortest,
    wins an overlap), or None when none does — a note about a day outside every
    trip stays unattached."""
    ranked = rank_trips(trips, on_date)
    if not ranked:
        return None
    on = normalize_entry_date(on_date) if on_date else _now().date().isoformat()
    top = ranked[0]
    return top["id"] if _trip_rank(top, on)[:2] == (0, 0) else None


def parse_captured_at(value) -> datetime:
    """The minimum time-context stamp (D-142). Accepts a datetime, an ISO-8601
    string (a trailing 'Z' allowed), or None → now. A naive datetime is treated
    as UTC. Unparseable input falls back to now rather than failing a save —
    losing the exact stamp must never lose the note."""
    if isinstance(value, datetime):
        dt = value
    elif value:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return _now()
    else:
        return _now()
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _owner_id(user):
    return getattr(user, "id", None)


def _accuracy(value) -> float | None:
    """GPS accuracy in metres (0 or more), or None. Same rule as an expense's."""
    if value is None:
        return None
    v = float(value)
    if not 0 <= v < 1e7:
        raise ValueError("place_accuracy_m must be 0 or more")
    return v


def create_entry(session: Session, user, *, entry_date=None, captured_at=None,
                 body: str = "", trip_id: str | None = None, values: dict | None = None,
                 place_lat: float | None = None, place_lon: float | None = None,
                 place_label: str | None = None, place_accuracy_m: float | None = None,
                 source: str = "api") -> JournalEntry:
    """Create one entry owned by `user`. If `trip_id` is given it must be a trip
    the user can see (else 404) — attaching is never a way to discover trips."""
    if trip_id:
        scope.require_trip(session, user, trip_id)  # view access; 404 if not visible
    entry = JournalEntry(
        owner_id=_owner_id(user),
        entry_date=normalize_entry_date(entry_date),
        captured_at=parse_captured_at(captured_at),
        trip_id=trip_id or None,
        body=body or "",
        values=dict(values or {}),
        place_lat=place_lat,
        place_lon=place_lon,
        place_label=(place_label or None),
        place_accuracy_m=_accuracy(place_accuracy_m),
        source=(source or "api")[:16],
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


def list_entries(session: Session, user, *, trip_id: str | None = None,
                 date_from: str | None = None, date_to: str | None = None,
                 limit: int = 200) -> list[JournalEntry]:
    """The user's own entries, newest day first. `trip_id` filters to one trip;
    `date_from`/`date_to` bound `entry_date` (ISO strings sort correctly)."""
    q = session.query(JournalEntry).filter(scope.journal_filter(user))
    if trip_id is not None:
        q = q.filter(JournalEntry.trip_id == trip_id)
    if date_from:
        q = q.filter(JournalEntry.entry_date >= date_from)
    if date_to:
        q = q.filter(JournalEntry.entry_date <= date_to)
    q = q.order_by(JournalEntry.entry_date.desc(), JournalEntry.captured_at.desc())
    return q.limit(max(1, min(1000, limit))).all()


def get_entry(session: Session, user, entry_id: str) -> JournalEntry:
    return scope.require_journal(session, user, entry_id)


def update_entry(session: Session, user, entry_id: str, *, entry_date=_UNSET,
                 body=_UNSET, trip_id=_UNSET, values=_UNSET, place_lat=_UNSET,
                 place_lon=_UNSET, place_label=_UNSET,
                 place_accuracy_m=_UNSET) -> JournalEntry:
    """Edit fields the author supplied. `trip_id=None` DETACHES; a non-null
    trip_id re-attaches (validated). Entries are freely mutable by their author
    (D-133), unlike the immutable commit records elsewhere in Yonder."""
    entry = scope.require_journal(session, user, entry_id)
    if entry_date is not _UNSET:
        entry.entry_date = normalize_entry_date(entry_date)
    if body is not _UNSET:
        entry.body = body or ""
    if trip_id is not _UNSET:
        if trip_id:
            scope.require_trip(session, user, trip_id)
        entry.trip_id = trip_id or None
    if values is not _UNSET:
        entry.values = dict(values or {})
    if place_lat is not _UNSET:
        entry.place_lat = place_lat
    if place_lon is not _UNSET:
        entry.place_lon = place_lon
    if place_label is not _UNSET:
        entry.place_label = place_label or None
    if place_accuracy_m is not _UNSET:
        entry.place_accuracy_m = _accuracy(place_accuracy_m)
    session.commit()
    session.refresh(entry)
    return entry


def attach_trip(session: Session, user, entry_id: str,
                trip_id: str | None) -> JournalEntry:
    """Attach the entry to exactly one trip, or detach with None (D-133)."""
    return update_entry(session, user, entry_id, trip_id=trip_id)


def duplicate_entry(session: Session, user, entry_id: str, *,
                    trip_id=_UNSET) -> JournalEntry:
    """Fork an entry so the same note can apply to another trip (D-133): one
    entry belongs to exactly one trip, so spanning trips means copies. The copy
    is independent (editing/deleting one never touches the other) and records
    `duplicated_from`. `trip_id` overrides the copy's trip (None = unattached);
    omitted keeps the original's trip."""
    orig = scope.require_journal(session, user, entry_id)
    new_trip = orig.trip_id if trip_id is _UNSET else trip_id
    if new_trip:
        scope.require_trip(session, user, new_trip)
    copy = JournalEntry(
        owner_id=orig.owner_id,
        entry_date=orig.entry_date,
        captured_at=parse_captured_at(None),  # the fork happens now
        trip_id=new_trip or None,
        body=orig.body,
        values=dict(orig.values or {}),
        place_lat=orig.place_lat,
        place_lon=orig.place_lon,
        place_label=orig.place_label,
        place_accuracy_m=orig.place_accuracy_m,
        source=orig.source,
        duplicated_from=orig.id,
    )
    session.add(copy)
    session.commit()
    session.refresh(copy)
    return copy


def delete_entry(session: Session, user, entry_id: str) -> None:
    entry = scope.require_journal(session, user, entry_id)
    session.delete(entry)
    session.commit()
