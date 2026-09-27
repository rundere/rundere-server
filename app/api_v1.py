"""Yonder 4.0 Phase 3 — the JSON `/api/v1` surface (SPEC-4 Workstream A).

The first machine-readable API: it feeds the iOS app's read-only offline cache
and its online writes (share-to-app, review actions). Every route reuses the
existing service layer and `app/scope.py`, so the API and the HTMX UI never
disagree on what a user may see.

Auth: every route requires a bearer access token (`Depends(authn.current_user)`)
— independent of TX_AUTH_REQUIRED; the API is always authenticated. Blob-shaped
columns (activity `values`, finding `data`) are passed through as-is.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import authn, journal as journal_svc, scope
from app.db import get_session
from app.models import (DiscoveryFinding, JournalEntry, PackingItem, PackingPlan,
                        TravelItem, TravelerIdentity, Trip, TripActivity,
                        TripTraveler)

router = APIRouter(prefix="/api/v1")


# --------------------------------------------------------------------------- schemas
class TripSummary(BaseModel):
    id: str
    name: str
    start_date: str | None = None
    end_date: str | None = None
    source: str
    owner_id: str | None = None
    activity_count: int = 0


class PlaceOut(BaseModel):
    key: str | None = None
    label: str | None = None
    city: str | None = None
    country: str | None = None
    lat: float | None = None
    lon: float | None = None


class ActivityFieldOut(BaseModel):
    key: str
    label: str
    value: Any
    kind: str = "text"          # text | textarea | url | datetime-local


class ActivityOut(BaseModel):
    id: str
    type: str
    starts_at: str | None = None
    values: dict[str, Any] = {}
    # Rendered view of `values`, from the same activity-type registry the web
    # timeline uses — so clients show identical titles, labels and map links.
    type_label: str | None = None
    title: str | None = None
    subtitle: str | None = None
    ends_at: str | None = None
    start_label: str | None = None      # "Departure", "Check-in", …
    end_label: str | None = None        # "Arrival", "Check-out", …
    # 4.1: which of the trip's travellers this is for. EMPTY = everyone on the
    # trip (the default; ids are TravelerOut.id from the snapshot's roster).
    traveler_ids: list[int] = []
    fields: list[ActivityFieldOut] = []  # labelled, non-empty, display order
    map_query: str | None = None        # place text to hand to a maps app
    map_url: str | None = None          # the saved Google/Apple Maps link, else a Google search
    # Where it is: {"points": [{role, label, lat, lon}], "route": {polyline
    # (polyline6), distance_km, duration_minutes}} — null until located.
    geo: dict | None = None


class TravelerOut(BaseModel):
    id: int
    display_name: str
    role: str
    kind: str


class FindingOut(BaseModel):
    id: str
    topic: str
    place_key: str | None = None
    body_md: str | None = None
    data: Any = None
    confidence: str | None = None
    status: str
    sources: Any = None
    locked: bool = False        # approved/edited on a lockable topic: nobody overwrites it
    external: bool = False      # written by an external AI agent over MCP/API
    agent: str | None = None    # which one (its OAuth app / API token name)


class PackingItemOut(BaseModel):
    id: str
    item_key: str
    label: str
    category: str
    quantity: int
    bag: str | None = None
    status: str
    reason: str | None = None


class PackingOut(BaseModel):
    generated_at: datetime | None = None
    archetypes: Any = None
    warnings: Any = None
    tasks: Any = None
    items: list[PackingItemOut] = []


class CaptureLite(BaseModel):
    id: str
    kind: str
    headline: str | None = None
    created_at: datetime | None = None


class TripSnapshot(BaseModel):
    trip: TripSummary
    activities: list[ActivityOut] = []
    travelers: list[TravelerOut] = []
    places: list[PlaceOut] = []
    discovery: list[FindingOut] = []
    packing: PackingOut | None = None
    captures: list[CaptureLite] = []


class ReviewItemOut(BaseModel):
    id: str
    bucket: str
    kind: str | None = None
    headline: str | None = None
    subline: str | None = None
    reference: str | None = None
    note: str | None = None
    reason_codes: list[str] = []
    blocked: bool = False
    confidence: float | None = None
    created_at: datetime | None = None


class ReviewListOut(BaseModel):
    counts: dict[str, int]
    to_process: int = 0          # captured but not processed yet (the funnel's first stage)
    items: list[ReviewItemOut]


class ReviewFieldOut(BaseModel):
    """One editable field of a capture (same specs as the web detail pane)."""
    label: str
    path: str                    # JSON pointer; PATCH `fields` keys
    kind: str                    # text | datetime | select
    value: str = ""
    tz: str | None = None        # datetime: the place's zone (display only)
    precision: str | None = None  # datetime: date | datetime
    options: list[str] | None = None
    missing: bool = False        # required and empty — why it can't log yet
    editable: bool = True


class TravelerOptionOut(BaseModel):
    id: str                      # passengers: identity id (str); who: traveller id
    name: str
    kind: str | None = None
    selected: bool = False


class TripOptionOut(BaseModel):
    id: str
    name: str
    start_date: str | None = None
    end_date: str | None = None
    contains: bool = False       # the trip's dates contain the capture's
    selected: bool = False


class ReviewItemDetail(ReviewItemOut):
    """GET /review/{id}: everything the review screen shows and edits."""
    note: str | None = None
    action_hint: str | None = None
    stages: Any = None
    fields: list[ReviewFieldOut] = []
    locked_fields: list[str] = []            # human-corrected pointers (D-13)
    violations: list[str] = []
    writes: Any = None                       # what logging would write, per target
    collection_name: str | None = None
    trip_id: str | None = None
    # Flights: "Who flew" — chosen from Travellers, never free text.
    passengers: list[TravelerOptionOut] | None = None
    passenger_names_unmatched: list[str] = []
    # Others filed to a 2+ traveller trip: "Who is this for" (that trip's roster).
    who: list[TravelerOptionOut] | None = None
    trip_options: list[TripOptionOut] = []   # trips the caller can FILE into
    can_approve: bool = False
    can_unapprove: bool = False
    can_ignore: bool = False


class ReviewPatch(BaseModel):
    fields: dict[str, str | None] | None = None   # {path: value} — editable fields only
    passengers: list[str] | None = None           # flights: identity ids ([] clears)
    traveler_ids: list[int] | None = None         # others: that trip's traveller ids
    approve: bool = False                         # "Save & approve"


class ReviewActionOut(BaseModel):
    ok: bool
    message: str | None = None
    changed: list[str] = []
    item: ReviewItemDetail


class CommitIn(BaseModel):
    dry_run: bool = True


class JobOut(BaseModel):
    id: str
    kind: str
    phase: str | None = None
    done: int = 0
    total: int = 0
    current: str | None = None
    finished: bool = False
    error: str | None = None
    toast: str | None = None
    details: list[str] = []


class NextTripOut(BaseModel):
    id: str
    name: str
    start: str | None = None
    end: str | None = None
    in_progress: bool = False
    days_until: int | None = None


class DashboardOut(BaseModel):
    next_trip: NextTripOut | None = None
    counts: dict[str, int]
    capture: dict[str, int]
    trip_count: int
    upcoming_count: int


class CaptureIn(BaseModel):
    url: str | None = None
    html: str | None = None          # shared page HTML (bookmarklet-style)
    travel_date: str | None = None
    travel_time: str | None = None
    end_date: str | None = None
    timezone: str | None = None
    mode: str | None = None          # directions: car / transit / walk (SPEC-4 §3)
    collection_id: str | None = None
    trip_id: str | None = None       # a Yonder trip to file it under (pinned)
    title: str | None = None
    notes: str | None = None


class CaptureOut(BaseModel):
    batch_id: str
    status: str = "received"


class AssignTripIn(BaseModel):
    trip_id: str


class TripPatch(BaseModel):
    name: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    notes: str | None = None


class ActivityIn(BaseModel):
    type: str
    values: dict[str, Any] = {}
    starts_at: str | None = None
    traveler_ids: list[int] | None = None     # None/[] = everyone on the trip


class ActivityPatch(BaseModel):
    type: str | None = None
    values: dict[str, Any] | None = None
    starts_at: str | None = None
    traveler_ids: list[int] | None = None     # omit = unchanged, [] = everyone


class PackingItemPatch(BaseModel):
    status: str | None = None            # draft | packed | removed (check-off)
    quantity: int | None = None
    bag: str | None = None
    label: str | None = None


class ActivityTypeOut(BaseModel):
    key: str
    label: str
    fields: list[dict[str, Any]] = []     # {key, label, type, hint?} — the `values` keys


class SourceIn(BaseModel):
    title: str | None = None
    url: str


class FindingIn(BaseModel):
    """A finding from an external agent for the caller's Discover tab (YONDER-108)."""
    topic: str | None = None               # research (default) | food | events | safety | visa | plugs | packing
    title: str                             # short heading, e.g. "Hikes near Banff"
    body_md: str                           # markdown: **bold**, ### headings, - bullets, [text](url)
    place_key: str | None = None           # "CA" / "CA:Banff"; research may omit it (= whole trip)
    sources: list[SourceIn] = []


class PackingItemIn(BaseModel):
    label: str
    category: str = "activity"
    quantity: int = 1
    bag: str | None = None


# --------------------------------------------------------------------------- helpers
def _me(user=Depends(authn.current_user)):
    return user


def _trip_summary(session: Session, t: Trip) -> TripSummary:
    n = session.query(TripActivity).filter(TripActivity.trip_id == t.id).count()
    return TripSummary(id=t.id, name=t.name, start_date=t.start_date,
                       end_date=t.end_date, source=t.source, owner_id=t.owner_id,
                       activity_count=n)


def _activity_out(a: TripActivity, traveler_ids: list[int] | None = None) -> ActivityOut:
    from app.trips import types as activity_types
    from app.trips.geo import public_geo

    v = a.values or {}
    t = activity_types.get_type(a.type)
    start_label, end_label = activity_types.time_labels(a.type)
    return ActivityOut(
        id=a.id, type=a.type, starts_at=a.starts_at, values=v,
        type_label=t.label, title=t.title(v) or None, subtitle=t.subtitle(v) or None,
        ends_at=v.get("endWhen") or None,
        start_label=start_label, end_label=end_label,
        fields=[ActivityFieldOut(key=f["key"], label=f["label"], value=f["value"],
                                 kind=f["kind"])
                for f in activity_types.detail_fields(a.type, v)],
        map_query=activity_types.map_query(a.type, v),
        map_url=activity_types.map_url(a.type, v),
        geo=public_geo(a.geo),
        traveler_ids=traveler_ids or [])


def _set_activity_travelers(session: Session, a: TripActivity,
                            ids: list[int]) -> list[int]:
    from app.trips import who
    off_trip = set(ids) - set(who.roster_ids(session, a.trip_id))
    if off_trip:
        raise HTTPException(400, f"not travellers on this trip: {sorted(off_trip)}")
    return who.set_travelers(session, a.id, a.trip_id, ids)


def _packing_item_out(i: PackingItem) -> PackingItemOut:
    return PackingItemOut(id=i.id, item_key=i.item_key, label=i.label,
                          category=i.category, quantity=i.quantity, bag=i.bag,
                          status=i.status, reason=i.reason)


def _packing_out(session: Session, trip_id: str, uid: str) -> "PackingOut | None":
    plan = (session.query(PackingPlan)
            .filter(PackingPlan.trip_id == trip_id, PackingPlan.user_id == uid)
            .order_by(PackingPlan.generated_at.desc()).first())
    items = (session.query(PackingItem)
             .filter(PackingItem.trip_id == trip_id, PackingItem.user_id == uid,
                     PackingItem.status != "removed").all())
    # Surface packing when there's a plan OR any items (a user may add a custom
    # line before ever generating a plan).
    if plan is None and not items:
        return None
    return PackingOut(generated_at=plan.generated_at if plan else None,
                      archetypes=plan.archetypes if plan else None,
                      warnings=plan.warnings if plan else None,
                      tasks=plan.tasks if plan else None,
                      items=[_packing_item_out(i) for i in items])


def _snapshot(session: Session, trip: Trip, user) -> TripSnapshot:
    from app.trips.routes import _airport_resolver, _photon, trip_activities
    from discover.places import derive_locations

    acts = list(trip_activities(session, trip.id))
    from app.trips import who
    tags = who.activity_map(session, [a.id for a in acts])
    activities = [_activity_out(a, tags.get(a.id)) for a in acts]

    # travellers (+ resolved names)
    travelers = []
    for tt in session.query(TripTraveler).filter_by(trip_id=trip.id).all():
        ident = session.get(TravelerIdentity, tt.traveler_id)
        travelers.append(TravelerOut(id=tt.traveler_id,
                                     display_name=ident.display_name if ident else "—",
                                     role=tt.role, kind=ident.kind if ident else "guest"))

    # materialized places (lat/lon) — the map/offline source
    places = []
    try:
        derived = derive_locations(acts, resolve_airport=_airport_resolver(),
                                   geocoder=_photon())
        places = [PlaceOut(key=p.place_key, label=p.label, city=p.city,
                           country=p.country, lat=p.lat, lon=p.lon)
                  for p in derived.places]
    except Exception:                                   # never fail the snapshot on geocode
        places = []

    # discovery findings — PER-USER (4.1): the caller's own Discover tab
    discovery = [_finding_out(f)
                 for f in session.query(DiscoveryFinding)
                 .filter(DiscoveryFinding.trip_id == trip.id,
                         DiscoveryFinding.user_id == user.id).all()]

    # packing — PER-USER (D-123)
    packing = _packing_out(session, trip.id, user.id)

    # linked captures (light) — visible because the user can see this trip
    from app.review.summary import headline as _headline, kind_label as _kind
    captures = []
    for c in (session.query(TravelItem).filter(TravelItem.trip_id == trip.id)
              .order_by(TravelItem.created_at).all()):
        payload = (c.extraction or {}).get("payload") or {}
        captures.append(CaptureLite(id=c.id, kind=_kind(payload),
                                    headline=_headline(payload), created_at=c.created_at))

    return TripSnapshot(trip=_trip_summary(session, trip), activities=activities,
                        travelers=travelers, places=places, discovery=discovery,
                        packing=packing, captures=captures)


def _review_item(v: dict) -> ReviewItemOut:
    return ReviewItemOut(
        id=v["id"], bucket=v["bucket"], kind=v.get("kind"), headline=v.get("headline"),
        subline=v.get("subline"), reference=v.get("reference"), note=v.get("note"),
        reason_codes=v.get("reason_codes") or [], blocked=bool(v.get("blocked")),
        confidence=v.get("confidence"), created_at=v.get("created_at"))


# --------------------------------------------------------------------------- API contract (YONDER-58)
def _collect_refs(node, out: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            out.add(ref.rsplit("/", 1)[-1])
        for v in node.values():
            _collect_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_refs(v, out)


def api_openapi_spec() -> dict:
    """The OpenAPI document for /api/v1 only (the app's contract): the app's
    full schema filtered to /api/v1 paths, pruned to the schemas they use.
    `scripts/dto_drift.py` checks the iOS DTOs against it."""
    from app.config import settings
    from app.main import app
    full = app.openapi()
    paths = {p: v for p, v in full.get("paths", {}).items() if p.startswith("/api/v1")}
    schemas = full.get("components", {}).get("schemas", {})
    keep: set[str] = set()
    _collect_refs(paths, keep)
    pending = list(keep)
    while pending:
        found: set[str] = set()
        _collect_refs(schemas.get(pending.pop(), {}), found)
        for name in found - keep:
            keep.add(name)
            pending.append(name)
    return {"openapi": full.get("openapi", "3.1.0"),
            "info": {"title": "Run.Dere API", "version": settings.app_version,
                     "description": "Every route needs a bearer token (or a web session); "
                                    "all data is scoped to the signed-in user."},
            "paths": paths,
            "components": {"schemas": {k: schemas[k] for k in sorted(keep)},
                           "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
            "security": [{"bearer": []}]}


@router.get("/openapi.json", include_in_schema=False)
def api_openapi(user=Depends(authn.current_user)):
    """The /api/v1 contract — behind auth: servers aren't public even though
    the code is."""
    return api_openapi_spec()


@router.get("/docs", include_in_schema=False)
def api_docs(user=Depends(authn.current_user)):
    """Swagger UI for /api/v1 (behind auth; the page fetches the schema with
    your web session)."""
    from fastapi.openapi.docs import get_swagger_ui_html
    return get_swagger_ui_html(openapi_url="/api/v1/openapi.json", title="Run.Dere API")


# --------------------------------------------------------------------------- read
@router.get("/dashboard", response_model=DashboardOut)
def api_dashboard(user=Depends(authn.current_user), session: Session = Depends(get_session)):
    from datetime import date

    from app.dashboard import (_capture_activity, _next_trip, _pipeline_counts,
                               _trip_status)
    today = date.today().isoformat()
    trips = session.query(Trip).filter(scope.trip_filter(session, user)).all()
    nxt = _next_trip(trips, today)
    upcoming = sum(1 for t in trips
                   if t.start_date and _trip_status(t, today) == "upcoming")
    return DashboardOut(
        next_trip=NextTripOut(**nxt) if nxt else None,
        counts=_pipeline_counts(session, user),
        capture=_capture_activity(session, today, user),
        trip_count=len(trips), upcoming_count=upcoming)


@router.get("/trips", response_model=list[TripSummary])
def api_trips(user=Depends(authn.current_user), session: Session = Depends(get_session)):
    trips = (session.query(Trip).filter(scope.trip_filter(session, user))
             .order_by(Trip.start_date.desc()).all())
    return [_trip_summary(session, t) for t in trips]


@router.get("/trips/{trip_id}", response_model=TripSnapshot)
def api_trip_snapshot(trip_id: str, user=Depends(authn.current_user),
                      session: Session = Depends(get_session)):
    trip = scope.require_trip(session, user, trip_id)       # 404 if not visible
    return _snapshot(session, trip, user)


@router.get("/trips/{trip_id}/timeline.pdf",
            responses={200: {"content": {"application/pdf": {}}}})
def api_trip_timeline_pdf(trip_id: str, download: bool = False,
                          user=Depends(authn.current_user),
                          session: Session = Depends(get_session)):
    """The trip timeline as a shareable PDF itinerary — identical to the web
    export (server-rendered)."""
    trip = scope.require_trip(session, user, trip_id)       # 404 if not visible
    from app.trips import pdf
    return pdf.pdf_response(session, trip, download=download)


@router.get("/review", response_model=ReviewListOut)
def api_review(bucket: str = "all", user=Depends(authn.current_user),
               session: Session = Depends(get_session)):
    from app.review.routes import _all_views, _counts, _to_process
    views = _all_views(session, user)
    counts = _counts(views)
    shown = views if bucket == "all" else [v for v in views if v["bucket"] == bucket]
    return ReviewListOut(counts=counts, to_process=_to_process(session, user),
                         items=[_review_item(v) for v in shown])


def _review_detail(session: Session, item: TravelItem, user) -> ReviewItemDetail:
    import rules as rules_module
    from app.resolving import load_enablement
    from app.review.routes import (_fields, _is_logged, _passenger_context,
                                   _rule_shape, _view, _who_context)
    from resolve.targets import item_date_span
    v = _view(item)
    payload = v["payload"]
    violations = ([str(x) for x in rules_module.check_item(_rule_shape(item),
                                                          load_enablement(session))]
                  if item.extraction else [])
    pax = _passenger_context(session, payload)
    who_ctx = _who_context(session, item, payload)
    start, end = (item_date_span(payload) or (None, None))
    trip_opts = []
    for t in (session.query(Trip).filter(scope.filing_trip_filter(session, user))
              .order_by(Trip.start_date.desc()).all()):
        contains = bool(start and end and t.start_date and t.end_date
                        and t.start_date <= start and end <= t.end_date)
        trip_opts.append(TripOptionOut(id=t.id, name=t.name or "(unnamed trip)",
                                       start_date=t.start_date, end_date=t.end_date,
                                       contains=contains, selected=t.id == item.trip_id))
    trip_opts.sort(key=lambda o: 0 if o.contains else 1)
    review = item.review or {}
    ignored = review.get("action_taken") == "ignored"
    logged = _is_logged(item)
    base = _review_item(v).model_dump()
    return ReviewItemDetail(
        **base, action_hint=v.get("action_hint"), stages=v.get("stages"),
        fields=[ReviewFieldOut(**{k: f.get(k) for k in ReviewFieldOut.model_fields
                                  if k in f}) for f in _fields(payload)],
        locked_fields=v.get("locked_fields") or [], violations=violations,
        writes=v.get("writes"), collection_name=v.get("collection_name"),
        trip_id=item.trip_id,
        passengers=([TravelerOptionOut(id=str(i["id"]), name=i["name"], kind=i["kind"],
                                       selected=i["selected"]) for i in pax["identities"]]
                    if pax else None),
        passenger_names_unmatched=(pax or {}).get("unmatched") or [],
        who=([TravelerOptionOut(id=str(t["id"]), name=t["name"], selected=t["checked"])
              for t in who_ctx["travelers"]] if who_ctx else None),
        trip_options=trip_opts,
        can_approve=(not ignored and not logged and v["bucket"] != "staged"
                     and not v["blocked"]),
        can_unapprove=v["bucket"] == "staged" and not logged,
        can_ignore=not ignored and not logged)


@router.get("/review/{item_id}", response_model=ReviewItemDetail)
def api_review_item(item_id: str, user=Depends(authn.current_user),
                    session: Session = Depends(get_session)):
    """One capture with its editable fields, who-flew / who-is-this-for
    options, the trips it can be filed into, and what it would write."""
    item = scope.require_capture(session, user, item_id)
    return _review_detail(session, item, user)


def _action_out(session: Session, item_id: str, user, ok: bool, message: str | None = None,
                changed: list[str] | None = None) -> ReviewActionOut:
    item = scope.require_capture(session, user, item_id)       # re-read after re-resolve
    session.refresh(item)
    return ReviewActionOut(ok=ok, message=message, changed=changed or [],
                           item=_review_detail(session, item, user))


@router.patch("/review/{item_id}", response_model=ReviewActionOut)
def api_review_edit(item_id: str, body: ReviewPatch, user=Depends(authn.current_user),
                    session: Session = Depends(get_session)):
    """Correct a capture: `fields` {path: value} (paths from GET's `fields`;
    each change becomes a field lock), `passengers` (flights, identity ids),
    `traveler_ids` (others, from GET's `who`). Re-resolves on change.
    `approve: true` then approves it if it now resolves ("Save & approve")."""
    from app.review import service
    item = scope.require_capture(session, user, item_id)
    try:
        changed = service.apply_edits(session, item, fields=body.fields,
                                      passenger_ids=body.passengers,
                                      traveler_ids=body.traveler_ids,
                                      strict=True, resolved_by="api")
    except service.ReviewError as e:
        raise HTTPException(400, str(e))
    ok, message = True, None
    if body.approve:
        session.refresh(item)
        ok = service.approve(session, item, resolved_by="api")
        message = None if ok else "saved, but it can't be approved yet — see note and reason_codes"
    return _action_out(session, item_id, user, ok, message, changed)


@router.post("/review/{item_id}/approve", response_model=ReviewActionOut)
def api_review_approve(item_id: str, user=Depends(authn.current_user),
                       session: Session = Depends(get_session)):
    """Stage a capture for logging (nothing is written yet — see /review/commit).
    `ok: false` when it doesn't resolve yet; the item says why."""
    from app.review import service
    item = scope.require_capture(session, user, item_id)
    ok = service.approve(session, item, resolved_by="api")
    return _action_out(session, item_id, user, ok,
                       None if ok else "can't be approved yet — see note and reason_codes")


@router.post("/review/{item_id}/unapprove", response_model=ReviewActionOut)
def api_review_unapprove(item_id: str, user=Depends(authn.current_user),
                         session: Session = Depends(get_session)):
    """Take back an approval that hasn't been logged yet."""
    from app.review import service
    item = scope.require_capture(session, user, item_id)
    ok = service.unapprove(session, item)
    return _action_out(session, item_id, user, ok,
                       None if ok else "already logged — it can't be un-approved")


@router.post("/review/{item_id}/ignore", response_model=ReviewActionOut)
def api_review_ignore(item_id: str, user=Depends(authn.current_user),
                      session: Session = Depends(get_session)):
    """Take a capture off the pipeline; it writes nothing."""
    from app.review import service
    from app.review.routes import _is_logged
    item = scope.require_capture(session, user, item_id)
    if _is_logged(item):
        return _action_out(session, item_id, user, False, "already logged")
    service.ignore(session, item, resolved_by="api")
    return _action_out(session, item_id, user, True)


# --------------------------------------------------------------------------- review: process + commit jobs
def _job_out(job: dict) -> JobOut:
    return JobOut(**{k: job.get(k) for k in JobOut.model_fields if job.get(k) is not None})


def _start_or_busy(user, kind: str, work):
    from app.review import jobs
    running = jobs.current_running()
    if running:
        if jobs.visible_to(running, user):
            return _job_out(running)                  # your own job: report it
        raise HTTPException(409, "another person's job is running — try again shortly")
    return _job_out(jobs.get(jobs.start(kind, work, owner=user.id)))


@router.post("/review/process", response_model=JobOut, status_code=202)
def api_review_process(user=Depends(authn.current_user)):
    """Extract + resolve the caller's captures waiting in the pipeline (the web
    "Process new captures"): their own and open ones — an admin's run also
    covers users who share. Returns a job to poll at GET /api/v1/jobs/{id}."""
    from app.db import SessionLocal
    from app.review.routes import _process_toast, _run_extract_resolve

    mine = scope.snapshot(user)                         # the job outlives the request

    def work(progress):
        with SessionLocal() as s:
            # only the caller's own (and open) captures — YONDER-113
            summary, resolved, error = _run_extract_resolve(s, progress, user=mine)
            return _process_toast(summary, resolved, error), error

    return _start_or_busy(user, "process", work)


@router.post("/review/commit")
def api_review_commit(body: CommitIn, user=Depends(authn.current_user),
                      session: Session = Depends(get_session)):
    """Log the caller's approved captures to AirTrail + AdventureLog — only
    their own (an admin: + users who share), never anyone else's.
    `dry_run: true` (default) returns the exact plan and writes nothing;
    `dry_run: false` starts the irreversible write as a job (202 + JobOut)."""
    from fastapi.responses import JSONResponse

    from app.committing import commit_pending
    from app.db import SessionLocal
    from app.resolving import load_identities
    from app.review.routes import _target_clients
    mine = scope.capture_filter(user)
    if body.dry_run:
        adventurelog, airtrail = _target_clients()
        summary = commit_pending(session, adventurelog, airtrail,
                                 identities=load_identities(session), dry_run=True,
                                 item_filter=mine)
        return {"dry_run": True, "summary": summary}

    def work(progress):
        with SessionLocal() as s:
            adventurelog, airtrail = _target_clients()
            summary = commit_pending(s, adventurelog, airtrail,
                                     identities=load_identities(s), dry_run=False,
                                     progress=progress, item_filter=mine)
            parts = [f"logged {summary['writes_ok']} write(s) across {summary['items']} item(s)"]
            if summary["writes_failed"]:
                parts.append(f"{summary['writes_failed']} failed")
            if summary["skipped"]:
                parts.append(f"{summary['skipped']} already present")
            details = [f"{plan['headline']} · {r['target']}: {r['error']}"
                       for plan in summary["plans"] for r in plan["results"]
                       if not r.get("ok") and r.get("error")]
            return ("Done — " + ", ".join(parts) + ".",
                    "some writes failed" if summary["writes_failed"] else None, details)

    job = _start_or_busy(user, "commit", work)
    return JSONResponse(job.model_dump(), status_code=202)


@router.get("/jobs/{job_id}", response_model=JobOut)
def api_job(job_id: str, user=Depends(authn.current_user)):
    """A process/commit job's progress. Only its starter can see it."""
    from app.review import jobs
    job = jobs.get(job_id)
    if not jobs.visible_to(job, user):
        raise HTTPException(404, "job not found")
    return _job_out(job)


# --------------------------------------------------------------------------- write
@router.post("/capture", response_model=CaptureOut, status_code=202)
def api_capture(body: CaptureIn, user=Depends(authn.current_user),
                session: Session = Depends(get_session)):
    """Share-to-app: a Google Maps directions URL, a webpage URL, or shared page
    HTML. Owned by the caller (D-120)."""
    from app.capture import _fetch_public_html, _looks_like_url, _queue_maps, _queue_web
    from extract import apple_maps_url, maps_url
    url = (body.url or "").strip()
    is_apple = bool(url) and apple_maps_url.looks_like_apple_maps_url(url)
    if body.html:
        bid = _queue_web(session, body.html, source_url=url or None,
                         submitted_by=user.username, travel_date=body.travel_date,
                         title=body.title, notes=body.notes, owner_user_id=user.id,
                         trip_id=body.trip_id)
    elif url and apple_maps_url.is_apple_short_link(url):
        raise HTTPException(
            422, "Apple Maps short link (maps.apple/p/…) carries no coordinates; "
                 "share the full maps.apple.com directions link")
    elif is_apple or (url and maps_url.looks_like_maps_url(url)):
        if not body.travel_date:
            raise HTTPException(400, "a maps directions link needs travel_date")
        bid = _queue_maps(session, url, submitted_by=user.username,
                          travel_date=body.travel_date, travel_time=body.travel_time,
                          end_date=body.end_date, timezone=body.timezone, mode=body.mode,
                          title=body.title, notes=body.notes,
                          collection_id=body.collection_id, owner_user_id=user.id,
                          provider="apple" if is_apple else "google", trip_id=body.trip_id)
    elif _looks_like_url(url):
        html = _fetch_public_html(url)
        if not html:
            raise HTTPException(422, "couldn't fetch that URL — share the page's HTML instead")
        bid = _queue_web(session, html, source_url=url, submitted_by=user.username,
                         travel_date=body.travel_date, title=body.title,
                         notes=body.notes, owner_user_id=user.id, trip_id=body.trip_id)
    else:
        raise HTTPException(400, "provide a url or html")
    return CaptureOut(batch_id=bid, status="received")


def assign_capture_to_trip(session: Session, user, item_id: str, trip_id: str) -> dict:
    """File a capture into a trip under the FILING rule (4.1,
    `scope.filing_trip_filter`: own, open, or one you travel on) — the same rule
    as the web picker. Being an admin, or having a view-only share, is not
    enough: 404 if the trip isn't visible, 403 if visible but not fileable.
    Pins the link so the resolver won't override a human choice."""
    from sqlalchemy.orm.attributes import flag_modified
    item = scope.require_capture(session, user, item_id)
    trip = scope.require_trip(session, user, trip_id)        # 404 if not visible
    fileable = (session.query(Trip.id)
                .filter(Trip.id == trip.id, scope.filing_trip_filter(session, user))
                .first())
    if fileable is None:
        raise HTTPException(403, "you can only file captures into your own trips, "
                                 "open trips, or trips you travel on")
    item.trip_id = trip.id
    item.review = {**(item.review or {}), "trip_pinned": True}
    flag_modified(item, "review")
    session.commit()
    return {"ok": True, "item_id": item.id, "trip_id": trip.id}


@router.post("/review/{item_id}/assign-trip")
def api_assign_trip(item_id: str, body: AssignTripIn,
                    user=Depends(authn.current_user),
                    session: Session = Depends(get_session)):
    """Link a capture to one of the user's trips (filing rule, see
    assign_capture_to_trip)."""
    return assign_capture_to_trip(session, user, item_id, body.trip_id)


# --------------------------------------------------------------------------- write: manual trip edits (D-125)
# Line-item editing of a trip the user can EDIT (owner / traveller / admin) — the
# same hand-edits the web trip page allows, for "updates on the go". NOT the
# capture review/commit pipeline. require_trip(edit=True): 404 if not visible,
# 403 for a view-only share.
@router.patch("/trips/{trip_id}", response_model=TripSummary)
def api_update_trip(trip_id: str, body: TripPatch,
                    user=Depends(authn.current_user), session: Session = Depends(get_session)):
    trip = scope.require_trip(session, user, trip_id, edit=True)
    if body.name is not None and body.name.strip():
        trip.name = body.name.strip()[:200]
    if body.start_date is not None:
        trip.start_date = body.start_date or None
    if body.end_date is not None:
        trip.end_date = body.end_date or None
    if body.notes is not None:
        trip.notes = body.notes or None
    session.commit()
    session.refresh(trip)
    return _trip_summary(session, trip)


def _activity_type_or_400(atype: str):
    from app.trips import types as at
    t = at.get_type(atype)
    if t.key != atype:
        raise HTTPException(400, f"unknown activity type {atype!r}")
    return t


@router.post("/trips/{trip_id}/activities", response_model=ActivityOut, status_code=201)
def api_add_activity(trip_id: str, body: ActivityIn,
                     user=Depends(authn.current_user), session: Session = Depends(get_session)):
    trip = scope.require_trip(session, user, trip_id, edit=True)
    t = _activity_type_or_400(body.type)
    from app.trips.enrich import prepare_activity
    values, geo = prepare_activity(t.key, dict(body.values or {}),
                                   session=session, trip_id=trip.id)
    a = TripActivity(trip_id=trip.id, type=t.key, values=values, geo=geo,
                     starts_at=body.starts_at or values.get("when") or "")
    session.add(a)
    session.flush()
    tagged = _set_activity_travelers(session, a, body.traveler_ids or [])
    session.commit()
    session.refresh(a)
    return _activity_out(a, tagged)


@router.patch("/trips/{trip_id}/activities/{activity_id}", response_model=ActivityOut)
def api_update_activity(trip_id: str, activity_id: str, body: ActivityPatch,
                        user=Depends(authn.current_user), session: Session = Depends(get_session)):
    scope.require_trip(session, user, trip_id, edit=True)
    a = session.get(TripActivity, activity_id)
    if a is None or a.trip_id != trip_id:
        raise HTTPException(404, "activity not found")
    old_type = a.type
    if body.type is not None:
        a.type = _activity_type_or_400(body.type).key
    if body.values is not None or a.type != old_type:
        from app.trips.enrich import prepare_activity
        a.values, a.geo = prepare_activity(
            a.type, dict(body.values if body.values is not None else (a.values or {})),
            old_geo=a.geo if a.type == old_type else None,
            session=session, trip_id=trip_id, activity_id=a.id)
    if body.starts_at is not None:
        a.starts_at = body.starts_at or ""
    elif body.values is not None:
        a.starts_at = (a.values or {}).get("when") or a.starts_at
    from app.trips import who
    tagged = (_set_activity_travelers(session, a, body.traveler_ids)
              if body.traveler_ids is not None
              else who.activity_map(session, [a.id]).get(a.id, []))
    session.commit()
    session.refresh(a)
    return _activity_out(a, tagged)


@router.delete("/trips/{trip_id}/activities/{activity_id}", status_code=204)
def api_delete_activity(trip_id: str, activity_id: str,
                        user=Depends(authn.current_user), session: Session = Depends(get_session)):
    scope.require_trip(session, user, trip_id, edit=True)
    a = session.get(TripActivity, activity_id)
    if a is not None and a.trip_id == trip_id:
        from app.trips import who
        who.clear_activity(session, a.id)
        session.delete(a)
        session.commit()
    return None


@router.post("/trips/{trip_id}/packing/generate", response_model=PackingOut)
def api_generate_packing(trip_id: str, user=Depends(authn.current_user),
                         session: Session = Depends(get_session)):
    """Generate/regenerate the CALLER's own packing plan (per-user, D-123)."""
    trip = scope.require_trip(session, user, trip_id, edit=True)
    from app.resolving import load_home_profiles
    from app.trips.routes import _airport_resolver, _photon
    from planning.runner import generate_plan
    generate_plan(session, trip, resolve_airport=_airport_resolver(), geocoder=_photon(),
                  home_profiles=load_home_profiles(session, user.traveler_id),
                  user_id=user.id, traveler_id=user.traveler_id)
    return _packing_out(session, trip.id, user.id) or PackingOut(items=[])


@router.patch("/trips/{trip_id}/packing-items/{item_id}", response_model=PackingItemOut)
def api_update_packing_item(trip_id: str, item_id: str, body: PackingItemPatch,
                            user=Depends(authn.current_user), session: Session = Depends(get_session)):
    """Check off / edit ONE of the caller's own packing lines (the phone use case)."""
    scope.require_trip(session, user, trip_id, edit=True)
    i = session.get(PackingItem, item_id)
    if i is None or i.trip_id != trip_id or i.user_id != user.id:
        raise HTTPException(404, "packing item not found")
    if body.status is not None:
        if body.status not in ("draft", "packed", "removed"):
            raise HTTPException(400, "status must be draft | packed | removed")
        i.status = body.status
    if body.quantity is not None:
        i.quantity = max(1, min(99, body.quantity))
        i.edited = True
    if body.bag is not None:
        i.bag = body.bag
        i.edited = True
    if body.label is not None and body.label.strip():
        i.label = body.label.strip()[:128]
        i.edited = True
    session.commit()
    session.refresh(i)
    return _packing_item_out(i)


@router.post("/trips/{trip_id}/packing-items", response_model=PackingItemOut, status_code=201)
def api_add_packing_item(trip_id: str, body: PackingItemIn,
                         user=Depends(authn.current_user), session: Session = Depends(get_session)):
    """Add a custom line to the caller's own packing list."""
    import uuid as _uuid
    scope.require_trip(session, user, trip_id, edit=True)
    label = (body.label or "").strip()[:128]
    if not label:
        raise HTTPException(400, "label required")
    i = PackingItem(trip_id=trip_id, user_id=user.id,
                    item_key=f"custom:{_uuid.uuid4().hex}", label=label,
                    category=body.category or "activity",
                    quantity=max(1, min(99, body.quantity)), bag=body.bag,
                    source="custom", status="draft", edited=True)
    session.add(i)
    session.commit()
    session.refresh(i)
    return _packing_item_out(i)


@router.get("/activity-types", response_model=list[ActivityTypeOut])
def api_activity_types(user=Depends(authn.current_user)):
    """The timeline activity types and the `values` fields each one takes."""
    from app.trips import types as at
    return [ActivityTypeOut(key=t.key, label=t.label,
                            fields=[dict(f) for f in t.fields])
            for t in at.TRIP_ACTIVITY_TYPES.values()]


# --------------------------------------------------------------------------- Discover: saved research (YONDER-108)
RESEARCH_TOPIC = "research"


def _finding_out(f: DiscoveryFinding) -> FindingOut:
    from discover import policy
    return FindingOut(id=f.id, topic=f.topic, place_key=f.place_key, body_md=f.body_md,
                      data=f.data, confidence=f.confidence, status=f.status,
                      sources=f.sources, locked=policy.is_locked(f),
                      external=policy.is_external(f), agent=policy.agent_label(f))


def save_research_finding(session: Session, user, trip_id: str, body: FindingIn,
                          agent: str | None = None) -> FindingOut:
    """Save an external AI agent's finding onto the CALLER's Discover tab for a
    trip they can edit, keyed (trip, user, topic, place_key) — the same key the
    local Discover run uses, so an agent can update e.g. the Food finding for
    "CA:Banff". Rules (discover/policy.py): only EXTERNAL_TOPICS; a LOCKED
    finding (plugs/visa/language, approved or edited) is refused with 409 — the
    lock is absolute; anything else is superseded, the old version kept in
    `data["history"]`. The result is a draft marked `resolver="agent"` with
    `data["agent"]` = `agent` (the connecting credential's name, supplied by
    the caller — never by the agent's own input), which a local whole-trip run
    then never overwrites. Never touches anyone else's Discover."""
    from datetime import timezone

    from discover import policy

    trip = scope.require_trip(session, user, trip_id, edit=True)
    topic = (body.topic or RESEARCH_TOPIC).strip()
    if topic not in policy.EXTERNAL_TOPICS:
        raise HTTPException(400, f"topic must be one of {', '.join(policy.EXTERNAL_TOPICS)}")
    title = (body.title or "").strip()[:200]
    text = (body.body_md or "").strip()
    if not title or not text:
        raise HTTPException(400, "title and body_md are required")
    place_key = (body.place_key or "").strip()[:128]
    if not place_key:
        if topic != RESEARCH_TOPIC:
            raise HTTPException(400, "place_key is required for this topic (a country code "
                                     "like \"CA\" or a city key like \"CA:Banff\")")
        place_key = "trip"
    md = f"**{title}**\n\n{text}"[:100_000]
    sources = [{"title": (s.title or s.url)[:300], "url": s.url[:2000]}
               for s in body.sources[:50] if s.url.startswith(("http://", "https://"))]
    f = (session.query(DiscoveryFinding)
         .filter(DiscoveryFinding.trip_id == trip.id, DiscoveryFinding.user_id == user.id,
                 DiscoveryFinding.topic == topic,
                 DiscoveryFinding.place_key == place_key).first())
    history: list = []
    if f is None:
        f = DiscoveryFinding(trip_id=trip.id, user_id=user.id, topic=topic,
                             place_key=place_key)
        session.add(f)
    else:
        if policy.is_locked(f):
            raise HTTPException(409, f"the {topic} finding for {place_key!r} is locked "
                                     "on the Discover tab; the person must unlock it first")
        history = policy.history_with(f, md)
    f.scope = "trip" if place_key == "trip" else ("country" if ":" not in place_key else "city")
    f.status = "draft"
    f.body_md = md
    f.sources = sources
    f.resolver = policy.EXTERNAL_RESOLVER
    f.confidence = None
    f.data = {"title": title, "agent": (agent or policy.DEFAULT_AGENT)[:128],
              "history": history}
    f.generated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(f)
    return _finding_out(f)


@router.post("/trips/{trip_id}/findings", response_model=FindingOut, status_code=201)
def api_save_finding(trip_id: str, body: FindingIn, request: Request,
                     user=Depends(authn.current_user),
                     session: Session = Depends(get_session)):
    """Save an AI agent's research into the caller's own Discover tab. The
    agent is named after the bearer credential used (OAuth app / API token)."""
    from app import oauth
    authz = request.headers.get("authorization", "")
    token = authz[7:].strip() if authz[:7].lower() == "bearer " else ""
    return save_research_finding(session, user, trip_id, body,
                                 agent=oauth.credential_label(session, token))


# --------------------------------------------------------------------------- journal (SPEC-6)
class JournalOut(BaseModel):
    id: str
    entry_date: str
    captured_at: datetime | None = None
    trip_id: str | None = None
    body: str = ""
    values: dict[str, Any] = {}
    place_lat: float | None = None
    place_lon: float | None = None
    place_label: str | None = None
    source: str = "api"
    duplicated_from: str | None = None


class JournalCreate(BaseModel):
    entry_date: str | None = None          # day the entry is ABOUT; defaults to today
    captured_at: str | None = None         # device time context (ISO-8601); defaults to now
    body: str = ""
    trip_id: str | None = None             # optional attach at create; validated
    values: dict[str, Any] | None = None
    place_lat: float | None = None
    place_lon: float | None = None
    place_label: str | None = None
    source: str = "api"


class JournalPatch(BaseModel):
    # trip_id is a first-class edit: null DETACHES, a value re-attaches (D-133).
    entry_date: str | None = None
    body: str | None = None
    trip_id: str | None = None
    values: dict[str, Any] | None = None
    place_lat: float | None = None
    place_lon: float | None = None
    place_label: str | None = None
    attach_trip: bool = False              # set true to apply trip_id (incl. null=detach)


class JournalDuplicate(BaseModel):
    trip_id: str | None = None             # the copy's trip (null = unattached)


def _journal_out(e: JournalEntry) -> JournalOut:
    return JournalOut(
        id=e.id, entry_date=e.entry_date, captured_at=e.captured_at,
        trip_id=e.trip_id, body=e.body or "", values=e.values or {},
        place_lat=e.place_lat, place_lon=e.place_lon, place_label=e.place_label,
        source=e.source or "api", duplicated_from=e.duplicated_from)


@router.post("/journal", response_model=JournalOut, status_code=201)
def api_journal_create(body: JournalCreate, user=Depends(authn.current_user),
                       session: Session = Depends(get_session)):
    """Create one journal entry owned by the caller. entry_date + captured_at are
    the minimum stamps (defaulted here when omitted); geo is optional (D-142).
    Writes are stateless about connectivity — offline queuing is the mobile
    client's outbox (SPEC-6 D-135), not this endpoint."""
    try:
        e = journal_svc.create_entry(
            session, user, entry_date=body.entry_date, captured_at=body.captured_at,
            body=body.body, trip_id=body.trip_id, values=body.values,
            place_lat=body.place_lat, place_lon=body.place_lon,
            place_label=body.place_label, source=body.source)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _journal_out(e)


@router.get("/journal", response_model=list[JournalOut])
def api_journal_list(trip_id: str | None = None, date_from: str | None = None,
                     date_to: str | None = None, user=Depends(authn.current_user),
                     session: Session = Depends(get_session)):
    """The caller's own entries (own-only, D-134), newest day first. Optionally
    scoped to one trip or an entry_date range."""
    rows = journal_svc.list_entries(session, user, trip_id=trip_id,
                                    date_from=date_from, date_to=date_to)
    return [_journal_out(e) for e in rows]


@router.get("/journal/{entry_id}", response_model=JournalOut)
def api_journal_get(entry_id: str, user=Depends(authn.current_user),
                    session: Session = Depends(get_session)):
    return _journal_out(journal_svc.get_entry(session, user, entry_id))


@router.patch("/journal/{entry_id}", response_model=JournalOut)
def api_journal_update(entry_id: str, body: JournalPatch,
                       user=Depends(authn.current_user),
                       session: Session = Depends(get_session)):
    """Edit the caller's own entry. To change the trip link set attach_trip=true
    (trip_id null detaches, a value re-attaches) — this makes an explicit intent
    out of the nullable field so a normal patch never accidentally detaches."""
    kw: dict[str, Any] = {}
    if body.entry_date is not None:
        kw["entry_date"] = body.entry_date
    if body.body is not None:
        kw["body"] = body.body
    if body.values is not None:
        kw["values"] = body.values
    if body.place_lat is not None:
        kw["place_lat"] = body.place_lat
    if body.place_lon is not None:
        kw["place_lon"] = body.place_lon
    if body.place_label is not None:
        kw["place_label"] = body.place_label
    if body.attach_trip:
        kw["trip_id"] = body.trip_id
    try:
        e = journal_svc.update_entry(session, user, entry_id, **kw)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _journal_out(e)


@router.post("/journal/{entry_id}/duplicate", response_model=JournalOut,
             status_code=201)
def api_journal_duplicate(entry_id: str, body: JournalDuplicate,
                          user=Depends(authn.current_user),
                          session: Session = Depends(get_session)):
    """Fork an entry to apply the same note to another trip (D-133). The copy is
    independent; `duplicated_from` records the origin."""
    e = journal_svc.duplicate_entry(session, user, entry_id, trip_id=body.trip_id)
    return _journal_out(e)


@router.delete("/journal/{entry_id}", status_code=204)
def api_journal_delete(entry_id: str, user=Depends(authn.current_user),
                       session: Session = Depends(get_session)):
    journal_svc.delete_entry(session, user, entry_id)
    return None
