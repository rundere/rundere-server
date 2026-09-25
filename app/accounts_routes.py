"""Account onboarding & management (Yonder 4.0 Phase 1a.1 — SPEC-4 D-117).

- **/setup**   first-run: create the initial admin while the users table is
               empty (always reachable then; 404s once any user exists).
- **/register** self-service sign-up, only when TX_AUTH_REGISTRATION_OPEN.
- **/forgot**, **/reset/{token}** — password reset via a one-time link (emailed
  when SMTP is configured; otherwise an admin hands over the link).
- **/users**   Administer → Users: admin creates accounts, sets/【resets】
               passwords, toggles admin/active, and links a Traveller
               (pick existing OR create new).

Every account maps to a TravelerIdentity (the self/guest roster) — one identity
per person, which Phase 2 trip-sharing builds on.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import authn
from app.config import settings
from app.db import get_session
from app.email import email_configured, send_email
from app.models import TravelerIdentity, User

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))


def _now():
    return datetime.now(timezone.utc)


def _travelers(session: Session) -> list[TravelerIdentity]:
    """The de-duplicated traveller roster surfaced to the admin for user↔traveller
    linking (D-124) — the ONLY user-entered item an admin sees in the UI. Names
    are already unique in the schema; generic placeholder guests (roster stand-ins,
    not real people) are excluded from selection."""
    return (session.query(TravelerIdentity)
            .filter(TravelerIdentity.generic.is_(False))
            .order_by(TravelerIdentity.display_name).all())


def _base_url(request: Request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


def _login_user(response, user: User):
    authn.set_session_cookie(response, authn.issue_web_token(user))
    return response


# --------------------------------------------------------------------------- first-run setup
@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, session: Session = Depends(get_session)):
    if authn.user_count(session) > 0:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html",
                                      {"travelers": _travelers(session)})


@router.post("/setup")
def setup_submit(request: Request, username: str = Form(...), password: str = Form(...),
                 email: str = Form(""), traveler_id: str = Form("none"),
                 new_traveler: str = Form(""), session: Session = Depends(get_session)):
    if authn.user_count(session) > 0:
        return RedirectResponse("/login", status_code=303)
    # The first traveller is the account holder → 'self' if a brand-new one.
    tid = authn.resolve_traveler(session, traveler_id=traveler_id,
                                 new_name=new_traveler, kind="self")
    user = authn.create_user(session, username, password, email=email or None,
                             is_admin=True, traveler_id=tid)
    resp = RedirectResponse("/", status_code=303)
    return _login_user(resp, user)


# --------------------------------------------------------------------------- self-service register
@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request, session: Session = Depends(get_session)):
    if not settings.auth_registration_open:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "register.html",
                                      {"travelers": _travelers(session)})


@router.post("/register")
def register_submit(request: Request, username: str = Form(...), password: str = Form(...),
                    email: str = Form(""), traveler_id: str = Form("none"),
                    new_traveler: str = Form(""), session: Session = Depends(get_session)):
    if not settings.auth_registration_open:
        return RedirectResponse("/login", status_code=303)
    if session.query(User).filter_by(username=username.strip()).one_or_none():
        return templates.TemplateResponse(
            request, "register.html",
            {"travelers": _travelers(session), "error": "That username is taken.",
             "username": username}, status_code=409)
    tid = authn.resolve_traveler(session, traveler_id=traveler_id, new_name=new_traveler)
    user = authn.create_user(session, username, password, email=email or None,
                             is_admin=False, traveler_id=tid)
    resp = RedirectResponse("/", status_code=303)
    return _login_user(resp, user)


# --------------------------------------------------------------------------- forgot / reset
@router.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    return templates.TemplateResponse(request, "forgot.html",
                                      {"smtp": email_configured()})


@router.post("/forgot", response_class=HTMLResponse)
def forgot_submit(request: Request, identifier: str = Form(...),
                  session: Session = Depends(get_session)):
    ident = identifier.strip()
    user = session.query(User).filter(
        (User.username == ident) | (User.email == ident)).one_or_none()
    # Neutral response regardless — never reveal whether an account exists.
    if user is not None and user.auth_source == "local":
        token = authn.create_reset_token(session, user)
        if user.email and email_configured():
            link = f"{_base_url(request)}/reset/{token}"
            send_email(user.email, "Reset your Yonder password",
                       f"Use this link to set a new password (valid ~1 hour):\n\n{link}\n\n"
                       "If you didn't request this, ignore this email.")
    return templates.TemplateResponse(request, "forgot.html",
                                      {"smtp": email_configured(), "sent": True})


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_form(request: Request, token: str, session: Session = Depends(get_session)):
    user = authn.peek_reset_token(session, token)
    return templates.TemplateResponse(request, "reset.html",
                                      {"token": token, "valid": user is not None,
                                       "username": user.username if user else None})


@router.post("/reset/{token}")
def reset_submit(request: Request, token: str, password: str = Form(...),
                 confirm: str = Form(...), session: Session = Depends(get_session)):
    if password != confirm:
        return templates.TemplateResponse(
            request, "reset.html",
            {"token": token, "valid": True, "error": "Passwords did not match."},
            status_code=400)
    user = authn.consume_reset_token(session, token, password)
    if user is None:
        return templates.TemplateResponse(
            request, "reset.html", {"token": token, "valid": False}, status_code=400)
    return RedirectResponse("/login?msg=reset", status_code=303)


# --------------------------------------------------------------------------- admin: Users
@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, msg: str = "", err: str = "", link: str = "",
               link_for: str = "", admin: User = Depends(authn.require_admin),
               session: Session = Depends(get_session)):
    users = session.query(User).order_by(User.username).all()
    tmap = {t.id: t for t in _travelers(session)}
    return templates.TemplateResponse(request, "users.html", {
        "users": users, "travelers": _travelers(session), "tmap": tmap,
        "me": admin, "registration_open": settings.auth_registration_open,
        "msg": msg, "err": err, "link": link, "link_for": link_for})


def _u(session, user_id: str) -> User | None:
    return session.get(User, user_id)


@router.post("/users/create")
def users_create(request: Request, username: str = Form(...), password: str = Form(...),
                 email: str = Form(""), is_admin: str = Form(""),
                 traveler_id: str = Form("none"), new_traveler: str = Form(""),
                 admin: User = Depends(authn.require_admin),
                 session: Session = Depends(get_session)):
    if session.query(User).filter_by(username=username.strip()).one_or_none():
        return RedirectResponse("/users?err=That+username+is+taken", status_code=303)
    tid = authn.resolve_traveler(session, traveler_id=traveler_id, new_name=new_traveler)
    authn.create_user(session, username, password, email=email or None,
                      is_admin=bool(is_admin), traveler_id=tid)
    return RedirectResponse(f"/users?msg=Created+{username.strip()}", status_code=303)


@router.post("/users/{user_id}/password")
def users_set_password(user_id: str, password: str = Form(...),
                       admin: User = Depends(authn.require_admin),
                       session: Session = Depends(get_session)):
    user = _u(session, user_id)
    if user is None:
        return RedirectResponse("/users?err=No+such+user", status_code=303)
    user.password_hash = authn.hash_password(password)
    user.token_version += 1
    session.commit()
    return RedirectResponse(f"/users?msg=Password+set+for+{user.username}", status_code=303)


@router.post("/users/{user_id}/reset-link")
def users_reset_link(request: Request, user_id: str,
                     admin: User = Depends(authn.require_admin),
                     session: Session = Depends(get_session)):
    user = _u(session, user_id)
    if user is None:
        return RedirectResponse("/users?err=No+such+user", status_code=303)
    token = authn.create_reset_token(session, user)
    link = f"{_base_url(request)}/reset/{token}"
    return RedirectResponse(f"/users?link={link}&link_for={user.username}", status_code=303)


@router.post("/users/{user_id}/admin")
def users_toggle_admin(user_id: str, admin: User = Depends(authn.require_admin),
                       session: Session = Depends(get_session)):
    user = _u(session, user_id)
    if user is None:
        return RedirectResponse("/users?err=No+such+user", status_code=303)
    if user.id == admin.id:
        return RedirectResponse("/users?err=You+can%27t+change+your+own+admin+role", status_code=303)
    if user.is_admin and session.query(User).filter_by(is_admin=True).count() <= 1:
        return RedirectResponse("/users?err=Can%27t+remove+the+last+admin", status_code=303)
    user.is_admin = not user.is_admin
    session.commit()
    return RedirectResponse(f"/users?msg=Updated+{user.username}", status_code=303)


@router.post("/users/{user_id}/active")
def users_toggle_active(user_id: str, admin: User = Depends(authn.require_admin),
                        session: Session = Depends(get_session)):
    user = _u(session, user_id)
    if user is None:
        return RedirectResponse("/users?err=No+such+user", status_code=303)
    if user.id == admin.id:
        return RedirectResponse("/users?err=You+can%27t+deactivate+yourself", status_code=303)
    user.is_active = not user.is_active
    user.token_version += 1                    # kick out any live sessions on deactivate
    session.commit()
    return RedirectResponse(f"/users?msg=Updated+{user.username}", status_code=303)


@router.post("/users/{user_id}/traveler")
def users_link_traveler(user_id: str, traveler_id: str = Form("none"),
                        new_traveler: str = Form(""),
                        admin: User = Depends(authn.require_admin),
                        session: Session = Depends(get_session)):
    user = _u(session, user_id)
    if user is None:
        return RedirectResponse("/users?err=No+such+user", status_code=303)
    user.traveler_id = authn.resolve_traveler(session, traveler_id=traveler_id,
                                              new_name=new_traveler)
    session.commit()
    return RedirectResponse(f"/users?msg=Linked+traveller+for+{user.username}", status_code=303)
