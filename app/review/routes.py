"""Review queue — the one hand-designed screen (D-18).

HTMX + Jinja, server-rendered from this same FastAPI app: no build step, no
Node in the image, nothing extra to deploy (OQ-07, owner decision 2026-08-03).

Approving STAGES an item; a separate explicit action does the writes (D-31).
Nothing here talks to AirTrail or AdventureLog.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

import rules as rules_module
from app import scope
from app.db import SessionLocal, get_session
from app.models import TravelItem, Trip
from app.review import jobs
from app.review import reasons as reason_text
from app.review import summary as sm

log = logging.getLogger("yonder.review")
router = APIRouter(prefix="/review")
# D-200 (YONDER-228): the queue is the Inbox, at /inbox. /review still answers
# (redirects) so bookmarks and older app links work; the item and action URLs
# under /review/… are unchanged.
inbox_router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
from app.icons import install as _install_icons  # noqa: E402
_install_icons(templates)


def _fields(payload: dict) -> list[dict]:
    """Editable field specs for the detail pane.

    Each carries the JSON pointer it edits, so a submitted value can be written
    straight back into the payload. Labels are what a traveller calls the thing,
    never the JSON path.
    """
    t = payload.get("item_type")
    rows: list[dict] = []

    def get(path: str):
        node = payload
        for tok in [p for p in path.split("/") if p]:
            if not isinstance(node, dict):
                return None
            node = node.get(tok)
        return node

    def dt_field(label: str, path: str, required: bool = False):
        d = get(path) or {}
        local = str(d.get("local") or "")
        precision = d.get("precision")
        rows.append({
            "label": label, "path": path, "kind": "datetime",
            "value": local, "tz": d.get("tz"), "precision": precision,
            # A flight without a full timestamp cannot be logged (RULE-04/D-06),
            # so flag it here rather than letting the rule engine be the first
            # place anyone hears about it.
            "missing": required and precision != "datetime",
            "editable": True,
        })

    def text_field(label: str, path: str, required: bool = False, editable: bool = True):
        v = get(path)
        rows.append({"label": label, "path": path, "kind": "text",
                     "value": "" if v is None else str(v),
                     "missing": required and not v, "editable": editable})

    def place_fields(prefix: str, path: str, airport: bool = False):
        if airport:
            # A flight can neither be keyed nor logged without its airport code
            # (missing_airport_code), so surface an empty one as the field to
            # fix rather than leaving the operator to guess what "unknown" means.
            text_field(f"{prefix} airport", f"{path}/iata", required=True)
        text_field(f"{prefix} name", f"{path}/name")
        if not airport:
            text_field(f"{prefix} address", f"{path}/address")
        text_field(f"{prefix} city", f"{path}/city")
        text_field(f"{prefix} country", f"{path}/country")

    if t == "flight":
        text_field("Flight number", "/flight/flight_number", required=True)
        text_field("Airline", "/flight/marketing_carrier/name")
        text_field("Airline code", "/flight/marketing_carrier/iata")
        dt_field("Departure time", "/flight/departure/time", required=True)
        place_fields("Departs", "/flight/departure/place", airport=True)
        dt_field("Arrival time", "/flight/arrival/time", required=True)
        place_fields("Arrives", "/flight/arrival/place", airport=True)
        # Passengers are NOT a free-text field — they are chosen from the
        # configured Travellers (see _passenger_context + the detail template),
        # so "who flew" is always a known identity (self/guest), never a typo.
    elif t == "lodging":
        text_field("Property", "/lodging/property_name", required=True)
        dt_field("Check in", "/lodging/check_in", required=False)
        dt_field("Check out", "/lodging/check_out", required=False)
        place_fields("", "/lodging/place")
    elif t == "event":
        text_field("Event", "/event/title", required=True)
        dt_field("Starts", "/event/start", required=False)
        dt_field("Ends", "/event/end", required=False)
        place_fields("Venue", "/event/venue")
    elif t == "ground_transport":
        # The kind is the reviewer's to correct — a taxi read as "car" logs as a
        # road trip otherwise (owner, 2026-10-12; D-193). "rideshare" is the
        # older name for a taxi: offered only on items that already have it.
        mode = (payload.get("ground_transport") or {}).get("mode") or ""
        rows.append({"label": "Kind", "path": "/ground_transport/mode", "kind": "select",
                     "value": mode,
                     "options": [m for m in _GROUND_MODES if m != "rideshare" or mode == m],
                     "labels": _GROUND_MODE_LABELS, "missing": False, "editable": True})
        text_field("Service number", "/ground_transport/service_number")
        dt_field("Departs", "/ground_transport/departure/time")
        place_fields("From", "/ground_transport/departure/place")
        dt_field("Arrives", "/ground_transport/arrival/time")
        place_fields("To", "/ground_transport/arrival/place")
        route = (payload.get("ground_transport") or {}).get("route") or {}
        if route.get("source_url"):
            # What the router worked out once both stops were located; blank
            # until they are (fix a From/To address and it is recomputed).
            text_field("Distance (km)", "/ground_transport/route/distance_km", editable=False)
            text_field("Drive time (min)", "/ground_transport/route/duration_minutes",
                       editable=False)
            text_field("Directions link", "/ground_transport/route/source_url", editable=False)

    rows.append({"label": "Status", "path": "/booking_status", "kind": "select",
                 "value": payload.get("booking_status") or "",
                 "options": ["planned", "confirmed", "changed", "cancelled",
                             "pending", "unknown"],
                 "missing": False, "editable": True})
    text_field("Reference", "/booking_reference")
    return rows


# Ground modes in the order the review dropdown offers them, and their labels.
_GROUND_MODES = ("taxi", "car", "car_rental", "train", "bus", "ferry", "transfer",
                 "walk", "other", "rideshare")
_GROUND_MODE_LABELS = {"taxi": "Taxi (Uber, Lyft…)", "car": "Car (a drive)",
                       "car_rental": "Car rental", "train": "Train", "bus": "Bus",
                       "ferry": "Ferry", "transfer": "Transfer / shuttle",
                       "walk": "Walk", "other": "Other", "rideshare": "Taxi (rideshare)"}


_LOCAL_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:T(\d{1,2}):?(\d{2}))?$")


def _normalize_local(raw: str) -> str | None:
    """Parse a reviewer-typed date/time into a schema DateTimeLocal `local`.

    Accepts a bare date, or a date and time with the time written as `07:20` OR
    `0720` OR space-separated (`2026-11-20 07:20`). Returns `YYYY-MM-DD` or
    `YYYY-MM-DDTHH:MM`, or None if it can't be read — the earlier code stored the
    raw string verbatim, so `2026-11-20T0720` (no colon) reached the payload as
    an invalid datetime that every downstream check then rejected."""
    m = _LOCAL_RE.fullmatch((raw or "").strip().replace(" ", "T"))
    if not m:
        return None
    date, hh, mm = m.group(1), m.group(2), m.group(3)
    if hh is None:
        return date
    if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
        return None
    return f"{date}T{int(hh):02d}:{mm}"


def _apply_edit(payload: dict, path: str, raw: str, kind: str) -> bool:
    """Write one submitted value into the payload. Returns True if it changed.

    Times set `precision` from the SHAPE of what was entered (D-06): a
    date-only entry stays date-only rather than being padded to midnight, which
    is the exact failure the whole DateTimeLocal design exists to prevent.
    """
    tokens = [t for t in path.split("/") if t]
    node = payload
    for tok in tokens[:-1]:
        nxt = node.get(tok)
        if not isinstance(nxt, dict):
            nxt = {}
            node[tok] = nxt
        node = nxt
    leaf = tokens[-1]
    raw = (raw or "").strip()

    if kind == "datetime":
        target = node.get(leaf)
        if not isinstance(target, dict):
            target = {}
            node[leaf] = target
        before = (target.get("local"), target.get("precision"))
        if not raw:
            return False
        local = _normalize_local(raw)
        if local is None:                        # unparseable — leave untouched
            return False
        target["local"] = local
        target["precision"] = "datetime" if "T" in local else "date"
        # The reviewer is transcribing what the original document says — the
        # split view exists for exactly that — so the value's origin is still
        # the document. That a HUMAN supplied it is recorded separately, as a
        # field lock (D-13/RULE-15), which is the audit trail that matters.
        target.setdefault("origin", "document")
        return before != (target["local"], target["precision"])

    before = node.get(leaf)
    after = raw or None
    if before == after:
        return False
    node[leaf] = after
    # A corrected name/address/city means the place we located from the OLD text
    # may be wrong: drop coordinates the geocoder guessed (never ones the
    # document or link stated) so the re-resolve locates the new text, and the
    # drive distance built from them.
    if leaf in {"name", "address", "city", "region", "country"} \
            and node.get("geocode_source") == "photon":
        for k in ("lat", "lon", "geocode_source"):
            node.pop(k, None)
        route = (payload.get("ground_transport") or {}).get("route")
        if isinstance(route, dict):
            route["distance_km"] = route["duration_minutes"] = route["polyline"] = None
    return True


templates.env.filters["fields"] = _fields

# Queue buckets, in the order an operator works them.
BUCKETS = ("attention", "waiting", "ready", "staged", "logged", "all")


def _is_logged(item: TravelItem) -> bool:
    """Committed for real (not a dry run) with every target written. Such an
    item is done — it must leave 'Approved' so that bucket keeps meaning
    'approved, awaiting the write', and land in 'Logged' as the record."""
    commit = item.commit or {}
    if commit.get("dry_run", True):
        return False
    results = commit.get("results") or []
    targets = set((item.resolution or {}).get("targets") or [])
    done = {r.get("target") for r in results if r.get("ok")}
    return bool(targets) and targets <= done


def _externally_present(item: TravelItem, airtrail, adventurelog,
                        al_cache: dict, flights: list | None) -> bool:
    """True when this item's record already exists in AirTrail or AdventureLog,
    even if the DB lost its commit record. Reprocess uses this so it never
    deletes+re-extracts a batch it would then re-log into a duplicate (D-60) —
    the failure that orphaned 11 already-logged items. Best-effort: a target it
    can't reach just isn't protected here (the commit stage dedup is the backstop)."""
    from app.committing import (_al_collection_records, _date, _match_lodging,
                                _match_note, _match_transportation,
                                build_lodging, build_note, build_transportation)
    payload = (item.extraction or {}).get("payload") or {}
    cid = (item.resolution or {}).get("collection_id")
    if item.item_type == "flight" and airtrail is not None:
        f = payload.get("flight") or {}
        mc = f.get("marketing_carrier") or {}
        dep = (f.get("departure", {}).get("place") or {})
        arr = (f.get("arrival", {}).get("place") or {})
        try:
            if airtrail.find_duplicate(
                    carrier_iata=mc.get("iata"),
                    flight_number=f.get("flight_number") or "",
                    departure_date=_date(f.get("departure", {}).get("time")) or "",
                    origin_iata=dep.get("iata"), destination_iata=arr.get("iata"),
                    flights=flights):
                return True
        except Exception:
            pass
    if adventurelog is not None and cid:
        records = _al_collection_records(adventurelog, cid, al_cache)
        try:
            if item.item_type in ("flight", "ground_transport"):
                body, _ = build_transportation(payload, collection_id=cid)
                if _match_transportation(records, body):
                    return True
            if item.item_type == "lodging" and \
                    _match_lodging(records, build_lodging(payload, collection_id=cid)):
                return True
            if item.item_type == "event" and \
                    _match_note(records, build_note(payload, collection_id=cid)):
                return True
        except Exception:
            pass
    return False


def _bucket_of(item: TravelItem) -> str:
    status = item.status or {}
    review = item.review or {}
    if _is_logged(item):
        return "logged"
    # ONLY an explicit approval stages an item (D-31). Editing or reassigning is
    # a correction, not a ruling: it changes the data that would be written, so
    # the item returns to "Ready to Approve" (below, via its resolve state) for a
    # conscious yes on the new payload. Treating "edited" as staged was the bug
    # behind "11 Approved but only 2 logged" — committable() only ever accepted
    # "approved", so edited items sat in Approved yet silently never wrote.
    if review.get("action_taken") == "approved" and not review.get("required"):
        return "staged"
    if review.get("required") or status.get("resolve") in ("needs_review", "failed"):
        return "attention"
    # Deferred is NOT "needs you": it is waiting by design for a trip to appear,
    # and only escalates after its TTL (D-24). Showing it as blocked would ask
    # for action the design says not to take yet.
    if status.get("resolve") == "deferred":
        return "waiting"
    if status.get("resolve") == "ok":
        return "ready"
    return "all"


# The linear pipeline every item walks. Not every item hits every stage — a
# clean flight with a trip enters straight at "Ready to Approve"; a hotel with
# no trip waits at stage 2 — but the order never changes and "Logged" is always
# the end. "Ignored" is the off-ramp, terminal from anywhere.
_STAGES = [
    ("attention", "Needs you"),
    ("waiting", "Waiting for a trip"),
    ("ready", "Ready to Approve"),
    ("staged", "Approved"),
    ("logged", "Logged"),
]


def _stage_path(bucket: str, review: dict) -> dict:
    """This item's position in the pipeline, for the detail-header chevron.
    Stages left of the current one read as 'behind you', right as 'ahead'; we
    don't claim an item 'completed' a stage it skipped, only where it is now."""
    if (review or {}).get("action_taken") == "ignored":
        return {"ignored": True, "steps": []}
    order = [k for k, _ in _STAGES]
    cur = order.index(bucket) if bucket in order else None
    steps = []
    for idx, (key, label) in enumerate(_STAGES):
        state = ("current" if idx == cur
                 else "past" if cur is not None and idx < cur
                 else "upcoming")
        steps.append({"key": key, "label": label, "state": state})
    return {"ignored": False, "steps": steps}


def _trip_name(item: TravelItem) -> str | None:
    """The name of the Run·dere trip the item is filed to, for the preview."""
    if not item.trip_id:
        return None
    from sqlalchemy.orm import object_session
    from app.models import Trip
    session = object_session(item)
    trip = session.get(Trip, item.trip_id) if session is not None else None
    return trip.name if trip is not None else None


# Review reasons from the AdventureLog collection ladder (see _view).
_COLLECTION_CODES = {"no_collection_match", "ambiguous_collection", "collection_not_selected"}


def _view(item: TravelItem) -> dict:
    payload = (item.extraction or {}).get("payload") or {}
    resolution = item.resolution or {}
    review = item.review or {}
    codes = [c for c in (review.get("reasons") or []) if isinstance(c, str)]
    bucket = _bucket_of(item)
    resolve_state = (item.status or {}).get("resolve")
    deferred_until = resolution.get("deferred_until")

    # An attention item must NEVER be reasonless — that was the invisible deadlock
    # (in 'Needs you', no note, Approve silently refused). If a code got lost,
    # name the trip problem when the outcome points at one, else a generic look.
    if bucket == "attention" and not codes:
        codes = (["no_collection_match"]
                 if resolution.get("outcome") in ("deferred", "needs_review")
                 else ["low_confidence"])

    note = reason_text.headline(codes)
    action_hint = reason_text.action_hint(codes)
    if resolution.get("outcome") == "deferred" and deferred_until:
        note = (f"No trip covers these dates yet. Waiting until {deferred_until}, "
                f"then it comes back to you.")
    # The collection codes come from the AdventureLog ladder. Say which "trip"
    # they mean (D-192): an item already on a Run·dere trip only lacks the
    # OPTIONAL AdventureLog collection; one with no trip lacks a Run·dere trip.
    al_on = "adventurelog" in resolution.get("integrations", ["adventurelog"])
    trip_codes = set(codes) & _COLLECTION_CODES
    if item.trip_id and (trip_codes or resolution.get("outcome") == "deferred"):
        if not al_on:
            note, action_hint = "", ""
        elif "ambiguous_collection" in trip_codes:
            note = ("More than one AdventureLog collection could hold this. It's on your "
                    "Run·dere trip; choosing a collection is optional.")
            action_hint = "Pick one under AdventureLog, or leave it."
        else:
            note = ("No AdventureLog collection matches these dates. It's on your "
                    "Run·dere trip; adding it to AdventureLog is optional.")
            action_hint = "Pick a collection under AdventureLog, or leave it."
    elif not item.trip_id and al_on and (trip_codes or resolution.get("outcome") == "deferred"):
        note = note.replace("No trip covers these dates yet.",
                            "No Run·dere trip or AdventureLog collection covers these dates yet.")
    if (item.revision or {}).get("action") == "no_change":
        note = "Already logged from an earlier capture — this one writes nothing."
    if (item.revision or {}).get("action") == "supersede":
        note = "Replaces an earlier version of the same booking."

    return {
        "id": item.id,
        "bucket": bucket,
        "trip_id": item.trip_id,
        "stages": _stage_path(bucket, review),
        "kind": sm.kind_label(payload),
        "headline": sm.headline(payload),
        "subline": sm.subline(payload),
        "reference": sm.reference(payload),
        "note": note,
        "action_hint": action_hint,
        "reason_codes": codes,
        # The optional AdventureLog collection is still unsettled (D-192).
        "al_pending": bool(resolution.get("collection_pending")),
        "canonical_key": item.canonical_key,
        "locked_fields": (item.revision or {}).get("field_locks") or [],
        "revision_action": (item.revision or {}).get("action"),
        "collection_name": resolution.get("collection_name"),
        "collection_action": resolution.get("collection_action"),
        "extractor": (item.extraction or {}).get("extractor"),
        "confidence": (item.extraction or {}).get("confidence"),
        "created_at": item.created_at,
        # "Blocked" = Approve would refuse. Approve stages only a resolved item
        # (status.resolve == "ok"), so blocked mirrors that exactly. A flight
        # whose trip is still unsettled but whose AirTrail write is ready resolves
        # to "ok" and is therefore approvable (it logs to AirTrail; the trip
        # record follows once a trip is picked) — it shows in 'Needs you' with a
        # reason but an ENABLED button, never the old reasonless dead end.
        "blocked": bucket == "attention" and resolve_state != "ok",
        "writes": sm.write_preview(payload, resolution, item.revision,
                                   trip_name=_trip_name(item)),
        "payload": payload,
        "source": item.source or {},
    }


def _all_views(session: Session, user=None) -> list[dict]:
    # Phase 2 (D-120): the review queue is per-submitter — a user sees their own
    # captures + open ones. No-op when auth is off or the user is admin.
    from app import scope
    items = (session.query(TravelItem).filter(scope.capture_filter(user))
             .order_by(TravelItem.created_at.desc()).all())
    return [_view(i) for i in items]


def _counts(views: list[dict]) -> dict:
    c = {b: 0 for b in BUCKETS}
    for v in views:
        c[v["bucket"]] = c.get(v["bucket"], 0) + 1
    c["all"] = len(views)
    return c


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def review_moved(request: Request):
    """D-200: the Review queue is now the Inbox. Keep the query (bucket, trip)."""
    from fastapi.responses import RedirectResponse
    q = request.url.query
    return RedirectResponse("/inbox" + (f"?{q}" if q else ""), status_code=301)


@inbox_router.get("/inbox", response_class=HTMLResponse)
def queue(request: Request, bucket: str = "attention", trip: str | None = None,
          session: Session = Depends(get_session)):
    views = _all_views(session, scope.user_of(request))
    trip_name = None
    if trip:
        # D-200: a trip's "waiting in Inbox" link narrows the Inbox to that trip;
        # the Inbox stays the one place to approve. Only a trip the viewer can see.
        t = session.get(Trip, trip)
        if t is None or not scope.can_view_trip(session, scope.user_of(request), t):
            trip = None
        else:
            trip_name = t.name
            views = [v for v in views if v["trip_id"] == trip]
    shown = views if bucket == "all" else [v for v in views if v["bucket"] == bucket]
    return templates.TemplateResponse(request, "queue.html", {
        "items": shown, "counts": _counts(views),
        "bucket": bucket if bucket in BUCKETS else "attention",
        "trip_filter": {"id": trip, "name": trip_name} if trip else None,
    })


# Buckets an item is still waiting in, i.e. not yet logged (approved and committed).
WAITING_BUCKETS = ("attention", "waiting", "ready", "staged")


def waiting_for_trip(session: Session, user, trip_id: str) -> int:
    """How many of the viewer's captures filed to this trip are still in the Inbox
    (D-200): the trip header's "N waiting in Inbox"."""
    items = (session.query(TravelItem).filter(scope.capture_filter(user),
                                              TravelItem.trip_id == trip_id).all())
    return sum(1 for i in items if _view(i)["bucket"] in WAITING_BUCKETS)


def _to_process(session: Session, user=None) -> int:
    """How much the "Process new captures" button has to do for this user:
    captured emails not extracted yet (batches still `received`) plus extracted
    items not resolved yet (resolve `pending`, which sit in no funnel stage).
    Scoped like the queue: own captures (admin: + ownerless and sharing users'; auth-off: all)."""
    from app.models import IngestBatch
    q = session.query(IngestBatch).filter(IngestBatch.status == "received")
    if not scope.unrestricted(user):
        q = q.filter(scope.batch_filter(user))              # own + open (+ sharing, admin)
    pending = sum(1 for i in session.query(TravelItem)
                  .filter(scope.capture_filter(user)).all()
                  if (i.status or {}).get("resolve") == "pending")
    return q.count() + pending


@router.get("/to-process", response_class=HTMLResponse)
def to_process_stage(request: Request, session: Session = Depends(get_session)):
    """The funnel's first stage, fetched on its own (hx-trigger=load) so every
    re-render of the funnel — after an action or a processing run — refreshes
    it without threading a count through every response."""
    n = _to_process(session, scope.user_of(request))
    return HTMLResponse(
        f'<div class="stage{"" if n else " muted"}" id="to-process" '
        f'title="Captured but not processed yet — use Process new captures">'
        f'<b>{n}</b><span>to process</span></div>')


@router.get("/item/{item_id}", response_class=HTMLResponse)
def detail(item_id: str, request: Request, bucket: str = "attention",
           session: Session = Depends(get_session)):
    item = scope.capture_or_none(session, request, item_id)
    if item is None:
        return HTMLResponse("<p class='empty'>That item no longer exists.</p>",
                            status_code=404)
    # Rule text is re-derived on demand: it is a pure function of the item, so
    # storing it would only create something to drift. `bucket` is the queue
    # the operator is viewing, so the action buttons post back to it and keep
    # them there instead of bouncing to the first queue.
    return _detail_response(request, session, item, bucket=bucket)


def _rule_shape(item: TravelItem) -> dict:
    return {"id": item.id, "canonical_key": item.canonical_key,
            "item_type": item.item_type, "source": item.source,
            "extraction": item.extraction, "resolution": item.resolution,
            "revision": item.revision, "review": item.review,
            "commit": item.commit, "status": item.status,
            "batch_id": item.batch_id, "segment_index": item.segment_index,
            "segment_count": item.segment_count}


@router.post("/item/{item_id}/save", response_class=HTMLResponse)
async def save(item_id: str, request: Request, bucket: str = "attention",
               session: Session = Depends(get_session)):
    """Apply the reviewer's edits, then re-resolve the item.

    Every edited pointer becomes a FIELD LOCK (D-13/RULE-15): a later capture
    that would overwrite a value a person corrected becomes a `conflict` for
    them to judge, never a silent overwrite. Re-resolving immediately means the
    rules re-run and a fixed item leaves the queue on the spot.
    """
    item = scope.capture_or_none(session, request, item_id)
    if item is None:
        return HTMLResponse("", status_code=404)

    form = await request.form()
    from app.review import service
    fields = {k[2:]: str(v) for k, v in form.multi_items() if k.startswith("f:")}
    # Passengers come from checkboxes (pax:<identity_id>), never free text; the
    # resolver re-derives self/guest on the re-resolve. `pax_present` marks that
    # the control was on the form, so "everything unchecked" clears the list.
    pax = ([k[4:] for k, _ in form.multi_items() if k.startswith("pax:")]
           if "pax_present" in form else None)
    # "Who is this for" (non-flight, 4.1): kept on the review so a re-resolve
    # never drops it; the commit stage tags the timeline activity with it.
    who_ids = ([int(k[4:]) for k, _ in form.multi_items()
                if k.startswith("who:") and k[4:].isdigit()]
               if "who_present" in form else None)
    changed = service.apply_edits(session, item, fields=fields, passenger_ids=pax,
                                  traveler_ids=who_ids)

    # "Save & approve": correcting the data and approving it in one click, while
    # keeping the two conceptually distinct (D-31). We only approve if the item
    # actually resolved ok after the edit — resolve==ok means it is out of the
    # "Needs you" bucket, so it is never blocked. Otherwise we just save and let
    # the operator see why it still needs attention.
    final = scope.capture_or_none(session, request, item_id)
    approved_now = False
    if "and_approve" in form:
        approved_now = service.approve(session, final)

    if approved_now:
        # Approving moves the item out of its current bucket, so re-render the
        # LIST (badges + membership), not just the detail — otherwise the strip
        # keeps its old "Ready" pill and stays in the queue. Retarget the swap to
        # the whole queue body (same effect as the plain Approve action); the
        # open detail collapses, which is the expected "it moved on" feedback.
        views = _all_views(session, scope.user_of(request))
        shown = views if bucket == "all" else [v for v in views if v["bucket"] == bucket]
        resp = templates.TemplateResponse(request, "_after_action.html", {
            "items": shown, "counts": _counts(views), "bucket": bucket, "oob": True})
        resp.headers["HX-Retarget"] = "#queue-body"
        resp.headers["HX-Reswap"] = "innerHTML"
        return resp

    # Asked to approve but it couldn't be (still not resolved): say so and why,
    # instead of a silent no-op that reads as "the button is broken".
    return _detail_response(request, session, final,
                            bucket=bucket, saved=bool(changed), oob_counts=True,
                            approve_refused="and_approve" in form)


@router.post("/item/{item_id}/assign", response_class=HTMLResponse)
def assign_trip(item_id: str, request: Request, collection_id: str = Form(""),
                bucket: str = "attention", session: Session = Depends(get_session)):
    """Assign this item to a specific AdventureLog trip (or clear the assignment).

    The pick is stored as capture_context.collection_id — the SAME lever the
    capture flow uses and the top of the resolver's collection ladder (D-05) — so
    the resolver honours it as `user_selected` and RULE-35b/c protect it. Choosing
    a trip re-resolves the item: an ambiguous/no-trip flight that was logging only
    to AirTrail now also files its AdventureLog transportation into the chosen
    trip. An empty value clears the pick and returns the item to auto-resolution.
    """
    item = scope.capture_or_none(session, request, item_id)
    if item is None:
        return HTMLResponse("", status_code=404)

    cid = (collection_id or "").strip()
    # The picker lists the capture OWNER's AdventureLog trips (D-150): an owner
    # with no AdventureLog connection has none to pick.
    if cid and cid not in {t["id"] for t in _list_trips(session, item.owner_id)}:
        # An id that names no known trip is ignored rather than trusted — never
        # file into a collection the resolver couldn't confirm exists.
        return _detail_response(request, session, item, bucket=bucket, saved=False)

    source = dict(item.source or {})
    cc = dict(source.get("capture_context") or {})
    if cid:
        cc["collection_id"] = cid
    else:
        cc.pop("collection_id", None)
    source["capture_context"] = cc
    item.source = source
    review = dict(item.review or {})
    review.update({"action_taken": "edited",
                   "resolved_at": datetime.now(timezone.utc).isoformat(),
                   "resolved_by": "review-ui"})
    item.review = review
    status = dict(item.status or {})
    status["resolve"] = "pending"
    item.status = status
    for attr in ("source", "review", "status"):
        flag_modified(item, attr)
    session.commit()
    _reresolve(session, item)

    final = scope.capture_or_none(session, request, item_id)
    return _detail_response(request, session, final, bucket=bucket,
                            saved=True, oob_counts=True)


def _list_trips(session: Session, owner_id: str | None) -> list[dict]:
    """The capture owner's AdventureLog trips for the picker, newest first
    (their own connection, D-150). Best-effort: an empty list when they have no
    AdventureLog connection or it can't be reached (the picker then simply
    doesn't render)."""
    from app import integrations
    adventurelog = integrations.clients_for_user(session, owner_id).adventurelog
    if adventurelog is None:
        return []
    try:
        return [{"id": str(c.id), "name": c.name or "(unnamed trip)",
                 "start_date": (c.start_date or "")[:10],
                 "end_date": (c.end_date or "")[:10]}
                for c in adventurelog.list_collections()]
    except Exception:
        log.warning("could not list trips for the picker", exc_info=True)
        return []


def _trip_context(session: Session, item: TravelItem, view: dict,
                  user=None) -> dict | None:
    """Options for the in-review trip picker, or None when it shouldn't show.

    Only for items still being worked (not staged/logged/ignored) that aren't
    home-only. Candidates the resolver flagged sort first, then trips whose dates
    contain the item, then the rest newest-first."""
    if view["bucket"] not in ("attention", "waiting", "ready"):
        return None
    payload = view["payload"]
    if (item.resolution or {}).get("within_home_radius"):
        return None
    trips = _list_trips(session, item.owner_id)
    if not trips:
        return None
    from resolve.targets import item_date_span
    start, end = (item_date_span(payload) or (None, None))
    candidates = set((item.resolution or {}).get("candidate_collections") or [])
    selected = ((item.source or {}).get("capture_context") or {}).get("collection_id")

    def _contains(t: dict) -> bool:
        if not (start and end and t["start_date"] and t["end_date"]):
            return False
        return t["start_date"] <= start and end <= t["end_date"]

    for t in trips:
        t["is_candidate"] = t["id"] in candidates
        t["contains"] = _contains(t)
        t["selected"] = t["id"] == selected
    trips.sort(key=lambda t: t["start_date"], reverse=True)          # newest first
    trips.sort(key=lambda t: 0 if t["is_candidate"] else 1 if t["contains"] else 2)
    return {"trips": trips, "selected": selected}


@router.post("/item/{item_id}/assign-trip", response_class=HTMLResponse)
def assign_yonder_trip(item_id: str, request: Request, trip_id: str = Form(""),
                       bucket: str = "attention",
                       session: Session = Depends(get_session)):
    """Link this item to an in-house Run·dere trip (or clear the link).

    Independent of the AdventureLog collection, but NOT of resolution: since
    D-148 the Run·dere trip is what settles a capture (`yonder.activity` is its
    primary target), so a deferred hotel becomes approvable the moment it is
    linked. The item is re-resolved here, as the AdventureLog picker does —
    without it the link was stored but the item sat in "Waiting for a trip"
    until its TTL sweep (YONDER-222).
    """
    from app.models import Trip
    item = scope.capture_or_none(session, request, item_id)
    if item is None:
        return HTMLResponse("", status_code=404)
    tid = (trip_id or "").strip()
    if tid and scope.trip_or_none(session, request, tid) is None:
        return _detail_response(request, session, item, bucket=bucket, saved=False)
    item.trip_id = tid or None
    # Pin the choice so the resolver's auto-link never overrides a human — this
    # holds for an explicit UNLINK too (empty tid), otherwise a later re-resolve
    # would silently re-link the one trip the operator just removed.
    review = dict(item.review or {})
    review["trip_pinned"] = True
    item.review = review
    flag_modified(item, "review")
    session.commit()
    # A logged item is done and an ignored one is off the pipeline: re-resolving
    # either would only churn state, so the link is recorded and nothing else.
    if not _is_logged(item) and review.get("action_taken") != "ignored":
        status = dict(item.status or {})
        status["resolve"] = "pending"
        item.status = status
        flag_modified(item, "status")
        session.commit()
        _reresolve(session, item)
    final = scope.capture_or_none(session, request, item_id)
    return _detail_response(request, session, final, bucket=bucket,
                            saved=True, oob_counts=True)


def _yonder_trip_context(session: Session, item: TravelItem,
                         view: dict, user=None) -> dict | None:
    """Options for the in-house Yonder trip picker, or None when it shouldn't
    show (item already staged/logged/ignored). Always available even with no
    trips yet — it then invites creating one. Trips whose dates contain the item
    sort first, then newest-first; the current link is preselected. Only offers
    trips the user can file into — own, open, or travelling on (4.1)."""
    from app.models import Trip
    if view["bucket"] not in ("attention", "waiting", "ready"):
        return None
    from resolve.targets import item_date_span
    start, end = (item_date_span(view["payload"]) or (None, None))
    trips = (session.query(Trip).filter(scope.filing_trip_filter(session, user))
             .order_by(Trip.start_date.desc()).all())
    selected = item.trip_id
    selected_name = None

    def _contains(t) -> bool:
        if not (start and end and t.start_date and t.end_date):
            return False
        return t.start_date <= start and end <= t.end_date

    opts = []
    for t in trips:
        if t.id == selected:
            selected_name = t.name
        opts.append({"id": t.id, "name": t.name or "(unnamed trip)",
                     "start_date": t.start_date or "", "end_date": t.end_date or "",
                     "contains": _contains(t), "selected": t.id == selected})
    opts.sort(key=lambda o: 0 if o["contains"] else 1)
    return {"trips": opts, "selected": selected, "selected_name": selected_name,
            "have_trips": bool(opts)}


def _reresolve(session: Session, item: TravelItem) -> None:
    """Resolve this one item in place, reusing the full resolver so the review
    path and the batch path can never diverge."""
    from app import integrations
    from app.config import settings
    from app.resolving import (load_enablement, load_homes_for_user, resolve_item)
    from resolve import airports as airports_mod, countries as countries_mod
    from resolve.collections import CollectionRef

    # The capture owner's own connections (D-150), like the batch path.
    clients = integrations.clients_for_user(session, item.owner_id)
    collections: list[CollectionRef] = []
    flown: dict = {}
    try:
        if clients.adventurelog is not None:
            collections = [CollectionRef(id=c.id, name=c.name,
                                         start_date=c.start_date or "",
                                         end_date=c.end_date or "")
                           for c in clients.adventurelog.list_collections()
                           if c.start_date and c.end_date]
    except Exception:
        # Resolving as if they had no trips would mint a duplicate: leave it.
        log.warning("re-resolve skipped: AdventureLog unreachable", exc_info=True)
        return
    if clients.airtrail is not None:
        try:
            flown = clients.airtrail.airport_reference()
        except Exception:
            log.warning("re-resolve ran without AirTrail's flown airports", exc_info=True)
    # Run·dere's own airport table and CLDR names (D-147, D-148).
    airports = airports_mod.reference(flown)
    country_names = countries_mod.country_names()

    # Photon too, as the batch path uses it: an edited From/To address or city
    # is only useful if the re-resolve can locate it (YONDER-121).
    from resolve.geocode import PhotonGeocoder
    geocoder = PhotonGeocoder(settings.photon_url)

    try:
        from app.resolving import load_identities
        resolve_item(session, item, load_homes_for_user(session, item.owner_id), collections,
                     load_enablement(session), airports=airports,
                     geocoder=geocoder, country_names=country_names,
                     identities=load_identities(session),  # self/guest needs these
                     external=clients.external)
        session.commit()
    except Exception:
        session.rollback()
        log.exception("re-resolve failed for item %s", item.id)


def _passenger_context(session: Session, payload: dict) -> dict | None:
    """Who-flew choices for a flight, drawn from the configured Travellers — NOT
    free text (owner, 2026-08-05). Each identity is offered as a checkbox,
    pre-checked when it matches a name the document listed; any document name
    that matches NO traveller is surfaced so the operator creates that profile
    (self or guest) rather than inventing a passenger by typing."""
    if payload.get("item_type") != "flight":
        return None
    from app.resolving import load_identities
    from resolve.identity import match_identity
    identities = load_identities(session)
    doc_names = [t.get("raw_name") for t in (payload.get("travelers") or [])
                 if t.get("raw_name")]
    selected, unmatched = set(), []
    for name in doc_names:
        ident, _, _ = match_identity(name, identities)
        (selected.add(ident.id) if ident else unmatched.append(name))
    return {
        "have_identities": bool(identities),
        "identities": [{"id": i.id, "name": i.display_name, "kind": i.kind,
                        "selected": i.id in selected} for i in identities],
        "doc_names": doc_names,
        "unmatched": unmatched,
    }


def _who_context(session: Session, item: TravelItem, payload: dict) -> dict | None:
    """4.1 "Who is this for" on a non-flight capture (a flight has "Who flew"
    above, which does the same job and also drives AirTrail). Only once the
    item is filed to a Yonder trip with 2+ travellers, listing only that trip's
    travellers. Pre-ticked: the reviewer's earlier choice, else the traveller of
    whoever captured it, else everyone."""
    if payload.get("item_type") == "flight" or not item.trip_id:
        return None
    from app.models import TravelerIdentity
    from app.trips import who
    roster = who.roster_ids(session, item.trip_id)
    if len(roster) < 2:
        return None
    names = {t.id: t.display_name for t in session.query(TravelerIdentity)
             .filter(TravelerIdentity.id.in_(roster)).all()}
    chosen = set((item.review or {}).get("traveler_ids") or [])
    if not chosen:
        chosen = {default_traveler(session, item)} & set(names)
    chosen = (chosen & set(names)) or set(names)
    return {"travelers": [{"id": tid, "name": n, "checked": tid in chosen}
                          for tid, n in sorted(names.items(), key=lambda kv: kv[1].lower())]}


def default_traveler(session: Session, item: TravelItem) -> int | None:
    """The traveller of the user who captured the item (None if open/unlinked)."""
    if not item.owner_id:
        return None
    from app.models import User
    user = session.get(User, item.owner_id)
    return user.traveler_id if user else None


def _original_context(session: Session, item: TravelItem) -> dict | None:
    """Whether the original email/page/file is still kept, and until when
    (SPEC-8 D-196): removed 12 months after its trip ends."""
    from app import retention
    from app.models import IngestBatch
    batch = session.get(IngestBatch, item.batch_id) if item.batch_id else None
    if batch is None:
        return None
    if retention.source_gone(session, batch.payload):
        return {"removed": True}
    items = session.query(TravelItem).filter(TravelItem.batch_id == batch.id).all()
    return {"removed": False,
            "until": retention._batch_keep_until(session, batch, items).isoformat()}


def _detail_response(request: Request, session: Session, item: TravelItem,
                     *, bucket: str = "attention", saved: bool = False,
                     oob_counts: bool = False, approve_refused: bool = False):
    view = _view(item)
    violations = []
    if item.extraction:
        from app.resolving import load_enablement
        violations = [str(v) for v in
                      rules_module.check_item(_rule_shape(item),
                                              load_enablement(session))]
    ctx = {"i": view, "violations": violations, "saved": saved, "bucket": bucket,
           "approve_refused": approve_refused,
           "pax": _passenger_context(session, view["payload"]),
           "who": _who_context(session, item, view["payload"]),
           # Two independent trip pickers (2.1.2): the AdventureLog collection the
           # item files into (drives the commit; only when AL is configured), and
           # the in-house Yonder trip it belongs to (association only). Either can
           # be absent and the template just omits that picker.
           "trip_al": _trip_context(session, item, view, scope.user_of(request)),
           "trip_yonder": _yonder_trip_context(session, item, view,
                                               scope.user_of(request)),
           "original": _original_context(session, item)}
    if oob_counts:
        # Saving can move the item between buckets; refresh the counts too.
        views = _all_views(session, scope.user_of(request))
        ctx.update({"counts": _counts(views), "oob": True})
        return templates.TemplateResponse(request, "_after_save.html", ctx)
    return templates.TemplateResponse(request, "detail.html", ctx)


def _process_toast(summary: dict, resolved: dict, error: str | None) -> str:
    """A one-line, human-readable result for the process button."""
    if error:
        return f"Processing failed — {error}"
    extracted = summary.get("extracted", 0)
    unreadable = summary.get("needs_review", 0)
    if not extracted and not unreadable and not resolved:
        return "No new captures were waiting."
    parts = []
    if extracted:
        parts.append(f"{extracted} extracted")
    if unreadable:
        parts.append(f"{unreadable} couldn’t be read")
    if resolved:
        parts.append(", ".join(f"{v} {k.replace('_', ' ')}"
                               for k, v in sorted(resolved.items())))
    return "Done — " + "; ".join(parts) + "."


def _run_extract_resolve(session: Session, progress=None, user=None
                         ) -> tuple[dict, dict, str | None]:
    """Run process_pending (extract) then resolve_pending — the SAME code the
    CLI runs, so the button and the batch path cannot diverge. `progress` is
    threaded through both stages so the job panel shows a live count.

    `user` scopes the run to the captures that person may see — their own and
    open ones (an admin: + users who share), the same rule as the queue and
    commit (YONDER-105/113). Nobody's button processes someone else's email."""
    batch_filter = item_filter = None
    if not scope.unrestricted(user):
        batch_filter, item_filter = scope.batch_filter(user), scope.capture_filter(user)
    from app.config import settings
    summary: dict = {"extracted": 0, "needs_review": 0}
    resolved: dict = {}
    try:
        from app.processing import process_pending
        from extract.llm import OllamaExtractor
        llm = OllamaExtractor(settings.ollama_url, settings.ollama_model,
                              settings.llm_timeout_seconds)
        summary = process_pending(session, llm, progress=progress,
                                  batch_filter=batch_filter)

        from app.integrations import ClientCache
        from app.resolving import resolve_pending
        geocoder = None
        try:
            from resolve.geocode import PhotonGeocoder
            geocoder = PhotonGeocoder(settings.photon_url)
        except Exception:                       # geocoding is optional
            geocoder = None
        # Each capture resolves with its owner's connections (D-150).
        resolved = resolve_pending(session, geocoder=geocoder, progress=progress,
                                   item_filter=item_filter,
                                   clients_for=ClientCache(session))
    except Exception as ex:
        log.exception("extract/resolve run failed")
        return summary, resolved, str(ex)
    return summary, resolved, None


def _job_panel(request: Request, job: dict, bucket: str):
    """The live, self-polling job card (or, when finished, the done card + an
    out-of-band refresh of the queue, counts and rail). Someone else's job is
    never shown — only that one is running (its progress names their items)."""
    b = bucket if bucket in BUCKETS else "attention"
    if not jobs.visible_to(job, scope.user_of(request)):
        return HTMLResponse('<div class="toast">Another person\'s job is running — '
                            'try again in a moment.</div>')
    if not job["finished"]:
        return templates.TemplateResponse(request, "_job_panel.html", {
            "job": job, "eta": jobs.eta_text(job), "bucket": b})
    return _job_done(request, job, b)


def _job_done(request: Request, job: dict, bucket: str):
    from app.db import SessionLocal as _SL
    with _SL() as s:                            # fresh session: the worker committed
        views = _all_views(s, scope.user_of(request))
    shown = views if bucket == "all" else [v for v in views if v["bucket"] == bucket]
    return templates.TemplateResponse(request, "_job_done.html", {
        "job": job, "items": shown, "counts": _counts(views), "bucket": bucket,
        "oob": True, "toast": job["toast"], "toast_error": job["toast_error"]})


@router.post("/process", response_class=HTMLResponse)
def process_captures(request: Request, bucket: str = "attention",
                     session: Session = Depends(get_session)):
    """Advance received captures through extract + resolve on demand (D-40).

    A captured email lands as a `received` ingest batch and sits until
    something runs the pipeline; until the scheduled worker exists (deferred by
    the owner — button first), this is that something. resolve_pending(redo=False)
    only touches `pending` items, so items already in review — or a human's
    decisions — are never disturbed. (To RE-run captures already processed under
    an older extractor/resolver, use Reprocess.)
    """
    if jobs.current_running():
        return _job_panel(request, jobs.current_running(), bucket)

    user = scope.snapshot(scope.user_of(request))      # the job outlives the request

    def work(progress):
        with SessionLocal() as s:
            summary, resolved, error = _run_extract_resolve(s, progress, user=user)
            return _process_toast(summary, resolved, error), error

    return _job_panel(request, jobs.get(jobs.start("process", work, owner=_owner_id(request))), bucket)


@router.post("/reprocess", response_class=HTMLResponse)
def reprocess(request: Request, bucket: str = "attention",
              session: Session = Depends(get_session)):
    """Re-EXTRACT + re-resolve every item not yet LOGGED, applying the current
    extractor and resolver to captures processed under an older version — the
    "Process new captures" button only touches NEW (received) batches, so a code
    fix like the airport-code grammar change never reaches already-processed
    emails otherwise.

    Destructive by design: the non-logged items are discarded and rebuilt from
    the original stored emails (D-15), so edits/approvals on them are redone. A
    batch with ANY logged item is left entirely alone — re-extracting it would
    duplicate a record already written to a target. Runs as a background job so
    the reset + full LLM pass reports live progress instead of dead air.

    Only the caller's captures (own + open; an admin also users who share —
    the same rule as Process and Commit) are reset and rebuilt: one person's
    button never discards another's edits or approvals (YONDER-139).
    """
    if jobs.current_running():
        return _job_panel(request, jobs.current_running(), bucket)
    user = scope.snapshot(scope.user_of(request))      # the job outlives the request

    def work(progress):
        from app.models import CommitAttempt, IngestBatch, ItemKey
        with SessionLocal() as s:
            progress("resetting", 0, 0, "clearing processed items")
            item_q, batch_q = s.query(TravelItem), s.query(IngestBatch)
            if not scope.unrestricted(user):
                item_q = item_q.filter(scope.capture_filter(user))
                batch_q = batch_q.filter(scope.batch_filter(user))
            items = item_q.all()
            protected = {i.batch_id for i in items if _is_logged(i)}
            # Also protect batches whose items already exist in AirTrail/
            # AdventureLog even without a DB commit record (D-60): re-extracting
            # then re-logging them would duplicate. Checked in each capture
            # owner's own accounts (D-150). Best-effort — if the targets can't be
            # reached, fall back to the DB-only protection above.
            from app.integrations import ClientCache
            clients_for = ClientCache(s)
            flights_by: dict = {}
            al_cache: dict = {}

            def present(i) -> bool:
                c = clients_for(i.owner_id)
                if c.airtrail is not None and id(c) not in flights_by:
                    try:
                        flights_by[id(c)] = c.airtrail.list_flights()
                    except Exception:
                        flights_by[id(c)] = []
                return _externally_present(i, c.airtrail, c.adventurelog,
                                           al_cache.setdefault(id(c), {}),
                                           flights_by.get(id(c), []))
            protected |= {i.batch_id for i in items
                          if i.batch_id not in protected and present(i)}
            # D-196: a capture whose original was removed by retention can't be
            # extracted again — rebuilding it would only lose what it has.
            from app import retention
            protected |= {b.id for b in batch_q.all()
                          if retention.source_gone(s, b.payload)}
            doomed = [i.id for i in items
                      if not _is_logged(i) and i.batch_id not in protected]
            if doomed:
                s.query(CommitAttempt).filter(
                    CommitAttempt.item_id.in_(doomed)).delete(synchronize_session=False)
                s.query(ItemKey).filter(
                    ItemKey.item_id.in_(doomed)).delete(synchronize_session=False)
                s.query(TravelItem).filter(
                    TravelItem.id.in_(doomed)).delete(synchronize_session=False)
            reset = 0
            for b in batch_q.all():
                if b.id not in protected and b.status != "received":
                    b.status = "received"
                    reset += 1
            s.commit()
            summary, resolved, error = _run_extract_resolve(s, progress, user=user)
            toast = (f"Reprocessing failed — {error}" if error else
                     f"Reprocessed {reset} batch(es): "
                     + _process_toast(summary, resolved, None).removeprefix("Done — "))
            return toast, error

    return _job_panel(request, jobs.get(jobs.start("reprocess", work, owner=_owner_id(request))), bucket)


def _owner_id(request) -> str | None:
    user = scope.user_of(request)
    return user.id if user is not None else None


@router.post("/commit", response_class=HTMLResponse)
def commit(request: Request, confirm: str = Form(""), bucket: str = Form("staged"),
           session: Session = Depends(get_session)):
    """Log approved items to AirTrail + AdventureLog (D-44).

    Two-step by design: the write is irreversible and reaches two external
    systems, so the first call PREVIEWS (dry-run — builds the exact bodies,
    resolves airport/airline ids, sends nothing) and the operator sees precisely
    what will happen; confirm=1 EXECUTES. This is the same dry-run-by-default the
    CLI enforces and the same approve-then-log split as D-31.
    """
    from app.committing import commit_pending
    from app.resolving import load_identities

    execute = confirm == "1"
    b = bucket if bucket in BUCKETS else "staged"
    user = scope.user_of(request)
    # Each capture logs to its OWNER's connections (D-150); an owner with none
    # gets Run·dere only, the AirTrail/AdventureLog targets skipped.
    # Only the captures this person may log — their own (an admin also
    # ownerless ones and those of users who share). Never someone else's approvals (YONDER-105).
    mine = scope.capture_filter(scope.user_of(request))

    if not execute:
        # PREVIEW stays synchronous — it only reads (target refs + dedup) and is
        # quick; the operator needs the plan back immediately to decide.
        from app.integrations import ClientCache
        error = None
        summary = {"items": 0, "writes_ok": 0, "writes_failed": 0,
                   "skipped": 0, "plans": []}
        try:
            summary = commit_pending(session, identities=load_identities(session),
                                     dry_run=True, item_filter=mine,
                                     clients_for=ClientCache(session))
        except Exception as ex:
            log.exception("commit preview failed")
            error = str(ex)
        _label_plans(session, summary)
        return templates.TemplateResponse(request, "_commit_preview.html", {
            "summary": summary, "error": error, "bucket": b,
            "systems_text": _systems_text(summary)})

    # EXECUTE is the slow, irreversible write — run it as a background job.
    if jobs.current_running():
        return _job_panel(request, jobs.current_running(), b)

    def work(progress):
        from app.integrations import ClientCache
        with SessionLocal() as s:
            summary = commit_pending(s, identities=load_identities(s),
                                     dry_run=False, progress=progress,
                                     item_filter=mine, clients_for=ClientCache(s))
            n = summary["items"]
            parts = [f"logged {summary['writes_ok']} write(s) across {n} item(s)"]
            if summary["writes_failed"]:
                parts.append(f"{summary['writes_failed']} failed")
            if summary["skipped"]:
                parts.append(f"{summary['skipped']} already present")
            if summary.get("external_skipped"):
                parts.append(f"{summary['external_skipped']} AirTrail/AdventureLog "
                             "write(s) skipped (not connected)")
            toast = "Done — " + ", ".join(parts) + "."
            # Surface the exact reason each write failed, so the operator sees it
            # in the job panel instead of having to open the CommitAttempts log.
            details = [f"{plan['headline']} · {r['target']}: {r['error']}"
                       for plan in summary["plans"]
                       for r in plan["results"]
                       if not r.get("ok") and r.get("error")]
            err = None if not summary["writes_failed"] else "some writes failed"
            return toast, err, details

    return _job_panel(request, jobs.get(jobs.start("commit", work, owner=_owner_id(request))), b)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_status(job_id: str, request: Request, bucket: str = "attention",
               session: Session = Depends(get_session)):
    """The self-polling job panel reads this each tick. A missing job (a restart
    orphaned it) clears the panel; a finished one stops polling and refreshes the
    queue out-of-band."""
    job = jobs.get(job_id)
    if job is None:
        return HTMLResponse("")
    return _job_panel(request, job, bucket)


@router.get("/jobs/{job_id}/dismiss", response_class=HTMLResponse)
def job_dismiss(job_id: str):
    jobs.dismiss(job_id)
    return HTMLResponse("")


def _label_plans(session: Session, summary: dict) -> None:
    """Readable names for the preview: the item's own headline (not its
    canonical key) and, per write, the system and what it is — the raw target
    code stays only as a tooltip."""
    from app.models import Trip
    from app.review import summary as sm
    for plan in summary.get("plans") or []:
        item = session.get(TravelItem, plan.get("item_id")) if plan.get("item_id") else None
        payload = ((item.extraction or {}).get("payload") or {}) if item else {}
        plan["title"] = sm.headline(payload) if payload else plan.get("headline")
        trip = session.get(Trip, item.trip_id) if item is not None and item.trip_id else None
        for r in plan.get("results") or []:
            system, label = sm._TARGET_LABEL.get(r.get("target"), ("", r.get("target")))
            if r.get("target") == "yonder.activity":
                label = f"On {trip.name}" if trip is not None else "On your trip"
            r["system"], r["label"] = system, label


_SYSTEM_NAMES = (("yonder", "your Run·dere trips"), ("airtrail", "AirTrail"),
                 ("adventurelog", "AdventureLog"))


def _systems_text(summary: dict) -> str:
    """Where this run will actually write, in plain words — only the systems a
    planned write goes to, so nobody without AdventureLog reads about it (D-192)."""
    hit = {str(r.get("target", "")).split(".", 1)[0]
           for plan in summary.get("plans") or [] for r in plan.get("results") or []
           if not r.get("skipped")}
    names = [name for key, name in _SYSTEM_NAMES if key in hit] or ["your Run·dere trips"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


@router.get("/commit/cancel", response_class=HTMLResponse)
def commit_cancel():
    """Dismiss the commit panel (HTMX has no built-in 'clear', so swap empty)."""
    return HTMLResponse("")


@router.post("/item/{item_id}/{action}", response_class=HTMLResponse)
def act(item_id: str, action: str, request: Request, bucket: str = "attention",
        session: Session = Depends(get_session)):
    """approve | ignore — both STAGE a decision; neither writes to a target."""
    item = scope.capture_or_none(session, request, item_id)
    if item is None:
        return HTMLResponse("", status_code=404)

    from app.review import service
    if action == "approve":
        # Only a resolved item stages (an unresolved one is re-resolved first).
        # Not resolved (deferred / needs_review) — show the refreshed detail so
        # the operator sees why (needs a trip, a fix…).
        if not service.approve(session, item):
            return _detail_response(request, session, item, bucket=bucket,
                                    saved=False, oob_counts=True)
        bucket = bucket if bucket in BUCKETS else "attention"
        views = _all_views(session, scope.user_of(request))
        shown = views if bucket == "all" else [v for v in views if v["bucket"] == bucket]
        return templates.TemplateResponse(request, "_after_action.html", {
            "items": shown, "counts": _counts(views), "bucket": bucket, "oob": True,
        })

    if action == "unapprove":
        # A way back once approved-but-not-committed (owner, 2026-08-05). A
        # LOGGED item cannot be un-approved — the write already happened.
        if not service.unapprove(session, item):
            return _detail_response(request, session, item, bucket=bucket,
                                    saved=False, oob_counts=True)
        bucket = bucket if bucket in BUCKETS else "staged"
        views = _all_views(session, scope.user_of(request))
        shown = views if bucket == "all" else [v for v in views if v["bucket"] == bucket]
        return templates.TemplateResponse(request, "_after_action.html", {
            "items": shown, "counts": _counts(views), "bucket": bucket, "oob": True,
        })

    if action == "ignore":
        service.ignore(session, item)
    else:
        return HTMLResponse("", status_code=400)

    # Stay on the bucket the operator was viewing; the acted item has left it,
    # and the counts refresh out-of-band.
    bucket = bucket if bucket in BUCKETS else "attention"
    views = _all_views(session, scope.user_of(request))
    shown = views if bucket == "all" else [v for v in views if v["bucket"] == bucket]
    return templates.TemplateResponse(request, "_after_action.html", {
        "items": shown, "counts": _counts(views), "bucket": bucket, "oob": True,
    })
