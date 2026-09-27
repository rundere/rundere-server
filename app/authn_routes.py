"""Auth routes (Yonder 4.0 Phase 1a — SPEC-4 Workstream B).

Two faces, one identity model:
- **Web** (server-rendered): GET/POST ``/login`` and POST ``/logout`` set/clear
  an httpOnly JWT session cookie (D-113 web-session choice).
- **Mobile / API**: ``/api/v1/auth/*`` issues a short access JWT + a rotating
  refresh token, and exposes ``/me``. This is the uniform token flow the iOS app
  and MCP will use.

OIDC endpoints arrive in Phase 1b behind the same router.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import authn
from app.db import get_session
from app.models import User

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))


def _safe_next(nxt: str | None) -> str:
    """Only allow local absolute paths — never an off-site open redirect."""
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/"


class UserOut(BaseModel):
    """The user payload (the API contract the app decodes — YONDER-58)."""
    id: str
    username: str
    is_admin: bool
    auth_source: str
    traveler_id: int | None = None
    email: str | None = None
    share_with_admins: bool = True
    sharing_prompt: bool = False
    opt_out_blocker: str = ""


class TokenOut(BaseModel):
    access: str
    refresh: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


def _user_public(u: User) -> dict:
    """The user payload of /me and every token response. The sharing fields
    (YONDER-101, SPEC-4.2 §3.3) let the app ask the first-sign-in sharing
    question and show the Settings toggle with the web's exact rules."""
    from app.accounts_routes import opt_out_blocker
    return {"id": u.id, "username": u.username, "is_admin": bool(u.is_admin),
            "auth_source": u.auth_source, "traveler_id": u.traveler_id,
            "email": u.email, "share_with_admins": bool(u.share_with_admins),
            "sharing_prompt": bool(u.sharing_prompt),
            # Why opting out isn't possible yet ("" = it is): a local account
            # needs server email + its own address so resets never go via an admin.
            "opt_out_blocker": opt_out_blocker(u)}


# --------------------------------------------------------------------------- web
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str | None = None,
               msg: str | None = None, session: Session = Depends(get_session)):
    if authn.resolve_user(session, request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    from app import oidc
    forced = oidc.forced_on(request)
    # SSO-only host (YONDER-164): straight to the IdP. After a failed attempt
    # (error set) show the page instead, with just a retry, so it can't loop.
    if forced and not error:
        return RedirectResponse(f"/oidc/login?next={quote(_safe_next(next), safe='')}",
                                status_code=303)
    return templates.TemplateResponse(request, "login.html",
                                      {"next": _safe_next(next), "error": error,
                                       "msg": msg, **_login_options(request)})


def _login_options(request: Request) -> dict:
    from app import oidc
    from app.config import settings
    forced = oidc.forced_on(request)
    return {"password_allowed": not forced,
            "registration_open": settings.auth_registration_open and not forced,
            "oidc_enabled": oidc.offered_on(request),
            "oidc_name": settings.oidc_display_name}


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                 next: str = Form("/"), session: Session = Depends(get_session)):
    from app import oidc
    if oidc.forced_on(request):
        return RedirectResponse(f"/oidc/login?next={quote(_safe_next(next), safe='')}",
                                status_code=303)
    user = authn.authenticate_local(session, username, password)
    if user is None:
        return templates.TemplateResponse(
            request, "login.html",
            {"next": _safe_next(next), "error": "Incorrect username or password.",
             "username": username, **_login_options(request)}, status_code=401)
    from datetime import datetime, timezone
    user.last_login_at = datetime.now(timezone.utc)
    session.commit()
    resp = RedirectResponse(_safe_next(next), status_code=303)
    authn.set_session_cookie(resp, authn.issue_web_token(user))
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    authn.clear_session_cookie(resp)
    return resp


# --------------------------------------------------------------------------- mobile / API
class LoginBody(BaseModel):
    username: str
    password: str


class RefreshBody(BaseModel):
    refresh: str


@router.post("/api/v1/auth/login", response_model=TokenOut)
def api_login(body: LoginBody, request: Request, session: Session = Depends(get_session)):
    from app import oidc
    from app.config import settings
    if settings.oidc_force_api_login and oidc.forced_on(request):
        return JSONResponse({"detail": "password sign-in is disabled on this address; "
                                       "use single sign-on"}, status_code=403)
    user = authn.authenticate_local(session, body.username, body.password)
    if user is None:
        return JSONResponse({"detail": "invalid credentials"}, status_code=401)
    from datetime import datetime, timezone
    user.last_login_at = datetime.now(timezone.utc)
    session.commit()
    refresh = authn.create_refresh_token(session, user, request.headers.get("user-agent"))
    return {"access": authn.issue_access_token(user), "refresh": refresh,
            "token_type": "bearer", "expires_in": _access_ttl(), "user": _user_public(user)}


@router.post("/api/v1/auth/refresh", response_model=TokenOut)
def api_refresh(body: RefreshBody, request: Request, session: Session = Depends(get_session)):
    user = authn.consume_refresh_token(session, body.refresh)   # validates + rotates
    if user is None:
        return JSONResponse({"detail": "invalid or expired refresh token"}, status_code=401)
    refresh = authn.create_refresh_token(session, user, request.headers.get("user-agent"))
    return {"access": authn.issue_access_token(user), "refresh": refresh,
            "token_type": "bearer", "expires_in": _access_ttl(), "user": _user_public(user)}


@router.post("/api/v1/auth/logout")
def api_logout(body: RefreshBody, session: Session = Depends(get_session)):
    authn.revoke_refresh_token(session, body.refresh)
    return JSONResponse(status_code=204, content=None)


@router.get("/api/v1/auth/me", response_model=UserOut)
def api_me(user: User = Depends(authn.current_user)):
    return _user_public(user)


class SharingBody(BaseModel):
    share: bool


class EmailBody(BaseModel):
    email: str = ""                     # empty clears it
    current_password: str = ""          # required for local accounts


@router.put("/api/v1/auth/me/sharing", response_model=UserOut)
def api_set_sharing(body: SharingBody, user: User = Depends(authn.first_party_user),
                    session: Session = Depends(get_session)):
    """Share your data with admins, or opt out — the same rules as the web
    (/account/sharing): a local account needs server email + its own address to
    opt out; the change is audited and clears the first-sign-in question.
    First-party only (the app or the web), never a connected app."""
    from app.accounts_routes import _set_sharing
    user = session.get(User, user.id)
    error = _set_sharing(session, user, body.share)
    if error:
        return JSONResponse({"detail": error}, status_code=400)
    session.refresh(user)
    return _user_public(user)


@router.put("/api/v1/auth/me/email", response_model=UserOut)
def api_set_email(body: EmailBody, user: User = Depends(authn.first_party_user),
                  session: Session = Depends(get_session)):
    """Set, change or clear your email — where password-reset links go. As on
    the web (/account/email): a local account must give its current password
    (a hijacked session must not be able to redirect resets), and can't remove
    the address while opted out of sharing. First-party only."""
    user = session.get(User, user.id)
    if user.auth_source == "local" and not authn.verify_password(
            body.current_password, user.password_hash):
        return JSONResponse({"detail": "current password is incorrect"}, status_code=403)
    if not body.email.strip() and not user.share_with_admins and user.auth_source == "local":
        return JSONResponse({"detail": "while you're not sharing with admins, your email is "
                                       "how password resets reach you — change it, don't "
                                       "remove it"}, status_code=400)
    error = authn.set_user_email(session, user, body.email)
    if error:
        status = 409 if "already used" in error else 400
        return JSONResponse({"detail": error}, status_code=status)
    session.refresh(user)
    return _user_public(user)


def _access_ttl() -> int:
    from app.config import settings
    return settings.jwt_access_ttl_seconds
