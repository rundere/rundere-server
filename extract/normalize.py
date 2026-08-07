"""Deterministic post-extraction normalization.

Applies to EVERY extractor's output before the schema gate. The principle:
anything mechanically derivable should be derived here, not left to the model
to remember — measured on the real corpus, an LLM will faithfully copy "YYZ"
into Place.raw and still leave Place.iata empty, which broke key generation
(RULE-03 too) for four United legs.

Nothing here guesses. Every rule is a shape test on data already present.
"""
from __future__ import annotations

import re

_IATA = re.compile(r"^[A-Z]{3}$")
_ICAO = re.compile(r"^[A-Z]{4}$")
# "Toronto Pearson (YYZ)" / "YYZ - Toronto Pearson" / "Toronto (YYZ) Intl"
_CODE_IN_PARENS = re.compile(r"\(([A-Z]{3})\)")
_CODE_PREFIX = re.compile(r"^([A-Z]{3})\s*[-–—:]\s*\S")


def _airport_code(raw: str) -> str | None:
    s = (raw or "").strip()
    if _IATA.match(s):
        return s
    m = _CODE_IN_PARENS.search(s) or _CODE_PREFIX.match(s)
    return m.group(1) if m else None


def normalize_place(place: dict, *, is_airport: bool) -> dict:
    """Fill iata/icao from raw when the code is unambiguously present.
    Only applied to flight movements — a lodging or event 'raw' that happens to
    be three capitals is not an airport."""
    out = dict(place)
    raw = str(out.get("raw") or "")
    if is_airport and not out.get("iata") and not out.get("icao"):
        code = _airport_code(raw)
        if code:
            out["iata"] = code
        elif _ICAO.match(raw.strip()):
            out["icao"] = raw.strip()
    if out.get("country"):
        c = str(out["country"]).strip().upper()
        out["country"] = c if len(c) == 2 else None
        if out["country"] is None:
            out.pop("country")
    return out


def normalize_datetime(dt: dict) -> dict:
    """Repair an internal contradiction between `local` and `precision`.

    Measured: a model emits {"local": "2026-07-08", "precision": "datetime"} —
    date-only text tagged as a full datetime. That is not a judgement call, it
    is self-inconsistent data, and downstream it fires RULE-02 (identical
    start/end) on a perfectly ordinary single-day event. Trust the VALUE, which
    is what the document actually said, and fix the label. Never the reverse:
    we do not invent a time to justify a 'datetime' label (D-06).
    """
    out = dict(dt)
    local = str(out.get("local") or "")
    if not local:
        return out
    has_time = "T" in local
    if out.get("precision") == "datetime" and not has_time:
        out["precision"] = "date"
    elif out.get("precision") == "date" and has_time:
        out["local"] = local.split("T", 1)[0]        # honour the stated precision
    return out


def _walk_datetimes(node):
    """Normalize every DateTimeLocal in place, wherever it sits."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, dict) and "local" in v and "precision" in v:
                node[k] = normalize_datetime(v)
            else:
                _walk_datetimes(v)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, dict) and "local" in v and "precision" in v:
                node[i] = normalize_datetime(v)
            else:
                _walk_datetimes(v)


def normalize_payload(payload: dict) -> dict:
    """Return a normalized copy. Never drops information."""
    import copy
    p = copy.deepcopy(payload)
    _walk_datetimes(p)

    if p.get("item_type") == "flight" and isinstance(p.get("flight"), dict):
        flight = dict(p["flight"])
        for side in ("departure", "arrival"):
            mv = flight.get(side)
            if isinstance(mv, dict) and isinstance(mv.get("place"), dict):
                mv = dict(mv)
                mv["place"] = normalize_place(mv["place"], is_airport=True)
                flight[side] = mv
        p["flight"] = flight

    for key in ("lodging", "event", "ground_transport"):
        detail = p.get(key)
        if not isinstance(detail, dict):
            continue
        detail = dict(detail)
        for field in ("place", "venue"):
            if isinstance(detail.get(field), dict):
                detail[field] = normalize_place(detail[field], is_airport=False)
        for side in ("departure", "arrival"):
            mv = detail.get(side)
            if isinstance(mv, dict) and isinstance(mv.get("place"), dict):
                mv = dict(mv)
                mv["place"] = normalize_place(mv["place"], is_airport=False)
                detail[side] = mv
        p[key] = detail

    return p


def normalize_batch(batch: dict) -> dict:
    out = dict(batch)
    out["items"] = [normalize_payload(i) for i in batch.get("items", [])]
    return out
