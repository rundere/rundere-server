"""Yonder 4.0 auth core (Phase 1a — SPEC-4 Workstream B, D-111/D-113).

Dependency-free by design, matching app/auth.py's hand-rolled-HMAC ethos:

- **passwords**: hashlib.scrypt (stdlib), stored as
  ``scrypt$<n>$<r>$<p>$<salt_hex>$<dk_hex>``.
- **access / web tokens**: hand-rolled **HS256 JWT** (stdlib hmac/hashlib/
  base64), signed with ``TX_JWT_SECRET``. Stateless; every token carries a
  ``ver`` claim = ``User.token_version`` so bumping the version invalidates all
  outstanding tokens ("sign out everywhere").
- **refresh tokens**: opaque random secret handed to the client; only its
  sha256 is persisted (``RefreshToken.token_hash``). Rotated on every use.

Providers all resolve to a Yonder ``User`` → one uniform token flow (D-111):
``local`` (username+password, here), ``proxy`` (trusted reverse-proxy header,
D-113, off by default), ``oidc`` (Phase 1b, Authlib — not here).

Phase 1a establishes **authentication** (who you are) only; per-user data
**isolation** is Phase 2.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import Request

from app.config import settings
from app.models import PasswordResetToken, RefreshToken, TravelerIdentity, User

# A process-stable fallback key so dev/first-run works before TX_JWT_SECRET is
# set. Prod MUST set the secret (else tokens die on every restart).
_EPHEMERAL_SECRET = secrets.token_hex(32)

_PROXY_HEADERS = ("x-authentik-username", "x-forwarded-user",
                  "x-auth-request-user", "remote-user")

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
# Verified against when the username doesn't exist, so an unknown name costs the
# same scrypt as a wrong password and response times don't reveal which
# usernames exist (YONDER-293). Made on first use: start-up stays fast.
_DUMMY_HASH: str | None = None


def _secret() -> str:
    return settings.jwt_secret or _EPHEMERAL_SECRET


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat them as UTC so comparisons with
    an aware _now() don't raise."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                        p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, n, r, p, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(dk_hex) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


# --------------------------------------------------------------------------- JWT (HS256)
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def jwt_encode(claims: dict) -> str:
    header = _b64u(b'{"alg":"HS256","typ":"JWT"}')
    payload = _b64u(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(_secret().encode(), f"{header}.{payload}".encode(),
                   hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64u(sig)}"


def jwt_decode(token: str) -> dict | None:
    """Verify signature + expiry; return claims or None."""
    try:
        header, payload, sig = token.split(".")
    except (ValueError, AttributeError):
        return None
    expected = hmac.new(_secret().encode(), f"{header}.{payload}".encode(),
                        hashlib.sha256).digest()
    if not hmac.compare_digest(_b64u(expected), sig):
        return None
    try:
        claims = json.loads(_b64u_dec(payload))
    except (ValueError, TypeError):
        return None
    exp = claims.get("exp")
    if exp is not None and time.time() > float(exp):
        return None
    return claims


def _token(user: User, typ: str, ttl: int) -> str:
    now = int(time.time())
    return jwt_encode({"sub": user.id, "username": user.username,
                       "adm": bool(user.is_admin), "ver": user.token_version,
                       "typ": typ, "iat": now, "exp": now + ttl})


def issue_access_token(user: User) -> str:
    return _token(user, "access", settings.jwt_access_ttl_seconds)


def issue_web_token(user: User) -> str:
    return _token(user, "web", settings.jwt_web_ttl_seconds)


def issue_api_token(user: User, *, ttl: int | None = None) -> str:
    """A long-lived bearer token for a MACHINE client (the MCP server, D-126).
    Same per-user scoping as any access token; revocable en masse by bumping the
    user's token_version ("sign out everywhere"). Minted via the CLI."""
    return _token(user, "api", ttl if ttl is not None else settings.jwt_api_ttl_seconds)


def user_from_token(session, token: str, kinds: tuple[str, ...] = ("access", "web"),
                    audiences: set[str] | None = None) -> User | None:
    """Resolve a Yonder JWT to its user. An `oauth` token (YONDER-107) must also
    name an audience in `audiences` (when given) and a grant that is still live."""
    claims = jwt_decode(token)
    if not claims or claims.get("typ") not in kinds:
        return None
    user = session.get(User, claims.get("sub"))
    if user is None or not user.is_active:
        return None
    if claims.get("ver") != user.token_version:   # revoked via "sign out everywhere"
        return None
    if claims.get("typ") == "oauth":
        from app import oauth
        if audiences is not None and claims.get("aud") not in audiences:
            return None
        if not oauth.grant_allows(session, claims, user):
            return None
    return user


def user_from_bearer(session, token: str, request=None) -> User | None:
    """A bearer credential → user: a personal API token (`ypat_…`), or a JWT of
    kind access / api (CLI-minted) / oauth (YONDER-107)."""
    from app import oauth
    if token.startswith(oauth.PAT_PREFIX):
        return oauth.pat_user(session, token)
    auds = oauth.audiences(request) if request is not None else None
    return user_from_token(session, token, kinds=("access", "api", "oauth"), audiences=auds)


# Machine endpoints (YONDER-107/109): reached from the open internet without the
# Authentik forward-auth, so an identity header on them is never trusted — even
# with TX_AUTH_PROXY_HEADER_TRUST on (Traefik strips it there too).
_MACHINE_EXACT = {"/mcp", "/oauth/token", "/oauth/register", "/oauth/revoke"}
_MACHINE_PREFIX = ("/mcp/", "/api/", "/.well-known/")


def is_machine_path(path: str) -> bool:
    return path in _MACHINE_EXACT or path.startswith(_MACHINE_PREFIX)


# --------------------------------------------------------------------------- refresh tokens
def _hash_opaque(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def create_refresh_token(session, user: User, user_agent: str | None = None) -> str:
    opaque = "ytr_" + secrets.token_hex(32)
    session.add(RefreshToken(
        user_id=user.id, token_hash=_hash_opaque(opaque),
        expires_at=_now() + timedelta(seconds=settings.jwt_refresh_ttl_seconds),
        user_agent=(user_agent or "")[:256] or None,
    ))
    session.commit()
    return opaque


def consume_refresh_token(session, opaque: str) -> User | None:
    """Validate AND rotate: the presented token is revoked; the caller issues a
    fresh one. Returns the owning user, or None if invalid/expired/revoked or
    the user is gone/inactive. Refresh rows carry no token_version: every bump
    goes through end_sessions(), which revokes them."""
    row = session.query(RefreshToken).filter_by(token_hash=_hash_opaque(opaque)).one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    if _now() > _as_aware(row.expires_at):
        return None
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    row.revoked_at = _now()
    session.commit()
    return user


def end_sessions(session, user: User) -> None:
    """Sign the user out everywhere: bump token_version (every JWT, OAuth grant
    and personal API token dies with it) AND revoke their refresh tokens, which
    don't carry the version — without this a stolen app refresh token outlived a
    password reset (YONDER-293). The caller commits."""
    user.token_version += 1
    (session.query(RefreshToken)
     .filter(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
     .update({RefreshToken.revoked_at: _now()}, synchronize_session=False))


def revoke_refresh_token(session, opaque: str) -> bool:
    row = session.query(RefreshToken).filter_by(token_hash=_hash_opaque(opaque)).one_or_none()
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = _now()
    session.commit()
    return True


# --------------------------------------------------------------------------- providers
def authenticate_local(session, username: str, password: str) -> User | None:
    global _DUMMY_HASH
    user = session.query(User).filter_by(username=username).one_or_none()
    if user is None or not user.is_active or user.auth_source != "local":
        _DUMMY_HASH = _DUMMY_HASH or hash_password(secrets.token_hex(8))
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def _proxy_identity(request) -> str | None:
    primary = (settings.auth_proxy_header or "").lower()
    for h in ([primary] if primary else []) + list(_PROXY_HEADERS):
        v = request.headers.get(h)
        if v and v.strip():
            return v.strip()
    return None


class SSOLinkConflict(Exception):
    """The IdP username belongs to an account already linked to a different
    OIDC subject — refuse rather than sign the caller in as that account."""


def provision_oidc_user(session, *, sub: str, username: str, email: str | None = None,
                        is_admin: bool = False) -> User | None:
    """Find-or-provision the User for an OIDC subject (Phase 1b, D-118). Matches
    on external_subject `oidc:<sub>` (or a same-named local account being adopted
    by SSO). Returns None if the user is unknown and auto-create is off."""
    subject = f"oidc:{sub}"
    # Subject first: once linked, the IdP subject is the identity (YONDER-143).
    # Only then fall back to adopting a same-named account — and never one that
    # is already bound to a DIFFERENT OIDC subject, or a renamed/recycled IdP
    # username would sign in as someone else.
    user = session.query(User).filter_by(external_subject=subject).one_or_none()
    if user is None:
        user = session.query(User).filter_by(username=username).one_or_none()
        if user is not None and (user.external_subject or "").startswith("oidc:"):
            raise SSOLinkConflict(username)
    if user is None:
        if not settings.oidc_auto_create:
            return None
        # Signed themselves up via SSO: ask at first sign-in whether to share
        # their data with admins (4.1).
        user = User(username=username, email=email, auth_source="oidc",
                    external_subject=subject, is_admin=is_admin, sharing_prompt=True)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    # Existing account: adopt the SSO subject, fill a missing email, and allow an
    # UP-grade to admin from the mapped group — never an automatic downgrade.
    # A proxy-header link came from the same IdP username, so SSO takes it over.
    if user.external_subject is None or user.external_subject.startswith("proxy:"):
        user.external_subject = subject
    if email and not user.email:
        user.email = email
    if is_admin and not user.is_admin:
        user.is_admin = True
    session.commit()
    return user


def resolve_proxy_user(session, request) -> User | None:
    """D-113: trust the reverse proxy's identity header (Authentik etc.) and
    auto-provision/link a User. Only reached when auth_proxy_header_trust is on
    — and it must only be enabled where the proxy sets the header and strips any
    client-supplied copy, or a client could spoof any identity."""
    name = _proxy_identity(request)
    if not name:
        return None
    subject = f"proxy:{name}"
    # Subject first, then username — never both at once, which 500s when they
    # match two different accounts (YONDER-143).
    user = (session.query(User).filter_by(external_subject=subject).one_or_none()
            or session.query(User).filter_by(username=name).one_or_none())
    if user is None:
        user = User(username=name, auth_source="proxy", external_subject=subject,
                    is_active=True, is_admin=settings.auth_proxy_auto_admin, sharing_prompt=True)
        session.add(user)
        session.commit()
    # A deactivated account stays out even when the proxy vouches for the name
    # (YONDER-293): every other sign-in path checks is_active too.
    return user if user.is_active else None


def resolve_user(session, request) -> User | None:
    """The one request→user resolver used by the web gate and the API. Order:
    bearer token (JWT or personal API token) → session cookie (web token) →
    trusted proxy header (browser routes only, never machine endpoints)."""
    authz = request.headers.get("authorization", "")
    if authz[:7].lower() == "bearer ":
        u = user_from_bearer(session, authz[7:].strip(), request)
        if u:
            return u
    cookie = request.cookies.get(settings.auth_cookie_name)
    if cookie:
        u = user_from_token(session, cookie, kinds=("web", "access"))
        if u:
            return u
    if settings.auth_proxy_header_trust and not is_machine_path(request.url.path):
        return resolve_proxy_user(session, request)
    return None


# --------------------------------------------------------------------------- cookie + FastAPI deps
def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        settings.auth_cookie_name, token, max_age=settings.jwt_web_ttl_seconds,
        httponly=True, secure=settings.auth_cookie_secure, samesite="lax", path="/")


def clear_session_cookie(response) -> None:
    response.delete_cookie(settings.auth_cookie_name, path="/")


def current_user_optional(request: "Request") -> "User | None":
    """FastAPI dependency: the authenticated user, or None. Prefers the user the
    web gate already resolved onto request.state (one lookup per request).

    `request` MUST be annotated `Request` — without the annotation FastAPI reads
    it as a query parameter and every call 422s."""
    from app.db import SessionLocal
    cached = getattr(request.state, "user", None)
    if cached is not None:
        return cached
    with SessionLocal() as session:
        user = resolve_user(session, request)
        if user is not None:
            session.expunge(user)
        return user


def is_first_party(request) -> bool:
    """Was this request made by Yonder itself — the web session cookie, the
    app's own login token (`typ="access"`, from /api/v1/auth/login|refresh), or
    an OAuth token issued to the built-in app client (D-194, YONDER-235)?
    Connected apps / agents (other OAuth clients), personal API tokens and
    CLI-minted API tokens are NOT: they may use the user's data, but must not
    change the account's own settings (sharing with admins, email). YONDER-101.
    This only classifies the credential; validity is current_user's job."""
    authz = request.headers.get("authorization", "")
    if authz[:7].lower() == "bearer ":
        claims = jwt_decode(authz[7:].strip()) or {}
        if claims.get("typ") == "oauth":
            from app import oauth
            from app.db import SessionLocal
            with SessionLocal() as session:
                return oauth.first_party_client(session, claims.get("cid"))
        return claims.get("typ") == "access"
    return bool(request.cookies.get(settings.auth_cookie_name))


def first_party_user(request: "Request") -> "User":
    """FastAPI dependency: an authenticated user on a first-party credential
    (403 for connected apps and API tokens)."""
    from fastapi import HTTPException
    user = current_user(request)
    if not is_first_party(request):
        raise HTTPException(status_code=403, detail="change this in the Run·dere app or on "
                            "the web — connected apps and API tokens can't")
    return user


def current_user(request: "Request") -> "User":
    """FastAPI dependency: require an authenticated user (401 otherwise)."""
    from fastapi import HTTPException
    user = current_user_optional(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def require_admin(request: "Request") -> "User":
    """FastAPI dependency: require an authenticated admin (403 otherwise)."""
    from fastapi import HTTPException
    user = current_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return user


# --------------------------------------------------------------------------- onboarding (D-117)
def user_count(session) -> int:
    return session.query(User).count()


def create_user(session, username: str, password: str | None = None, *,
                email: str | None = None, is_admin: bool = False,
                auth_source: str = "local", traveler_id: int | None = None,
                external_subject: str | None = None,
                sharing_prompt: bool = False) -> User:
    user = User(username=username.strip(), email=(email or "").strip() or None,
                password_hash=hash_password(password) if password else None,
                is_admin=is_admin, auth_source=auth_source,
                traveler_id=traveler_id, external_subject=external_subject,
                sharing_prompt=sharing_prompt)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def email_in_use(session, email: str, *, exclude_id: str | None = None) -> bool:
    """Emails are unique, case-insensitively: /forgot looks a user up by email,
    so two accounts sharing one would make that lookup ambiguous."""
    from sqlalchemy import func
    q = session.query(User).filter(func.lower(User.email) == email.strip().lower())
    if exclude_id:
        q = q.filter(User.id != exclude_id)
    return q.count() > 0


def set_user_email(session, user: User, email: str | None) -> str | None:
    """Set (or clear, with an empty value) a user's email. Returns an error
    message instead of saving when it's malformed or already taken."""
    email = (email or "").strip() or None
    if email is not None:
        if "@" not in email or " " in email or len(email) > 255:
            return "That doesn't look like an email address."
        if email_in_use(session, email, exclude_id=user.id):
            return "That email is already used by another account."
    user.email = email
    session.commit()
    return None


def resolve_traveler(session, *, traveler_id: str | int | None,
                     new_name: str | None, kind: str = "guest") -> int | None:
    """Onboarding traveller link (D-117: pick existing OR create new). A non-empty
    `new_name` mints a TravelerIdentity; else an existing id is used as given."""
    if new_name and new_name.strip():
        traveler = TravelerIdentity(display_name=new_name.strip(), kind=kind)
        session.add(traveler)
        session.commit()
        session.refresh(traveler)
        return traveler.id
    if traveler_id in (None, "", "none"):
        return None
    return int(traveler_id)


def create_reset_token(session, user: User) -> str:
    opaque = "yrst_" + secrets.token_hex(32)
    session.add(PasswordResetToken(
        user_id=user.id, token_hash=_hash_opaque(opaque),
        expires_at=_now() + timedelta(seconds=settings.password_reset_ttl_seconds)))
    session.commit()
    return opaque


def peek_reset_token(session, opaque: str) -> User | None:
    """Validate a reset token WITHOUT consuming it (for rendering the reset form)."""
    row = session.query(PasswordResetToken).filter_by(token_hash=_hash_opaque(opaque)).one_or_none()
    if row is None or row.used_at is not None or _now() > _as_aware(row.expires_at):
        return None
    return session.get(User, row.user_id)


def consume_reset_token(session, opaque: str, new_password: str) -> User | None:
    """Validate + spend the token, set the new password, and sign the user out
    everywhere (token_version bump). Returns the user or None if invalid."""
    row = session.query(PasswordResetToken).filter_by(token_hash=_hash_opaque(opaque)).one_or_none()
    if row is None or row.used_at is not None or _now() > _as_aware(row.expires_at):
        return None
    user = session.get(User, row.user_id)
    if user is None:
        return None
    user.password_hash = hash_password(new_password)
    end_sessions(session, user)
    row.used_at = _now()
    session.commit()
    return user
