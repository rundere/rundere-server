"""Scan untrusted files before anything parses them (YONDER-243).

Email captures (the forward-to inbox, the Gmail add-on) carry attachments we
then parse — PDFs through pypdf (extract/pdf.py). Nothing may reach a parser
unchecked:

1. **ClamAV** over the WHOLE raw message (it unpacks MIME itself, so every
   attachment type is covered), through a clamav-rest service
   (`TX_CLAMAV_URL`, e.g. http://clamav-rest:9000; empty = no AV). A hit
   quarantines the capture: nothing is extracted, it goes to review.
2. **Static PDF checks, always on** — signatures miss a booking PDF crafted to
   misbehave. BLOCKED (dropped from extraction, the capture goes to review,
   the email body is still read): not really a PDF, over 25 MB or 200 pages,
   malformed, encrypted beyond the empty-password kind tickets use, or any
   action that reaches outside the document — Launch, ImportData, GoToE — or
   XFA forms / rich media, which no booking needs. NOTED but read: JavaScript
   and embedded files. Real tickets carry viewer scripts (a Salesforce-made
   booking PDF in the owner's own inbox sets `this.zoom = 100;`); Rundere never
   runs them — it only reads text — and ClamAV has already unpacked and
   scanned any embedded file.
3. **Fail closed:** ClamAV configured but unreachable → attachments aren't
   parsed at all (the body still is), never parsed unscanned.

Rundere only reads text out of these files; nothing is executed or rendered.
File contents are never logged. Stdlib only, plus pypdf (already a dependency)
for the structural PDF walk.
"""
from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

MAX_FILE_BYTES = 25 * 1024 * 1024        # owner, 2026-10-13 (also the 179 cap)
MAX_PDF_PAGES = 200
CLAMAV_TIMEOUT = 30

# PDF name tokens that make a document active rather than a document.
_BLOCK = {"Launch", "ImportData", "GoToE", "RichMedia", "XFA"}       # stop, don't parse
_NOTE = {"JavaScript", "JS", "EmbeddedFile", "EmbeddedFiles", "FileAttachment"}
_ACTIVE = _BLOCK | _NOTE
_NAME = re.compile(rb"/([A-Za-z0-9#]+)")
_STREAM = re.compile(rb"stream\r?\n.*?endstream", re.DOTALL)


class ScanUnavailable(Exception):
    """The virus scanner is configured but couldn't give an answer."""


@dataclass
class Verdict:
    ok: bool
    reason: str | None = None
    notes: tuple[str, ...] = ()          # read anyway, but worth a line in the trace


def _decode_name(raw: bytes) -> str:
    """PDF names may hide letters as #xx escapes (/J#61vaScript)."""
    return re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]),
                  raw).decode("latin-1")


def _raw_active_names(data: bytes) -> set[str]:
    """Active names visible in the file's uncompressed dictionaries. Stream
    bodies are cut out first: compressed bytes hit "/JS" by chance."""
    text = _STREAM.sub(b" ", data)
    return {n for n in (_decode_name(m.group(1)) for m in _NAME.finditer(text))
            if n in _ACTIVE}


def _walk_active(reader) -> set[str]:
    """Active features found by walking the parsed structure — this sees
    dictionaries that live inside compressed object streams too."""
    from pypdf.generic import DictionaryObject, IndirectObject

    found: set[str] = set()
    seen: set[tuple[int, int]] = set()
    budget = [20000]                      # objects visited; a huge file stops early

    def visit(obj, depth=0):
        if budget[0] <= 0 or depth > 40:
            return
        budget[0] -= 1
        if isinstance(obj, IndirectObject):
            key = (obj.idnum, obj.generation)
            if key in seen:
                return
            seen.add(key)
            try:
                obj = obj.get_object()
            except Exception:
                return
        if isinstance(obj, DictionaryObject):
            for k, v in obj.items():
                name = str(k).lstrip("/")
                if name in _ACTIVE:
                    found.add(name)
                if name == "S" and str(v).lstrip("/") in _ACTIVE:
                    found.add(str(v).lstrip("/"))
                if name == "Subtype" and str(v).lstrip("/") in _ACTIVE:
                    found.add(str(v).lstrip("/"))
                if name in ("Parent", "P"):           # back-links: no new content
                    continue
                visit(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                visit(v, depth + 1)

    visit(reader.trailer)
    return found


def check_pdf(data: bytes) -> Verdict:
    """The static checks for one PDF attachment (always on)."""
    if len(data) > MAX_FILE_BYTES:
        return Verdict(False, f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    if not data.lstrip()[:5].startswith(b"%PDF-"):
        return Verdict(False, "not actually a PDF")
    active = _raw_active_names(data)
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    return Verdict(False, "password-protected")
            except Exception:
                return Verdict(False, "password-protected")
        if len(reader.pages) > MAX_PDF_PAGES:
            return Verdict(False, f"more than {MAX_PDF_PAGES} pages")
        active |= _walk_active(reader)
    except Exception:
        return Verdict(False, "malformed PDF")
    blocked = active & _BLOCK
    if blocked:
        return Verdict(False, "contains active content (" + ", ".join(sorted(blocked)) + ")")
    notes = []
    if active & {"JavaScript", "JS"}:
        notes.append("has JavaScript (never run; text read only)")
    if active & {"EmbeddedFile", "EmbeddedFiles", "FileAttachment"}:
        notes.append("has embedded files (scanned with the message; not opened)")
    return Verdict(True, notes=tuple(notes))


def clamav_scan(url: str, name: str, data: bytes, timeout: int = CLAMAV_TIMEOUT) -> str | None:
    """POST the bytes to clamav-rest's /scan. None = clean; otherwise the
    signature name. Raises ScanUnavailable when there's no clear answer."""
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/scan", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()       # 406 = something was FOUND
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ScanUnavailable(type(e).__name__) from None
    try:
        out = json.loads(raw)
        if isinstance(out, list):
            out = out[0] if out else {}
    except (ValueError, IndexError):
        raise ScanUnavailable(f"unreadable answer (HTTP {status})") from None
    if not isinstance(out, dict):
        raise ScanUnavailable(f"unreadable answer (HTTP {status})")
    verdict = str(out.get("Status") or "").upper()
    if verdict == "OK" and status == 200:
        return None
    if verdict == "FOUND":
        return str(out.get("Description") or "malware")
    raise ScanUnavailable(f"HTTP {status}")


class Scanner:
    """What the pipeline calls. `clamav_url` empty = static checks only."""

    def __init__(self, clamav_url: str = "", timeout: int = CLAMAV_TIMEOUT):
        self.clamav_url = (clamav_url or "").strip()
        self.timeout = timeout

    def scan_message(self, raw: bytes) -> str | None:
        """ClamAV over the whole raw message: None = clean (or no AV
        configured), else the signature. Raises ScanUnavailable."""
        if not self.clamav_url:
            return None
        return clamav_scan(self.clamav_url, "message.eml", raw, self.timeout)

    def check_pdf(self, data: bytes) -> Verdict:
        return check_pdf(data)
