"""OIDC login/callback routes (Yonder 4.0 Phase 1b — SPEC-4 D-118).

/oidc/login  → redirect to the IdP (Authorization Code + PKCE); the transient
               state/nonce/verifier/next ride in a short-lived signed cookie
               (no server-side session store needed).
/oidc/callback → validate state, exchange the code, validate the id_token
               claims, provision/adopt the User, and mint the SAME Yonder
               session cookie every other path issues (D-111).

Inert unless TX_OIDC_ENABLED and the issuer/client are configured.
"""
from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER

from app import authn, oidc
from app.config import settings
from app.db import SessionLocal

router = APIRouter()

_STATE_COOKIE = "yonder_oidc"
_STATE_TTL = 600            # 10 minutes to complete the handshake


def _safe_next(nxt: str | None) -> str:
    return nxt if (nxt and nxt.startswith("/") and not nxt.startswith("//")) else "/"


def _redirect_uri(request: Request) -> str:
    if settings.oidc_redirect_url:
        return settings.oidc_redirect_url
    # A host named in the per-host settings gets its own callback (YONDER-164) —
    # safe to take from the request because the name is on an explicit list.
    if oidc.listed_host(request):
        return f"{str(request.base_url).rstrip('/')}/oidc/callback"
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    return f"{base}/oidc/callback"


def _fail(reason: str) -> RedirectResponse:
    # Keep IdP errors terse and non-leaky in the URL.
    return RedirectResponse(f"/login?error=SSO+sign-in+failed+({reason})",
                            status_code=HTTP_303_SEE_OTHER)


@router.get("/oidc/login")
def oidc_login(request: Request, next: str = "/"):
    if not oidc.enabled():
        return _fail("not configured")
    if not oidc.offered_on(request):
        return _fail("not available on this address")
    verifier, challenge = oidc.new_pkce()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    auth_url = oidc.build_auth_url(_redirect_uri(request), state, nonce, challenge)
    if not auth_url:
        return _fail("discovery unavailable")
    state_token = authn.jwt_encode({"typ": "oidc_state", "st": state, "no": nonce,
                                    "vf": verifier, "nx": _safe_next(next),
                                    "exp": int(time.time()) + _STATE_TTL})
    resp = RedirectResponse(auth_url, status_code=HTTP_303_SEE_OTHER)
    resp.set_cookie(_STATE_COOKIE, state_token, max_age=_STATE_TTL, httponly=True,
                    secure=settings.auth_cookie_secure, samesite="lax", path="/")
    return resp


@router.get("/oidc/callback")
def oidc_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return _fail(error[:40])
    if not oidc.offered_on(request) or not code:
        return _fail("bad request")
    saved = authn.jwt_decode(request.cookies.get(_STATE_COOKIE, ""))
    if not saved or saved.get("typ") != "oidc_state":
        return _fail("expired handshake")
    if not secrets.compare_digest(saved.get("st", ""), state):
        return _fail("state mismatch")

    tokens = oidc.exchange_code(code, _redirect_uri(request), saved.get("vf", ""))
    if not tokens or not tokens.get("id_token"):
        return _fail("token exchange failed")
    claims = oidc.decode_id_token(tokens["id_token"])
    if claims is None:
        return _fail("invalid id_token")
    bad = oidc.validate_claims(claims, saved.get("no", ""))
    if bad:
        return _fail(bad)

    fields = oidc.user_fields(claims)
    if not fields.get("sub") or not fields.get("username"):
        return _fail("missing subject")
    with SessionLocal() as session:
        try:
            user = authn.provision_oidc_user(
                session, sub=fields["sub"], username=fields["username"],
                email=fields.get("email"), is_admin=fields["is_admin"])
        except authn.SSOLinkConflict:
            return _fail("username linked to another SSO account")
        if user is None:
            return _fail("account not provisioned")
        from datetime import datetime, timezone
        user.last_login_at = datetime.now(timezone.utc)
        session.commit()
        token = authn.issue_web_token(user)

    resp = RedirectResponse(_safe_next(saved.get("nx", "/")), status_code=HTTP_303_SEE_OTHER)
    authn.set_session_cookie(resp, token)
    resp.delete_cookie(_STATE_COOKIE, path="/")
    return resp
