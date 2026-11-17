"""Manual entry: the details of a capture AI didn't read, typed in by hand
(SPEC-12 §6, D-228, YONDER-354).

With AI off, a capture none of the structured readers can read waits in review
as "add the details by hand". The person picks what it is and fills in the
fields; the result then passes the same extraction schema gate as anything a
reader produced (D-11), and the re-resolve runs the same rules. The fields are
review's own editor (routes._fields), so a hand-entered item edits like any
other afterwards.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

# What a person can say a capture is, in the order the form offers them.
TYPES = (("flight", "Flight"),
         ("lodging", "Hotel or stay"),
         ("event", "Event, tour or reservation"),
         ("ground_transport", "Train, bus, car or transfer"))
TYPE_NAMES = dict(TYPES)

# Beyond the fields review already marks as needed (routes._fields), what the
# schema requires of each type before it can pass the gate.
_ALSO_NEEDED = {
    "lodging": ("/lodging/check_in", "/lodging/check_out"),
    "event": ("/event/start",),
    "ground_transport": ("/ground_transport/departure/time",),
}
# Each type's places, and whether the schema needs it (a flight or a journey
# needs both ends, a stay its address; an event's venue is optional).
_PLACES = {
    "flight": (("/flight/departure/place", "Departs"), ("/flight/arrival/place", "Arrives")),
    "lodging": (("/lodging/place", "Where"),),
    "event": (),
    "ground_transport": (("/ground_transport/departure/place", "From"),
                         ("/ground_transport/arrival/place", "To")),
}
_OPTIONAL_PLACES = {"event": ("/event/venue",)}
_PLACE_KEYS = ("name", "iata", "address", "city", "region", "country")


def skeleton(item_type: str) -> dict:
    """An empty payload of `item_type`, for the form to render."""
    if item_type not in TYPE_NAMES:
        raise ValueError(f"unknown item type {item_type!r}")
    body: dict = {}
    if item_type == "ground_transport":
        body["mode"] = "train"
    return {"item_type": item_type, "booking_status": "confirmed", item_type: body}


def _node(payload: dict, path: str):
    node = payload
    for tok in [p for p in path.split("/") if p]:
        if not isinstance(node, dict):
            return None
        node = node.get(tok)
    return node


def _tidy(payload: dict, item_type: str, specs: dict) -> None:
    """Shape what was typed the way the schema wants it.

    - A Place needs `raw` (what the document said). By hand, it's what the
      person typed, joined; a place they left empty is dropped.
    - An empty date is dropped, except a journey's own departure or arrival
      time, which the schema requires: that becomes "no date yet" (D-210),
      never a made-up one.
    A typed time keeps origin=document, as a review edit does: the person
    copies it from the original shown beside the form. That a person entered
    it is the field lock (D-13), not the origin.
    """
    for path, spec in specs.items():
        if spec["kind"] != "datetime":
            continue
        tokens = [p for p in path.split("/") if p]
        parent = _node(payload, "/" + "/".join(tokens[:-1]))
        if not isinstance(parent, dict):
            continue
        if tokens[-1] == "time" and not isinstance(parent.get("time"), dict):
            parent["time"] = {}                  # a journey's end, left out of the form
        if not isinstance(parent.get(tokens[-1]), dict):
            continue
        when = parent[tokens[-1]]
        if when.get("local"):
            continue
        if tokens[-1] == "time":
            parent[tokens[-1]] = {"precision": "none"}
        else:
            parent.pop(tokens[-1])
    paths = [p for p, _ in _PLACES[item_type]] + list(_OPTIONAL_PLACES.get(item_type, ()))
    for path in paths:
        tokens = [p for p in path.split("/") if p]
        parent = _node(payload, "/" + "/".join(tokens[:-1]))
        if not isinstance(parent, dict):
            continue
        place = parent.get(tokens[-1])
        if not isinstance(place, dict):
            continue
        typed = [str(place[k]) for k in _PLACE_KEYS if place.get(k)]
        if typed:
            place["raw"] = ", ".join(typed)
        else:
            parent.pop(tokens[-1])


def build(item_type: str, fields: dict[str, str]) -> tuple[dict, list[str], list[str]]:
    """(payload, the paths typed in, problems in plain words). An empty problem
    list means the payload passed the schema gate."""
    from app.review.routes import _apply_edit, _fields
    from extract.normalize import normalize_payload
    from extract.pipeline import _SCHEMA
    from validation import validate_schema

    payload = skeleton(item_type)
    specs = {f["path"]: f for f in _fields(payload) if f.get("editable")}
    typed = [path for path, raw in fields.items()
             if path in specs and _apply_edit(payload, path, raw, specs[path]["kind"])]

    problems = []
    labels = {f["path"]: f["label"] for f in _fields(payload)}
    for f in _fields(payload):
        if f.get("missing"):
            problems.append(f"{f['label']} is needed." if f["kind"] != "datetime"
                            else f"{f['label']} needs a date and time.")
    for path in _ALSO_NEEDED.get(item_type, ()):
        if not (_node(payload, path) or {}).get("local"):
            problems.append(f"{labels.get(path, path)} is needed.")
    _tidy(payload, item_type, specs)
    for path, label in _PLACES[item_type]:
        if item_type != "flight" and not _node(payload, path):   # a flight's airports say it
            problems.append(f"{label}: add a name, address or city.")
    if problems:
        return payload, typed, problems

    payload = normalize_payload(copy.deepcopy(payload))
    errors = validate_schema({"items": [payload], "declared_segment_count": None}, _SCHEMA)
    if errors:
        # The checks above cover what the form can leave out; this is the gate
        # itself (D-11), so say plainly that something didn't fit.
        problems.append("These details don't fit together yet: " + errors[0][:200])
    return payload, typed, problems


def extraction(payload: dict) -> dict:
    """The item's `extraction` block for a hand-entered payload."""
    return {"extractor": "manual", "model": None, "prompt_version": None,
            "ran_at": datetime.now(timezone.utc).isoformat(), "confidence": 1.0,
            "payload": payload}


def apply(session, item, item_type: str, fields: dict[str, str]) -> list[str]:
    """Fill a capture AI didn't read with what was typed. Returns the problems
    (nothing is saved while there are any); on success the item has the
    payload, every typed field is locked (D-13/RULE-15, as a review edit is),
    and the re-resolve has run the rules."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.review.routes import _reresolve
    if item.extraction:
        return ["This already has its details; edit them instead."]
    if item_type not in TYPE_NAMES:
        return ["Choose what this is."]
    payload, typed, problems = build(item_type, fields)
    if problems:
        return problems
    now = datetime.now(timezone.utc).isoformat()
    item.item_type = item_type
    item.extraction = extraction(payload)
    item.revision = {**(item.revision or {}), "field_locks": sorted(typed)}
    # The "add the details by hand" reasons are answered; the rules give the
    # item its new ones on the re-resolve.
    item.review = {"required": False, "reasons": [], "action_taken": "edited",
                   "resolved_at": now, "resolved_by": "manual-entry"}
    item.status = {**(item.status or {}), "extract": "ok", "resolve": "pending",
                   "airtrail": "pending"}
    for f in ("extraction", "revision", "review", "status"):
        flag_modified(item, f)
    session.commit()
    _reresolve(session, item)
    return []


_ORIGINAL_CHARS = 12_000


def original(session, item) -> dict | None:
    """The capture's own words, to type the details from: an email's subject,
    sender and text, a web page's text, a calendar file or a PDF's text. None
    when there's no batch; {"gone": True} once retention removed it (D-196)."""
    import base64

    from app import documents, retention
    from app.models import IngestBatch
    from extract import filescan, mime, pdf, web
    batch = session.get(IngestBatch, item.batch_id) if item.batch_id else None
    if batch is None:
        return None
    payload = batch.payload or {}
    if retention.purged(payload):
        return {"gone": True}
    if payload.get("capture_channel") == "file":
        payload = documents.pipeline_payload(session, payload)
        if payload.get("missing"):
            return {"gone": True}
    out: dict = {"facts": [], "text": ""}
    try:
        if payload.get("raw_message_base64"):
            msg = mime.decode_raw_message(payload["raw_message_base64"])
            out["facts"] = [("Subject", msg.subject), ("From", msg.sender), ("Date", msg.date)]
            out["facts"] += [("Attachment", name) for name, _ in msg.pdf_attachments]
            out["text"] = mime.best_text(msg)
        elif payload.get("html"):
            out["facts"] = [("Page", payload.get("source_url"))]
            out["text"] = web.html_to_text(payload["html"])
        elif payload.get("file_base64"):
            data = base64.b64decode(payload["file_base64"])
            out["facts"] = [("File", payload.get("filename"))]
            if payload.get("content_type") == "text/calendar":
                out["text"] = data.decode("utf-8", errors="replace")
            elif filescan.check_pdf(data).ok:              # static checks, as the pipeline
                out["text"] = pdf.extract_pdf_text(data)
        else:
            out["facts"] = [("Link", payload.get("url") or payload.get("source_url"))]
    except Exception:                                       # a bad capture shows no text
        out["text"] = ""
        out["unreadable"] = True
    out["facts"] = [(k, v) for k, v in out["facts"] if v]
    out["cut"] = len(out["text"]) > _ORIGINAL_CHARS
    out["text"] = out["text"][:_ORIGINAL_CHARS]
    return out
