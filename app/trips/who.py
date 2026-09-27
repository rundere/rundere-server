"""Who each timeline activity is for (4.1, migration 0025).

On a trip with several travellers, each activity can name the ones it belongs
to — two people on different flights, a hotel booked for part of the group.
The rule everything here keeps:

- **No rows = everyone on the trip.** That's the default, so a solo trip, a
  shared dinner or any pre-4.1 activity never needs tagging.
- **Only the trip's own travellers.** Ids not on the trip's roster are dropped,
  and choosing the whole roster is stored as "everyone" (no rows), so adding a
  traveller later includes them in the shared activities automatically.

Shared by the web routes, /api/v1 and the commit stage.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.models import TripActivityTraveler, TripTraveler


def roster_ids(session: Session, trip_id: str) -> list[int]:
    return [r.traveler_id for r in
            session.query(TripTraveler).filter(TripTraveler.trip_id == trip_id).all()]


def activity_map(session: Session, activity_ids: Iterable[str]) -> dict[str, list[int]]:
    """activity id -> the traveller ids it is tagged with (absent = everyone)."""
    ids = list(activity_ids)
    out: dict[str, list[int]] = {}
    if not ids:
        return out
    for row in (session.query(TripActivityTraveler)
                .filter(TripActivityTraveler.activity_id.in_(ids)).all()):
        out.setdefault(row.activity_id, []).append(row.traveler_id)
    for v in out.values():
        v.sort()
    return out


def normalize(chosen: Iterable[int | str], roster: Iterable[int]) -> list[int]:
    """The ids to store for a choice: roster members only; the whole roster
    (or nothing) means everyone, stored as []."""
    roster_set = set(roster)
    picked: set[int] = set()
    for c in chosen:
        try:
            tid = int(c)
        except (TypeError, ValueError):
            continue
        if tid in roster_set:
            picked.add(tid)
    if not picked or picked == roster_set:
        return []
    return sorted(picked)


def set_travelers(session: Session, activity_id: str, trip_id: str,
                  chosen: Iterable[int | str]) -> list[int]:
    """Replace an activity's tags with `chosen` (normalized). Returns what was
    stored ([] = everyone). Caller commits."""
    ids = normalize(chosen, roster_ids(session, trip_id))
    clear_activity(session, activity_id)
    for tid in ids:
        session.add(TripActivityTraveler(activity_id=activity_id, traveler_id=tid))
    session.flush()                 # sessions run with autoflush off (app/db.py)
    return ids


def clear_activity(session: Session, activity_id: str) -> None:
    session.query(TripActivityTraveler).filter(
        TripActivityTraveler.activity_id == activity_id).delete(synchronize_session=False)


def clear_activities(session: Session, activity_ids: Iterable[str]) -> None:
    ids = list(activity_ids)
    if ids:
        session.query(TripActivityTraveler).filter(
            TripActivityTraveler.activity_id.in_(ids)).delete(synchronize_session=False)


def remove_from_trip(session: Session, trip_id: str, traveler_id: int) -> None:
    """A traveller left the trip: drop their tags on its activities. An activity
    left with no tags then reads as everyone, which is the remaining roster."""
    from app.models import TripActivity
    act_ids = [a.id for a in session.query(TripActivity.id)
               .filter(TripActivity.trip_id == trip_id).all()]
    if act_ids:
        session.query(TripActivityTraveler).filter(
            TripActivityTraveler.activity_id.in_(act_ids),
            TripActivityTraveler.traveler_id == traveler_id,
        ).delete(synchronize_session=False)
