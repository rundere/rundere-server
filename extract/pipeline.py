"""Extraction orchestrator — runs one stored ingest batch through the chain
in D-10/D-27 order: JSON-LD → iCal → PDF text → maps_url → LLM.

Every result is validated against travel-extraction.schema.json before it
leaves this module: an invalid batch NEVER propagates — it becomes
needs_review with the errors attached. The LLM is injected by the caller
(None => anything that would need it goes to needs_review instead), keeping
this module import-safe and unit-testable without a model.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from extract import apple_maps_url, filescan, gcal, ical, jsonld, maps_url, mime, pdf, web
from extract.normalize import normalize_batch, normalize_payload
from extract.llm import ExtractionFailed, OllamaExtractor, load_extraction_schema
from validation import validate_schema

_SCHEMA = load_extraction_schema()


@dataclass
class ExtractionResult:
    extractor: str | None = None          # which stage produced the items
    batch: dict | None = None             # valid PayloadBatch, or None
    needs_review: bool = False
    reasons: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)   # what was tried, in order
    llm_attempts: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.batch is not None and not self.needs_review


def _validated(items: list[dict], extractor: str,
               result: ExtractionResult) -> ExtractionResult | None:
    """Wrap items into a PayloadBatch and gate it. Returns the completed
    result on success; records the failure and returns None to fall through."""
    items = [normalize_payload(i) for i in items]
    batch = {"items": items, "declared_segment_count": None}
    errors = validate_schema(batch, _SCHEMA)
    if errors:
        result.trace.append(f"{extractor}: produced {len(items)} item(s) but "
                            f"schema-invalid ({errors[0]})")
        return None
    result.extractor, result.batch = extractor, batch
    result.trace.append(f"{extractor}: ok ({len(items)} item(s))")
    return result


def extract_ingest_payload(ingest_payload: dict,
                           llm: OllamaExtractor | None = None,
                           scanner: filescan.Scanner | None = None) -> ExtractionResult:
    """ingest_payload is the JSON the capture client POSTed to /ingest
    (the ingest_batches.payload column). `scanner` checks an email and its
    attachments before anything parses them (YONDER-243); None = the static
    PDF checks only, no antivirus."""
    result = ExtractionResult()
    channel = ingest_payload.get("capture_channel") or ""
    cc = ingest_payload.get("capture_context") or {}

    url = ingest_payload.get("url") or ""

    # ---- apple_maps_url channel: an Apple Maps directions link (not an email).
    # Checked before the Google branch so an Apple link never reaches the Google
    # extractor by way of the shared maps handling (SPEC-4 §2).
    if channel == "apple_maps_url" or apple_maps_url.looks_like_apple_maps_url(url):
        try:
            items = apple_maps_url.extract_apple_maps_url(url, cc)
            return _validated(items, "apple_maps_url", result) or _fail(result)
        except apple_maps_url.ExtractionError as ex:
            result.needs_review = True
            result.reasons.append(str(ex))
            result.trace.append(f"apple_maps_url: {ex}")
            return result

    # ---- maps_url channel: not an email at all
    if channel == "maps_url" or url.find("/maps/dir/") >= 0:
        try:
            items = maps_url.extract_maps_url(url, cc)
            return _validated(items, "maps_url", result) or _fail(result)
        except maps_url.ExtractionError as ex:
            result.needs_review = True
            result.reasons.append(str(ex))
            result.trace.append(f"maps_url: {ex}")
            return result

    # ---- gcal channel: a Google Calendar Event resource, sent as JSON by the
    # Calendar add-on (not an email). Deterministic, no LLM.
    if channel == "gcal" or ingest_payload.get("event"):
        items = gcal.extract_gcal_event(ingest_payload.get("event") or {})
        done = _validated(items, "gcal", result)
        if done:
            return done
        result.needs_review = True
        result.reasons.append("gcal event had no usable title/start")
        result.trace.append("gcal: empty extraction")
        return result

    # ---- web channel (1.1): a page the operator sent (bookmarklet outerHTML or
    # pasted source). JSON-LD is the common case — hotels/restaurants/events all
    # embed schema.org; the LLM reads the stripped text when a page carries none.
    if channel == "web" or ingest_payload.get("html"):
        html = ingest_payload.get("html") or ""
        ld_items = web.extract_web(html)
        if ld_items:
            done = _validated(ld_items, "jsonld", result)
            if done:
                return done
            result.trace.append("jsonld: page markup was schema-invalid")
        else:
            result.trace.append("jsonld: no reservation markup in page")
        if llm is None:
            result.needs_review = True
            result.reasons.append(
                "web page had no schema.org reservation markup and no LLM configured")
            return result
        doc = web.html_to_text(html)
        context = web.llm_context(ingest_payload.get("source_url"), cc)
        try:
            batch, attempts = llm.extract(doc, context)
        except ExtractionFailed as ex:
            result.needs_review = True
            result.llm_attempts = ex.attempts
            result.reasons.append(str(ex))
            result.trace.append("llm: failed validation gate")
            return result
        batch = normalize_batch(batch)
        result.llm_attempts = attempts
        result.extractor = "llm"
        result.batch = batch
        result.trace.append(f"llm: ok ({len(batch['items'])} item(s) from web page)")
        return result

    # ---- file channel (YONDER-179): an uploaded PDF or .ics, read from the
    # documents store by app/processing. An uploaded .eml arrives with
    # raw_message_base64 and is read as an email below, scan and all.
    if channel == "file" and not ingest_payload.get("raw_message_base64"):
        return _extract_file(ingest_payload, llm, scanner or filescan.Scanner(), result)

    # ---- email channel (gmail add-on shape)
    raw_b64 = ingest_payload.get("raw_message_base64")
    if not raw_b64:
        result.needs_review = True
        result.reasons.append("payload has neither raw_message_base64 nor a maps URL")
        return result

    try:
        decoded = mime.decode_raw_message(raw_b64)
    except Exception as ex:
        result.needs_review = True
        result.reasons.append(f"MIME decode failed: {ex}")
        return result

    # 0 — scan before anything parses an attachment (YONDER-243). An infected
    # message is quarantined; a scanner that can't answer means its
    # attachments aren't parsed (fail closed), the body still is.
    scanner = scanner or filescan.Scanner()
    try:
        import base64
        hit = scanner.scan_message(base64.b64decode(raw_b64))
    except filescan.ScanUnavailable as ex:
        hit = None
        if decoded.pdf_attachments:
            result.trace.append(f"scan: antivirus unavailable ({ex}) — "
                                f"{len(decoded.pdf_attachments)} attachment(s) not parsed")
            decoded.pdf_attachments = []
    if hit:
        result.needs_review = True
        result.reasons.append(f"quarantined: the antivirus scan found {hit} — "
                              f"nothing was extracted from this email")
        result.trace.append(f"scan: FOUND {hit}")
        return result
    safe_pdfs = []
    for name, data in decoded.pdf_attachments:
        verdict = scanner.check_pdf(data)
        if verdict.ok:
            safe_pdfs.append((name, data))
            for note in verdict.notes:
                result.trace.append(f"scan: {name!r} {note}")
        else:
            result.needs_review = True
            result.reasons.append(f"attachment {name!r} blocked: {verdict.reason}")
            result.trace.append(f"scan: {name!r} blocked ({verdict.reason})")
    decoded.pdf_attachments = safe_pdfs

    # 1 — JSON-LD (measured ~0% hit rate, but free and authoritative when present)
    ld_items = [p for html in decoded.html_parts for p in jsonld.extract_jsonld(html)]
    if ld_items:
        done = _validated(ld_items, "jsonld", result)
        if done:
            return done
    else:
        result.trace.append("jsonld: no reservation markup")

    # 2 — iCal (calendar invites are legitimate events, D-28)
    ical_items = [p for part in decoded.ical_parts for p in ical.extract_ical(part)]
    if ical_items:
        done = _validated(ical_items, "ical", result)
        if done:
            return done
    else:
        result.trace.append("ical: no VEVENT parts")

    # 3 — PDF attachments (D-27): extracted text joins the LLM input
    pdf_texts = pdf.extract_all_pdfs(decoded.pdf_attachments)
    if decoded.pdf_attachments:
        result.trace.append(
            f"pdf: {len(pdf_texts)}/{len(decoded.pdf_attachments)} attachment(s) "
            f"yielded text")

    # 4 — LLM (the primary path in practice)
    if llm is None:
        result.needs_review = True
        result.reasons.append("no deterministic extraction matched and no LLM configured")
        return result

    doc = mime.best_text(decoded)
    for name, text in pdf_texts.items():
        doc += f"\n\n=== Attachment: {name} ===\n{text}"
    context = (f"Email subject: {decoded.subject}\nEmail from: {decoded.sender}\n"
               f"Email date: {decoded.date}")
    try:
        batch, attempts = llm.extract(doc, context)
    except ExtractionFailed as ex:
        result.needs_review = True
        result.llm_attempts = ex.attempts
        result.reasons.append(str(ex))
        result.trace.append("llm: failed validation gate")
        return result
    batch = normalize_batch(batch)
    result.llm_attempts = attempts
    result.extractor = "llm"        # schema enum has no llm+pdf; pdf use is in trace
    result.batch = batch
    result.trace.append(f"llm: ok ({len(batch['items'])} item(s), "
                        f"{len(attempts)} attempt(s)"
                        f"{', with pdf text' if pdf_texts else ''})")
    return result


def _extract_file(payload: dict, llm, scanner, result: ExtractionResult) -> ExtractionResult:
    """An uploaded PDF or calendar file. Already virus-scanned when it was
    stored (app/documents.py); a PDF's static checks run again before parsing."""
    import base64
    name = payload.get("filename") or "file"
    if payload.get("missing") or not payload.get("file_base64"):
        result.needs_review = True
        result.reasons.append(f"the captured file {name!r} is no longer stored")
        return result
    data = base64.b64decode(payload["file_base64"])
    if payload.get("content_type") == "text/calendar":
        items = ical.extract_ical(data.decode("utf-8", errors="replace"))
        if items:
            done = _validated(items, "ical", result)
            if done:
                return done
        result.needs_review = True
        result.reasons.append(f"no usable events in the calendar file {name!r}")
        return result
    verdict = scanner.check_pdf(data)
    if not verdict.ok:
        result.needs_review = True
        result.reasons.append(f"file {name!r} blocked: {verdict.reason}")
        result.trace.append(f"scan: {name!r} blocked ({verdict.reason})")
        return result
    for note in verdict.notes:
        result.trace.append(f"scan: {name!r} {note}")
    text = pdf.extract_pdf_text(data)
    if not text:
        result.needs_review = True
        result.reasons.append(f"no text could be read from {name!r} (a scanned image?)")
        return result
    result.trace.append(f"pdf: {len(text)} characters of text")
    if llm is None:
        result.needs_review = True
        result.reasons.append("an uploaded PDF needs the LLM, and none is configured")
        return result
    cc = payload.get("capture_context") or {}
    context = f"Uploaded file: {name}" + (f"\nNotes: {cc['notes']}" if cc.get("notes") else "")
    try:
        batch, attempts = llm.extract(f"=== Attachment: {name} ===\n{text}", context)
    except ExtractionFailed as ex:
        result.needs_review = True
        result.llm_attempts = ex.attempts
        result.reasons.append(str(ex))
        result.trace.append("llm: failed validation gate")
        return result
    batch = normalize_batch(batch)
    result.llm_attempts = attempts
    result.extractor = "llm"
    result.batch = batch
    result.trace.append(f"llm: ok ({len(batch['items'])} item(s) from {name!r})")
    return result


def _fail(result: ExtractionResult) -> ExtractionResult:
    result.needs_review = True
    result.reasons.append("extractor output failed schema validation")
    return result
