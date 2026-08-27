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


def user_from_token(session, token: str, kinds: tuple[str, ...] = ("access", "web")) -> User | None:
    claims = jwt_decode(token)
    if not claims or claims.get("typ") not in kinds:
        return None
    user = session.get(User, claims.get("sub"))
    if user is None or not user.is_active:
        return None
    if claims.get("ver") != user.token_version:   # revoked via "sign out everywhere"
        return None
    return user


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
    fresh one. Returns the owning user, or None if invalid/expired/revoked, the
    user is gone/inactive, or its token_version has moved on."""
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


def revoke_refresh_token(session, opaque: str) -> bool:
    row = session.query(RefreshToken).filter_by(token_hash=_hash_opaque(opaque)).one_or_none()
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = _now()
    session.commit()
    return True


# --------------------------------------------------------------------------- providers
def authenticate_local(session, username: str, password: str) -> User | None:
    user = session.query(User).filter_by(username=username).one_or_none()
    if user is None or not user.is_active or user.auth_source != "local":
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


def resolve_proxy_user(session, request) -> User | None:
    """D-113: trust the reverse proxy's identity header (Authentik etc.) and
    auto-provision/link a User. Only reached when auth_proxy_header_trust is on
    — and it must only be enabled where the proxy sets the header and strips any
    client-supplied copy, or a client could spoof any identity."""
    name = _proxy_identity(request)
    if not name:
        return None
    subject = f"proxy:{name}"
    user = session.query(User).filter(
        (User.external_subject == subject) | (User.username == name)
    ).one_or_none()
    if user is None:
        user = User(username=name, auth_source="proxy", external_subject=subject,
                    is_active=True, is_admin=settings.auth_proxy_auto_admin)
        session.add(user)
        session.commit()
    return user


def resolve_user(session, request) -> User | None:
    """The one request→user resolver used by the web gate and the API. Order:
    bearer access token → session cookie (web token) → trusted proxy header."""
    authz = request.headers.get("authorization", "")
    if authz[:7].lower() == "bearer ":
        u = user_from_token(session, authz[7:].strip(), kinds=("access",))
        if u:
            return u
    cookie = request.cookies.get(settings.auth_cookie_name)
    if cookie:
        u = user_from_token(session, cookie, kinds=("web", "access"))
        if u:
            return u
    if settings.auth_proxy_header_trust:
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
                external_subject: str | None = None) -> User:
    user = User(username=username.strip(), email=(email or "").strip() or None,
                password_hash=hash_password(password) if password else None,
                is_admin=is_admin, auth_source=auth_source,
                traveler_id=traveler_id, external_subject=external_subject)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


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
    user.token_version += 1
    row.used_at = _now()
    session.commit()
    return user
