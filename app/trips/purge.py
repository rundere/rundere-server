"""Deleting a trip removes everything that only exists for that trip.

Tables link to trips by a plain indexed `trip_id` (no DB foreign keys — house
convention, and SQLite in tests wouldn't cascade anyway), so the delete has to
sweep each one explicitly. Two kinds of child:

- **Trip-only data — deleted:** the timeline (activities and who each is for),
  the traveller roster, sharing, Discover (runs, findings, items, excluded
  places), packing (plans and items), expenses with their shares (SPEC-10) and
  the files filed to it (documents, SPEC-8 D-196 — what was extracted stays).
- **The person's own content — kept, unlinked:** captured booking emails and
  journal entries exist on their own, so they lose the trip link and stay in
  Review / the Journal, where they can be filed again.
"""
from __future__ import annotations

from sqlalchemy.orm import Session


def purge_trip(session: Session, trip) -> None:
    """Delete `trip` and its trip-only data; unlink captures and journal
    entries. The caller commits."""
    from app.models import (DiscoveryExclusion, DiscoveryFinding, DiscoveryItem,
                            DiscoveryRun, JournalEntry, PackingItem, PackingPlan,
                            TravelItem, TripActivity, TripMember, TripTraveler)
    from app.trips import who

    tid = trip.id
    from app import documents, expenses as expenses_svc
    expenses_svc.purge_trip(session, tid)
    documents.purge_trip(session, tid)          # its files go at once (D-196)
    who.clear_activities(session, [a.id for a in session.query(TripActivity.id)
                                   .filter(TripActivity.trip_id == tid).all()])
    for model in (TripActivity, TripTraveler, TripMember,
                  DiscoveryItem, DiscoveryFinding, DiscoveryRun, DiscoveryExclusion,
                  PackingItem, PackingPlan):
        session.query(model).filter(model.trip_id == tid).delete(synchronize_session=False)
    for model in (TravelItem, JournalEntry):
        session.query(model).filter(model.trip_id == tid).update(
            {"trip_id": None}, synchronize_session=False)
    session.delete(trip)


def purge_orphans(session: Session, *, execute: bool = False) -> dict[str, int]:
    """Clean up after trips deleted before purge_trip existed: the same
    trip-only rows whose trip is gone are deleted, captures and journal
    entries pointing at a missing trip are unlinked. Returns a count per table;
    changes nothing unless `execute` (the caller commits)."""
    from app.models import (DiscoveryExclusion, DiscoveryFinding, DiscoveryItem,
                            DiscoveryRun, JournalEntry, PackingItem, PackingPlan,
                            TravelItem, Trip, TripActivity, TripActivityTraveler,
                            TripMember, TripTraveler)

    live = session.query(Trip.id)
    counts: dict[str, int] = {}
    gone_acts = [a for (a,) in session.query(TripActivity.id)
                 .filter(~TripActivity.trip_id.in_(live)).all()]
    counts["trip_activity_travelers"] = (
        session.query(TripActivityTraveler)
        .filter(TripActivityTraveler.activity_id.in_(gone_acts)).count() if gone_acts else 0)
    if execute and gone_acts:
        session.query(TripActivityTraveler).filter(
            TripActivityTraveler.activity_id.in_(gone_acts)).delete(synchronize_session=False)
    for model in (TripActivity, TripTraveler, TripMember,
                  DiscoveryItem, DiscoveryFinding, DiscoveryRun, DiscoveryExclusion,
                  PackingItem, PackingPlan):
        q = session.query(model).filter(~model.trip_id.in_(live))
        counts[model.__tablename__] = q.count()
        if execute and counts[model.__tablename__]:
            q.delete(synchronize_session=False)
    from app.models import TripExpense, TripExpenseShare
    gone_exp = [e for (e,) in session.query(TripExpense.id)
                .filter(~TripExpense.trip_id.in_(live)).all()]
    counts["trip_expense_shares"] = (
        session.query(TripExpenseShare)
        .filter(TripExpenseShare.expense_id.in_(gone_exp)).count() if gone_exp else 0)
    counts["trip_expenses"] = len(gone_exp)
    if execute and gone_exp:
        session.query(TripExpenseShare).filter(
            TripExpenseShare.expense_id.in_(gone_exp)).delete(synchronize_session=False)
        session.query(TripExpense).filter(
            TripExpense.id.in_(gone_exp)).delete(synchronize_session=False)
    for model in (TravelItem, JournalEntry):
        q = session.query(model).filter(model.trip_id.isnot(None),
                                        ~model.trip_id.in_(live))
        counts[f"{model.__tablename__} (unlinked)"] = q.count()
        if execute and counts[f"{model.__tablename__} (unlinked)"]:
            q.update({"trip_id": None}, synchronize_session=False)
    return counts
