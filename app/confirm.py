"""Run·dere — a fresh confirmation of who you are (SPEC-13 §3.2).

Downloading an archive (D-241) and, from wave 9b, asking to delete an account
(D-242) need more than a signed-in session: a stolen session cookie must not be
enough. Both use this one helper.

How a person confirms depends on how they sign in:

- **Local account:** the password, typed on the page.
- **SSO account:** a round trip to the provider that asks it to sign the person
  in again (`prompt=login`, `max_age=0`). Where the provider returns
  `auth_time` it must be recent; a provider that ignores the request still has
  to complete the round trip.
- **Trusted proxy header:** Run·dere can't re-authenticate, because the proxy is
  the authenticator. The person types their username, and the page says so.

A confirmation lasts 10 minutes, is bound to the user and their
`token_version` (so "sign out everywhere" ends it), and is held in a signed,
http-only cookie. It grants nothing by itself: every route that asks for it
also requires the web session (`web_user`).
"""
from __future__ import annotations

import hmac
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import authn
from app.auth import RateLimiter
from app.config import settings
from app.db import SessionLocal
from app.models import User

COOKIE = "rundere_confirm"
TTL = 600                          # 10 minutes (SPEC-13 §3.2); there is no setting
_AUTH_TIME_SKEW = 60               # clock drift allowed against the provider's auth_time
PASSWORD, SSO, PROXY = "password", "sso", "proxy"

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))
from app.icons import install as _install_icons  # noqa: E402
_install_icons(templates)

# Wrong passwords per account, like the sign-in throttle (YONDER-293): a stolen
# session mustn't get unlimited guesses at the password here.
_attempts = RateLimiter(settings.login_rate_limit, settings.login_rate_window_seconds)


# --------------------------------------------------------------------------- who is asking
def web_user(request: Request) -> User:
    """FastAPI dependency: the person signed in on the WEB. A bearer credential
    (a personal API token, an OAuth or app token) is refused, so no API client,
    agent or MCP tool reaches a route behind this (D-241). With sign-in off
    there are no accounts, so these pages don't exist (SPEC-13 §1.1)."""
    if not settings.auth_required:
        raise HTTPException(status_code=404, detail="not found")
    if request.headers.get("authorization"):
        raise HTTPException(status_code=403, detail="this is only available on the web")
    with SessionLocal() as session:
        cookie = request.cookies.get(settings.auth_cookie_name)
        user = authn.user_from_token(session, cookie, kinds=("web",)) if cookie else None
        if user is None and settings.auth_proxy_header_trust:
            user = authn.resolve_proxy_user(session, request)
        if user is not None:
            session.expunge(user)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def method(user) -> str:
    """How this account confirms: by its sign-in kind."""
    return {"oidc": SSO, "proxy": PROXY}.get(user.auth_source, PASSWORD)


# --------------------------------------------------------------------------- the cookie
def grant(response, user) -> None:
    """Mark this browser as freshly confirmed for `user`, for 10 minutes."""
    now = int(time.time())
    token = authn.jwt_encode({"typ": "confirm", "sub": user.id, "ver": user.token_version,
                              "iat": now, "exp": now + TTL})
    response.set_cookie(COOKIE, token, max_age=TTL, httponly=True,
                        secure=settings.auth_cookie_secure, samesite="lax", path="/account")


def clear(response) -> None:
    response.delete_cookie(COOKIE, path="/account")


def is_fresh(request, user) -> bool:
    """Has this browser confirmed as `user` in the last 10 minutes? Bound to
    the user and to their current `token_version`."""
    claims = authn.jwt_decode(request.cookies.get(COOKIE, "") or "")
    if not claims or claims.get("typ") != "confirm":
        return False
    return claims.get("sub") == user.id and claims.get("ver") == user.token_version


# The only pages a confirmation leads back to.
_DESTINATIONS = ("/account", "/account/export", "/account/delete")


def safe_next(nxt: str | None) -> str:
    """Where to go after confirming: one of our own Account pages, by name.
    The value returned is always one of the constants above, never the text
    that came in, so nothing a request carries can steer a redirect."""
    wanted = (nxt or "").strip()
    for dest in _DESTINATIONS:
        if wanted == dest:
            return dest
    return "/account"


# Why a round trip to the provider didn't confirm. The redirect back to the
# page carries the CODE, and the page shows the sentence for it: text from the
# address bar is never put on the page, so a crafted link can't make this
# page say something of an attacker's choosing.
_REASONS = {
    "sso_unreachable": "Your sign-in provider can't be reached right now.",
    "signed_out": "Sign in again, then confirm.",
    "other_account": "That was a different account at your sign-in provider.",
    "not_recent": ("Your sign-in provider didn't ask you to sign in again. "
                   "Sign out there and retry."),
}


def page_url(nxt: str) -> str:
    return f"/account/confirm?next={quote(safe_next(nxt), safe='')}"


# --------------------------------------------------------------------------- the page
def _sso_available(request) -> bool:
    from app import oidc
    return oidc.offered_on(request)


def _page(request: Request, user, nxt: str, error: str = "", status: int = 200):
    how = method(user)
    return templates.TemplateResponse(request, "confirm.html", {
        "u": user, "next": safe_next(nxt), "how": how, "error": error,
        "sso_available": how != SSO or _sso_available(request),
        "sso_name": settings.oidc_display_name, "minutes": TTL // 60}, status_code=status)


@router.get("/account/confirm", response_class=HTMLResponse)
def confirm_page(request: Request, next: str = "/account", error: str = "",
                 user: User = Depends(web_user)):
    if is_fresh(request, user):
        return RedirectResponse(safe_next(next), status_code=303)
    return _page(request, user, next, _REASONS.get(error, ""))


@router.post("/account/confirm")
def confirm_submit(request: Request, next: str = Form("/account"),
                   password: str = Form(""), username: str = Form(""),
                   user: User = Depends(web_user)):
    how = method(user)
    if how == SSO:                       # never by typing: only the provider's round trip
        return _page(request, user, next, "Confirm with your sign-in provider.", 400)
    if how == PASSWORD:
        if not _attempts.allow(user.id):
            return _page(request, user, next,
                         "Too many attempts. Wait a few minutes and try again.", 429)
        with SessionLocal() as session:
            row = session.get(User, user.id)
            ok = row is not None and authn.verify_password(password, row.password_hash)
        if not ok:
            return _page(request, user, next, "That password is incorrect.", 400)
    else:
        # Behind a trusted proxy the proxy is what protects this step; typing
        # the username only makes it a deliberate act.
        if not hmac.compare_digest(username.strip().encode(), user.username.encode()):
            return _page(request, user, next, "That isn't your username.", 400)
    resp = RedirectResponse(safe_next(next), status_code=303)
    grant(resp, user)
    return resp


# --------------------------------------------------------------------------- SSO round trip
@router.get("/account/confirm/sso")
def confirm_sso(request: Request, next: str = "/account", user: User = Depends(web_user)):
    """Send an SSO account back to its provider to sign in again. The answer
    comes back to the one registered callback, /oidc/callback, which hands a
    `confirm` handshake to `finish_sso` below."""
    from app import oidc
    from app.oidc_routes import _STATE_COOKIE, _STATE_TTL, _redirect_uri
    if method(user) != SSO or not oidc.offered_on(request):
        return RedirectResponse(page_url(next), status_code=303)
    verifier, challenge = oidc.new_pkce()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    url = oidc.build_auth_url(_redirect_uri(request), state, nonce, challenge,
                              extra={"prompt": "login", "max_age": "0"})
    if not url:
        return _fail(next, "sso_unreachable")
    token = authn.jwt_encode({"typ": "oidc_state", "pu": "confirm", "uid": user.id,
                              "st": state, "no": nonce, "vf": verifier,
                              "nx": safe_next(next), "exp": int(time.time()) + _STATE_TTL})
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(_STATE_COOKIE, token, max_age=_STATE_TTL, httponly=True,
                    secure=settings.auth_cookie_secure, samesite="lax", path="/")
    return resp


def _fail(nxt: str, code: str) -> RedirectResponse:
    """Back to the confirm page with one of the _REASONS codes."""
    assert code in _REASONS
    return RedirectResponse(f"{page_url(nxt)}&error={code}", status_code=303)


def is_confirm_handshake(saved: dict) -> bool:
    return saved.get("pu") == "confirm"


def finish_sso(request, saved: dict, claims: dict) -> RedirectResponse:
    """The end of the round trip (called by /oidc/callback once the state, the
    code exchange and the id_token's claims have been checked). It signs nobody
    in and provisions nothing: it only confirms the person ALREADY signed in
    here, when the provider says it is the same person and that they have just
    authenticated."""
    from app.oidc_routes import _STATE_COOKIE
    nxt = saved.get("nx", "/account")
    with SessionLocal() as session:
        cookie = request.cookies.get(settings.auth_cookie_name)
        user = authn.user_from_token(session, cookie, kinds=("web",)) if cookie else None
        if user is None or user.id != saved.get("uid"):
            resp = _fail(nxt, "signed_out")
        elif not claims.get("sub") or user.external_subject != f"oidc:{claims['sub']}":
            resp = _fail(nxt, "other_account")
        elif not _recent(claims.get("auth_time")):
            resp = _fail(nxt, "not_recent")
        else:
            resp = RedirectResponse(safe_next(nxt), status_code=303)
            grant(resp, user)
    resp.delete_cookie(_STATE_COOKIE, path="/")
    return resp


def _recent(auth_time) -> bool:
    """`auth_time`, where the provider returns it, must be within the last 10
    minutes. A provider that leaves it out has still completed the round trip,
    which is all that can be asked of it (SPEC-13 §3.2)."""
    if auth_time is None:
        return True
    try:
        at = float(auth_time)
    except (TypeError, ValueError):
        return False
    return -_AUTH_TIME_SKEW <= time.time() - at <= TTL + _AUTH_TIME_SKEW
