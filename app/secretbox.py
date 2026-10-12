"""Credentials encrypted at rest (SPEC-8 D-151, YONDER-192).

Per-user integration tokens are stored as AES-256-GCM ciphertext under a
dedicated key that is separate from `jwt_secret`:

- **Keyring.** `TX_SECRETS_KEYS="v2:<key>,v1:<key>"` — new values use the first
  key; any listed key decrypts. `TX_SECRETS_KEY` (or `TX_SECRETS_KEY_FILE`, a
  Docker secret) is shorthand for a single key named `v1`. A key is 32 random
  bytes as base64 or 64 hex characters (`openssl rand -base64 32`).
- **No key, no connections.** There's deliberately no per-process fallback key:
  stored credentials would be unreadable after a restart.
- **Bound to its row.** Each value is `<key id>:<base64(nonce ‖ ciphertext)>`
  with a fresh 12-byte nonce, and the associated data is
  `integration_id|user_id|kind`, so a ciphertext copied onto another row fails
  to decrypt.
- **Rotation.** Add the new key first in TX_SECRETS_KEYS, run
  `python -m app.cli secrets-rotate` to re-encrypt every row, then retire the
  old key.

Errors never carry plaintext or key material.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
from functools import lru_cache

_KID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


class SecretError(Exception):
    """Unusable keyring or a value that can't be decrypted. The message names
    the problem, never the secret."""


def _parse_key(raw: str) -> bytes:
    raw = (raw or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        return bytes.fromhex(raw)
    try:
        key = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_"
                               if ("-" in raw or "_" in raw) else None, validate=True)
    except (binascii.Error, ValueError):
        raise SecretError("a secrets key must be 32 bytes as base64 or hex") from None
    if len(key) != 32:
        raise SecretError("a secrets key must be exactly 32 bytes")
    return key


def parse_keyring(keys: str = "", key: str = "", key_file: str = "") -> list[tuple[str, bytes]]:
    """[(key id, key)] newest first, from the three settings (keys wins)."""
    ring: list[tuple[str, bytes]] = []
    if keys.strip():
        for part in keys.split(","):
            part = part.strip()
            if not part:
                continue
            kid, sep, val = part.partition(":")
            if not sep or not _KID.match(kid):
                raise SecretError("TX_SECRETS_KEYS entries look like v2:<key>")
            if any(k == kid for k, _ in ring):
                raise SecretError(f"key id {kid} is listed twice")
            ring.append((kid, _parse_key(val)))
        return ring
    if not key.strip() and key_file.strip():
        try:
            with open(key_file.strip(), encoding="utf-8") as fh:
                key = fh.read()
        except OSError:
            raise SecretError("TX_SECRETS_KEY_FILE can't be read") from None
    if key.strip():
        ring.append(("v1", _parse_key(key)))
    return ring


@lru_cache(maxsize=1)
def _ring_from(keys: str, key: str, key_file: str) -> tuple[tuple[str, bytes], ...]:
    return tuple(parse_keyring(keys, key, key_file))


def keyring() -> tuple[tuple[str, bytes], ...]:
    from app.config import settings
    return _ring_from(settings.secrets_keys, settings.secrets_key, settings.secrets_key_file)


def available() -> bool:
    """True when a usable key is configured. A malformed key counts as none
    (and is reported by `problem()`)."""
    try:
        return bool(keyring())
    except SecretError:
        return False


def problem() -> str | None:
    """Why connections can't be stored, for the admin page; None when fine."""
    try:
        return None if keyring() else "No secrets key is configured (TX_SECRETS_KEY)."
    except SecretError as err:
        return str(err)


def aad_for(integration_id: str, user_id: str, kind: str) -> bytes:
    return f"{integration_id}|{user_id}|{kind}".encode()


def encrypt(plaintext: str, aad: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ring = keyring()
    if not ring:
        raise SecretError("no secrets key is configured")
    kid, key = ring[0]
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
    return f"{kid}:{base64.b64encode(nonce + ct).decode()}"


def key_id(token: str | None) -> str | None:
    kid, sep, _ = (token or "").partition(":")
    return kid if sep else None


def decrypt(token: str, aad: bytes) -> str:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    kid, sep, body = (token or "").partition(":")
    if not sep:
        raise SecretError("not an encrypted value")
    key = dict(keyring()).get(kid)
    if key is None:
        raise SecretError(f"encrypted with key {kid}, which isn't configured")
    try:
        blob = base64.b64decode(body, validate=True)
        return AESGCM(key).decrypt(blob[:12], blob[12:], aad).decode("utf-8")
    except (InvalidTag, binascii.Error, ValueError):
        raise SecretError("the stored credential can't be decrypted") from None


def hint(plaintext: str) -> str:
    """The last four characters, for "saved · ends in …1234". Short secrets show
    none, so a hint never gives most of one away."""
    s = (plaintext or "").strip()
    return s[-4:] if len(s) >= 12 else ""
