"""Run·dere — the account purge (SPEC-13 §3.5, D-242).

One service deletes an account, for self-service, admins and the command line.
`purge()` does all of its database work in the caller's session and commits
nothing, so the caller decides: commit and then call `remove_files()`, or roll
back (the command line's `--dry-run` is this same code, rolled back).

Rules it keeps (SPEC-13 §3.5):

- **Every statement is keyed to the leaver**: their user id, their traveller
  id, or ids read from their own rows. There is no general orphan sweep here
  (`purge_orphans` would touch other people's rows).
- **Order**: trips (§3.4), per-person trip data, own-only content, the capture
  pipeline, profiles and the traveller, sign-in rows, unlinking, and the
  `users` row last, so a purge that stops halfway leaves a pending account
  that the next daily run finishes.
- **No reliance on cascades**: SQLite in the tests doesn't enforce the few
  database foreign keys, so children go explicitly, in an order Postgres
  accepts.
- **Files after the commit**: bytes can't be rolled back, so nothing on disk
  is touched here. `Result.files` and `Result.folders` say what to remove.
- **A NULL traveller is the server's defaults**: every traveller-keyed
  statement is skipped when the account has no traveller, or when another
  account points at the same one.

The registry (app/ownership.py) says what happens to each table; `_STEPS`
below says which step does it, and importing this module fails when a table an
account deletion touches has no step.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import ownership
from app.models import (AccountDeletion, AccountEvent, AiCall, AlertState, ApiKey,
                        ArchiveExport, Artifact, Bag, CommitAttempt, DiscoveryExclusion,
                        DiscoveryFinding, DiscoveryItem, DiscoveryProfile, DiscoveryRun,
                        Document, HomeProfile, InboxAddress, InboxMessage, IngestBatch,
                        InventoryItem, ItemKey, JournalEntry, NotificationTarget,
                        OAuthClient, OAuthCode, OAuthGrant, PackingItem, PackingPlan,
                        PackingProfile, PackingTemplate, PackingTemplateItem,
                        PasswordResetToken, PersonalApiToken, PlaceList, PlaceListMember,
                        RefreshToken, SavedPlace, SavedPlaceFiling, ServerSetting,
                        TravelItem, TravelerIdentity, Trip, TripActivityTraveler,
                        TripBudgetItem, TripBudgetTraveler, TripExpense,
                        TripExpenseComment, TripExpenseShare, TripMember, TripRate,
                        TripTraveler, User, UserIntegration)

log = logging.getLogger("yonder.account_purge")

HAND, DELETE = "hand", "delete"

# Which step below deals with each table. Checked against the registry at
# import: a new table whose deletion treatment isn't "untouched" must be given
# a step here before anything runs (SPEC-13 §1.1, §4.2).
_STEPS: dict[str, str] = {
    # trips (§3.4)
    "trips": "trips", "trip_travelers": "trips", "trip_activities": "trips",
    "trip_activity_travelers": "trips", "trip_expense_shares": "trips",
    "trip_budget_travelers": "trips",
    "trip_members": "surviving_trips", "trip_expenses": "surviving_trips",
    "trip_expense_comments": "surviving_trips", "trip_budget_items": "surviving_trips",
    "trip_rates": "surviving_trips",
    # per-person data on a trip
    "discovery_runs": "per_person", "discovery_findings": "per_person",
    "discovery_items": "per_person", "discovery_exclusions": "per_person",
    "packing_plans": "per_person", "packing_items": "per_person",
    # own-only content
    "journal_entries": "own", "saved_places": "own", "saved_place_filings": "own",
    "place_lists": "own", "place_list_members": "own", "inventory_items": "own",
    "packing_templates": "own", "packing_template_items": "own", "bags": "own",
    "documents": "own",
    # the capture pipeline
    "ingest_batches": "captures", "travel_items": "captures", "item_keys": "captures",
    "commit_attempts": "captures", "artifacts": "captures", "inbox_messages": "captures",
    "api_keys": "captures",
    # profiles and the traveller
    "traveler_identities": "traveller", "home_profiles": "traveller",
    "packing_profiles": "traveller", "discovery_profiles": "traveller",
    # sign-in rows
    "refresh_tokens": "sign_in", "password_reset_tokens": "sign_in",
    "oauth_codes": "sign_in", "oauth_grants": "sign_in", "personal_api_tokens": "sign_in",
    "user_integrations": "sign_in", "inbox_addresses": "sign_in",
    "archive_exports": "sign_in",
    # rows that stay, without the person
    "account_events": "unlink", "ai_calls": "unlink", "server_settings": "unlink",
    "notification_targets": "unlink", "oauth_clients": "unlink", "alert_states": "unlink",
    "account_deletions": "unlink",
    "users": "user",
}


def uncovered() -> list[str]:
    """Tables an account deletion touches that no step here handles."""
    return sorted(t for t, e in ownership.REGISTRY.items()
                  if e.deletion != ownership.UNTOUCHED and t not in _STEPS)


assert not uncovered(), f"app/account_purge.py has no step for: {uncovered()}"


@dataclass
class Result:
    username: str = ""
    email: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    trips_deleted: int = 0
    trips_handed: int = 0
    handed: list[tuple[str, str]] = field(default_factory=list)   # (trip id, new owner id)
    files: list[Path] = field(default_factory=list)       # other people's files on deleted trips
    folders: list[Path] = field(default_factory=list)     # the leaver's own folders
    mail_ids: list[str] = field(default_factory=list)     # Captured mail to move to Trash
    traveller: str = ""                                    # "", "guest" or "deleted"

    def add(self, table: str, n: int | None) -> None:
        if n:
            self.counts[table] = self.counts.get(table, 0) + int(n)


class Refused(Exception):
    """The purge won't run for this account right now."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- who is on a trip
def accounts_on(session: Session, trip: Trip, *, leaver_id: str) -> list[User]:
    """The other accounts on a trip, in the order a hand-over prefers them
    (SPEC-13 §3.4): travellers with a login first, then view-only shares; the
    longest-standing account first. An account that can't use the trip today
    (deactivated, or itself waiting to be deleted) comes after every account
    that can, so a trip isn't handed to someone on their way out while
    somebody else is there. A guest with no login isn't an account."""
    roster = [t for (t,) in session.query(TripTraveler.traveler_id)
              .filter(TripTraveler.trip_id == trip.id)]
    travellers = (session.query(User).filter(User.traveler_id.in_(roster)).all()
                  if roster else [])
    member_ids = [u for (u,) in session.query(TripMember.user_id)
                  .filter(TripMember.trip_id == trip.id)]
    members = session.query(User).filter(User.id.in_(member_ids)).all() if member_ids else []
    seen, out = {leaver_id}, []
    for rank, group in ((0, travellers), (1, members)):
        for u in group:
            if u.id in seen:
                continue
            seen.add(u.id)
            usable = u.is_active and u.deletion_due_at is None
            out.append(((0 if usable else 1, rank, _aware(u.created_at) or _now(), u.id), u))
    return [u for _, u in sorted(out, key=lambda pair: pair[0])]


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def decide(session: Session, trip: Trip, choices: dict | None, *,
           leaver_id: str) -> tuple[str, User | None]:
    """What happens to one trip the leaver owns, decided when the purge runs
    (SPEC-13 §3.4), because the roster can change during the 7 days:

    - marked "delete": deleted;
    - other accounts on it: handed to the chosen person if they are still on
      it, else to the first by the rule above. A trip with no recorded choice
      is handed over too, never deleted by default;
    - nobody else with an account: deleted with the account."""
    choice = (choices or {}).get(trip.id) or {}
    if choice.get("action") == DELETE:
        return DELETE, None
    others = accounts_on(session, trip, leaver_id=leaver_id)
    if not others:
        return DELETE, None
    wanted = choice.get("to") if choice.get("action") == HAND else None
    for u in others:
        if u.id == wanted:
            return HAND, u
    return HAND, others[0]


# --------------------------------------------------------------------------- the purge
def purge(session: Session, user_id: str, *, choices: dict | None = None,
          actor_id: str | None = None, by_admin: bool = False) -> Result:
    """Delete the account `user_id` and everything of theirs. The caller
    commits (then calls `remove_files`) or rolls back. `actor_id` and
    `by_admin` only shape the one audit row that remains."""
    user = session.get(User, user_id)
    if user is None:
        raise Refused("no such account")
    uid = user.id
    res = Result(username=user.username, email=user.email)

    # The leaver's traveller, unless it is NULL (the server's defaults live
    # under NULL) or another account points at the same row.
    tid = user.traveler_id
    if tid is not None and session.query(User).filter(
            User.traveler_id == tid, User.id != uid).count():
        tid = None

    doomed_guests = _trips(session, uid, tid, choices, res)
    _surviving_trips(session, uid, tid, res)
    _per_person(session, uid, res)
    _own(session, uid, res)
    _captures(session, uid, res)
    _traveller(session, tid, doomed_guests, res)
    _sign_in(session, uid, res)
    _unlink(session, uid, res)

    # One row stays, tied to no user: an account was deleted, when, and
    # whether by the person or by which admin (SPEC-13 §3.5).
    session.add(AccountEvent(user_id=None, actor_id=actor_id if by_admin else None,
                             action="account_deleted",
                             detail="by an admin" if by_admin else "by the person"))
    session.flush()
    res.add("users", session.query(User).filter(User.id == uid)
            .delete(synchronize_session=False))
    session.flush()
    session.expire_all()
    return res


def _delete(session: Session, res: Result, model, *where) -> int:
    n = session.query(model).filter(*where).delete(synchronize_session=False)
    res.add(model.__tablename__, n)
    return n or 0


# --- trips (§3.4) ---------------------------------------------------------------------
def _trips(session: Session, uid: str, tid: int | None, choices: dict | None,
           res: Result) -> set[int]:
    """Delete or hand over each trip the leaver owns. Returns the guests who
    were on the deleted trips, for `_traveller` to look at afterwards."""
    from app.trips.purge import purge_trip
    doomed_guests: set[int] = set()
    for trip in session.query(Trip).filter(Trip.owner_id == uid).order_by(Trip.id).all():
        action, heir = decide(session, trip, choices, leaver_id=uid)
        if action == HAND:
            trip.owner_id = heir.id
            # An owner needs no view-only share of their own trip.
            _delete(session, res, TripMember, TripMember.trip_id == trip.id,
                    TripMember.user_id == heir.id)
            res.trips_handed += 1
            res.handed.append((trip.id, heir.id))
            continue
        doomed_guests.update(t for (t,) in session.query(TripTraveler.traveler_id)
                             .filter(TripTraveler.trip_id == trip.id) if t != tid)
        # The trip's files go with it (D-196), other people's included, as on
        # any trip delete. Their rows are deleted here so that `purge_trip`
        # finds none and removes no bytes before the commit.
        for doc in session.query(Document).filter(Document.trip_id == trip.id).all():
            if doc.owner_id != uid:                # the leaver's whole folder goes anyway
                from app import documents
                p = documents.path_for(doc)
                if p is not None:
                    res.files.append(p)
            session.delete(doc)
            res.add("documents", 1)
        session.flush()
        purge_trip(session, trip)
        res.trips_deleted += 1
    session.flush()
    res.add("trips", res.trips_deleted)
    return doomed_guests


def _surviving_trips(session: Session, uid: str, tid: int | None, res: Result) -> None:
    """What the leaver left on trips that go on: other people's, and the ones
    just handed over."""
    _delete(session, res, TripMember, TripMember.user_id == uid)
    _delete(session, res, TripExpenseComment, TripExpenseComment.author_id == uid)
    # A planned cost the leaver entered for themself alone means nothing to
    # anyone else; one that others are on stays, with no link to the leaver.
    for b in session.query(TripBudgetItem).filter(TripBudgetItem.owner_id == uid).all():
        people = {t for (t,) in session.query(TripBudgetTraveler.traveler_id)
                  .filter(TripBudgetTraveler.budget_item_id == b.id)}
        if tid is not None and people == {tid}:
            from app import expenses
            expenses.delete_budget(session, b)
            res.add("trip_budget_items", 1)
        else:
            b.owner_id = None
    session.flush()
    # The group's balances depend on these rows, so they stay (§4.3).
    session.query(TripExpense).filter(TripExpense.owner_id == uid).update(
        {"owner_id": None}, synchronize_session=False)
    session.query(TripRate).filter(TripRate.fetched_by == uid).update(
        {"fetched_by": None}, synchronize_session=False)


# --- per-person data on a trip (§1.6) -----------------------------------------------
def _per_person(session: Session, uid: str, res: Result) -> None:
    for model in (DiscoveryItem, DiscoveryFinding, DiscoveryRun, DiscoveryExclusion,
                  PackingItem, PackingPlan):
        _delete(session, res, model, model.user_id == uid)


# --- own-only content (§1.4) -----------------------------------------------------------
def _own(session: Session, uid: str, res: Result) -> None:
    from app import archive_jobs, documents
    _delete(session, res, JournalEntry, JournalEntry.owner_id == uid)
    for model in (PlaceListMember, PlaceList, SavedPlaceFiling, SavedPlace):
        _delete(session, res, model, model.owner_id == uid)
    templates = [t for (t,) in session.query(PackingTemplate.id)
                 .filter(PackingTemplate.user_id == uid)]
    if templates:
        _delete(session, res, PackingTemplateItem,
                PackingTemplateItem.template_id.in_(templates))
    for model in (PackingTemplate, InventoryItem, Bag):
        _delete(session, res, model, model.user_id == uid)
    _delete(session, res, Document, Document.owner_id == uid)
    root = documents._root()
    if root is not None:
        res.folders.append(root / uid)             # D-153, D-196: the whole folder
    res.folders.append(archive_jobs.folder_for(uid))


# --- the capture pipeline (§1.7) -------------------------------------------------------
def _captures(session: Session, uid: str, res: Result) -> None:
    items = session.query(TravelItem.id, TravelItem.artifact_sha256).filter(
        TravelItem.owner_id == uid).all()
    item_ids = [i for i, _ in items]
    batches = session.query(IngestBatch).filter(IngestBatch.user_id == uid).all()
    batch_ids = [b.id for b in batches]
    hashes = {h for _, h in items if h} | {b.artifact_sha256 for b in batches
                                           if b.artifact_sha256}
    # The leaver's forwarded emails still in the hoster's Captured folder,
    # found by Message-ID as the retention job finds them (D-196).
    for b in batches:
        p = b.payload or {}
        if p.get("capture_channel") == "email" and p.get("message_id"):
            res.mail_ids.append(p["message_id"])
    res.mail_ids = list(dict.fromkeys(res.mail_ids))

    if item_ids:
        # A capture chain (§4.3): a later capture of someone else's must not
        # keep pointing at one of the leaver's items. Captures are per owner,
        # so none should; this clears the link if one ever does.
        gone = set(item_ids)
        for other in session.query(TravelItem).filter(
                TravelItem.revision.isnot(None),
                or_(TravelItem.owner_id != uid, TravelItem.owner_id.is_(None))).all():
            rev = other.revision
            if isinstance(rev, dict) and rev.get("prior_item_id") in gone:
                other.revision = {**rev, "prior_item_id": None}
        _delete(session, res, ItemKey, ItemKey.item_id.in_(item_ids))
        _delete(session, res, CommitAttempt, CommitAttempt.item_id.in_(item_ids))
        _delete(session, res, TravelItem, TravelItem.id.in_(item_ids))
    if batch_ids:
        # An item of someone else's cut from one of the leaver's batches keeps
        # its own content and loses the pointer (a database foreign key).
        session.query(TravelItem).filter(TravelItem.batch_id.in_(batch_ids)).update(
            {"batch_id": None}, synchronize_session=False)
        # No tombstone: with no owner there is nobody to show it to (§3.5).
        _delete(session, res, IngestBatch, IngestBatch.id.in_(batch_ids))
    _delete(session, res, InboxMessage, InboxMessage.user_id == uid)
    session.flush()
    for sha in sorted(hashes):                     # only hashes the leaver's rows named
        if (session.query(IngestBatch).filter(IngestBatch.artifact_sha256 == sha).count()
                or session.query(TravelItem)
                .filter(TravelItem.artifact_sha256 == sha).count()):
            continue
        _delete(session, res, Artifact, Artifact.sha256 == sha)
    # Ingest keys. One that a batch of another owner still points at is
    # revoked and unlinked instead (a database foreign key holds it).
    for key in session.query(ApiKey).filter(ApiKey.user_id == uid).all():
        if session.query(IngestBatch).filter(IngestBatch.api_key_id == key.id).count():
            key.user_id = None
            key.revoked_at = key.revoked_at or _now()
        else:
            session.delete(key)
            res.add("api_keys", 1)
    session.flush()


# --- profiles and the traveller (§1.3, §3.5) -----------------------------------------
def _referenced(session: Session, traveler_id: int) -> bool:
    """Does any trip still name this traveller: on a roster, on a timeline
    item, as a payer, on a share or on a planned cost?"""
    return bool(
        session.query(TripTraveler).filter(TripTraveler.traveler_id == traveler_id).count()
        or session.query(TripActivityTraveler)
        .filter(TripActivityTraveler.traveler_id == traveler_id).count()
        or session.query(TripExpense).filter(TripExpense.payer_id == traveler_id).count()
        or session.query(TripExpenseShare)
        .filter(TripExpenseShare.traveler_id == traveler_id).count()
        or session.query(TripBudgetTraveler)
        .filter(TripBudgetTraveler.traveler_id == traveler_id).count())


def _traveller(session: Session, tid: int | None, doomed_guests: set[int],
               res: Result) -> None:
    session.flush()
    if tid is not None:
        for model in (HomeProfile, PackingProfile, DiscoveryProfile):
            _delete(session, res, model, model.traveler_id == tid)
        row = session.get(TravelerIdentity, tid)
        if row is not None and _referenced(session, tid):
            # Other people's timelines still say who flew: the name stays, as a
            # guest with no account behind it, and nothing else does.
            row.kind = "guest"
            row.aliases = []
            row.airtrail_user_id = None
            row.nationality = row.base_currency = row.timezone = row.notes = None
            res.traveller = "guest"
        elif row is not None:
            session.delete(row)
            res.add("traveler_identities", 1)
            res.traveller = "deleted"
    # Guests who were only on the leaver's deleted trips, are on no other trip
    # and are linked to no account. The ids come from the leaver's own trips.
    for gid in sorted(doomed_guests):
        row = session.get(TravelerIdentity, gid)
        if row is None or row.generic or row.kind == "self":
            continue
        if session.query(User).filter(User.traveler_id == gid).count():
            continue
        if _referenced(session, gid):
            continue
        for model in (HomeProfile, PackingProfile, DiscoveryProfile):
            _delete(session, res, model, model.traveler_id == gid)
        session.delete(row)
        res.add("traveler_identities", 1)
    session.flush()


# --- sign-in rows (§1.2) ---------------------------------------------------------------
def _sign_in(session: Session, uid: str, res: Result) -> None:
    for model in (RefreshToken, PasswordResetToken, OAuthCode, OAuthGrant,
                  PersonalApiToken, UserIntegration, InboxAddress, ArchiveExport):
        _delete(session, res, model, model.user_id == uid)   # D-151: ciphertext included


# --- rows that stay, without the person (§1.2, §1.8) ---------------------------------
def _unlink(session: Session, uid: str, res: Result) -> None:
    session.query(AccountEvent).filter(AccountEvent.user_id == uid).update(
        {"user_id": None, "detail": None}, synchronize_session=False)
    session.query(AccountEvent).filter(AccountEvent.actor_id == uid).update(
        {"actor_id": None}, synchronize_session=False)
    session.query(AiCall).filter(AiCall.user_id == uid).update(
        {"user_id": None}, synchronize_session=False)
    session.query(ServerSetting).filter(ServerSetting.updated_by == uid).update(
        {"updated_by": None}, synchronize_session=False)
    for model in (NotificationTarget, OAuthClient):
        session.query(model).filter(model.created_by == uid).update(
            {"created_by": None}, synchronize_session=False)
    # The health of the person's own connections: `integration:<user id>:<kind>`.
    prefix = f"integration:{uid}:"
    for row in session.query(AlertState).filter(AlertState.key.like(prefix + "%")).all():
        if row.key.startswith(prefix):             # LIKE's wildcards can't widen it
            session.delete(row)
            res.add("alert_states", 1)
    # Deletion records. The leaver's own stay without them, so the mailbox
    # step can be retried; in other people's, an admin who asked or an heir
    # who was named is cleared (the purge decides again anyway, §3.4).
    for rec in session.query(AccountDeletion).filter(
            AccountDeletion.user_id == uid).all():
        pending = rec.status == "pending"
        rec.user_id, rec.choices = None, None
        if pending:
            rec.status, rec.finished_at = "done", _now()
            rec.mail_ids = list(res.mail_ids) or None
    session.query(AccountDeletion).filter(AccountDeletion.requested_by == uid).update(
        {"requested_by": None}, synchronize_session=False)
    session.flush()
    for rec in session.query(AccountDeletion).filter(
            AccountDeletion.status == "pending").all():
        if not rec.choices:
            continue
        kept = {t: ({k: v for k, v in c.items() if k != "to"}
                    if isinstance(c, dict) and c.get("to") == uid else c)
                for t, c in (rec.choices or {}).items()}
        if kept != rec.choices:
            rec.choices = kept
    session.flush()


# --------------------------------------------------------------------------- after the commit
def remove_files(res: Result) -> None:
    """Remove what the purge left on disk. Call it only after the commit: a
    removal that fails is finished by the daily job, which removes any
    documents or export folder whose user no longer exists."""
    import shutil
    for p in res.files:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as ex:
            log.warning("account purge: a file couldn't be removed (%s)", type(ex).__name__)
    for folder in res.folders:
        try:
            shutil.rmtree(folder)
        except FileNotFoundError:
            pass
        except OSError as ex:
            log.warning("account purge: a folder couldn't be removed (%s)",
                        type(ex).__name__)
