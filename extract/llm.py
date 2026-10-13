"""LLM extractor — last in the try-order, PRIMARY in practice (D-10 addendum:
~0% of real booking emails carry structured markup).

Local Ollama only (D-11). Three defenses against local models' unreliable
JSON, in series:
  1. Ollama structured outputs: the request carries the (inlined) extraction
     schema as `format`, constraining generation at the token level.
  2. Hard validation gate: the response is validated against
     travel-extraction.schema.json via the real jsonschema library.
  3. Exactly ONE retry (D-11), with the validation errors quoted back to the
     model. Still invalid -> ExtractionFailed -> needs_review. Never a third try,
     never a silent fallback, never a cloud call.

Stdlib HTTP (urllib) — no new runtime dependency for one POST endpoint.
"""
from __future__ import annotations

import copy
import json
import urllib.error
import urllib.request
from pathlib import Path

from validation import validate_schema

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "travel-extraction.schema.json"


class ExtractionFailed(Exception):
    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


def load_extraction_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text())


def inline_refs(schema: dict) -> dict:
    """Resolve local $refs into an inline schema for Ollama's `format` field —
    its grammar compiler does not reliably follow $ref/$defs. The derived
    schema is a tree (no cycles), so plain recursive substitution is safe."""
    defs = schema.get("$defs", {})

    def resolve(node, depth=0):
        if depth > 40:
            raise ValueError("$ref nesting too deep — cycle?")
        if isinstance(node, dict):
            if "$ref" in node:
                target = defs[node["$ref"].rsplit("/", 1)[-1]]
                merged = {**copy.deepcopy(target),
                          **{k: v for k, v in node.items() if k != "$ref"}}
                return resolve(merged, depth + 1)
            return {k: resolve(v, depth + 1) for k, v in node.items()
                    if k not in ("$defs",)}
        if isinstance(node, list):
            return [resolve(v, depth + 1) for v in node]
        return node

    root = {k: v for k, v in schema.items() if k not in ("$defs",)}
    return resolve(root)


def build_format_schema(schema: dict) -> dict:
    """The `format` grammar sent to Ollama. Two transforms beyond inlining:

    1. llama.cpp grammars cannot express if/then, and grammar-constrained
       models routinely omit optional nested objects — measured on the first
       real corpus: 11/14 LLM extractions failed exactly this way (item_type
       set, matching detail object missing). So the Payload becomes an anyOf
       of per-type VARIANTS, each pinning item_type via const and hard-
       requiring its detail object. The model literally cannot emit a lodging
       item without a lodging object.
    2. The if/then allOf is dropped from the grammar (superseded by the
       variants); the real schema still enforces it at the validation gate.
    """
    inlined = inline_refs(schema)
    payload = inlined["properties"]["items"]["items"]
    base = {k: v for k, v in payload.items() if k != "allOf"}

    # Nullable but REQUIRED in the grammar: under grammar constraint the model
    # simply never emits optional keys. Measured three times now — missing
    # detail objects, then PNRs sitting in subject lines, then street
    # addresses. Required-but-nullable forces a conscious choice.
    always = {"booking_reference"}
    # iata is here for the SAME reason as address/city/country (D-29): under
    # grammar constraint an optional key is one the model never emits, so airport
    # codes that WERE printed in the document (e.g. an itinerary table
    # "Toronto YYZ Munich MUC") were silently dropped, leaving every multi-leg
    # flight un-keyable. Required-but-nullable forces a conscious choice.
    place_always = {"address", "city", "country", "iata"}

    variants = []
    for item_type in ("flight", "ground_transport", "lodging", "event"):
        v = copy.deepcopy(base)
        v["properties"]["item_type"] = {"const": item_type}
        v["required"] = sorted(set(v.get("required", [])) | {item_type} | always)
        # Force ticket_numbers on flights — like booking_reference/iata, it is an
        # optional key the grammar-constrained model otherwise never emits, so
        # e-ticket numbers (an AirTrail custom field) were going uncaptured. The
        # flight detail is a nullable anyOf[object, null], so reach the object.
        if item_type == "flight":
            detail = v["properties"].get("flight")
            candidates = detail.get("anyOf") if isinstance(detail, dict) else None
            for cand in (candidates or ([detail] if isinstance(detail, dict) else [])):
                props = cand.get("properties") if isinstance(cand, dict) else None
                if isinstance(props, dict) and "ticket_numbers" in props:
                    cand["required"] = sorted(
                        set(cand.get("required", [])) | {"ticket_numbers"})
        variants.append(v)
    other = copy.deepcopy(base)                       # note/unknown: no detail object
    other["properties"]["item_type"] = {"enum": ["note", "unknown"]}
    other["required"] = sorted(set(other.get("required", [])) | always)
    variants.append(other)

    inlined["properties"]["items"]["items"] = {"anyOf": variants}
    _require_place_fields(inlined, place_always)
    return inlined


def _require_place_fields(node, fields: set[str]) -> None:
    """Mark address/city/country required-but-nullable on every inlined Place,
    for the same reason as booking_reference: an optional key under grammar
    constraint is a key the model will not emit, and a hotel without its
    street address cannot be located (Photon needs the address, not the
    chain name — see resolve/geocode.py)."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict) and "raw" in props and "city" in props:
            node["required"] = sorted(set(node.get("required", [])) | fields)
        for value in node.values():
            _require_place_fields(value, fields)
    elif isinstance(node, list):
        for value in node:
            _require_place_fields(value, fields)


PROMPT_VERSION = "v3"   # v3: mode "taxi" (owner, 2026-10-12)

SYSTEM_PROMPT = """You extract travel bookings from documents (emails, tickets, invoices) into strict JSON.

Rules — every one of these exists because of a real past failure:
- Output ONLY a JSON object matching the provided schema: {"items": [...], "declared_segment_count": N or null}.
- The email SUBJECT and SENDER lines above the document are part of the source. Booking references and PNR codes often appear ONLY in the subject (e.g. "Booking Reference: Q2H7VB", "booking confirmation - W8NJ3C"). Mine them.
- One item per SEGMENT: a 2-leg flight booking is 2 items; set declared_segment_count to the total the document states.
- NEVER invent a time. If the document gives only a date, use precision "date" and local "YYYY-MM-DD". If it gives date+time, precision "datetime" and local "YYYY-MM-DDTHH:MM". Do not write 00:00 for an unknown time.
- precision MUST match local exactly: precision "datetime" requires local to contain "THH:MM"; precision "date" requires local to be "YYYY-MM-DD" with no time. A date-only local with precision "datetime" is a contradiction — never emit it.
- Times are LOCAL wall-clock as printed in the document. Set tz only when the document names a real IANA zone (Area/Location); otherwise null. Set origin to "document".
- flight_number is digits only, optional single letter suffix (e.g. "888", not "AC652"). The airline goes in marketing_carrier as an object {"name": ..., "iata": ...}: a flight printed as "AC 1830" means iata "AC"; a United confirmation means United Airlines even if legs only show numbers. Also set the top-level provider the same way.
- Airport iata codes: whenever the document prints a 3-letter uppercase code, put it in departure.place.iata / arrival.place.iata — INCLUDING compact itinerary tables that pair each city with its code. In a row like "Toronto YYZ Munich MUC" the departure is YYZ and the arrival is MUC; the next row "Munich MUC Bangalore BLR" is departure MUC, arrival BLR. Set iata to null only when NO code is printed — never invent one from a city name.
- booking_status: "confirmed" for confirmations, "cancelled" for cancellations, "changed" when the subject or body says the booking was modified/changed/rescheduled — a modification notice is NOT "confirmed" — "planned" for invites/plans, pending/unknown otherwise.
- booking_reference: the confirmation/booking/PNR code (check the subject line), or null.
- Every item MUST include the detail object named by its item_type: item_type "flight" requires a "flight" object, "lodging" requires "lodging", "event" requires "event", and train/bus/car-rental items are item_type "ground_transport" and require a "ground_transport" object (set its mode).
- A taxi, or a ride-hailing trip (Uber, Lyft, Bolt, Grab, a car service), is item_type "ground_transport" with mode "taxi": the pickup is departure.place, the drop-off arrival.place, and the company is provider.name. A taxi has no service number; a vehicle, driver or trip count is not one.
- ADDRESSES MATTER. For hotels, venues, stations and rental desks, fill place.address with the full street address printed in the document (street number and name), place.city with the city, place.region with the state/province, and place.country with its 2-letter code. These are almost always printed — you need them to physically get there — and they are what locates the booking on a map. Do not put the street address in place.name; name is the property/venue name.
- travelers: passenger/guest names exactly as printed (raw_name).
- cost: total amount + 3-letter currency code, only if the document states them.
- The document may be a forwarded email; extract the underlying booking, not the forwarder.
- Use null for anything the document does not state. Never invent data."""


class OllamaExtractor:
    def __init__(self, base_url: str = "http://ollama:11434",
                 model: str = "qwen3:14b", timeout: int = 600):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.schema = load_extraction_schema()
        self.format_schema = build_format_schema(self.schema)

    # separated for tests to monkeypatch
    def _post(self, messages: list[dict]) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": self.format_schema,
            "think": False,
            "options": {"temperature": 0, "num_ctx": 16384},
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        return data.get("message", {}).get("content", "")

    def extract(self, document_text: str, context_note: str = "") -> tuple[dict, list[dict]]:
        """Returns (valid PayloadBatch, attempts). Raises ExtractionFailed
        after the single permitted retry (D-11)."""
        doc = document_text[:60_000]
        user = (f"{context_note}\n\n" if context_note else "") + \
            f"Document:\n---\n{doc}\n---\nExtract the travel booking(s) as JSON."
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user}]
        attempts: list[dict] = []

        for attempt in (1, 2):                       # exactly one retry (D-11)
            try:
                content = self._post(messages)
            except (urllib.error.URLError, TimeoutError, OSError) as ex:
                attempts.append({"attempt": attempt, "error": f"ollama unreachable: {ex}"})
                raise ExtractionFailed(f"ollama call failed: {ex}", attempts)
            try:
                batch = json.loads(content)
            except ValueError as ex:
                errors = [f"response is not JSON: {ex}"]
            else:
                errors = validate_schema(batch, self.schema)
                if not errors:
                    attempts.append({"attempt": attempt, "ok": True})
                    return batch, attempts
            attempts.append({"attempt": attempt, "ok": False, "errors": errors[:10],
                             "raw": content[:2000]})
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content":
                             "That JSON failed schema validation:\n"
                             + "\n".join(errors[:10])
                             + "\nReturn a corrected JSON object. JSON only."})

        raise ExtractionFailed("schema-invalid after retry (D-11: goes to review)",
                               attempts)
