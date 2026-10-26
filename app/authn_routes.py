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

from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import authn
from app.auth import RateLimiter, client_ip
from app.config import settings
from app.db import get_session
from app.models import User

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))
from app.icons import install as _install_icons  # noqa: E402
_install_icons(templates)


def _safe_next(nxt: str | None) -> str:
    """Only allow local absolute paths — never an off-site open redirect.
    Browsers read a backslash as a slash, so "/\\evil.example" is off-site too,
    and control characters can split the Location header (YONDER-293)."""
    if (nxt and nxt.startswith("/") and not nxt.startswith("//")
            and "\\" not in nxt and not any(ord(c) < 32 or ord(c) == 127 for c in nxt)):
        return nxt
    return "/"


# Failed password sign-ins, per client address and per username (YONDER-293).
# In-process like the other limiters: one app process per server.
_login_by_ip = RateLimiter(settings.login_rate_limit, settings.login_rate_window_seconds)
_login_by_user = RateLimiter(settings.login_rate_limit, settings.login_rate_window_seconds)


def login_throttled(request: Request, username: str) -> bool:
    return (_login_by_ip.blocked(client_ip(request))
            or _login_by_user.blocked((username or "").strip().lower()))


def login_failed(request: Request, username: str) -> None:
    _login_by_ip.record(client_ip(request))
    _login_by_user.record((username or "").strip().lower())


_TOO_MANY = "Too many sign-in attempts. Try again in a few minutes."


class UserOut(BaseModel):
    """The user payload (the API contract the app decodes — YONDER-58)."""
    id: str
    username: str
    is_admin: bool
    auth_source: str
    traveler_id: int | None = None
    email: str | None = None
    share_with_admins: bool = False        # private by default (D-220)
    sharing_prompt: bool = False
    opt_out_blocker: str = ""              # always "" since D-220; kept for the app
    reset_note: str = ""                   # what a forgotten password means here (D-220)
    email_required: bool = False           # a local account must add an email first (D-220)
    support_until: datetime | None = None  # when support access ends; null = until turned off (D-211)
    # From the person's own travel profile — theirs alone, so only on their own
    # payload: the currency an expense defaults to (YONDER-213) and the zone
    # their "now" runs on (D-187).
    base_currency: str | None = None
    timezone: str | None = None


class TokenOut(BaseModel):
    access: str
    refresh: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


def _support_until(u: User):
    """A live grant's end time, or None (off, or until turned off)."""
    from app import scope, support
    return support.until(u) if scope.support_live(u) else None


def _user_public(u: User) -> dict:
    """The user payload of /me and every token response. The sharing fields
    (YONDER-101, SPEC-4.2 §3.3) let the app ask the first-sign-in sharing
    question and show the Settings toggle with the web's exact rules."""
    from app.accounts_routes import opt_out_blocker, reset_note
    base_currency = tz = None
    if u.traveler_id is not None:
        from app.db import SessionLocal
        from app.models import TravelerIdentity
        with SessionLocal() as s:
            t = s.get(TravelerIdentity, u.traveler_id)
            if t is not None:
                base_currency, tz = t.base_currency, t.timezone
    return {"base_currency": base_currency, "timezone": tz,
            "id": u.id, "username": u.username, "is_admin": bool(u.is_admin),
            "auth_source": u.auth_source, "traveler_id": u.traveler_id,
            "email": u.email, "share_with_admins": bool(u.share_with_admins),
            "sharing_prompt": bool(u.sharing_prompt),
            # Always "" since D-220 (going private never waits on an email); kept
            # for the app, which reads it.
            "opt_out_blocker": opt_out_blocker(u),
            # The same note the web shows a private local account without a
            # reachable email: only the server's operator could reset its password.
            "reset_note": reset_note(u),
            # The web asks for it at every visit; the app should too (SPEC-4.2 §3.3).
            "email_required": authn.needs_email(u),
            "support_until": _support_until(u)}


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
    from app import demo, oidc
    from app.config import settings
    forced = oidc.forced_on(request)
    if demo.enabled():
        # D-198: the shared login is shown on the page; it isn't a secret.
        return {"password_allowed": True, "registration_open": False,
                "oidc_enabled": False, "oidc_name": "",
                "demo": {"username": settings.demo_username.strip(),
                         "password": settings.demo_password}}
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
    if login_throttled(request, username):
        return templates.TemplateResponse(
            request, "login.html",
            {"next": _safe_next(next), "error": _TOO_MANY,
             "username": username, **_login_options(request)}, status_code=429)
    user = authn.authenticate_local(session, username, password)
    if user is None:
        login_failed(request, username)
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


class AuthMethodsOut(BaseModel):
    """How someone may sign in on the host this was asked on (YONDER-180)."""
    password: bool                 # POST /api/v1/auth/login is accepted here
    oauth: bool                    # /oauth/authorize (the server's own OAuth, YONDER-107)
    # The built-in app client to use with it (D-194); None = this server can't
    # sign the app in with OAuth (older server, or no app redirect configured).
    oauth_client_id: str | None = None
    sso: bool                      # "Sign in with <sso_name>" is offered here
    sso_name: str | None = None
    sso_forced: bool = False       # SSO is the only way in on the web here
    registration_open: bool = False
    server_name: str = "Run·dere"
    # D-198: a read-only demo server. The app shows its banner and hides edit
    # controls (every trip also reports can_edit=false).
    demo: bool = False
    version: str


@router.get("/api/v1/auth/methods", response_model=AuthMethodsOut)
def api_auth_methods(request: Request):
    """The sign-in choices for THIS host, unauthenticated, for the app's login
    screen (YONDER-180): the same per-host rules as the web login
    (_login_options, YONDER-164). The host is the one the request was made to —
    never a forwarded or proxy-supplied identity header. An older server
    answers 404, which the app reads as "password only"."""
    from app import demo, oauth, oidc
    from app.config import settings
    from app.db import SessionLocal
    if demo.enabled():
        # Plain username and password only: no OAuth, SSO or registration.
        return {"password": True, "oauth": False, "oauth_client_id": None, "sso": False,
                "sso_name": None, "sso_forced": False, "registration_open": False,
                "demo": True, "version": settings.app_version}
    forced = oidc.forced_on(request)
    sso = oidc.offered_on(request)
    with SessionLocal() as session:
        app_client = oauth.APP_CLIENT_ID if oauth.app_client_ready(session) else None
    return {"password": not (settings.oidc_force_api_login and forced),
            "oauth": True,
            "oauth_client_id": app_client,
            "sso": sso,
            "sso_name": settings.oidc_display_name if sso else None,
            "sso_forced": forced,
            "registration_open": settings.auth_registration_open and not forced,
            "version": settings.app_version}


@router.post("/api/v1/auth/login", response_model=TokenOut)
def api_login(body: LoginBody, request: Request, session: Session = Depends(get_session)):
    from app import oidc
    from app.config import settings
    if settings.oidc_force_api_login and oidc.forced_on(request):
        return JSONResponse({"detail": "password sign-in is disabled on this address; "
                                       "use single sign-on"}, status_code=403)
    if login_throttled(request, body.username):
        return JSONResponse({"detail": _TOO_MANY}, status_code=429)
    user = authn.authenticate_local(session, body.username, body.password)
    if user is None:
        login_failed(request, body.username)
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
    duration: str = "standing"             # 24h | 7d | standing (D-211)


class EmailBody(BaseModel):
    email: str = ""                     # empty clears it
    current_password: str = ""          # required for local accounts


@router.put("/api/v1/auth/me/sharing", response_model=UserOut)
def api_set_sharing(body: SharingBody, user: User = Depends(authn.first_party_user),
                    session: Session = Depends(get_session)):
    """Turn support access on (for `duration`: 24h, 7d or standing) or off, as on
    the web (D-211). Audited, and clears the first-sign-in question. First-party
    only (the app or the web), never a connected app."""
    from app.accounts_routes import _set_sharing
    user = session.get(User, user.id)
    error = _set_sharing(session, user, body.share, body.duration)
    if error:
        return JSONResponse({"detail": error}, status_code=400)
    session.refresh(user)
    return _user_public(user)


@router.put("/api/v1/auth/me/email", response_model=UserOut)
def api_set_email(body: EmailBody, user: User = Depends(authn.first_party_user),
                  session: Session = Depends(get_session)):
    """Set, change or clear your email — where password-reset links go. As on
    the web (/account/email): a local account must give its current password
    (a hijacked session must not be able to redirect resets). First-party only.
    Removing it is allowed (D-220); `reset_note` then says what that means."""
    user = session.get(User, user.id)
    if user.auth_source == "local" and not authn.verify_password(
            body.current_password, user.password_hash):
        return JSONResponse({"detail": "current password is incorrect"}, status_code=403)
    error = authn.set_user_email(session, user, body.email)
    if error:
        status = 409 if "already used" in error else 400
        return JSONResponse({"detail": error}, status_code=status)
    session.refresh(user)
    return _user_public(user)


def _access_ttl() -> int:
    from app.config import settings
    return settings.jwt_access_ttl_seconds
