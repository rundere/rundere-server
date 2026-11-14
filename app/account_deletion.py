"""Run·dere — asking to delete an account, the 7 days, and the daily step
(SPEC-13 §3.3, §3.4 and §3.6, D-242).

- `request()` locks the account at once and stores the request; `cancel()`
  undoes it. Nothing about the account's data changes until the purge.
- `run_due()` is the daily step: it purges every account whose 7 days are up
  (app/account_purge.py), one account at a time, then retries the mailbox step
  and removes folders a failed removal left behind.
- `delete_now()` is the operator's way to skip the wait (the command line).

The lock itself is `users.deletion_due_at`; the places that refuse a locked
account are in app/authn.py, app/oauth.py, app/ingest.py, app/inbox.py and the
web gate in app/main.py.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import account_purge, authn
from app.account_purge import DELETE, HAND
from app.config import settings
from app.email import email_configured, send_email
from app.models import AccountDeletion, AccountEvent, Trip, User

log = logging.getLogger("yonder.account_deletion")

GRACE_DAYS = 7                    # fixed (SPEC-13 §3.1); there is no setting
MAIL_RETRY_DAYS = 30              # how long the mailbox step is retried (§3.5)
PENDING, CANCELLED, DONE = "pending", "cancelled", "done"


class Refused(Exception):
    """The request can't be accepted; the message is safe to show."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def available() -> bool:
    """Deletion exists only where accounts do (SPEC-13 §1.1), and never on a
    demo server (D-198)."""
    from app import demo
    return bool(settings.auth_required) and not demo.enabled()


def pending(user) -> bool:
    return getattr(user, "deletion_due_at", None) is not None


def record(session: Session, user) -> AccountDeletion | None:
    """The live request for this account, if it is waiting to be deleted."""
    return (session.query(AccountDeletion)
            .filter(AccountDeletion.user_id == user.id, AccountDeletion.status == PENDING)
            .order_by(AccountDeletion.requested_at.desc()).first())


# --------------------------------------------------------------------------- guards
def usable_admins(session: Session, *, without: str | None = None) -> int:
    """Admins who can actually administer: active, and not waiting to be
    deleted. `without` leaves one account out of the count."""
    q = session.query(User).filter(User.is_admin.is_(True), User.is_active.is_(True),
                                   User.deletion_due_at.is_(None))
    if without:
        q = q.filter(User.id != without)
    return q.count()


def last_admin(session: Session, user) -> bool:
    """Would deleting (or locking) this account leave the server without an
    active admin? (SPEC-13 §3.1)"""
    return bool(user.is_admin and user.is_active
                and usable_admins(session, without=user.id) == 0)


# --------------------------------------------------------------------------- trips that need a choice
def shared_trips(session: Session, user) -> list[tuple[Trip, list[User]]]:
    """The trips this person owns that another account is on, each with the
    people it could be handed to (SPEC-13 §3.4). A guest with no login doesn't
    count: there is no one to hand the trip to."""
    out = []
    for trip in (session.query(Trip).filter(Trip.owner_id == user.id)
                 .order_by(Trip.start_date, Trip.name).all()):
        others = account_purge.accounts_on(session, trip, leaver_id=user.id)
        if others:
            out.append((trip, others))
    return out


def preview(session: Session, user) -> dict[str, int]:
    """Counts only, for an admin, who can't see the trips (D-211): how many
    pass to other travellers and how many are deleted."""
    owned = session.query(Trip).filter(Trip.owner_id == user.id).count()
    handed = len(shared_trips(session, user))
    return {"handed": handed, "deleted": owned - handed}


def _check_choices(session: Session, user, choices: dict | None) -> dict:
    """Self-service: every shared trip needs an answer, and a hand-over must
    name someone who is on that trip. Anything else in the form is dropped."""
    clean: dict = {}
    for trip, others in shared_trips(session, user):
        choice = (choices or {}).get(trip.id) or {}
        if choice.get("action") == DELETE:
            clean[trip.id] = {"action": DELETE}
        elif choice.get("action") == HAND and choice.get("to") in {u.id for u in others}:
            clean[trip.id] = {"action": HAND, "to": choice["to"]}
        else:
            raise Refused("Choose what happens to each trip that other people are on.")
    return clean


# --------------------------------------------------------------------------- request and cancel
def request(session: Session, user: User, *, actor: User | None, choices: dict | None = None,
            operator: bool = False, base_url: str = "") -> AccountDeletion:
    """Lock `user` now and delete the account in 7 days. `actor` is the person
    themself (self-service) or an admin; `operator` is the command line. An
    admin and the operator make no per-trip choice: every shared trip is
    handed over by the rule of §3.4. Commits, then sends the emails."""
    by_admin = operator or (actor is not None and actor.id != user.id)
    if pending(user):
        raise Refused("This account is already waiting to be deleted.")
    if last_admin(session, user):
        raise Refused("This is the only active admin. Make someone else an admin first.")
    clean = None if by_admin else _check_choices(session, user, choices)
    now = _now()
    due = now + timedelta(days=GRACE_DAYS)
    affected = _trip_notices(session, user, clean)
    # As "sign out everywhere": every session, refresh token, personal API
    # token and OAuth grant ends. Cancelling doesn't bring them back.
    authn.end_sessions(session, user)
    user.share_with_admins, user.support_until = False, None     # support access off
    user.deletion_due_at = due
    rec = AccountDeletion(user_id=user.id, requested_by=actor.id if actor else None,
                          by_admin=by_admin, status=PENDING, requested_at=now, due_at=due,
                          choices=clean)
    session.add(rec)
    session.add(AccountEvent(user_id=user.id, actor_id=actor.id if actor else None,
                             action="deletion_requested"))
    session.commit()
    _mail_requested(user, due, by_admin, affected, base_url)
    return rec


def cancel(session: Session, user: User, *, actor: User | None,
           operator: bool = False, base_url: str = "") -> None:
    """Clear the request. The person cancels their own; an admin's deletion
    is cancelled by an admin or the operator, never by the person (§3.6)."""
    rec = record(session, user)
    if not pending(user) or rec is None:
        raise Refused("This account isn't waiting to be deleted.")
    by_admin = operator or (actor is not None and actor.id != user.id)
    if rec.by_admin and not by_admin:
        raise Refused("An administrator asked for this deletion. Only an administrator "
                      "can cancel it.")
    if by_admin and not operator and not (actor is not None and actor.is_admin):
        raise Refused("Only an administrator can cancel someone else's deletion.")
    affected = _trip_notices(session, user, rec.choices)
    user.deletion_due_at = None
    rec.status, rec.finished_at, rec.choices = CANCELLED, _now(), None
    session.add(AccountEvent(user_id=user.id, actor_id=actor.id if actor else None,
                             action="deletion_cancelled"))
    session.commit()
    _mail_cancelled(user, affected, base_url)


# --------------------------------------------------------------------------- emails
# Fixed text, sent only when the account has an address and SMTP is set up.
# An email never says more than the page does, and links to a page, not to data.
def _send(to: str | None, subject: str, body: str) -> None:
    if not to or not email_configured():
        return
    try:
        send_email(to, subject, body)
    except Exception as ex:                        # mail must never stop a deletion
        log.warning("account deletion: an email couldn't be sent (%s)", type(ex).__name__)


def _day(dt: datetime) -> str:
    return _aware(dt).strftime("%-d %B %Y")


def _trip_notices(session: Session, user, choices: dict | None) -> list[tuple[User, str, str]]:
    """Who to tell about a trip, and what: (person, trip name, 'delete' or
    'hand'). Only for choices the person made themself: an admin makes none,
    and what the rule will do isn't settled until the purge runs."""
    out = []
    for trip, others in shared_trips(session, user):
        choice = (choices or {}).get(trip.id) or {}
        if choice.get("action") == DELETE:
            out += [(u, trip.name, DELETE) for u in others]
        elif choice.get("action") == HAND:
            out += [(u, trip.name, HAND) for u in others if u.id == choice.get("to")]
    return out


def _mail_requested(user, due, by_admin, affected, base_url) -> None:
    link = f"\n\n{base_url}/account/delete" if base_url else ""
    who = "An administrator asked" if by_admin else "You asked"
    how = ("Sign in to take a copy of your data first. Only an administrator can cancel it."
           if by_admin else "To cancel, sign in on the web before then.")
    _send(user.email, "Your Run·dere account will be deleted",
          f"{who} for the Run·dere account {user.username} to be deleted.\n\n"
          f"The account is locked now and will be deleted on {_day(due)}. {how}{link}\n\n"
          "If this wasn't you, sign in and cancel, then change your password.")
    for person, trip_name, what in affected:
        if what == DELETE:
            body = (f"The owner of the trip \"{trip_name}\" is deleting their Run·dere "
                    f"account, and the trip will be deleted with it on {_day(due)}.\n\n"
                    "Until then you can still open it and download its PDF, or ask the "
                    "owner to hand the trip to you instead.")
        else:
            body = (f"The owner of the trip \"{trip_name}\" is deleting their Run·dere "
                    f"account. On {_day(due)} the trip will become yours.")
        _send(person.email, "A Run·dere trip you're on is changing", body)


def _mail_cancelled(user, affected, base_url) -> None:
    _send(user.email, "Your Run·dere account will not be deleted",
          f"The deletion of the Run·dere account {user.username} was cancelled. Nothing "
          "was deleted.\n\nYou were signed out everywhere when it was requested: sign in "
          "to the apps again, and make new API tokens if you used any.")
    for person, trip_name, _ in affected:
        _send(person.email, "A Run·dere trip you're on is staying as it is",
              f"The owner of the trip \"{trip_name}\" is keeping their Run·dere account. "
              "The trip stays as it is.")


def _mail_finished(email: str | None, username: str) -> None:
    # Sent to the address read before the purge. Not retried: the address is gone.
    _send(email, "Your Run·dere account was deleted",
          f"The Run·dere account {username} and its data have been deleted.\n\n"
          "Your name remains on trips other people own, as a guest. Anything in your "
          "own AirTrail or AdventureLog, and the server operator's backups, are outside "
          "what Run·dere can delete.")


# --------------------------------------------------------------------------- the purge, run
def _purge_one(session_factory, user_id: str, *, mailbox: bool = True,
               connect=None) -> account_purge.Result | None:
    """Purge one account in its own transaction, then remove its files, try
    the mailbox and send the last email. Returns None when a guard held it."""
    with session_factory() as session:
        user = session.get(User, user_id)
        if user is None:
            return None
        if last_admin(session, user):
            # Decided again when the purge runs: the other admin may have gone
            # during the 7 days. The account stays locked and pending.
            log.warning("account deletion: held, it would leave the server without an "
                        "active admin")
            return None
        rec = record(session, user)
        res = account_purge.purge(
            session, user.id, choices=rec.choices if rec else None,
            actor_id=rec.requested_by if rec else None,
            by_admin=bool(rec.by_admin) if rec else True)
        session.commit()
    account_purge.remove_files(res)
    _mail_finished(res.email, res.username)
    if res.mail_ids and mailbox:
        retry_mail(session_factory, connect=connect)
    return res


def delete_now(session_factory, user_id: str, *, connect=None) -> account_purge.Result | None:
    """The operator's `--now` (SPEC-13 §3.6): purge without the 7 days."""
    return _purge_one(session_factory, user_id, connect=connect)


def run_due(session_factory, *, now: datetime | None = None, mailbox: bool = True,
            connect=None) -> int:
    """The daily step. Purges every account whose date has come, each in its
    own transaction, so one account that fails doesn't hold up the others and
    is tried again tomorrow. Returns how many accounts were deleted."""
    if not settings.auth_required:
        return 0
    now = now or _now()
    with session_factory() as session:
        due = [u.id for u in session.query(User).filter(User.deletion_due_at.isnot(None)).all()
               if _aware(u.deletion_due_at) <= now]
    done = 0
    for uid in due:
        try:
            if _purge_one(session_factory, uid, mailbox=mailbox, connect=connect) is not None:
                done += 1
        except Exception as ex:
            log.error("account deletion: a purge failed (%s); retried tomorrow",
                      type(ex).__name__)
    return done


# --------------------------------------------------------------------------- the mailbox
def retry_mail(session_factory, *, connect=None) -> int:
    """Move deleted accounts' emails in Rundere/Captured to Trash, by
    Message-ID (D-196). Only the Message-IDs are kept, on a record with no
    user. After 30 days the retry stops and logs a count (SPEC-13 §3.5)."""
    from app import inbox, retention
    with session_factory() as session:
        recs = (session.query(AccountDeletion)
                .filter(AccountDeletion.status == DONE,
                        AccountDeletion.mail_ids.isnot(None)).all())
        todo = {r.id: list(r.mail_ids or []) for r in recs if r.mail_ids}
        ages = {r.id: _aware(r.finished_at) for r in recs}
    if not todo:
        return 0
    handled: set[str] = set()
    if inbox.configured():
        try:
            conn = (connect or inbox._connect)()
            try:
                trash = retention._trash_folder(conn)
                typ, _ = conn.select(inbox._quote(settings.inbox_done_folder))
                if trash and typ == "OK":
                    for mid in dict.fromkeys(m for ids in todo.values() for m in ids):
                        moved = True
                        for uid in retention._uids(conn, "HEADER", "Message-ID",
                                                   retention._imap_quote(mid)):
                            moved = inbox._move(conn, uid, trash) and moved
                        if moved:
                            handled.add(mid)       # moved, or already gone from the folder
            finally:
                inbox._logout(conn)
        except Exception as ex:
            log.warning("account deletion: the mailbox step failed (%s); retried tomorrow",
                        type(ex).__name__)
    else:
        handled = {m for ids in todo.values() for m in ids}      # no mailbox: nothing to move
    cutoff = _now() - timedelta(days=MAIL_RETRY_DAYS)
    with session_factory() as session:
        for rid, ids in todo.items():
            rec = session.get(AccountDeletion, rid)
            left = [m for m in ids if m not in handled]
            if left and ages.get(rid) and ages[rid] < cutoff:
                log.warning("account deletion: gave up moving %d email(s) to Trash after "
                            "%d days", len(left), MAIL_RETRY_DAYS)
                left = []
            rec.mail_ids = left or None
        session.commit()
    return len(handled)


# --------------------------------------------------------------------------- folders left behind
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def remove_orphan_folders(session_factory) -> int:
    """Finish a folder removal that failed: remove a documents or export
    folder whose user no longer exists (SPEC-13 §3.5). Only folders named like
    a user id are looked at, and nothing is removed when the users table is
    empty, which would mean the database isn't the one these folders belong to."""
    import shutil
    from app import archive_jobs, documents
    with session_factory() as session:
        ids = {i for (i,) in session.query(User.id)}
    if not ids:
        return 0
    removed = 0
    for root in (documents._root(), archive_jobs.root()):
        if root is None or not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and _UUID.match(child.name) and child.name not in ids:
                try:
                    shutil.rmtree(child)
                    removed += 1
                except OSError as ex:
                    log.warning("account deletion: a leftover folder couldn't be removed "
                                "(%s)", type(ex).__name__)
    return removed
