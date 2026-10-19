"""OAuth 2.1 authorization-server endpoints (YONDER-107, SPEC-4.2 §3.7).

- `/.well-known/oauth-protected-resource[/mcp]` (RFC 9728) and
  `/.well-known/oauth-authorization-server` (RFC 8414) — how claude.ai finds us.
- `GET/POST /oauth/authorize` — a BROWSER page: sign in (local / OIDC / proxy,
  whatever the deployment has, via the normal /login), then consent.
- `POST /oauth/token` — authorization_code (PKCE S256) and refresh_token.
- `POST /oauth/register` — dynamic client registration (RFC 7591), rate-limited.
- `POST /oauth/revoke` — token revocation (RFC 7009).

The machine endpoints (token/register/revoke/well-known) are public in the auth
gate and never honour a proxy identity header (app/authn.is_machine_path).
Protocol logic lives in app/oauth.py.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import authn, oauth
from app.auth import RateLimiter, client_ip
from app.config import settings
from app.db import get_session
from app.models import OAuthClient

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))
from app.icons import install as _install_icons  # noqa: E402
_install_icons(templates)

_CONSENT_TTL = 600            # seconds to decide on the consent page
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

register_limiter = RateLimiter(settings.oauth_register_rate_limit,
                               settings.oauth_register_rate_window_seconds)
token_limiter = RateLimiter(settings.oauth_token_rate_limit,
                            settings.oauth_token_rate_window_seconds)


def _client_ip(request: Request) -> str:
    return client_ip(request)


def _oauth_error(e: oauth.OAuthError) -> JSONResponse:
    body = {"error": e.error}
    if e.description:
        body["error_description"] = e.description
    headers = dict(_NO_STORE)
    if e.status == 401:
        headers["WWW-Authenticate"] = 'Basic realm="yonder"'
    return JSONResponse(body, status_code=e.status, headers=headers)


# --------------------------------------------------------------------------- metadata
def protected_resource_metadata(request: Request) -> dict:
    return {"resource": oauth.resource_url(request),
            "authorization_servers": [oauth.issuer(request)],
            "scopes_supported": [oauth.DEFAULT_SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": "Run·dere"}


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def well_known_protected_resource(request: Request):
    return protected_resource_metadata(request)


@router.get("/.well-known/oauth-authorization-server")
def well_known_authorization_server(request: Request):
    b = oauth.issuer(request)
    methods = ["none", "client_secret_post", "client_secret_basic"]
    return {"issuer": b,
            "authorization_endpoint": f"{b}/oauth/authorize",
            "token_endpoint": f"{b}/oauth/token",
            "registration_endpoint": f"{b}/oauth/register",
            "revocation_endpoint": f"{b}/oauth/revoke",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": methods,
            "revocation_endpoint_auth_methods_supported": methods,
            "scopes_supported": list(oauth.SCOPES)}


# --------------------------------------------------------------------------- authorize
def _error_page(request: Request, message: str, status: int = 400):
    return templates.TemplateResponse(request, "oauth_error.html",
                                      {"message": message}, status_code=status)


def _redirect_with(uri: str, params: dict) -> RedirectResponse:
    sep = "&" if urlsplit(uri).query else "?"
    clean = {k: v for k, v in params.items() if v}
    return RedirectResponse(f"{uri}{sep}{urlencode(clean)}", status_code=303)


def _validate_request(session, request: Request, p: dict):
    """Validate an authorization request. Returns (client, None) when OK, or
    (None, response) — an error PAGE while the client/redirect can't be trusted,
    else an error REDIRECT back to the client (RFC 6749 §4.1.2.1)."""
    client = session.get(OAuthClient, p.get("client_id") or "")
    if client is None:
        return None, _error_page(request, "Unknown application (client_id).")
    if not oauth.redirect_matches(client, p.get("redirect_uri") or ""):
        return None, _error_page(request, "This application's redirect address isn't "
                                          "registered, so Run·dere won't send you there.")
    ru, st = p["redirect_uri"], p.get("state")
    if p.get("response_type") != "code":
        return None, _redirect_with(ru, {"error": "unsupported_response_type", "state": st})
    if not p.get("code_challenge") or p.get("code_challenge_method") != "S256":
        return None, _redirect_with(ru, {"error": "invalid_request", "state": st,
                                         "error_description": "PKCE S256 is required"})
    if not oauth.resource_ok(request, p.get("resource")):
        return None, _redirect_with(ru, {"error": "invalid_target", "state": st})
    return client, None


_PARAMS = ("response_type", "client_id", "redirect_uri", "code_challenge",
           "code_challenge_method", "state", "scope", "resource")


@router.get("/oauth/authorize", response_class=HTMLResponse)
def authorize_page(request: Request, session: Session = Depends(get_session)):
    p = {k: request.query_params.get(k) for k in _PARAMS}
    client, bad = _validate_request(session, request, p)
    if bad is not None:
        return bad
    here = request.url.path + ("?" + request.url.query if request.url.query else "")
    user = authn.resolve_user(session, request)
    if user is None:
        return RedirectResponse(f"/login?next={quote(here, safe='')}", status_code=303)
    if getattr(user, "sharing_prompt", False):
        return RedirectResponse(f"/account/sharing?next={quote(here, safe='')}",
                                status_code=303)
    if client.first_party:
        # The Run·dere app itself (D-194): signing in is the consent. The code
        # can only go to the app's own registered universal link.
        return _issue_code(session, request, client, user, p)
    # Everything the decision needs rides in one signed, user-bound, short-lived
    # token: the POST can't be replayed by another user or tampered with, and a
    # cross-site page can't read it (so it doubles as the CSRF token).
    req = authn.jwt_encode({"typ": "oauth_consent", "uid": user.id,
                            "p": {k: p[k] for k in _PARAMS if p.get(k)},
                            "exp": int(time.time()) + _CONSENT_TTL})
    return templates.TemplateResponse(request, "oauth_consent.html", {
        "client": client, "user": user, "req": req,
        "redirect_host": urlsplit(p["redirect_uri"]).hostname,
        "loopback_only": oauth.loopback_only(client)})


@router.post("/oauth/authorize")
def authorize_decide(request: Request, req: str = Form(""), decision: str = Form(""),
                     session: Session = Depends(get_session)):
    user = authn.resolve_user(session, request)
    claims = authn.jwt_decode(req)
    if user is None or not claims or claims.get("typ") != "oauth_consent" \
            or claims.get("uid") != user.id:
        return _error_page(request, "This sign-in request expired. Start again from "
                                    "the application.", status=400)
    p = {k: None for k in _PARAMS} | (claims.get("p") or {})
    client, bad = _validate_request(session, request, p)
    if bad is not None:
        return bad
    if decision != "allow":
        return _redirect_with(p["redirect_uri"], {"error": "access_denied",
                                                  "state": p.get("state")})
    return _issue_code(session, request, client, user, p)


def _issue_code(session, request: Request, client: OAuthClient, user, p: dict):
    code = oauth.create_code(session, client=client, user=user,
                             redirect_uri=p["redirect_uri"],
                             code_challenge=p["code_challenge"],
                             scope=p.get("scope"), resource=p.get("resource"))
    oauth._audit(session, user.id, "oauth_sign_in" if client.first_party else "oauth_consent",
                 client.name, actor_id=user.id)
    session.commit()
    return _redirect_with(p["redirect_uri"], {"code": code, "state": p.get("state"),
                                              "iss": oauth.issuer(request)})


# --------------------------------------------------------------------------- token
def _client_credentials(request: Request, client_id: str | None,
                        client_secret: str | None) -> tuple[str | None, str | None]:
    authz = request.headers.get("authorization", "")
    if authz[:6].lower() == "basic ":
        try:
            from urllib.parse import unquote_plus
            raw = base64.b64decode(authz[6:].strip()).decode()
            cid, _, sec = raw.partition(":")
            return unquote_plus(cid), unquote_plus(sec)
        except (ValueError, UnicodeDecodeError):
            return None, None
    return client_id, client_secret


@router.post("/oauth/token")
def token(request: Request, grant_type: str = Form(""), code: str = Form(None),
          redirect_uri: str = Form(None), code_verifier: str = Form(None),
          refresh_token: str = Form(None), client_id: str = Form(None),
          client_secret: str = Form(None), resource: str = Form(None),
          session: Session = Depends(get_session)):
    if not token_limiter.allow(_client_ip(request)):
        return JSONResponse({"error": "slow_down"}, status_code=429, headers=_NO_STORE)
    try:
        cid, secret = _client_credentials(request, client_id, client_secret)
        client = oauth.authenticate_client(session, cid, secret)
        if not oauth.resource_ok(request, resource):
            raise oauth.OAuthError("invalid_target", "unknown resource")
        aud = oauth.resource_url(request)
        if grant_type == "authorization_code":
            body = oauth.exchange_code(session, client=client, code=code,
                                       redirect_uri=redirect_uri,
                                       code_verifier=code_verifier,
                                       resource=resource, default_aud=aud)
        elif grant_type == "refresh_token":
            body = oauth.refresh(session, client=client, refresh_token=refresh_token,
                                 default_aud=aud)
        else:
            raise oauth.OAuthError("unsupported_grant_type")
    except oauth.OAuthError as e:
        return _oauth_error(e)
    return JSONResponse(body, headers=_NO_STORE)


@router.post("/oauth/revoke")
def revoke(request: Request, token: str = Form(""), client_id: str = Form(None),
           client_secret: str = Form(None), session: Session = Depends(get_session)):
    try:
        cid, secret = _client_credentials(request, client_id, client_secret)
        client = oauth.authenticate_client(session, cid, secret)
    except oauth.OAuthError as e:
        return _oauth_error(e)
    oauth.revoke_token(session, client=client, token=token)
    return JSONResponse({}, headers=_NO_STORE)


# --------------------------------------------------------------------------- DCR
@router.post("/oauth/register", status_code=201)
async def register(request: Request, session: Session = Depends(get_session)):
    if not register_limiter.allow(_client_ip(request)):
        return JSONResponse({"error": "slow_down",
                             "error_description": "too many registrations"},
                            status_code=429)
    try:
        meta = await request.json()
    except ValueError:
        meta = None
    if not isinstance(meta, dict):
        return _oauth_error(oauth.OAuthError("invalid_client_metadata", "JSON body required"))
    method = meta.get("token_endpoint_auth_method") or "none"
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        return _oauth_error(oauth.OAuthError("invalid_client_metadata",
                                             "unsupported token_endpoint_auth_method"))
    grants = meta.get("grant_types") or ["authorization_code", "refresh_token"]
    if not isinstance(grants, list) or not set(grants) <= {"authorization_code", "refresh_token"}:
        return _oauth_error(oauth.OAuthError("invalid_client_metadata",
                                             "unsupported grant_types"))
    name = meta.get("client_name") if isinstance(meta.get("client_name"), str) else ""
    try:
        client, secret = oauth.register_client(
            session, name=name, redirect_uris=meta.get("redirect_uris"),
            confidential=method != "none", source="dcr")
    except oauth.OAuthError as e:
        return _oauth_error(e)
    out = {"client_id": client.id, "client_id_issued_at": int(time.time()),
           "client_name": client.name, "redirect_uris": client.redirect_uris,
           "grant_types": grants, "response_types": ["code"],
           "token_endpoint_auth_method": method, "scope": " ".join(oauth.SCOPES)}
    if secret:
        out["client_secret"] = secret
        out["client_secret_expires_at"] = 0
    return JSONResponse(out, status_code=201, headers=_NO_STORE)
