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

from app import authn, scope
from app.db import get_session
from app.models import (DiscoveryFinding, PackingItem, PackingPlan, TravelItem,
                        TravelerIdentity, Trip, TripActivity, TripTraveler)

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


class ActivityOut(BaseModel):
    id: str
    type: str
    starts_at: str | None = None
    values: dict[str, Any] = {}


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
    title: str | None = None
    notes: str | None = None


class CaptureOut(BaseModel):
    batch_id: str
    status: str = "received"


class AssignTripIn(BaseModel):
    trip_id: str


# --------------------------------------------------------------------------- helpers
def _me(user=Depends(authn.current_user)):
    return user


def _trip_summary(session: Session, t: Trip) -> TripSummary:
    n = session.query(TripActivity).filter(TripActivity.trip_id == t.id).count()
    return TripSummary(id=t.id, name=t.name, start_date=t.start_date,
                       end_date=t.end_date, source=t.source, owner_id=t.owner_id,
                       activity_count=n)


def _snapshot(session: Session, trip: Trip, user) -> TripSnapshot:
    from app.trips.routes import _airport_resolver, _photon, trip_activities
    from discover.places import derive_locations

    acts = list(trip_activities(session, trip.id))
    activities = [ActivityOut(id=a.id, type=a.type, starts_at=a.starts_at,
                              values=a.values or {}) for a in acts]

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
    uid = user.id
    plan = (session.query(PackingPlan)
            .filter(PackingPlan.trip_id == trip.id, PackingPlan.user_id == uid)
            .order_by(PackingPlan.generated_at.desc()).first())
    packing = None
    if plan is not None:
        items = (session.query(PackingItem)
                 .filter(PackingItem.trip_id == trip.id, PackingItem.user_id == uid,
                         PackingItem.status != "removed").all())
        packing = PackingOut(
            generated_at=plan.generated_at, archetypes=plan.archetypes,
            warnings=plan.warnings, tasks=plan.tasks,
            items=[PackingItemOut(id=i.id, item_key=i.item_key, label=i.label,
                                  category=i.category, quantity=i.quantity, bag=i.bag,
                                  status=i.status, reason=i.reason) for i in items])

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
    from extract import maps_url
    url = (body.url or "").strip()
    if body.html:
        bid = _queue_web(session, body.html, source_url=url or None,
                         submitted_by=user.username, travel_date=body.travel_date,
                         title=body.title, notes=body.notes, owner_user_id=user.id)
    elif url and maps_url.looks_like_maps_url(url):
        if not body.travel_date:
            raise HTTPException(400, "a Google Maps directions link needs travel_date")
        bid = _queue_maps(session, url, submitted_by=user.username,
                          travel_date=body.travel_date, title=body.title,
                          notes=body.notes, owner_user_id=user.id)
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
