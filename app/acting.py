"""Whose profile a profile page shows (4.1): an admin acting for a traveller.

The home, travel, packing and discovery profile pages show the viewer's OWN
profile. An admin may also pick another traveller — `?for=<traveler_id>` —
to set things up for someone:
- a traveller with no account yet (someone the admin plans trips for), or
- a traveller whose user SHARES with admins (User.share_with_admins).
Never a user who opted out: their profiles are theirs alone. Non-admins can't
act for anyone; a `for` they aren't allowed is ignored (they see their own).

Pages keep the choice by carrying `acting.qs` ("?for=5" or "") on their links
and form/HTMX URLs.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app import scope
from app.models import TravelerIdentity, User


@dataclass
class Acting:
    traveler: TravelerIdentity | None      # whose profile the page shows
    for_id: int | None = None              # set when acting for someone else
    choices: list[dict] = field(default_factory=list)   # admin selector options
    owner_mode: bool = False               # auth off: the deployment owner's

    @property
    def qs(self) -> str:
        return f"?for={self.for_id}" if self.for_id is not None else ""

    @property
    def is_other(self) -> bool:
        return self.for_id is not None


def _self_identity(session: Session) -> TravelerIdentity | None:
    return (session.query(TravelerIdentity).filter(TravelerIdentity.kind == "self")
            .order_by(TravelerIdentity.id).first())


def eligible_travelers(session: Session, admin: User) -> list[TravelerIdentity]:
    """Travellers an admin may manage profiles for: their own, ones with no
    account, and ones whose user shares with admins. Generic placeholder guests
    carry no profile (D-100), so they're left out."""
    linked = {u.traveler_id: u for u in session.query(User)
              .filter(User.traveler_id.isnot(None)).all()}
    out = []
    for t in (session.query(TravelerIdentity).filter(TravelerIdentity.generic.is_(False))
              .order_by(TravelerIdentity.display_name).all()):
        u = linked.get(t.id)
        if u is None or u.id == admin.id or u.share_with_admins:
            out.append(t)
    return out


def acting_for(session: Session, request) -> Acting:
    user = scope.user_of(request)
    if user is None:                                   # auth off: single-user
        return Acting(traveler=_self_identity(session), owner_mode=True)
    own = session.get(TravelerIdentity, user.traveler_id) if user.traveler_id else None
    if not scope.is_admin(user):
        return Acting(traveler=own)
    choices = eligible_travelers(session, user)
    want = (request.query_params.get("for") or "").strip()
    chosen = None
    if want.isdigit():
        chosen = next((t for t in choices if t.id == int(want)), None)
    is_other = chosen is not None and (own is None or chosen.id != own.id)
    opts = [{"id": t.id, "name": t.display_name,
             "account": "you" if own is not None and t.id == own.id else None,
             "selected": (t.id == chosen.id) if is_other else (own is not None and t.id == own.id)}
            for t in choices]
    return Acting(traveler=chosen if is_other else own,
                  for_id=chosen.id if is_other else None, choices=opts)


def audit_if_other(session: Session, request, acting: Acting, what: str) -> None:
    """Record an admin editing a USER's profile on their behalf (account_events)."""
    if not acting.is_other or acting.traveler is None:
        return
    target = (session.query(User).filter(User.traveler_id == acting.traveler.id)
              .first())
    if target is None:
        return                          # a traveller with no account: nothing to audit
    from app.models import AccountEvent
    actor = scope.user_of(request)
    session.add(AccountEvent(user_id=target.id, actor_id=actor.id if actor else None,
                             action="profile_edited", detail=what))
