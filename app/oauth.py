"""Yonder as an OAuth 2.1 authorization server (YONDER-107, SPEC-4.2 §3.7).

The protocol logic, free of HTTP (the routes are app/oauth_routes.py):

- **clients**: dynamic registration (RFC 7591) or `app.cli oauth-client create`.
  Public clients (PKCE only) and confidential ones (sha256'd secret).
- **codes**: single-use, ≤60 s, stored hashed, bound to client + redirect_uri +
  PKCE S256 challenge + user. A replayed code revokes the grant it produced.
- **grants**: one per consent. Access token = a Yonder HS256 JWT (`typ="oauth"`,
  `aud` = this server's /mcp resource, `gid` = the grant) for ~1 h; refresh
  token = opaque `yrt_<grant id>_<secret>`, rotated on every use, sliding
  ~90-day idle expiry. Presenting a retired refresh token revokes the grant.
- **the app client** (D-194, YONDER-235): `rundere-ios`, built in on every
  server and upserted at startup. First-party: no consent screen, and its
  tokens may change account settings like the app's password login can.
- **personal API tokens**: opaque `ypat_…`, stored hashed, per-token revoke and
  optional expiry, for clients that can't do OAuth (Claude Code, stdio MCP).

Every token also dies when the user is deactivated or their `token_version`
moves on (password reset / "sign out everywhere").
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from app.config import settings
from app.models import (AccountEvent, OAuthClient, OAuthCode, OAuthGrant,
                        PersonalApiToken, User)

SCOPES = ("yonder", "offline_access")
APP_CLIENT_ID = "rundere-ios"           # the built-in iOS app client (D-194)
DEFAULT_SCOPE = "yonder"
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")
_MAX_REDIRECTS = 10
_PREV_HASHES_KEPT = 20
_LAST_USED_EVERY = timedelta(minutes=5)       # throttle last_used_at writes


class OAuthError(Exception):
    """An RFC 6749 error: `error` is the wire code, `description` the text."""

    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description or error)
        self.error, self.description, self.status = error, description, status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# --------------------------------------------------------------------------- URLs
def base_url(request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


def issuer(request) -> str:
    return base_url(request)


def resource_url(request) -> str:
    """The protected resource (RFC 9728) — the MCP endpoint's exact URL."""
    return f"{base_url(request)}/mcp"


def audiences(request) -> set[str]:
    """What an access token's `aud` may be: the /mcp resource or the server
    itself (the iOS app asks for the base URL)."""
    b = base_url(request)
    return {b, f"{b}/", f"{b}/mcp", f"{b}/mcp/"}


def resource_ok(request, resource: str | None) -> bool:
    return not resource or resource in audiences(request)


# --------------------------------------------------------------------------- redirect URIs
def _is_loopback(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    return parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK_HOSTS


def registrable_redirect(uri: str) -> bool:
    """https anywhere, or http on a loopback host (native clients / dev)."""
    if not isinstance(uri, str) or len(uri) > 2000 or "#" in uri:
        return False
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if not parts.hostname:
        return False
    return parts.scheme == "https" or _is_loopback(uri)


def redirect_matches(client: OAuthClient, uri: str) -> bool:
    """Exact match against the registered URIs — except a loopback URI, whose
    port is ignored (RFC 8252 §7.3; Claude Code picks an ephemeral port)."""
    if not uri:
        return False
    registered = client.redirect_uris or []
    if uri in registered:
        return True
    if not _is_loopback(uri):
        return False
    got = urlsplit(uri)
    for r in registered:
        if not _is_loopback(r):
            continue
        want = urlsplit(r)
        if (want.hostname == got.hostname and want.path == got.path
                and want.query == got.query):
            return True
    return False


def loopback_only(client: OAuthClient) -> bool:
    uris = client.redirect_uris or []
    return bool(uris) and all(_is_loopback(u) for u in uris)


# --------------------------------------------------------------------------- clients
def register_client(session, *, name: str, redirect_uris: list[str],
                    confidential: bool = False, source: str = "dcr",
                    created_by: str | None = None) -> tuple[OAuthClient, str | None]:
    """Create a client; returns (client, plaintext secret or None). Raises
    OAuthError(invalid_redirect_uri / invalid_client_metadata) on bad input."""
    name = (name or "").strip()[:128] or "Unnamed client"
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise OAuthError("invalid_redirect_uri", "redirect_uris is required")
    if len(redirect_uris) > _MAX_REDIRECTS:
        raise OAuthError("invalid_client_metadata", "too many redirect_uris")
    for u in redirect_uris:
        if not registrable_redirect(u):
            raise OAuthError("invalid_redirect_uri",
                             "redirect_uris must be https (or http on localhost)")
    secret = ("ycs_" + secrets.token_urlsafe(32)) if confidential else None
    client = OAuthClient(id="yc_" + secrets.token_urlsafe(18), name=name,
                         redirect_uris=list(dict.fromkeys(redirect_uris)),
                         secret_hash=_hash(secret) if secret else None,
                         source=source, created_by=created_by)
    session.add(client)
    session.commit()
    return client, secret


def app_redirect_uris() -> list[str]:
    return [u.strip() for u in settings.oauth_app_redirect_uris.split(",")
            if u.strip() and registrable_redirect(u.strip())]


def ensure_app_client(session) -> OAuthClient | None:
    """Upsert the built-in iOS app client (D-194) so every server supports the
    app's OAuth sign-in with no admin setup. Public (PKCE) and first-party; the
    redirect URIs follow TX_OAUTH_APP_REDIRECT_URIS. With none configured the
    row is kept but no redirect matches, so it can't be used."""
    uris = app_redirect_uris()
    client = session.get(OAuthClient, APP_CLIENT_ID)
    if client is None:
        if not uris:
            return None
        client = OAuthClient(id=APP_CLIENT_ID, name="Run·dere for iOS", source="builtin")
        session.add(client)
    client.redirect_uris, client.secret_hash, client.first_party = uris, None, True
    session.commit()
    return client


def app_client_ready(session) -> bool:
    c = session.get(OAuthClient, APP_CLIENT_ID)
    return bool(c is not None and c.first_party and c.redirect_uris)


def first_party_client(session, client_id: str | None) -> bool:
    c = session.get(OAuthClient, client_id) if client_id else None
    return bool(c is not None and c.first_party)


def authenticate_client(session, client_id: str | None, secret: str | None) -> OAuthClient:
    """Resolve the calling client. A confidential client must present its
    secret; a public client must not claim one it doesn't have."""
    client = session.get(OAuthClient, client_id) if client_id else None
    if client is None:
        raise OAuthError("invalid_client", "unknown client", status=401)
    if client.secret_hash:
        if not secret or not hmac.compare_digest(_hash(secret), client.secret_hash):
            raise OAuthError("invalid_client", "client authentication failed", status=401)
    return client


def normalize_scope(scope: str | None) -> str:
    """Keep the scopes Yonder knows; always includes `yonder`."""
    asked = [s for s in (scope or "").split() if s in SCOPES]
    return " ".join(dict.fromkeys([DEFAULT_SCOPE, *asked]))


# --------------------------------------------------------------------------- codes
def pkce_ok(verifier: str | None, challenge: str) -> bool:
    if not verifier or not (43 <= len(verifier) <= 128):
        return False
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return hmac.compare_digest(digest.rstrip(b"=").decode(), challenge or "")


def create_code(session, *, client: OAuthClient, user: User, redirect_uri: str,
                code_challenge: str, scope: str | None, resource: str | None) -> str:
    opaque = "yac_" + secrets.token_urlsafe(32)
    session.add(OAuthCode(
        code_hash=_hash(opaque), client_id=client.id, user_id=user.id,
        redirect_uri=redirect_uri, code_challenge=code_challenge,
        scope=normalize_scope(scope), resource=resource or None,
        expires_at=_now() + timedelta(seconds=settings.oauth_code_ttl_seconds)))
    session.commit()
    return opaque


# --------------------------------------------------------------------------- tokens
def issue_access(user: User, grant: OAuthGrant, aud: str) -> str:
    from app.authn import jwt_encode
    now = int(time.time())
    return jwt_encode({"sub": user.id, "username": user.username,
                       "adm": bool(user.is_admin), "ver": user.token_version,
                       "typ": "oauth", "gid": grant.id, "cid": grant.client_id,
                       "aud": aud, "scope": grant.scope or DEFAULT_SCOPE,
                       "iat": now, "exp": now + settings.oauth_access_ttl_seconds})


def _new_refresh(grant: OAuthGrant) -> str:
    opaque = f"yrt_{grant.id}_{secrets.token_urlsafe(32)}"
    if grant.refresh_hash:
        prev = list(grant.prev_refresh_hashes or [])
        prev.append(grant.refresh_hash)
        grant.prev_refresh_hashes = prev[-_PREV_HASHES_KEPT:]
    grant.refresh_hash = _hash(opaque)
    grant.expires_at = _now() + timedelta(seconds=settings.oauth_refresh_idle_ttl_seconds)
    return opaque


def _token_response(user: User, grant: OAuthGrant, refresh: str, aud: str) -> dict:
    return {"access_token": issue_access(user, grant, aud), "token_type": "Bearer",
            "expires_in": settings.oauth_access_ttl_seconds,
            "refresh_token": refresh, "scope": grant.scope or DEFAULT_SCOPE}


def _audit(session, user_id: str, action: str, detail: str = "",
           actor_id: str | None = None) -> None:
    session.add(AccountEvent(user_id=user_id, actor_id=actor_id, action=action,
                             detail=(detail or None)[:255] if detail else None))


def client_name(session, client_id: str) -> str:
    c = session.get(OAuthClient, client_id)
    return c.name if c else client_id


def revoke_grant(session, grant: OAuthGrant, *, action: str = "oauth_revoked",
                 actor_id: str | None = None) -> None:
    if grant.revoked_at is None:
        grant.revoked_at = _now()
        _audit(session, grant.user_id, action, client_name(session, grant.client_id),
               actor_id=actor_id)
    session.commit()


def _user_ok(user: User | None, grant: OAuthGrant) -> bool:
    return (user is not None and user.is_active
            and user.token_version == grant.token_version)


def exchange_code(session, *, client: OAuthClient, code: str | None,
                  redirect_uri: str | None, code_verifier: str | None,
                  resource: str | None, default_aud: str) -> dict:
    row = (session.query(OAuthCode).filter_by(code_hash=_hash(code)).one_or_none()
           if code else None)
    if row is None:
        raise OAuthError("invalid_grant", "unknown authorization code")
    if row.used_at is not None:
        # Replay: treat as a leak and kill whatever the first exchange made.
        if row.grant_id:
            g = session.get(OAuthGrant, row.grant_id)
            if g is not None:
                revoke_grant(session, g, action="oauth_code_replay")
        raise OAuthError("invalid_grant", "authorization code already used")
    row.used_at = _now()           # spend it before any other check
    session.commit()
    if _now() > _aware(row.expires_at):
        raise OAuthError("invalid_grant", "authorization code expired")
    if row.client_id != client.id:
        raise OAuthError("invalid_grant", "code was issued to another client")
    if row.redirect_uri != (redirect_uri or ""):
        raise OAuthError("invalid_grant", "redirect_uri mismatch")
    if not pkce_ok(code_verifier, row.code_challenge):
        raise OAuthError("invalid_grant", "PKCE verification failed")
    if resource and row.resource and resource != row.resource:
        raise OAuthError("invalid_target", "resource differs from the authorization request")
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise OAuthError("invalid_grant", "user is not active")
    grant = OAuthGrant(client_id=client.id, user_id=user.id, scope=row.scope,
                       resource=row.resource, token_version=user.token_version,
                       last_used_at=_now(), expires_at=_now())
    session.add(grant)
    session.flush()
    refresh = _new_refresh(grant)
    row.grant_id = grant.id
    session.commit()
    return _token_response(user, grant, refresh, row.resource or default_aud)


def _grant_from_refresh(session, opaque: str | None) -> OAuthGrant | None:
    if not opaque or not opaque.startswith("yrt_"):
        return None
    grant_id = opaque[4:].split("_", 1)[0]
    return session.get(OAuthGrant, grant_id) if grant_id else None


def refresh(session, *, client: OAuthClient, refresh_token: str | None,
            default_aud: str) -> dict:
    grant = _grant_from_refresh(session, refresh_token)
    if grant is None or grant.client_id != client.id:
        raise OAuthError("invalid_grant", "unknown refresh token")
    if grant.revoked_at is not None:
        raise OAuthError("invalid_grant", "grant revoked")
    h = _hash(refresh_token)
    if grant.refresh_hash is None or not hmac.compare_digest(h, grant.refresh_hash):
        if h in (grant.prev_refresh_hashes or []):
            # A retired token came back: someone else holds a copy. Kill the grant.
            revoke_grant(session, grant, action="oauth_refresh_reuse")
        raise OAuthError("invalid_grant", "refresh token is no longer valid")
    if _now() > _aware(grant.expires_at):
        raise OAuthError("invalid_grant", "grant expired")
    user = session.get(User, grant.user_id)
    if not _user_ok(user, grant):
        revoke_grant(session, grant, action="oauth_revoked_signout")
        raise OAuthError("invalid_grant", "user signed out or deactivated")
    new = _new_refresh(grant)
    grant.last_used_at = _now()
    session.commit()
    return _token_response(user, grant, new, grant.resource or default_aud)


def revoke_token(session, *, client: OAuthClient, token: str | None) -> None:
    """RFC 7009: revoking a refresh token (or an access token, via its `gid`)
    ends the grant. Unknown tokens are silently accepted."""
    grant = _grant_from_refresh(session, token)
    if grant is None and token:
        from app.authn import jwt_decode
        claims = jwt_decode(token) or {}
        if claims.get("typ") == "oauth" and claims.get("gid"):
            grant = session.get(OAuthGrant, claims["gid"])
    if grant is not None and grant.client_id == client.id:
        revoke_grant(session, grant)


def grant_allows(session, claims: dict, user: User) -> bool:
    """Is the grant behind an oauth access token still live?"""
    grant = session.get(OAuthGrant, claims.get("gid") or "")
    if grant is None or grant.revoked_at is not None or grant.user_id != user.id:
        return False
    if _now() > _aware(grant.expires_at):
        return False
    last = _aware(grant.last_used_at)
    if last is None or _now() - last > _LAST_USED_EVERY:
        grant.last_used_at = _now()
        session.commit()
    return True


def credential_label(session, token: str) -> str | None:
    """A human name for the credential behind a bearer token — the OAuth app's
    registered name or the personal API token's name — used to record WHICH
    agent wrote something (e.g. "Claude", "Perplexica"). None if unknown."""
    if not token:
        return None
    if token.startswith(PAT_PREFIX):
        row = session.query(PersonalApiToken).filter_by(token_hash=_hash(token)).one_or_none()
        return row.name if row is not None else None
    from app.authn import jwt_decode
    claims = jwt_decode(token) or {}
    if claims.get("typ") == "oauth" and claims.get("cid"):
        return client_name(session, claims["cid"])
    if claims.get("typ") == "api":
        return "API token"
    return None


def connected_apps(session, user: User) -> list[dict]:
    rows = (session.query(OAuthGrant)
            .filter(OAuthGrant.user_id == user.id, OAuthGrant.revoked_at.is_(None))
            .order_by(OAuthGrant.created_at.desc()).all())
    now = _now()
    return [{"id": g.id, "client": client_name(session, g.client_id),
             "app": first_party_client(session, g.client_id),
             "created_at": g.created_at, "last_used_at": g.last_used_at}
            for g in rows if _aware(g.expires_at) >= now
            and g.token_version == user.token_version]


# --------------------------------------------------------------------------- personal API tokens
PAT_PREFIX = "ypat_"


def create_pat(session, user: User, name: str,
               days: int | None = None) -> tuple[PersonalApiToken, str]:
    opaque = PAT_PREFIX + secrets.token_urlsafe(32)
    row = PersonalApiToken(
        user_id=user.id, name=(name or "").strip()[:128] or "API token",
        token_hash=_hash(opaque), token_version=user.token_version,
        expires_at=(_now() + timedelta(days=days)) if days else None)
    session.add(row)
    _audit(session, user.id, "pat_created", row.name, actor_id=user.id)
    session.commit()
    return row, opaque


def pat_user(session, opaque: str) -> User | None:
    row = session.query(PersonalApiToken).filter_by(token_hash=_hash(opaque)).one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at is not None and _now() > _aware(row.expires_at):
        return None
    user = session.get(User, row.user_id)
    if user is None or not user.is_active or user.token_version != row.token_version:
        return None
    last = _aware(row.last_used_at)
    if last is None or _now() - last > _LAST_USED_EVERY:
        row.last_used_at = _now()
        session.commit()
    return user


def list_pats(session, user: User) -> list[PersonalApiToken]:
    now = _now()
    rows = (session.query(PersonalApiToken)
            .filter(PersonalApiToken.user_id == user.id,
                    PersonalApiToken.revoked_at.is_(None))
            .order_by(PersonalApiToken.created_at.desc()).all())
    return [r for r in rows if (r.expires_at is None or _aware(r.expires_at) >= now)
            and r.token_version == user.token_version]


def revoke_pat(session, user: User, token_id: str) -> bool:
    row = session.get(PersonalApiToken, token_id)
    if row is None or row.user_id != user.id or row.revoked_at is not None:
        return False
    row.revoked_at = _now()
    _audit(session, user.id, "pat_revoked", row.name, actor_id=user.id)
    session.commit()
    return True
