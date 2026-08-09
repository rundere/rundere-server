"""1.1 web capture: extractor, pipeline dispatch, and the /capture routes.

The pure extraction tests need only stdlib + extract.jsonld and always run. The
pipeline test needs jsonschema; the route tests need fastapi — both guarded, so
a bare host skips them and the container/CI runs the full set (mirrors
test_ingest.py).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A booking page with an embedded schema.org reservation — the common real case.
_LD = json.dumps({
    "@context": "https://schema.org", "@type": "LodgingReservation",
    "reservationNumber": "ABC123",
    "checkinDate": "2026-09-20", "checkoutDate": "2026-09-23",
    "reservationFor": {
        "@type": "LodgingBusiness", "name": "Riad Harar",
        "address": {"@type": "PostalAddress",
                    "addressLocality": "Harar", "addressCountry": "MA"}},
})
_HTML = ('<!doctype html><html><head>'
         f'<script type="application/ld+json">{_LD}</script>'
         '<style>.x{color:red}</style></head>'
         '<body><h1>Your stay</h1><p>Riad&nbsp;Harar</p></body></html>')
_HTML_NO_LD = "<!doctype html><html><body><h1>Just a page</h1><p>Nothing here.</p></body></html>"


# ----------------------------------------------------------- pure (always run)

def test_html_to_text_strips_markup_and_unescapes():
    from extract.web import html_to_text
    txt = html_to_text(_HTML)
    assert "<" not in txt and ">" not in txt         # tags gone
    assert ".x{color:red}" not in txt                # <style> body dropped
    assert "ABC123" not in txt                        # <script> body dropped
    assert "Riad Harar" in txt                    # &nbsp; unescaped to space
    assert "Your stay" in txt


def test_extract_web_reads_jsonld_reservation():
    from extract.web import extract_web
    items = extract_web(_HTML)
    assert len(items) == 1
    assert items[0]["item_type"] == "lodging"
    assert items[0]["lodging"]["property_name"] == "Riad Harar"


def test_extract_web_empty_on_plain_page():
    from extract.web import extract_web
    assert extract_web(_HTML_NO_LD) == []


def test_llm_context_wires_operator_date_time_as_gap_filler():
    from extract.web import llm_context
    c = llm_context("https://x.example/booking",
                    {"travel_date": "2026-09-13", "travel_time": "15:35"})
    assert "https://x.example/booking" in c
    assert "2026-09-13 at 15:35" in c
    assert "never override" in c            # page always wins


def test_llm_context_omits_date_line_when_absent():
    from extract.web import llm_context
    c = llm_context(None, {})
    assert c.startswith("Captured web page")
    assert "travel date" not in c.lower()   # no misleading hint when none given


# ------------------------------------------------------- pipeline (needs jsonschema)

def _pipeline_or_skip():
    try:
        from extract.pipeline import extract_ingest_payload
        return extract_ingest_payload
    except Exception:
        import pytest
        pytest.skip("jsonschema not installed on this host")


def test_pipeline_web_channel_extracts_via_jsonld():
    extract_ingest_payload = _pipeline_or_skip()
    res = extract_ingest_payload({"capture_channel": "web", "html": _HTML})
    assert res.ok
    assert res.extractor == "jsonld"
    assert res.batch["items"][0]["item_type"] == "lodging"


def test_pipeline_web_no_markup_no_llm_needs_review():
    extract_ingest_payload = _pipeline_or_skip()
    res = extract_ingest_payload({"capture_channel": "web", "html": _HTML_NO_LD}, llm=None)
    assert res.needs_review and not res.ok
    assert any("no schema.org" in r for r in res.reasons)


def test_pipeline_web_detected_by_html_key_without_channel():
    extract_ingest_payload = _pipeline_or_skip()
    res = extract_ingest_payload({"html": _HTML})       # no capture_channel
    assert res.ok and res.extractor == "jsonld"


# ------------------------------------------------------------- routes (needs fastapi)

os.environ.setdefault("TX_DATABASE_URL",
                      f"sqlite:///{tempfile.mkdtemp(prefix='tx-web-')}/test.sqlite3")

try:
    from fastapi.testclient import TestClient
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, IngestBatch
    Base.metadata.create_all(engine)
    _client = TestClient(app)
    ROUTES = True
except Exception:
    ROUTES = False


def _route_or_skip(fn):
    if not ROUTES:
        import pytest
        pytest.skip("fastapi/httpx not installed on this host")
    return fn()


def _web_batches():
    with SessionLocal() as s:
        return [b for b in s.query(IngestBatch).all()
                if (b.payload or {}).get("capture_channel") == "web"]


def test_capture_page_serves_bookmarklet():
    def run():
        r = _client.get("/capture")
        assert r.status_code == 200
        assert "/capture/web" in r.text            # bookmarklet targets the endpoint
        assert "Capture a web page" in r.text
    _route_or_skip(run)


def test_capture_web_bookmarklet_stores_web_batch():
    def run():
        before = len(_web_batches())
        r = _client.post("/capture/web", content=_HTML.encode(),
                         headers={"Content-Type": "text/html"})
        assert r.status_code == 200
        assert "captured this page" in r.text.lower()
        after = _web_batches()
        assert len(after) == before + 1
        assert after[-1].payload["html"].startswith("<!doctype html>")
    _route_or_skip(run)


def test_capture_web_form_paste_stores_web_batch():
    def run():
        before = len(_web_batches())
        r = _client.post("/capture/web",
                         data={"content": _HTML, "title": "My stay",
                               "travel_date": "2026-09-13", "travel_time": "15:35"})
        assert r.status_code == 200
        assert "Captured" in r.text
        after = _web_batches()
        assert len(after) == before + 1
        cc = after[-1].payload["capture_context"]
        assert cc["title"] == "My stay"
        assert cc["travel_date"] == "2026-09-13" and cc["travel_time"] == "15:35"
    _route_or_skip(run)


def test_capture_web_rejects_private_url_ssrf():
    def run():
        before = len(_web_batches())
        r = _client.post("/capture/web", data={"content": "http://127.0.0.1:8000/admin"})
        assert r.status_code == 200
        assert "bookmarklet" in r.text.lower()      # the "couldn't fetch" guidance
        assert len(_web_batches()) == before        # nothing stored
    _route_or_skip(run)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except Exception as ex:                 # includes pytest.skip
                kind = type(ex).__name__
                print(f"  {'skip' if 'Skip' in kind else 'FAIL'} {name}: {str(ex)[:200]}")
                failures += 0 if "Skip" in kind else 1
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
