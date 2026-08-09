"""Web page extractor for the `web` capture channel (1.1).

A page the operator sent — the bookmarklet's rendered `outerHTML`, or source
pasted into the capture form. The common case is free: hotels, restaurants,
event tickets and airlines all embed schema.org JSON-LD, which the SAME reader
the email path uses (`extract.jsonld`) turns into reservations. `html_to_text`
exists only for the LLM fallback the pipeline runs when a page has no markup.

This module parses nothing itself beyond delegating to jsonld and stripping
tags — the channel plumbing (LLM fallback, validation) lives in the pipeline.
"""
from __future__ import annotations

import html as _htmllib
import re

from extract import jsonld

# Blocks whose text is never document content — dropped whole before stripping.
_DROP_BLOCKS = re.compile(r"(?is)<(script|style|head|noscript|template|svg)[^>]*>.*?</\1>")
_BR = re.compile(r"(?i)<br\s*/?>")
_BLOCK_END = re.compile(r"(?i)</(p|div|li|tr|h[1-6]|section|article|header|footer)>")
_TAG = re.compile(r"(?s)<[^>]+>")
# Include U+00A0 (&nbsp;) and other non-breaking spaces so unescaped entities
# collapse to plain spaces rather than leaving the model odd glyphs.
_INLINE_WS = re.compile("[ \t\r\f\v   ]+")
_MANY_NL = re.compile(r"\n\s*\n\s*")


def extract_web(html: str) -> list[dict]:
    """schema.org reservations embedded in a captured page. Empty list when the
    page carries no reservation markup — the pipeline then falls back to the LLM."""
    return jsonld.extract_jsonld(html or "")


def llm_context(source_url: str | None, capture_context: dict | None) -> str:
    """The context string handed to the LLM for a web-page fallback (a page with
    no JSON-LD). Beyond the URL, it passes any operator-supplied travel date/time
    as a GAP-FILLER: the prompt tells the model to use it only when the page
    itself states no date or time, never to override what the page gives. This is
    what makes the capture form's date/time fields meaningful for web — the
    common JSON-LD path ignores them because the page already carries its dates."""
    cc = capture_context or {}
    parts = ["Captured web page" + (f" — {source_url}" if source_url else "")]
    date = (cc.get("travel_date") or "").strip()
    if date:
        time = (cc.get("travel_time") or "").strip()
        when = f"{date} at {time}" if time else date
        tz = (cc.get("timezone") or "").strip()
        parts.append(
            "The person capturing this page says the travel date/time is "
            f"{when}{f' ({tz})' if tz else ''}. Use it ONLY to fill a date or time "
            "the page itself does not state; never override a date or time the page gives.")
    return "\n".join(parts)


def html_to_text(html: str) -> str:
    """A readable text rendering of a page for the LLM fallback. Not a parser —
    it drops script/style, turns block boundaries into newlines, strips the rest
    of the tags, and unescapes entities, so the model sees prose rather than
    markup. Best-effort by design."""
    s = html or ""
    s = _DROP_BLOCKS.sub(" ", s)
    s = _BR.sub("\n", s)
    s = _BLOCK_END.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = _htmllib.unescape(s)
    s = _INLINE_WS.sub(" ", s)
    s = _MANY_NL.sub("\n\n", s)
    return s.strip()
