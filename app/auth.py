"""/ingest authentication (HANDOFF: the security-critical surface).

Model:
- Per-client revocable API keys (Karakeep style). Plaintext `tx_<40 hex>` is
  shown once at creation; the DB stores only its sha256.
- Signed timestamp replay guard: the client sends
      X-Api-Key:    tx_...
      X-Timestamp:  unix seconds
      X-Signature:  hex(HMAC_SHA256(api_key, f"{timestamp}.{raw_body}"))
  The signature binds timestamp+body to this request, so a captured request
  cannot be replayed with a new body or outside the ~5-minute window.
- Per-key in-process rate limit (sliding window) so retry storms from Apps
  Script can never escalate into CrowdSec banning Google's egress ranges.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from app.config import settings
from app.models import ApiKey


def generate_key() -> str:
    return "tx_" + secrets.token_hex(20)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def sign(key: str, timestamp: str, body: bytes) -> str:
    return hmac.new(key.encode(), f"{timestamp}.".encode() + body,
                    hashlib.sha256).hexdigest()


def lookup_key(session, presented: str) -> ApiKey | None:
    row = session.query(ApiKey).filter_by(key_hash=hash_key(presented)).one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    return row


def check_timestamp(timestamp: str, now: float | None = None) -> str | None:
    """Returns an error string, or None if the timestamp is acceptable."""
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return "invalid timestamp"
    now = now if now is not None else time.time()
    if now - ts > settings.ingest_max_skew_seconds:
        return "timestamp too old (replay window exceeded)"
    if ts - now > 60:
        return "timestamp in the future"
    return None


def check_signature(key: str, timestamp: str, body: bytes, presented_sig: str) -> bool:
    return hmac.compare_digest(sign(key, timestamp, body), (presented_sig or "").lower())


class RateLimiter:
    """Per-key sliding window, in-process. Deliberately simple for Phase 0."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}

    def allow(self, key_id: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        hits = [t for t in self._hits.get(key_id, []) if now - t < self.window]
        if len(hits) >= self.limit:
            self._hits[key_id] = hits
            return False
        hits.append(now)
        self._hits[key_id] = hits
        return True


rate_limiter = RateLimiter(settings.ingest_rate_limit,
                           settings.ingest_rate_window_seconds)
