"""Extract stage: ingest_batches(status=received) -> TravelItem rows.

One batch fans out to one TravelItem per Payload (D-12). Extraction failure
produces a single placeholder item with status.extract=needs_review so the
failure is visible in the review queue — never a silently stuck batch.
Resolution (canonical keys, collections, home) is the NEXT stage; items leave
here with resolve=pending and no canonical_key (x-owner: resolver).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import IngestBatch, TravelItem
from extract.llm import PROMPT_VERSION, OllamaExtractor
from extract.pipeline import extract_ingest_payload

log = logging.getLogger("yonder.processing")

_CONFIDENCE = {"jsonld": 0.9, "ical": 0.9, "maps_url": 0.9,
               "apple_maps_url": 0.9, "llm": 0.6}


def _source(batch: IngestBatch, payload: dict) -> dict:
    channel = payload.get("capture_channel") or "gmail"
    return {
        "type": {"gmail": "gmail", "maps_url": "maps", "apple_maps_url": "maps",
                 "gcal": "gcal", "web": "web"}.get(channel, "manual"),
        "ref": payload.get("message_id") or batch.artifact_sha256 or batch.id,
        "thread_ref": payload.get("thread_id"),
        "received_at": batch.received_at.isoformat(),
        "document_timestamp": payload.get("internal_date"),
        "api_key_id": batch.api_key_id,
        "capture_context": payload.get("capture_context"),
    }


def process_batch(session: Session, batch: IngestBatch,
                  llm: OllamaExtractor | None) -> str:
    """Runs the chain for one batch; returns the new batch status."""
    result = extract_ingest_payload(batch.payload, llm=llm)
    now = datetime.now(timezone.utc).isoformat()
    src = _source(batch, batch.payload)

    if result.ok:
        items = result.batch["items"]
        for i, payload_item in enumerate(items):
            session.add(TravelItem(
                batch_id=batch.id,
                owner_id=batch.user_id,          # Phase 2 (D-120): capture ownership
                item_type=payload_item["item_type"],
                segment_index=i,
                segment_count=len(items),
                schema_version="1.4.0",
                artifact_sha256=batch.artifact_sha256,
                source=src,
                extraction={
                    "extractor": result.extractor,
                    "model": llm.model if (llm and result.extractor == "llm") else None,
                    "prompt_version": PROMPT_VERSION if result.extractor == "llm" else None,
                    "ran_at": now,
                    "confidence": _CONFIDENCE.get(result.extractor, 0.5),
                    "payload": payload_item,
                },
                status={"extract": "ok", "resolve": "pending",
                        "airtrail": "pending", "adventurelog": {}},
            ))
        batch.status = "extracted"
        log.info("batch %s -> %d item(s) via %s", batch.id, len(items), result.extractor)
    else:
        session.add(TravelItem(
            batch_id=batch.id,
            item_type=None,
            schema_version="1.4.0",
            artifact_sha256=batch.artifact_sha256,
            source=src,
            extraction=None,
            review={"required": True, "reasons": result.reasons,
                    "llm_attempts": result.llm_attempts[-1:] if result.llm_attempts else []},
            status={"extract": "needs_review", "resolve": "skipped",
                    "airtrail": "skipped", "adventurelog": {}},
        ))
        batch.status = "needs_review"
        log.warning("batch %s -> needs_review: %s", batch.id, "; ".join(result.reasons)[:300])

    session.commit()
    return batch.status


def process_pending(session: Session, llm: OllamaExtractor | None,
                    limit: int = 0, progress=None, batch_filter=None) -> dict:
    """Process every received batch (oldest first). Per-batch commit: one bad
    batch never blocks the rest, and a crash mid-run loses nothing.

    progress(phase, done, total, current) is called before each batch (the LLM
    step is the slow one, so naming the email being read is the useful signal)
    and once at the end, so the operator sees a live count.

    `batch_filter` (an IngestBatch predicate, e.g. scope.batch_filter(user))
    limits the run to one person's captures (YONDER-113); None = all (CLI)."""
    q = session.query(IngestBatch).filter_by(status="received")
    if batch_filter is not None:
        q = q.filter(batch_filter)
    q = q.order_by(IngestBatch.received_at)
    if limit:
        q = q.limit(limit)
    batches = q.all()
    total = len(batches)
    summary = {"extracted": 0, "needs_review": 0}
    for i, batch in enumerate(batches):
        if progress:
            label = (batch.payload or {}).get("subject") or str(batch.id)[:8]
            progress("extracting", i, total, str(label)[:70])
        outcome = process_batch(session, batch, llm)
        summary["extracted" if outcome == "extracted" else "needs_review"] += 1
    if progress:
        progress("extracting", total, total, "")
    return summary
