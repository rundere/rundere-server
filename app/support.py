"""Support access (SPEC-8 D-211, YONDER-336): a person lets admins look.

Admins have no standing access to anyone's data. A person may turn on "Let
admins see my trips and Inbox" for 24 hours, 7 days or until they turn it off.
While it's live, admins can VIEW that person's trips and Inbox captures, read-only
(`app/scope.py` holds the rule); never their journal, credentials, saved places or
profiles.

Everything about a grant is visible to the person: turning it on or off and its
expiry are recorded in the account log, they're emailed when they turn it on, and
the first time each day an admin opens something under it is logged
(`scope.note_support_view`) and shown on their account page.

A grant ends on its own at `support_until`; `scope.support_live` checks the time,
so no job is needed for it to stop. The daily retention sweep then records the
expiry (`expire_due`).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import AccountEvent, User

log = logging.getLogger(__name__)

# How long a grant lasts; None = until the person turns it off.
DURATIONS: dict[str, timedelta | None] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "standing": None,
}
LABELS = {"24h": "for 24 hours", "7d": "for 7 days", "standing": "until you turn it off"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(session: Session, user: User, action: str, detail: str = "",
           actor_id: str | None = None) -> None:
    session.add(AccountEvent(user_id=user.id, actor_id=actor_id or user.id, action=action,
                             detail=detail or None))


def grant(session: Session, user: User, duration: str = "standing") -> None:
    """Turn support access on (or extend it) for `duration`. Audited, the notice
    cleared, and the person emailed if they have an address. Commits."""
    if duration not in DURATIONS:
        raise ValueError("duration is 24h, 7d or standing")
    span = DURATIONS[duration]
    user.share_with_admins = True
    user.support_until = (_now() + span) if span is not None else None
    user.support_notice = False
    _audit(session, user, "sharing_on", LABELS[duration])
    session.commit()
    if user.email:
        from app import email as mail
        mail.send_email(user.email, "You let admins see your Run·dere trips",
                   "You turned on support access for your Run·dere account "
                   f"({user.username}) {LABELS[duration]}.\n\n"
                   "Admins can now view your trips and Inbox, read-only. They can't "
                   "change anything, and they never see your journal. Each day an "
                   "admin looks is listed under Settings → Account, where you can "
                   "turn it off at any time.")


def revoke(session: Session, user: User) -> None:
    """Turn support access off. Audited only when it was on. Commits."""
    was_on = bool(user.share_with_admins)
    user.share_with_admins = False
    user.support_until = None
    user.support_notice = False
    if was_on:
        _audit(session, user, "sharing_off")
    session.commit()


def until(user: User) -> datetime | None:
    """When a live grant ends (aware UTC), or None for a standing one."""
    u = user.support_until
    if u is not None and u.tzinfo is None:
        u = u.replace(tzinfo=timezone.utc)
    return u


def expire_due(session: Session) -> int:
    """Record every grant whose end time has passed, and switch it off. Run by
    the daily sweep; the grant has already stopped working by then. Commits."""
    now = _now()
    due = (session.query(User)
           .filter(User.share_with_admins.is_(True), User.support_until.isnot(None),
                   User.support_until <= now).all())
    for u in due:
        u.share_with_admins = False
        u.support_until = None
        _audit(session, u, "support_expired", actor_id=None)
    if due:
        session.commit()
    return len(due)


def send_notices(session: Session) -> int:
    """Email, once, each person whose 4.1 sharing became read-only support access
    (the `support_notice` flag the 0046 migration set). The web banner covers
    people without an email. Run by the daily sweep; safe to repeat."""
    from app import email as mail
    if not mail.email_configured():
        return 0
    told = {uid for (uid,) in session.query(AccountEvent.user_id)
            .filter(AccountEvent.action == "support_notice_emailed")}
    sent = 0
    for u in (session.query(User).filter(User.support_notice.is_(True), User.email.isnot(None))):
        if u.id in told:
            continue
        mail.send_email(u.email, "Admins can now only look at your Run·dere trips",
                   f"Your Run·dere account ({u.username}) shared its data with admins. "
                   "From this version that sharing is read-only support access: admins "
                   "can view your trips and Inbox, but can't change them, and never see "
                   "your journal.\n\nYou can turn it off, or limit it to 24 hours or "
                   "7 days, under Settings → Account.")
        _audit(session, u, "support_notice_emailed", actor_id=None)
        sent += 1
    if sent:
        session.commit()
    return sent


def recent_views(session: Session, user: User, days: int = 30) -> list[dict]:
    """Who looked under support access in the last `days`, newest first:
    `[{"admin": username, "at": datetime}]`."""
    since = _now() - timedelta(days=days)
    rows = (session.query(AccountEvent)
            .filter(AccountEvent.user_id == user.id, AccountEvent.action == "support_viewed",
                    AccountEvent.at >= since)
            .order_by(AccountEvent.at.desc()).limit(50).all())
    names = {u.id: u.username for u in session.query(User).filter(
        User.id.in_({r.actor_id for r in rows if r.actor_id}))} if rows else {}
    return [{"admin": names.get(r.actor_id, "an admin"), "at": r.at} for r in rows]
