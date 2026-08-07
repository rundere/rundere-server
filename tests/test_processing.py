"""Extract-stage processing tests: ingest_batches -> TravelItem rows.
Synthetic fixtures only (D-21). SQLite via tests/conftest.py env."""
import base64
import sys
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import sqlalchemy  # noqa: F401
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app.db import SessionLocal, engine
    from app.models import Base, IngestBatch, TravelItem
    from app.processing import process_pending
    Base.metadata.create_all(engine)

INVITE = "\r\n".join([
    "BEGIN:VCALENDAR", "BEGIN:VEVENT", "UID:p1@example.com",
    "SUMMARY:Team Dinner", "DTSTART;TZID=America/Toronto:20260920T190000",
    "END:VEVENT", "END:VCALENDAR"])


def _email_b64(html=None, ical_text=None):
    msg = EmailMessage()
    msg["Subject"] = "Synthetic"
    msg["From"] = "noreply@example.com"
    msg.set_content("body")
    if html:
        msg.add_alternative(html, subtype="html")
    if ical_text:
        msg.add_attachment(ical_text.encode(), maintype="text", subtype="calendar")
    return base64.b64encode(msg.as_bytes()).decode()


def _skip_or(fn):
    if not DEPS:
        import pytest
        pytest.skip("sqlalchemy not installed on this host")
    return fn()


def test_ical_batch_becomes_travel_item():
    def run():
        with SessionLocal() as s:
            b = IngestBatch(api_key_id="k1", payload={
                "capture_channel": "gmail", "message_id": "m-1", "thread_id": "t-1",
                "raw_message_base64": _email_b64(ical_text=INVITE),
                "capture_context": {"submitted_by": "test@example.com"}})
            s.add(b)
            s.commit()
            summary = process_pending(s, llm=None)
            assert summary["extracted"] >= 1
            b2 = s.get(IngestBatch, b.id)
            assert b2.status == "extracted"
            items = s.query(TravelItem).filter_by(batch_id=b.id).all()
            assert len(items) == 1
            it = items[0]
            assert it.item_type == "event"
            assert it.segment_index == 0 and it.segment_count == 1
            assert it.extraction["extractor"] == "ical"
            assert it.extraction["model"] is None
            assert it.extraction["payload"]["event"]["title"] == "Team Dinner"
            assert it.status == {"extract": "ok", "resolve": "pending",
                                 "airtrail": "pending", "adventurelog": {}}
            assert it.source["type"] == "gmail" and it.source["ref"] == "m-1"
            assert it.canonical_key is None            # resolver-owned, still null
    _skip_or(run)


def test_unparseable_batch_gets_review_placeholder():
    def run():
        with SessionLocal() as s:
            b = IngestBatch(api_key_id="k1", payload={
                "capture_channel": "gmail", "message_id": "m-2",
                "raw_message_base64": _email_b64(html="<p>opaque booking</p>")})
            s.add(b)
            s.commit()
            process_pending(s, llm=None)               # no LLM -> needs_review
            b2 = s.get(IngestBatch, b.id)
            assert b2.status == "needs_review"
            items = s.query(TravelItem).filter_by(batch_id=b.id).all()
            assert len(items) == 1
            it = items[0]
            assert it.item_type is None and it.extraction is None
            assert it.review["required"] is True
            assert it.status["extract"] == "needs_review"
    _skip_or(run)


def test_processed_batches_are_not_reprocessed():
    def run():
        with SessionLocal() as s:
            before = s.query(TravelItem).count()
            summary = process_pending(s, llm=None)     # nothing left in 'received'
            assert summary == {"extracted": 0, "needs_review": 0}
            assert s.query(TravelItem).count() == before
    _skip_or(run)


if __name__ == "__main__":
    if not DEPS:
        print("SKIP: sqlalchemy not installed on this host")
        sys.exit(0)
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as ex:
                failures += 1
                print(f"  FAIL {name}: {str(ex)[:300]}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
