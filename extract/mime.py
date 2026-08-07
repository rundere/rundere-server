"""Shared MIME front-end for the extractor chain.

The Gmail add-on stores the entire raw RFC 2822 message (D-15). This module
decodes it once and hands each extractor the part it cares about: HTML bodies
for jsonld, text/calendar parts for ical, PDF attachments for pdf, and a
plain-text rendering for the LLM. Stdlib only.
"""
from __future__ import annotations

import base64
import html as html_lib
import re
from dataclasses import dataclass, field
from email import message_from_bytes, policy


@dataclass
class DecodedMessage:
    subject: str | None = None
    sender: str | None = None
    date: str | None = None
    html_parts: list[str] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    ical_parts: list[str] = field(default_factory=list)
    pdf_attachments: list[tuple[str, bytes]] = field(default_factory=list)   # (filename, bytes)


def decode_raw_message(raw_base64: str) -> DecodedMessage:
    raw = base64.b64decode(raw_base64)
    msg = message_from_bytes(raw, policy=policy.default)
    out = DecodedMessage(
        subject=str(msg.get("Subject") or "") or None,
        sender=str(msg.get("From") or "") or None,
        date=str(msg.get("Date") or "") or None,
    )
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        filename = part.get_filename() or ""
        try:
            payload = part.get_content()
        except Exception:
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
        if ctype == "text/html" and isinstance(payload, str):
            out.html_parts.append(payload)
        elif ctype == "text/plain" and isinstance(payload, str):
            out.text_parts.append(payload)
        elif ctype == "text/calendar":
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            if isinstance(payload, str):
                out.ical_parts.append(payload)
        elif ctype == "application/pdf" or filename.lower().endswith(".pdf"):
            data = part.get_payload(decode=True)
            if isinstance(data, bytes) and data[:4] == b"%PDF":
                out.pdf_attachments.append((filename or "attachment.pdf", data))
    return out


_TAG_DROP = re.compile(r"(?is)<(script|style|head)\b.*?</\1>")
_TAG = re.compile(r"(?s)<[^>]+>")
_BLOCK_TAG = re.compile(r"(?i)</?(p|div|tr|table|br|li|h[1-6])[^>]*>")
_WS = re.compile(r"[ \t\xa0]+")
_BLANKS = re.compile(r"\n{3,}")


def html_to_text(html: str) -> str:
    """Crude but adequate HTML->text for LLM input: booking emails are table
    soup; what matters is that label/value text survives with line structure."""
    s = _TAG_DROP.sub(" ", html)
    s = _BLOCK_TAG.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = html_lib.unescape(s)
    s = _WS.sub(" ", s)
    s = "\n".join(line.strip() for line in s.splitlines())
    s = _BLANKS.sub("\n\n", s)
    return s.strip()


def best_text(decoded: DecodedMessage) -> str:
    """Preferred plain-text rendering of the whole message for the LLM."""
    if decoded.html_parts:
        return "\n\n".join(html_to_text(h) for h in decoded.html_parts)
    return "\n\n".join(decoded.text_parts)
