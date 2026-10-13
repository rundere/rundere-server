"""OIDC client — pure stdlib (Yonder 4.0 Phase 1b, SPEC-4 D-118).

Authorization Code + PKCE against an external IdP (Authentik/Keycloak/…). No new
dependency: discovery and the token exchange use urllib (the app's HTTP style),
and the id_token is decoded — not JWKS-verified — because it arrives directly
from the token endpoint over a server-to-server TLS channel. OIDC Core §3.1.3.5
permits relying on that TLS channel in the code flow; we still validate the
`iss`, `aud`, `exp`, and `nonce` claims, and PKCE binds the code to this client.

Everything here is inert unless TX_OIDC_ENABLED plus an issuer and client id are
configured. The result of a successful login is a Yonder User (auth_source=oidc)
and the SAME Yonder session token every other path issues (D-111).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from app.config import settings

_DISCO_CACHE: dict = {}


def enabled() -> bool:
    return bool(settings.oidc_enabled and settings.oidc_issuer and settings.oidc_client_id)


def _host_list(csv: str) -> set[str]:
    return {h.strip().lower() for h in csv.split(",") if h.strip()}


def request_host(request) -> str:
    return (request.url.hostname or "").lower()


def listed_host(request) -> bool:
    """The request came in on a host named in TX_OIDC_HOSTS / TX_OIDC_FORCE_HOSTS."""
    host = request_host(request)
    return host in _host_list(settings.oidc_hosts) | _host_list(settings.oidc_force_hosts)


_DEFAULT_PORTS = {"https": 443, "http": 80}


def request_origin(request) -> str:
    """scheme://host[:port] for this request, rebuilt from the parsed hostname —
    never the raw Host header. A client can send `Host: name:443`, which passes
    the hostname-only allowlist but would put ":443" in the OIDC redirect_uri
    and fail the IdP's exact match (YONDER-234). Default ports are dropped; a
    real non-default port (a dev server on :8000) is kept."""
    scheme = request.url.scheme
    port = request.url.port
    origin = f"{scheme}://{request_host(request)}"
    if port and port != _DEFAULT_PORTS.get(scheme):
        origin += f":{port}"
    return origin


def forced_on(request) -> bool:
    """SSO is the only way to sign in on this host (YONDER-164)."""
    return enabled() and request_host(request) in _host_list(settings.oidc_force_hosts)


def offered_on(request) -> bool:
    """Show / allow "Sign in with SSO" on this host. No TX_OIDC_HOSTS = everywhere."""
    if not enabled():
        return False
    return not _host_list(settings.oidc_hosts) or listed_host(request)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def discovery() -> dict | None:
    """Fetch (and cache) the IdP's OpenID configuration."""
    if _DISCO_CACHE.get("issuer") == settings.oidc_issuer and _DISCO_CACHE.get("doc"):
        return _DISCO_CACHE["doc"]
    base = settings.oidc_issuer.rstrip("/")
    url = base + "/.well-known/openid-configuration"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Rundere-OIDC"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            doc = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    _DISCO_CACHE.update(issuer=settings.oidc_issuer, doc=doc)
    return doc


def new_pkce() -> tuple[str, str]:
    """(verifier, challenge) for PKCE S256."""
    verifier = _b64u(secrets.token_bytes(32))
    challenge = _b64u(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def build_auth_url(redirect_uri: str, state: str, nonce: str, challenge: str) -> str | None:
    doc = discovery()
    if not doc or not doc.get("authorization_endpoint"):
        return None
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return doc["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str, redirect_uri: str, verifier: str) -> dict | None:
    """Exchange the authorization code for tokens at the token endpoint (direct
    TLS back-channel). Returns the token response dict, or None on failure."""
    doc = discovery()
    if not doc or not doc.get("token_endpoint"):
        return None
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": settings.oidc_client_id,
        "client_secret": settings.oidc_client_secret,
        "code_verifier": verifier,
    }).encode()
    try:
        req = urllib.request.Request(
            doc["token_endpoint"], data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json", "User-Agent": "Rundere-OIDC"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def decode_id_token(id_token: str) -> dict | None:
    """Decode the JWT payload WITHOUT signature verification (see module docstring:
    the token came over the direct TLS token-endpoint channel)."""
    try:
        return json.loads(_b64u_dec(id_token.split(".")[1]))
    except (ValueError, IndexError, TypeError):
        return None


def validate_claims(claims: dict, nonce: str) -> str | None:
    """Return an error string, or None if the id_token claims are acceptable."""
    doc = discovery() or {}
    issuer = doc.get("issuer") or settings.oidc_issuer.rstrip("/")
    if claims.get("iss", "").rstrip("/") != issuer.rstrip("/"):
        return "issuer mismatch"
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if settings.oidc_client_id not in auds:
        return "audience mismatch"
    if claims.get("exp") and time.time() > float(claims["exp"]) + 30:
        return "id_token expired"
    if nonce and claims.get("nonce") != nonce:
        return "nonce mismatch"
    return None


def user_fields(claims: dict) -> dict:
    """Map id_token claims to Yonder User fields."""
    username = (claims.get(settings.oidc_username_claim)
                or claims.get("preferred_username") or claims.get("email")
                or claims.get("sub"))
    groups = claims.get("groups") or []
    is_admin = bool(settings.oidc_admin_group and settings.oidc_admin_group in groups)
    return {"sub": claims.get("sub"), "username": username,
            "email": claims.get("email"), "is_admin": is_admin}
