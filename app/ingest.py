"""POST /ingest — publicly reachable, hence the ceremony.

Returns 202 immediately after persisting the raw batch (Apps Script has a 30s
URL-fetch timeout; nothing is processed inline). Failure paths are deliberately
uniform 401s with distinct machine-readable `reason` codes for the tests.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app import auth
from app.config import settings
from app.db import get_session
from app.models import Artifact, IngestBatch

log = logging.getLogger("yonder.ingest")
router = APIRouter()


def _reject(status: int, reason: str) -> Response:
    return Response(status_code=status,
                    content=json.dumps({"error": reason}),
                    media_type="application/json")


def store_batch(session: Session, body: bytes, payload: dict,
                api_key_id: str) -> IngestBatch:
    """Content-address the raw submission (D-15) and queue it as a received
    batch. Shared by the public /ingest route (HMAC-authed) and the internal
    /capture/web route (proxy-authed): one storage path, two front doors. The
    caller commits."""
    sha = hashlib.sha256(body).hexdigest()
    if session.get(Artifact, sha) is None:
        session.add(Artifact(sha256=sha, content_type="application/json",
                             size_bytes=len(body)))
    batch = IngestBatch(
        api_key_id=api_key_id,
        declared_segment_count=payload.get("declared_segment_count"),
        payload=payload,
        artifact_sha256=sha,
    )
    session.add(batch)
    return batch


@router.post("/ingest", status_code=202)
async def ingest(request: Request, session: Session = Depends(get_session)):
    body = await request.body()
    if len(body) > settings.ingest_max_body_bytes:
        return _reject(413, "body too large")

    presented = request.headers.get("X-Api-Key", "")
    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-Signature", "")

    key_row = auth.lookup_key(session, presented) if presented else None
    if key_row is None:
        return _reject(401, "unknown or revoked api key")
    ts_error = auth.check_timestamp(timestamp)
    if ts_error:
        return _reject(401, ts_error)
    if not auth.check_signature(presented, timestamp, body, signature):
        log.warning("bad signature from key_id=%s (body_len=%d)", key_row.id, len(body))
        return _reject(401, "bad signature")
    if not auth.rate_limiter.allow(key_row.id):
        return _reject(429, "rate limit exceeded")

    try:
        payload = json.loads(body)
        assert isinstance(payload, dict)
    except Exception:
        return _reject(400, "body is not a JSON object")

    batch = store_batch(session, body, payload, key_row.id)
    key_row.last_used_at = datetime.now(timezone.utc)
    session.commit()
    return {"batch_id": batch.id, "status": "received"}
