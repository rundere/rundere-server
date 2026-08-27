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


def _user_public(u: User) -> dict:
    return {"id": u.id, "username": u.username, "is_admin": bool(u.is_admin),
            "auth_source": u.auth_source, "traveler_id": u.traveler_id}


# --------------------------------------------------------------------------- web
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str | None = None,
               msg: str | None = None, session: Session = Depends(get_session)):
    from app.config import settings
    if authn.resolve_user(session, request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html",
                                      {"next": _safe_next(next), "error": error,
                                       "msg": msg,
                                       "registration_open": settings.auth_registration_open})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                 next: str = Form("/"), session: Session = Depends(get_session)):
    user = authn.authenticate_local(session, username, password)
    if user is None:
        return templates.TemplateResponse(
            request, "login.html",
            {"next": _safe_next(next), "error": "Incorrect username or password.",
             "username": username}, status_code=401)
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


@router.post("/api/v1/auth/login")
def api_login(body: LoginBody, request: Request, session: Session = Depends(get_session)):
    user = authn.authenticate_local(session, body.username, body.password)
    if user is None:
        return JSONResponse({"detail": "invalid credentials"}, status_code=401)
    from datetime import datetime, timezone
    user.last_login_at = datetime.now(timezone.utc)
    session.commit()
    refresh = authn.create_refresh_token(session, user, request.headers.get("user-agent"))
    return {"access": authn.issue_access_token(user), "refresh": refresh,
            "token_type": "bearer", "expires_in": _access_ttl(), "user": _user_public(user)}


@router.post("/api/v1/auth/refresh")
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


@router.get("/api/v1/auth/me")
def api_me(user: User = Depends(authn.current_user)):
    return _user_public(user)


def _access_ttl() -> int:
    from app.config import settings
    return settings.jwt_access_ttl_seconds
