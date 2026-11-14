"""Account › Delete my account, and deleting other accounts (SPEC-13 §3, D-242).

- **/account/delete**           what is deleted and what stays, the trips that
                                need a choice, and the request (POST). For an
                                account already waiting: the date, the cancel
                                button and the way to the export.
- **/account/delete/cancel**    cancel your own request (POST).
- **/users/{id}/delete**        an admin asks for another account to be deleted
                                (POST), by typing its username.
- **/users/{id}/delete/cancel** an admin cancels it (POST).

Web only: the Account routes take `confirm.web_user`, which refuses bearer
credentials, so there is no way to a deletion through /api/v1, MCP, a personal
API token or an OAuth token. The request also needs a fresh confirmation of who
the person is (app/confirm.py) and their typed username. There is no "delete
now" here: only the operator's command line skips the 7 days (§3.3). With
sign-in off the pages don't exist; on a demo server `demo_guard` answers 404.
"""
from __future__ import annotations

import hmac
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import account_deletion as deletion
from app import authn, confirm
from app.account_purge import DELETE, HAND
from app.config import settings
from app.db import get_session
from app.email import email_configured
from app.models import User

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))
from app.icons import install as _install_icons  # noqa: E402
_install_icons(templates)


def _base_url(request: Request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


def _same(typed: str, username: str) -> bool:
    return hmac.compare_digest(typed.strip().encode(), username.encode())


def _page(request: Request, session: Session, user: User, err: str = "",
          status: int = 200):
    person = session.get(User, user.id)
    rec = deletion.record(session, person)
    ctx = {"u": person, "err": err, "pending": deletion.pending(person), "record": rec,
           "due": deletion._aware(person.deletion_due_at),
           "by_admin": bool(rec and rec.by_admin),
           "grace_days": deletion.GRACE_DAYS,
           "smtp": email_configured(), "has_email": bool(person.email),
           "last_admin": deletion.last_admin(session, person),
           "confirmed": confirm.is_fresh(request, person),
           "shared": [] if deletion.pending(person) else deletion.shared_trips(session, person)}
    resp = templates.TemplateResponse(request, "account_delete.html", ctx, status_code=status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/account/delete", response_class=HTMLResponse)
def delete_page(request: Request, user: User = Depends(confirm.web_user),
                session: Session = Depends(get_session)):
    return _page(request, session, user)


async def _choices(request: Request) -> dict:
    """The per-trip answers: `trip_<id>` is "delete" or "hand:<user id>". The
    service checks them against the trips and the people actually on them."""
    form = await request.form()
    out: dict = {}
    for key, value in form.items():
        if not key.startswith("trip_") or not isinstance(value, str):
            continue
        if value == DELETE:
            out[key[5:]] = {"action": DELETE}
        elif value.startswith("hand:"):
            out[key[5:]] = {"action": HAND, "to": value[5:]}
    return out


@router.post("/account/delete")
async def delete_submit(request: Request, username: str = Form(""),
                        user: User = Depends(confirm.web_user),
                        session: Session = Depends(get_session)):
    person = session.get(User, user.id)
    if deletion.pending(person):
        return RedirectResponse("/account/delete", status_code=303)
    if not confirm.is_fresh(request, person):
        return RedirectResponse(confirm.page_url("/account/delete"), status_code=303)
    if not _same(username, person.username):
        return _page(request, session, user, "Type your username to confirm.", 400)
    try:
        deletion.request(session, person, actor=person, choices=await _choices(request),
                         base_url=_base_url(request))
    except deletion.Refused as why:
        return _page(request, session, user, str(why), 400)
    # Every session ended with the request, this one included. The browser
    # that asked gets a new web session, which the lock holds to this page.
    resp = RedirectResponse("/account/delete", status_code=303)
    authn.set_session_cookie(resp, authn.issue_web_token(person))
    confirm.clear(resp)
    return resp


@router.post("/account/delete/cancel")
def delete_cancel(request: Request, user: User = Depends(confirm.web_user),
                  session: Session = Depends(get_session)):
    person = session.get(User, user.id)
    try:
        deletion.cancel(session, person, actor=person, base_url=_base_url(request))
    except deletion.Refused as why:
        return _page(request, session, user, str(why), 400)
    return RedirectResponse("/account?msg=" + quote_plus(
        "Your account will not be deleted. Sign in to the apps again."), status_code=303)


# --------------------------------------------------------------------------- admin: Users
def _admin_target(session: Session, user_id: str, admin: User) -> User:
    if not deletion.available():
        raise HTTPException(status_code=404, detail="not found")
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="not found")
    if target.id == admin.id:
        # Deleting your own account is yours, under Account, with the
        # confirmation it needs (D-242 amends D-211).
        raise HTTPException(status_code=400, detail="delete your own account under Account")
    return target


def _users(msg: str = "", err: str = "") -> RedirectResponse:
    q = f"msg={quote_plus(msg)}" if msg else f"err={quote_plus(err)}"
    return RedirectResponse(f"/users?{q}", status_code=303)


@router.post("/users/{user_id}/delete")
def users_delete(request: Request, user_id: str, confirm_username: str = Form(""),
                 admin: User = Depends(authn.require_admin),
                 session: Session = Depends(get_session)):
    if request.headers.get("authorization"):       # the web only, as for your own
        raise HTTPException(status_code=403, detail="this is only available on the web")
    target = _admin_target(session, user_id, admin)
    if not _same(confirm_username, target.username):
        return _users(err="Type the account's username to confirm.")
    try:
        deletion.request(session, target, actor=session.get(User, admin.id),
                         base_url=_base_url(request))
    except deletion.Refused as why:
        return _users(err=str(why))
    return _users(msg=f"{target.username} is locked and will be deleted in "
                      f"{deletion.GRACE_DAYS} days.")


@router.post("/users/{user_id}/delete/cancel")
def users_delete_cancel(request: Request, user_id: str,
                        admin: User = Depends(authn.require_admin),
                        session: Session = Depends(get_session)):
    if request.headers.get("authorization"):
        raise HTTPException(status_code=403, detail="this is only available on the web")
    target = _admin_target(session, user_id, admin)
    try:
        deletion.cancel(session, target, actor=session.get(User, admin.id),
                        base_url=_base_url(request))
    except deletion.Refused as why:
        return _users(err=str(why))
    return _users(msg=f"{target.username} will not be deleted.")
