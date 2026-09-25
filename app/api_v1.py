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

from fastapi import APIRouter, Depends, HTTPException
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
    fields: list[ActivityFieldOut] = []  # labelled, non-empty, display order
    map_query: str | None = None        # place text to hand to a maps app
    map_url: str | None = None          # Google Maps search/directions link


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
    items: list[ReviewItemOut]


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


class ActivityPatch(BaseModel):
    type: str | None = None
    values: dict[str, Any] | None = None
    starts_at: str | None = None


class PackingItemPatch(BaseModel):
    status: str | None = None            # draft | packed | removed (check-off)
    quantity: int | None = None
    bag: str | None = None
    label: str | None = None


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


def _activity_out(a: TripActivity) -> ActivityOut:
    from app.trips import types as activity_types

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
        map_url=activity_types.map_url(a.type, v))


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
    activities = [_activity_out(a) for a in acts]

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

    # discovery findings (trip-wide/joint)
    discovery = [FindingOut(id=f.id, topic=f.topic, place_key=f.place_key,
                            body_md=f.body_md, data=f.data, confidence=f.confidence,
                            status=f.status, sources=f.sources)
                 for f in session.query(DiscoveryFinding)
                 .filter(DiscoveryFinding.trip_id == trip.id).all()]

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
    from app.review.routes import _all_views, _counts
    views = _all_views(session, user)
    counts = _counts(views)
    shown = views if bucket == "all" else [v for v in views if v["bucket"] == bucket]
    return ReviewListOut(counts=counts, items=[_review_item(v) for v in shown])


@router.get("/review/{item_id}", response_model=ReviewItemOut)
def api_review_item(item_id: str, user=Depends(authn.current_user),
                    session: Session = Depends(get_session)):
    from app.review.routes import _view
    item = scope.require_capture(session, user, item_id)
    return _review_item(_view(item))


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
                         title=body.title, notes=body.notes, owner_user_id=user.id)
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
                          provider="apple" if is_apple else "google")
    elif _looks_like_url(url):
        html = _fetch_public_html(url)
        if not html:
            raise HTTPException(422, "couldn't fetch that URL — share the page's HTML instead")
        bid = _queue_web(session, html, source_url=url, submitted_by=user.username,
                         travel_date=body.travel_date, title=body.title,
                         notes=body.notes, owner_user_id=user.id)
    else:
        raise HTTPException(400, "provide a url or html")
    return CaptureOut(batch_id=bid, status="received")


@router.post("/review/{item_id}/assign-trip")
def api_assign_trip(item_id: str, body: AssignTripIn,
                    user=Depends(authn.current_user),
                    session: Session = Depends(get_session)):
    """Link a capture to one of the user's trips (requires edit access to the
    trip). Pins the link so the resolver won't override a human choice."""
    from sqlalchemy.orm.attributes import flag_modified
    item = scope.require_capture(session, user, item_id)
    trip = scope.require_trip(session, user, body.trip_id, edit=True)
    item.trip_id = trip.id
    item.review = {**(item.review or {}), "trip_pinned": True}
    flag_modified(item, "review")
    session.commit()
    return {"ok": True, "item_id": item.id, "trip_id": trip.id}


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
    values = dict(body.values or {})
    a = TripActivity(trip_id=trip.id, type=t.key, values=values,
                     starts_at=body.starts_at or values.get("when") or "")
    session.add(a)
    session.commit()
    session.refresh(a)
    return _activity_out(a)


@router.patch("/trips/{trip_id}/activities/{activity_id}", response_model=ActivityOut)
def api_update_activity(trip_id: str, activity_id: str, body: ActivityPatch,
                        user=Depends(authn.current_user), session: Session = Depends(get_session)):
    scope.require_trip(session, user, trip_id, edit=True)
    a = session.get(TripActivity, activity_id)
    if a is None or a.trip_id != trip_id:
        raise HTTPException(404, "activity not found")
    if body.type is not None:
        a.type = _activity_type_or_400(body.type).key
    if body.values is not None:
        a.values = dict(body.values)
    if body.starts_at is not None:
        a.starts_at = body.starts_at or ""
    elif body.values is not None:
        a.starts_at = (a.values or {}).get("when") or a.starts_at
    session.commit()
    session.refresh(a)
    return _activity_out(a)


@router.delete("/trips/{trip_id}/activities/{activity_id}", status_code=204)
def api_delete_activity(trip_id: str, activity_id: str,
                        user=Depends(authn.current_user), session: Session = Depends(get_session)):
    scope.require_trip(session, user, trip_id, edit=True)
    a = session.get(TripActivity, activity_id)
    if a is not None and a.trip_id == trip_id:
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
