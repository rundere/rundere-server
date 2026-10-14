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

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request, Response,
                     UploadFile)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import (authn, expenses as expenses_svc, journal as journal_svc,
                 places as places_svc, scope)
from app.db import get_session
from app.models import (DiscoveryFinding, DiscoveryRun, JournalEntry, PackingItem,
                        PackingPlan, SavedPlace, TravelItem, TravelerIdentity, Trip,
                        TripActivity, TripTraveler, User)

router = APIRouter(prefix="/api/v1")
log = logging.getLogger("rundere.api")


# --------------------------------------------------------------------------- schemas
class TripSummary(BaseModel):
    id: str
    name: str
    start_date: str | None = None
    end_date: str | None = None
    source: str
    owner_id: str | None = None
    activity_count: int = 0
    # YONDER-177: the airport codes the trip's flights visit, in flight order, no
    # repeats ("LHR", "NRT", "KIX") — the app's Upcoming cards. Real 3-letter codes only.
    airports: list[str] = []
    # YONDER-239: the trip's free-text notes (the web trip modal's Notes), and
    # whether the caller may edit the trip — false for a view-only share (D-122),
    # so the app can hide Edit instead of meeting a 403.
    notes: str | None = None
    can_edit: bool = False
    # D-199 (YONDER-185): the caller's tier on the trip — owner | traveller |
    # admin (edit through admin sharing) | viewer (a view-only share). `can_edit`
    # stays for older app builds; it is true exactly when my_role isn't viewer.
    my_role: str = "viewer"


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
    # D-199: true when a Run·dere account is linked to this person (they can sign
    # in); false for a guest who is only on the roster.
    has_account: bool = False


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


class DiscoveryItemOut(BaseModel):
    """A place, event or activity inside a Food & Drink or Events & Activities
    finding (4.5, YONDER-147). `finding_id` ties it to its city's write-up."""
    id: str
    finding_id: str
    topic: str
    place_key: str
    kind: str
    name: str
    neighbourhood: str | None = None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    website: str | None = None
    what_to_get: str | None = None
    price_tier: int | None = None
    starts_on: str | None = None
    ends_on: str | None = None
    when_text: str | None = None
    venue: str | None = None
    details: str | None = None
    duration: str | None = None
    booking: bool | None = None
    sources: Any = None
    extra: Any = None
    origin: str = "local"
    agent: str | None = None
    hidden: bool = False
    hidden_reason: str | None = None        # not_my_thing | closed | been_there
    edited: bool = False
    activity_id: str | None = None


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


class TripRateOut(BaseModel):
    """One row of the trip's shared rate table (D-195): 1 `currency` = `rate`
    `quote` for `rate_date`; `as_of` is the day the feed published it."""
    currency: str
    quote: str
    rate_date: str
    rate: str
    as_of: str | None = None
    source: str                            # frankfurter | discover
    fetched_at: datetime | None = None


class DiscoveryPlaceOut(BaseModel):
    """One place Discovery will research on a trip, for the caller (YONDER-264).
    `is_home`: home, never researched. `home_country`: in the home country, so the
    local topics are skipped. `excluded`: the caller chose to skip it."""
    key: str
    label: str | None = None
    city: str | None = None
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    sources: list[str] = []
    is_home: bool = False
    home_country: bool = False
    excluded: bool = False


class DiscoveryPlacesOut(BaseModel):
    places: list[DiscoveryPlaceOut] = []
    countries: list[str] = []
    # Places named on the trip that couldn't be verified (shown, never guessed).
    dropped: list[str] = []


class DiscoveryPlaceIn(BaseModel):
    excluded: bool


class DiscoveryStatusOut(BaseModel):
    """The caller's OWN Discovery run on a trip (YONDER-238; runs are per person,
    4.1) — what the app's Run / Refresh button and progress bar poll."""
    running: bool = False
    phase: str | None = None
    done: int = 0
    total: int = 0
    pct: int = 0
    # D-157: an ended trip can't be researched; `ended_message` says why.
    ended: bool = False
    ended_message: str | None = None
    # The latest run: running | done | failed | cancelled, None = never run.
    # The error text stays server-side (it can carry provider detail).
    last_status: str | None = None
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    # What the latest run covered: None = the whole suite, else one topic
    # (and one place) — a targeted refresh.
    last_topic: str | None = None
    last_place_key: str | None = None


class DiscoverIn(BaseModel):
    """Optional: refresh one topic (weather, currency, food…), or one topic for
    one place (`place_key` as on the finding, e.g. "PT:Lisbon"), instead of
    the whole suite."""
    topic: str | None = None
    place_key: str | None = None


class TripSnapshot(BaseModel):
    trip: TripSummary
    activities: list[ActivityOut] = []
    travelers: list[TravelerOut] = []
    places: list[PlaceOut] = []
    discovery: list[FindingOut] = []
    discovery_items: list[DiscoveryItemOut] = []
    discovery_profile: Any = None          # the caller's own preferences (4.5)
    packing: PackingOut | None = None
    captures: list[CaptureLite] = []
    # The newest stored rate per currency into the trip's balance currency —
    # the app's calculator uses these over the Discover snapshot, no live call
    # (D-195). Older than today → offer "Update rate for today" (rates refresh).
    trip_rates: list[TripRateOut] = []


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


class MomentOut(BaseModel):
    """One moment on the viewer's timeline (YONDER-177, D-172): an activity's start
    or end. `at` is its local wall-clock time as stored ("YYYY-MM-DDTHH:MM", or a
    bare date); `label` is what it's called ("Check-in", "Drop-off", "Departure")."""
    edge: str                           # "start" | "end"
    at: str
    label: str
    trip_id: str
    trip_name: str = ""
    activity: ActivityOut


class DashboardOut(BaseModel):
    next_trip: NextTripOut | None = None
    # D-172: the soonest moment still ahead (a multi-day booking never holds it for
    # its whole span), and what's under way now as each item's end moment.
    next_activity: MomentOut | None = None
    in_progress: list[MomentOut] = []
    # YONDER-250 (SPEC-11 §6): the next moments within `window_h`, across every trip,
    # soonest first; the first is `next_activity`. The Apple Watch rolls forward
    # through it instead of rebuilding the list from one trip's cached snapshot.
    upcoming: list[MomentOut] = []
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


class ItemIn(BaseModel):
    """A place, event or activity an agent adds to a Food & Drink or Events &
    Activities finding (4.5, YONDER-148). Only `kind` and `name` are required."""
    kind: str                              # food: restaurant|cafe|bar|pub|brewery|distillery|winery|market|bakery|dessert|street_food; events: event|activity
    name: str
    neighbourhood: str | None = None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    website: str | None = None
    what_to_get: str | None = None         # food: the dish or drink to order there
    price_tier: int | None = None          # food: 1 (cheap) – 4 (splurge)
    starts_on: str | None = None           # event: YYYY-MM-DD or YYYY-MM-DDTHH:MM
    ends_on: str | None = None
    when_text: str | None = None           # free wording, e.g. "Fridays in October"
    venue: str | None = None
    details: str | None = None             # what it is / why go
    duration: str | None = None            # activity, e.g. "3 h"
    booking: bool | None = None            # needs booking ahead
    heads_up: bool = False                 # event that affects travel (strike, holiday closures)
    source_url: str | None = None
    sources: list[SourceIn] = []


class PhraseIn(BaseModel):
    phrase: str
    meaning: str = ""
    pronunciation: str | None = None
    note: str | None = None                # when/with whom to use it (register)


class PhrasesIn(BaseModel):
    """Local phrases & register for one city (topic `language_local`)."""
    register_summary: str = ""             # how formal/informal locals are, in a paragraph
    greetings: list[PhraseIn] = []
    expressions: list[PhraseIn] = []
    etiquette: list[str] = []


class AdvisoryIn(BaseModel):
    text: str
    source_url: str                        # every safety claim carries its source


class SafetyIn(BaseModel):
    summary: str = ""                      # brief, factual, non-alarmist
    advisories: list[AdvisoryIn] = []
    scams: list[str] = []


class TransportIn(BaseModel):
    summary: str = ""                      # getting around, in a paragraph
    public_transit: str = ""
    from_airport: str = ""
    ride_hailing: list[str] = []           # apps that actually work here
    apps: list[str] = []
    tips: list[str] = []


class SafetyTransportIn(BaseModel):
    """Safety & Transport for one city (topic `safety`, 4.5) — the same
    structure the local resolver stores, so it renders as headed subsections."""
    safety: SafetyIn | None = None
    transport: TransportIn | None = None


class FindingIn(BaseModel):
    """A finding from an external agent for the caller's Discover tab (YONDER-108).
    Food/events may carry `items` (4.5); language_local carries `phrases`."""
    topic: str | None = None               # research (default) | food | events | safety | visa | plugs | packing | language_local
    title: str                             # short heading, e.g. "Hikes near Banff"
    body_md: str = ""                      # markdown; for food/events the city summary + recommendation
    place_key: str | None = None           # "CA" / "CA:Banff"; research may omit it (= whole trip)
    sources: list[SourceIn] = []
    items: list[ItemIn] | None = None      # food/events rows (replace the list unless replace_items=False)
    replace_items: bool = True
    phrases: PhrasesIn | None = None       # language_local only
    safety_transport: SafetyTransportIn | None = None   # safety only (structured)


class PackingItemIn(BaseModel):
    label: str
    category: str = "activity"
    quantity: int = 1
    bag: str | None = None


# --------------------------------------------------------------------------- helpers
def _me(user=Depends(authn.current_user)):
    return user


def _trip_summary(session: Session, t: Trip, user) -> TripSummary:
    from app.trips.routes import trip_route_codes
    n = session.query(TripActivity).filter(TripActivity.trip_id == t.id).count()
    flights = session.query(TripActivity).filter(
        TripActivity.trip_id == t.id, TripActivity.type == "flight").all()
    return TripSummary(id=t.id, name=t.name, start_date=t.start_date,
                       end_date=t.end_date, source=t.source, owner_id=t.owner_id,
                       activity_count=n, airports=trip_route_codes(flights, limit=None),
                       notes=t.notes, can_edit=scope.can_edit_trip(session, user, t),
                       my_role=scope.trip_role(session, user, t) or "viewer")


def _traveler_ids_with_accounts(session: Session, traveler_ids: list[int]) -> set[int]:
    """Which of these roster people have a Run·dere account (User.traveler_id, D-112)."""
    if not traveler_ids:
        return set()
    return {row[0] for row in session.query(User.traveler_id)
            .filter(User.traveler_id.in_(traveler_ids)).all()}


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
    # D-199: a viewer gets the timeline and map only. Discover, packing, the
    # discovery profile and linked captures belong to the people travelling.
    viewer = scope.trip_role(session, user, trip) == "viewer"
    from app.trips import who
    tags = who.activity_map(session, [a.id for a in acts])
    activities = [_activity_out(a, tags.get(a.id)) for a in acts]

    # travellers (+ resolved names)
    travelers = []
    roster = session.query(TripTraveler).filter_by(trip_id=trip.id).all()
    with_login = _traveler_ids_with_accounts(session, [tt.traveler_id for tt in roster])
    for tt in roster:
        ident = session.get(TravelerIdentity, tt.traveler_id)
        travelers.append(TravelerOut(id=tt.traveler_id,
                                     display_name=ident.display_name if ident else "—",
                                     role=tt.role, kind=ident.kind if ident else "guest",
                                     has_account=tt.traveler_id in with_login))

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

    if viewer:
        return TripSnapshot(trip=_trip_summary(session, trip, user), activities=activities,
                            travelers=travelers, places=places)

    # discovery findings — PER-USER (4.1): the caller's own Discover tab
    discovery = [_finding_out(f)
                 for f in session.query(DiscoveryFinding)
                 .filter(DiscoveryFinding.trip_id == trip.id,
                         DiscoveryFinding.user_id == user.id).all()]

    from app.models import DiscoveryItem
    from discover.items import item_dict
    discovery_items = [DiscoveryItemOut(**item_dict(i))
                       for i in session.query(DiscoveryItem)
                       .filter(DiscoveryItem.trip_id == trip.id,
                               DiscoveryItem.user_id == user.id)
                       .order_by(DiscoveryItem.place_key, DiscoveryItem.topic,
                                 DiscoveryItem.kind, DiscoveryItem.name).all()]

    # the caller's own discovery profile, so an agent researches with it (4.5)
    discovery_profile = my_discovery_profile(session, user)

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

    from app import rates as rates_svc
    trip_rates = [_rate_out(r) for _, r in sorted(rates_svc.latest(session, trip).items())]
    return TripSnapshot(trip=_trip_summary(session, trip, user), activities=activities,
                        travelers=travelers, places=places, discovery=discovery,
                        discovery_items=discovery_items,
                        discovery_profile=discovery_profile, packing=packing,
                        captures=captures, trip_rates=trip_rates)


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
            "info": {"title": "Run·dere API", "version": settings.app_version,
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
    return get_swagger_ui_html(openapi_url="/api/v1/openapi.json", title="Run·dere API")


# --------------------------------------------------------------------------- read
@router.get("/dashboard", response_model=DashboardOut)
def api_dashboard(now: Annotated[str | None, Query(
                      pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$",
                      description="Your local wall-clock time, YYYY-MM-DDTHH:MM. Timeline "
                                  "times have no zone, so the app sends the device's time; "
                                  "the server's own clock is used when omitted.")] = None,
                  window_h: Annotated[int, Query(
                      ge=1, le=168, description="How far ahead `upcoming` looks, in hours.")] = 48,
                  limit: Annotated[int, Query(
                      ge=1, le=50, description="The most moments `upcoming` returns.")] = 12,
                  user=Depends(authn.current_user), session: Session = Depends(get_session)):
    from datetime import date

    from app.dashboard import (_capture_activity, _next_trip, _pipeline_counts,
                               _trip_status, viewer_activities)
    from app import clock
    from app.trips import nextup, who
    # The app's own clock when it sends one; else the caller's travel-profile
    # time zone; else the server's (D-172, D-187).
    now = now or nextup.now_key(clock.now_for(session, user))
    today = now[:10]
    trips = session.query(Trip).filter(scope.trip_filter(session, user)).all()
    nxt = _next_trip(trips, today)
    upcoming = sum(1 for t in trips
                   if t.start_date and _trip_status(t, today) == "upcoming")
    acts = viewer_activities(session, trips, now, user)
    moment, under_way = nextup.plan(acts, now)
    ahead = nextup.upcoming(acts, now, window_h=window_h, limit=limit)
    names = {t.id: t.name for t in trips}
    tags = who.activity_map(session, list({m.activity.id for m in [moment, *under_way, *ahead]
                                           if m}))

    def _moment(m) -> MomentOut:
        a = m.activity
        return MomentOut(edge=m.edge, at=m.at, label=nextup.label(m), trip_id=a.trip_id,
                         trip_name=names.get(a.trip_id, ""),
                         activity=_activity_out(a, tags.get(a.id)))

    return DashboardOut(
        next_trip=NextTripOut(**nxt) if nxt else None,
        next_activity=_moment(moment) if moment else None,
        in_progress=[_moment(m) for m in under_way],
        upcoming=[_moment(m) for m in ahead],
        counts=_pipeline_counts(session, user),
        capture=_capture_activity(session, today, user),
        trip_count=len(trips), upcoming_count=upcoming)


@router.get("/trips", response_model=list[TripSummary])
def api_trips(user=Depends(authn.current_user), session: Session = Depends(get_session)):
    trips = (session.query(Trip).filter(scope.trip_filter(session, user))
             .order_by(Trip.start_date.desc()).all())
    return [_trip_summary(session, t, user) for t in trips]


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
    "Process new captures"): their own — an admin's run also covers
    ownerless captures and users who share. Returns a job to poll at GET /api/v1/jobs/{id}."""
    from app.db import SessionLocal
    from app.review.routes import _process_toast, _run_extract_resolve

    mine = scope.snapshot(user)                         # the job outlives the request

    def work(progress):
        with SessionLocal() as s:
            # only the caller's own captures (admin: + ownerless/sharing) — YONDER-113
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
    from app.integrations import ClientCache
    mine = scope.capture_filter(user)
    # Each capture logs to its OWNER's connections (D-150); an owner with none
    # gets Run·dere only, the AirTrail/AdventureLog targets skipped.
    if body.dry_run:
        summary = commit_pending(session, identities=load_identities(session), dry_run=True,
                                 item_filter=mine, clients_for=ClientCache(session))
        return {"dry_run": True, "summary": summary}

    def work(progress):
        with SessionLocal() as s:
            summary = commit_pending(s, identities=load_identities(s), dry_run=False,
                                     progress=progress, item_filter=mine,
                                     clients_for=ClientCache(s))
            parts = [f"logged {summary['writes_ok']} write(s) across {summary['items']} item(s)"]
            if summary["writes_failed"]:
                parts.append(f"{summary['writes_failed']} failed")
            if summary["skipped"]:
                parts.append(f"{summary['skipped']} already present")
            if summary.get("external_skipped"):
                parts.append(f"{summary['external_skipped']} AirTrail/AdventureLog "
                             "write(s) skipped (not connected)")
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


# --------------------------------------------------------------------------- email forward (D-190)
class InboxAddressOut(BaseModel):
    enabled: bool                    # the server's mailbox is set up
    address: str | None = None       # your own forward-to address


@router.get("/capture/email-address", response_model=InboxAddressOut)
def api_inbox_address(user=Depends(authn.current_user), session: Session = Depends(get_session)):
    """Your own email-forward address (SPEC-9 D-162): forward a confirmation to
    it and it lands in your review queue. Made the first time it's asked for;
    `enabled: false` when the server's mailbox isn't set up."""
    from app import inbox
    return {"enabled": inbox.configured(), "address": inbox.user_address(session, user)}


@router.post("/capture/email-address/rotate", response_model=InboxAddressOut)
def api_inbox_rotate(user=Depends(authn.first_party_user),
                     session: Session = Depends(get_session)):
    """A new address; the old one stops working at once. First-party only: a
    connected app can't retire the address your forwarding rules use."""
    from app import inbox
    if not inbox.configured():
        raise HTTPException(409, "the email inbox isn't set up on this server")
    return {"enabled": True, "address": inbox.rotate(session, user)}


# --------------------------------------------------------------------------- write
# --------------------------------------------------------------------------- documents (YONDER-179, D-196)
# Captured files: PDF, .ics, .eml. Only the uploader ever lists or downloads
# a file (D-196); rules live in app/documents.py.
class DocumentOut(BaseModel):
    id: str
    filename: str
    content_type: str                      # application/pdf | text/calendar | message/rfc822
    size_bytes: int
    trip_id: str | None = None
    batch_id: str | None = None            # the capture it went into
    source: str
    scan_notes: str | None = None          # e.g. "has JavaScript (never run; text read only)"
    created_at: datetime | None = None
    removed_on: str | None = None          # when the stored copy is removed (D-196)
    purged: bool = False                   # the stored copy is already gone


class DocumentsOut(BaseModel):
    enabled: bool                          # file capture is on for this server
    reason: str | None = None              # why not, when it's off
    used_bytes: int = 0
    quota_bytes: int = 0
    max_file_bytes: int = 0
    documents: list[DocumentOut] = []


class FileCaptureOut(BaseModel):
    document: DocumentOut
    batch_id: str | None = None
    duplicate: bool = False                # you'd already captured this exact file


def _document_out(session: Session, d) -> DocumentOut:
    from app import documents
    return DocumentOut(id=d.id, filename=d.filename, content_type=d.content_type,
                       size_bytes=d.size_bytes, trip_id=d.trip_id, batch_id=d.batch_id,
                       source=d.source, scan_notes=d.scan_notes, created_at=d.created_at,
                       removed_on=documents.removal_date(session, d).isoformat(),
                       purged=d.purged_at is not None)


@router.get("/documents", response_model=DocumentsOut)
def api_documents(user=Depends(authn.current_user), session: Session = Depends(get_session)):
    """Your captured files, newest first, with how much of your quota they use.
    `enabled` says whether this server takes files at all."""
    from app import documents
    from app.config import settings
    from app.models import Document
    enabled, reason = documents.status(session)
    rows = (session.query(Document).filter(Document.owner_id == user.id)
            .order_by(Document.created_at.desc()).all())
    return DocumentsOut(enabled=enabled, reason=reason,
                        used_bytes=documents.used_bytes(session, user),
                        quota_bytes=documents.quota_bytes(user),
                        max_file_bytes=settings.document_max_mb * 1024 * 1024,
                        documents=[_document_out(session, d) for d in rows])


@router.post("/capture/file", response_model=FileCaptureOut, status_code=202)
def api_capture_file(file: UploadFile = File(...), trip_id: str | None = Form(None),
                     notes: str | None = Form(None), source: str = Form("api"),
                     user=Depends(authn.current_user),
                     session: Session = Depends(get_session)):
    """Capture a file — a booking PDF, a calendar .ics or a saved .eml — for
    iPad drag-and-drop, the Share Extension and the web (YONDER-179). It's
    virus-scanned and stored (D-196), then extracted like any capture. Your
    exact same file again returns the first one (`duplicate`), with no new
    capture. 503 when file capture is off or the scanner is down; 413 too big
    or over quota; 415 not a PDF/.ics/.eml; 422 unsafe."""
    trip_id = (trip_id or "").strip() or None
    if trip_id:
        scope.require_trip(session, user, trip_id, edit=True)
    return _capture_file(session, user, file, trip_id=trip_id, notes=notes,
                         source=(source or "api")[:16])


def _capture_file(session: Session, user, file, *, trip_id, notes, source) -> FileCaptureOut:
    from app import documents
    from app.config import settings
    data = file.file.read(settings.document_max_mb * 1024 * 1024 + 1)
    try:
        doc, created = documents.save(session, user, data, file.filename or "",
                                      source=source, trip_id=trip_id)
    except documents.Unavailable as err:
        raise HTTPException(503, str(err)) from None
    except documents.Refused as err:
        raise HTTPException(err.status, str(err)) from None
    batch_id = doc.batch_id
    if created:
        batch_id = documents.queue_capture(session, doc, user, notes=notes, via=source)
    session.commit()
    if created:
        from app.review import autoprocess
        try:
            autoprocess.kick(user)              # D-158: start processing now
        except Exception:
            log.exception("auto-process could not start; the batch stays received")
    return FileCaptureOut(document=_document_out(session, doc), batch_id=batch_id,
                          duplicate=not created)


@router.get("/documents/{doc_id}/download")
def api_document_download(doc_id: str, user=Depends(authn.current_user),
                          session: Session = Depends(get_session)):
    """The file itself — yours only, always as a download, never shown inline
    (a PDF's scripts must never run in the app; D-196). 410 once removed."""
    from app import documents
    doc = documents.own(session, user, doc_id)
    if doc is None:
        raise HTTPException(404, "file not found")
    data = documents.read(doc)
    if data is None:
        raise HTTPException(410, "this file's stored copy has been removed")
    return document_response(doc, data)


def document_response(doc, data: bytes) -> Response:
    from urllib.parse import quote
    name = doc.filename or "capture"
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "'")
    return Response(content=data, media_type=doc.content_type, headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; "
                               f"filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
        "Cache-Control": "private, no-store"})


@router.delete("/documents/{doc_id}", status_code=204)
def api_document_delete(doc_id: str, user=Depends(authn.current_user),
                        session: Session = Depends(get_session)):
    """Delete your file now. What was extracted from it stays."""
    from app import documents
    doc = documents.own(session, user, doc_id)
    if doc is None:
        raise HTTPException(404, "file not found")
    documents.delete(session, doc)
    session.commit()
    return None


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
        raise HTTPException(403, "you can only file captures into your own trips "
                                 "or trips you travel on")
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
    dates_before = (trip.start_date, trip.end_date)
    if body.name is not None and body.name.strip():
        trip.name = body.name.strip()[:200]
    if body.start_date is not None:
        trip.start_date = body.start_date or None
    if body.end_date is not None:
        trip.end_date = body.end_date or None
    if body.notes is not None:
        trip.notes = body.notes.strip() or None     # same as the web form (_save_trip_fields)
    session.commit()
    if (trip.start_date, trip.end_date) != dates_before:
        # Re-dated: it may now cover captures waiting for a trip (YONDER-222).
        from app.trips.routes import _recheck_waiting
        _recheck_waiting(session, trip, user)
    session.refresh(trip)
    return _trip_summary(session, trip, user)


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


class ItemToTimelineIn(BaseModel):
    """Add a Discover item to the timeline (4.5). The activity is built from the
    item (name, address, map link, website, what to get); give the time."""
    starts_at: str | None = None           # YYYY-MM-DDTHH:MM local; defaults to the event's date
    traveler_ids: list[int] | None = None  # None/[] = everyone on the trip
    notes: str | None = None               # replaces the notes built from the item


def add_item_to_timeline(session: Session, user, trip_id: str, item_id: str,
                         body: ItemToTimelineIn) -> ActivityOut:
    """Create the timeline activity a Discover item describes and link the two."""
    from app.models import DiscoveryItem
    from discover import items as discovery_items
    trip = scope.require_trip(session, user, trip_id, edit=True)
    item = session.get(DiscoveryItem, item_id)
    if item is None or item.trip_id != trip.id or item.user_id != user.id:
        raise HTTPException(404, "no such Discover item on this trip")
    if item.activity_id and session.get(TripActivity, item.activity_id) is not None:
        raise HTTPException(409, "this item is already on the timeline "
                                 f"(activity {item.activity_id})")
    atype, values = discovery_items.activity_for(item)
    if body.starts_at:
        values["when"] = body.starts_at
    if body.notes is not None:
        values["notes"] = body.notes
    t = _activity_type_or_400(atype)
    from app.trips.enrich import prepare_activity
    values, geo = prepare_activity(t.key, {k: v for k, v in values.items() if v},
                                   session=session, trip_id=trip.id)
    a = TripActivity(trip_id=trip.id, type=t.key, values=values, geo=geo,
                     starts_at=values.get("when") or "")
    session.add(a)
    session.flush()
    tagged = _set_activity_travelers(session, a, body.traveler_ids or [])
    item.activity_id = a.id
    session.commit()
    session.refresh(a)
    return _activity_out(a, tagged)


# --------------------------------------------------------------------------- Discovery runs (YONDER-238)
# The web Discover button's launch and progress, for the app. Same gate as the
# web: any trip the caller can SEE (a view-only sharer researches for
# themselves — findings are per person, 4.1), refused once the trip has ended.
def _discovery_status(session: Session, trip: Trip, user) -> DiscoveryStatusOut:
    from app.trips import discovery_jobs as dj
    from discover.runner import TRIP_ENDED_MESSAGE, trip_has_ended
    live = dj.running_run(session, trip.id, user.id)       # also settles an orphan
    last = (session.query(DiscoveryRun)
            .filter(DiscoveryRun.trip_id == trip.id, DiscoveryRun.user_id == user.id)
            .order_by(DiscoveryRun.started_at.desc()).first())
    ended = trip_has_ended(trip)
    out = DiscoveryStatusOut(ended=ended, ended_message=TRIP_ENDED_MESSAGE if ended else None)
    if last is not None:
        out.last_status = last.status
        out.last_started_at = last.started_at
        out.last_finished_at = last.finished_at
        out.last_topic = (last.params or {}).get("topic")
        out.last_place_key = (last.params or {}).get("place_key")
    if live is not None:
        out.running = True
        out.phase = live.phase or "starting"
        out.done, out.total = live.done or 0, live.total or 0
        out.pct = int(100 * out.done / out.total) if out.total else 0
    return out


@router.post("/trips/{trip_id}/discover", response_model=DiscoveryStatusOut,
             status_code=202)
def api_run_discovery(trip_id: str, body: DiscoverIn | None = None,
                      user=Depends(authn.current_user),
                      session: Session = Depends(get_session)):
    """Start the caller's Discovery run (in the background) and return its
    status to poll. No body: the whole suite. `topic` (+ `place_key`): just
    that — e.g. the weather, or the weather for one city — without redoing
    the rest. Already running: that run's status, no second run. An ended
    trip: 409 with the reason (D-157)."""
    from app.trips.routes import discovery_topics, start_discovery
    from discover.runner import TRIP_ENDED_MESSAGE
    # Edit access, like the web's Run button: a view-only share (D-122/D-199)
    # can't start runs on someone else's trip.
    trip = scope.require_trip(session, user, trip_id, edit=True)
    topic = ((body.topic if body else None) or "").strip() or None
    place_key = ((body.place_key if body else None) or "").strip() or None
    if place_key and not topic:
        raise HTTPException(400, "place_key needs a topic")
    if topic and topic not in discovery_topics():
        raise HTTPException(400, f"unknown topic {topic!r}")
    if not start_discovery(session, trip, user, topic=topic, place_key=place_key):
        raise HTTPException(status_code=409, detail=TRIP_ENDED_MESSAGE)
    return _discovery_status(session, trip, user)


def _discovery_places_out(session: Session, trip: Trip, user) -> DiscoveryPlacesOut:
    from app.trips.routes import discovery_places
    found = discovery_places(session, trip, user.id)
    return DiscoveryPlacesOut(
        places=[DiscoveryPlaceOut(**p) for p in found["places"]],
        countries=list(found["countries"] or []),
        dropped=[str(d.get("raw") if isinstance(d, dict) else d) for d in (found["dropped"] or [])])


@router.get("/trips/{trip_id}/discover/places", response_model=DiscoveryPlacesOut)
def api_discovery_places(trip_id: str, user=Depends(authn.current_user),
                         session: Session = Depends(get_session)):
    """The places the caller's Discovery would research on this trip (Refine
    Discovery, YONDER-264): derived from the trip's own activities, with home and
    the caller's own exclusions marked. Check this, exclude what you don't want,
    then start a run."""
    trip = scope.require_trip(session, user, trip_id, edit=True)
    return _discovery_places_out(session, trip, user)


@router.put("/trips/{trip_id}/discover/places/{place_key}", response_model=DiscoveryPlacesOut)
def api_set_discovery_place(trip_id: str, place_key: str, body: DiscoveryPlaceIn,
                            user=Depends(authn.current_user),
                            session: Session = Depends(get_session)):
    """Exclude (`excluded: true`) or include again one place for the caller's
    Discovery on this trip. Idempotent. Only a place the trip actually has (404
    otherwise). Excluding drops that place's draft findings. Returns the list."""
    from app.trips.routes import discovery_places, set_place_excluded
    trip = scope.require_trip(session, user, trip_id, edit=True)
    keys = {p["key"] for p in discovery_places(session, trip, user.id)["places"]}
    if place_key not in keys:
        raise HTTPException(404, "no such place on this trip")
    set_place_excluded(session, trip.id, user.id, place_key, body.excluded)
    return _discovery_places_out(session, trip, user)


@router.get("/trips/{trip_id}/discover/status", response_model=DiscoveryStatusOut)
def api_discovery_status(trip_id: str, user=Depends(authn.current_user),
                         session: Session = Depends(get_session)):
    """Poll the caller's run. When `running` turns false, reload the trip."""
    trip = scope.require_trip(session, user, trip_id)
    return _discovery_status(session, trip, user)


@router.post("/trips/{trip_id}/discovery-items/{item_id}/timeline",
             response_model=ActivityOut, status_code=201)
def api_item_to_timeline(trip_id: str, item_id: str, body: ItemToTimelineIn,
                         user=Depends(authn.current_user),
                         session: Session = Depends(get_session)):
    """Put a Discover place/event/activity on the timeline, filled in from the item."""
    return add_item_to_timeline(session, user, trip_id, item_id, body)


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
        from discover import items as discovery_items
        discovery_items.unlink_activity(session, a.id)
        from app import expenses as expenses_svc
        expenses_svc.unlink_activity(session, a.id)   # SPEC-10: keep the expense, unlinked
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
PHRASES_TOPIC = "language_local"
MAX_ITEMS = 200


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
    then never overwrites. Never touches anyone else's Discover.

    4.5: food/events may carry `items` (synced as that finding's rows: they
    replace the list, keeping rows the person hid, edited or put on the
    timeline; `replace_items=False` only adds/updates), with `body_md` as the
    city summary. language_local carries structured `phrases`."""
    from datetime import timezone

    from discover import items as discovery_items
    from discover import policy
    from discover.resolvers.language_local import render_phrases

    trip = scope.require_trip(session, user, trip_id, edit=True)
    topic = (body.topic or RESEARCH_TOPIC).strip()
    if topic not in policy.EXTERNAL_TOPICS:
        raise HTTPException(400, f"topic must be one of {', '.join(policy.EXTERNAL_TOPICS)}")
    if body.items is not None and topic not in discovery_items.ITEM_TOPICS:
        raise HTTPException(400, "items are only for the food and events topics")
    if (body.phrases is not None) != (topic == PHRASES_TOPIC):
        raise HTTPException(400, "phrases go with topic language_local, and that topic "
                                 "needs phrases")
    rows = []
    try:
        rows = [discovery_items.row_from_input(topic, i.model_dump())
                for i in (body.items or [])[:MAX_ITEMS]]
    except ValueError as e:
        raise HTTPException(400, str(e))
    title = (body.title or "").strip()[:200]
    text = (body.body_md or "").strip()
    if topic == PHRASES_TOPIC:
        ph = body.phrases.model_dump()
        if not (ph["register_summary"].strip() or ph["greetings"] or ph["expressions"]):
            raise HTTPException(400, "phrases need a register_summary, greetings or expressions")
    st = None
    if body.safety_transport is not None:
        if topic != "safety":
            raise HTTPException(400, "safety_transport goes with topic safety")
        st = _safety_data(body.safety_transport)
        if st is None:
            raise HTTPException(400, "safety_transport needs a safety summary, advisories "
                                     "or scams, or a transport summary")
    if not title or not (text or rows or st or topic == PHRASES_TOPIC):
        raise HTTPException(400, "title and body_md are required")
    place_key = (body.place_key or "").strip()[:128]
    if not place_key:
        if topic != RESEARCH_TOPIC:
            raise HTTPException(400, "place_key is required for this topic (a country code "
                                     "like \"CA\" or a city key like \"CA:Banff\")")
        place_key = "trip"
    if topic == PHRASES_TOPIC:
        md = render_phrases(title, ph["register_summary"].strip(), ph["greetings"],
                            ph["expressions"], [e for e in ph["etiquette"] if e.strip()])
    elif st is not None:
        from discover.grounding import corroborate
        from discover.resolvers.safety import render_safety
        sd = st["safety"] or {}
        md = render_safety(title, sd, sd.get("advisories") or [], sd.get("scams") or [],
                           corroborate([a["source_url"] for a in sd.get("advisories") or []]),
                           st["transport"])
        if text:
            md += "\n\n" + text
    else:
        md = f"**{title}**\n\n{text}"[:100_000]
    sources = [{"title": (s.title or s.url)[:300], "url": s.url[:2000]}
               for s in body.sources[:50] if s.url.startswith(("http://", "https://"))]
    if st is not None:          # every advisory's source is a source of the finding
        seen = {s["url"] for s in sources}
        for a in (st["safety"] or {}).get("advisories") or []:
            if a["source_url"] not in seen:
                seen.add(a["source_url"])
                sources.append({"title": a["source_url"], "url": a["source_url"]})
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
    agent_name = (agent or policy.DEFAULT_AGENT)[:128]
    f.data = {"title": title, "agent": agent_name, "history": history}
    if topic in discovery_items.ITEM_TOPICS and text:
        f.data["summary"] = text                      # the city write-up (4.5)
    if st is not None:
        f.data.update(st)                              # renders as subsections (4.5)
    if topic == PHRASES_TOPIC:
        f.data.update({"register_summary": ph["register_summary"].strip(),
                       "greetings": ph["greetings"], "expressions": ph["expressions"],
                       "etiquette": [e for e in ph["etiquette"] if e.strip()]})
    f.generated_at = datetime.now(timezone.utc)
    if body.items is not None:
        session.flush()
        discovery_items.sync_items(session, f, rows, origin="agent", agent=agent_name,
                                   prune=body.replace_items)
    session.commit()
    session.refresh(f)
    return _finding_out(f)


def _safety_data(body: SafetyTransportIn) -> dict | None:
    """Clean an agent's Safety & Transport into the resolver's stored shape.
    Advisories need an http(s) source; empty parts are dropped."""
    from discover.grounding import corroborate

    def strs(xs, n=20):
        return [x.strip()[:500] for x in (xs or [])[:n] if x and x.strip()]

    safety = transport = None
    if body.safety:
        adv = [{"text": a.text.strip()[:1000], "source_url": a.source_url.strip()[:2000]}
               for a in body.safety.advisories[:20]
               if a.text.strip() and a.source_url.strip().startswith(("http://", "https://"))]
        summary = body.safety.summary.strip()[:3000]
        scams = strs(body.safety.scams)
        if summary or adv or scams:
            safety = {"summary": summary, "advisories": adv, "scams": scams,
                      "corroborated": corroborate([a["source_url"] for a in adv]).corroborated}
    if body.transport and body.transport.summary.strip():
        t = body.transport
        transport = {"summary": t.summary.strip()[:3000],
                     "public_transit": t.public_transit.strip()[:2000],
                     "from_airport": t.from_airport.strip()[:2000],
                     "ride_hailing": strs(t.ride_hailing), "apps": strs(t.apps),
                     "tips": strs(t.tips)}
    if not (safety or transport):
        return None
    return {"safety": safety, "transport": transport}


def my_discovery_profile(session: Session, user) -> dict:
    """The caller's own discovery profile (dietary, cuisines to avoid, budget,
    drink/activity interests, mobility), labelled, with how to apply it."""
    from app.trips.routes import discovery_profile_public
    trav = session.get(TravelerIdentity, user.traveler_id) if user.traveler_id else None
    return discovery_profile_public(session, trav, user_id=user.id)


@router.get("/me/discovery-profile")
def api_my_discovery_profile(user=Depends(authn.current_user),
                             session: Session = Depends(get_session)):
    """Your discovery profile — what Discover (and your agents) research with."""
    return my_discovery_profile(session, user)


# --------------------------------------------------------------------------- research kits (4.5)
def _kit_out(kit, full: bool = False) -> dict:
    out = {"slug": kit.slug, "name": kit.name, "title": kit.title,
           "description": kit.description,
           "mcp_prompt": "research_" + kit.slug.replace("-", "_"),
           "download": f"/api/v1/research-kits/{kit.slug}.zip"}
    if full:
        out["skill_md"] = kit.text
    return out


@router.get("/research-kits")
def api_research_kits(user=Depends(authn.current_user)):
    """Reusable agent instructions that research part of a trip and save it
    to Discover over MCP (also served as MCP prompts)."""
    from app import research_kits
    return [_kit_out(k) for k in research_kits.all_kits()]


@router.get("/research-kits/{slug}.zip")
def api_research_kit_zip(slug: str, user=Depends(authn.current_user)):
    """The kit as a skill zip, to add to an agent that takes skills."""
    from fastapi import Response
    from app import research_kits
    kit = research_kits.get(slug)
    if not kit:
        raise HTTPException(404, "No such research kit")
    return Response(research_kits.zip_bytes(kit), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{kit.name}.zip"'})


@router.get("/research-kits/{slug}")
def api_research_kit(slug: str, user=Depends(authn.current_user)):
    from app import research_kits
    kit = research_kits.get(slug)
    if not kit:
        raise HTTPException(404, "No such research kit")
    return _kit_out(kit, full=True)


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
    place_accuracy_m: float | None = None   # metres (parity with expenses)
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
    place_accuracy_m: float | None = None   # metres (parity with expenses)
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
    place_accuracy_m: float | None = None   # metres (parity with expenses)
    attach_trip: bool = False              # set true to apply trip_id (incl. null=detach)


class JournalDuplicate(BaseModel):
    trip_id: str | None = None             # the copy's trip (null = unattached)


def _journal_out(e: JournalEntry) -> JournalOut:
    return JournalOut(
        id=e.id, entry_date=e.entry_date, captured_at=e.captured_at,
        trip_id=e.trip_id, body=e.body or "", values=e.values or {},
        place_lat=e.place_lat, place_lon=e.place_lon, place_label=e.place_label,
        place_accuracy_m=e.place_accuracy_m,
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
            place_label=body.place_label, place_accuracy_m=body.place_accuracy_m,
            source=body.source)
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
    if body.place_accuracy_m is not None:
        kw["place_accuracy_m"] = body.place_accuracy_m
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


# --------------------------------------------------------------------------- saved places (YONDER-200)
class SavedPlaceOut(BaseModel):
    id: str
    name: str
    lat: float | None = None
    lon: float | None = None
    address: str | None = None
    category: str | None = None
    activity_type: str
    provider: str
    source_url: str | None = None
    notes: str | None = None
    source: str = "api"
    created_at: datetime | None = None
    status: str = "saved"                  # saved | filed | dismissed (the last two are tombstones)
    suggested_trip_id: str | None = None   # a hint only: the person confirms before filing
    # What a save just did (POST only): created | revived | duplicate | near_duplicate |
    # already_filed | removed_earlier. Lets a retrying client tell "added" from "already there".
    outcome: str | None = None


class SavedPlacePatch(BaseModel):
    """Edit a saved place before filing it. Only the fields sent change."""
    name: str | None = None
    address: str | None = None
    notes: str | None = None
    activity_type: str | None = None


class SavedPlaceIn(BaseModel):
    """Save a place. Either a Google/Apple Maps `url` for the server to parse, or
    the details the app already resolved on the phone (MapKit, D-166); anything
    supplied alongside a url overrides what the url yielded."""
    url: str | None = None
    name: str | None = None
    lat: float | None = None
    lon: float | None = None
    address: str | None = None
    category: str | None = None            # MKPointOfInterestCategory or an OSM tag
    provider: str | None = None            # google | apple
    source_url: str | None = None
    notes: str | None = None
    source: str = "api"
    # Re-add a place the person removed earlier. Never re-adds one that was filed.
    revive: bool = False


class SavedPlaceBatchIn(BaseModel):
    """Save a list of places in one call (a Google list, a Takeout export). Safe to
    send twice: each place is saved at most once, and anything filed or removed
    earlier is skipped and reported."""
    places: list[SavedPlaceIn]
    dry_run: bool = False                  # report what each would do; write nothing
    enrich: bool = False                   # Photon address/category; small batches only
    revive: bool = False
    allow_near_duplicates: bool = False    # also add a same-name place within 150 m


class SavedPlaceBatchResult(BaseModel):
    index: int
    outcome: str                           # created | revived | duplicate | near_duplicate |
                                           # already_filed | removed_earlier | error
    id: str | None = None
    name: str | None = None
    existing_status: str | None = None
    error: str | None = None


class SavedPlaceBatchOut(BaseModel):
    dry_run: bool
    counts: dict[str, int]
    results: list[SavedPlaceBatchResult]


class SavedPlaceFileIn(BaseModel):
    """File a saved place to a trip as a timeline activity. All optional: with no
    date it lands undated (sorts last) to be scheduled later."""
    starts_at: str | None = None           # YYYY-MM-DD or YYYY-MM-DDTHH:MM local
    activity_type: str | None = None       # override the category's suggestion
    notes: str | None = None
    traveler_ids: list[int] | None = None  # None/[] = everyone on the trip


def _place_out(p, suggested_trip_id: str | None = None,
               outcome: str | None = None) -> SavedPlaceOut:
    return SavedPlaceOut(
        id=p.id, name=p.name, lat=p.lat, lon=p.lon, address=p.address,
        category=p.category, activity_type=p.activity_type, provider=p.provider,
        source_url=p.source_url, notes=p.notes, source=p.source, created_at=p.created_at,
        status=p.status, suggested_trip_id=suggested_trip_id, outcome=outcome)


@router.post("/places", response_model=SavedPlaceOut, status_code=201)
def api_place_save(body: SavedPlaceIn, response: Response, user=Depends(authn.current_user),
                   session: Session = Depends(get_session)):
    """Save a place for the caller. Safe to repeat or retry (an offline outbox, a
    double tap): 201 when it was added, 200 when it was already there or had been
    filed or removed, with `outcome` saying which."""
    try:
        place, outcome = places_svc.save(
            session, user, url=body.url, name=body.name, lat=body.lat, lon=body.lon,
            address=body.address, category=body.category, provider=body.provider,
            source_url=body.source_url, notes=body.notes, source=body.source,
            enrich=True, revive=body.revive)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if outcome not in (places_svc.CREATED, places_svc.REVIVED):
        response.status_code = 200
    suggested = (places_svc.suggest_trip(session, fileable_trips(session, user), place)
                 if place.status == places_svc.SAVED else None)
    return _place_out(place, suggested, outcome)


@router.post("/places/batch", response_model=SavedPlaceBatchOut)
def api_place_batch(body: SavedPlaceBatchIn, user=Depends(authn.current_user),
                    session: Session = Depends(get_session)):
    """Save many places in one call. Each item is judged and committed on its own:
    a bad item is reported, not fatal, and sending the same list again adds
    nothing. Use `dry_run` first to see what would happen."""
    try:
        out = places_svc.save_many(
            session, user, [p.model_dump() for p in body.places], dry_run=body.dry_run,
            enrich=body.enrich, revive=body.revive,
            allow_near_duplicates=body.allow_near_duplicates, source="api")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return out


def fileable_trips(session: Session, user) -> list:
    """The trips this person can file a saved place into (the same filing rule as
    captures, scope.filing_trip_filter)."""
    return session.query(Trip).filter(scope.filing_trip_filter(session, user)).all()


@router.get("/places", response_model=list[SavedPlaceOut])
def api_place_list(user=Depends(authn.current_user),
                   session: Session = Depends(get_session)):
    """The caller's saved places, newest first, each with a suggested trip."""
    trips = fileable_trips(session, user)
    return [_place_out(p, places_svc.suggest_trip(session, trips, p))
            for p in places_svc.list_places(session, user)]


@router.patch("/places/{place_id}", response_model=SavedPlaceOut)
def api_place_update(place_id: str, body: SavedPlacePatch,
                     user=Depends(authn.current_user),
                     session: Session = Depends(get_session)):
    """Rename or otherwise edit a saved place before filing it."""
    try:
        p = places_svc.update_place(session, user, place_id,
                                    **body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _place_out(p, places_svc.suggest_trip(session, fileable_trips(session, user), p))


@router.delete("/places/{place_id}", status_code=204)
def api_place_delete(place_id: str, user=Depends(authn.current_user),
                     session: Session = Depends(get_session)):
    places_svc.delete_place(session, user, place_id)
    return None


def file_place(session: Session, user, place_id: str, trip_id: str,
               body: SavedPlaceFileIn) -> ActivityOut:
    """File a saved place to a trip: creates the timeline activity and marks the
    place filed (D-166, D-169). Needs edit access to the trip; the activity is
    undated unless `starts_at` is given. Filing twice is a 409. Shared by the API
    and the web page."""
    place = places_svc.require_place(session, user, place_id)
    trip = scope.require_trip(session, user, trip_id, edit=True)
    atype, values = places_svc.activity_for(place, body.activity_type, body.notes)
    t = _activity_type_or_400(atype)
    if body.starts_at:
        import re
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?", body.starts_at):
            raise HTTPException(400, "starts_at must be YYYY-MM-DD or YYYY-MM-DDTHH:MM")
        values["when"] = body.starts_at
    from app.trips.enrich import prepare_activity
    values, geo = prepare_activity(t.key, values, session=session, trip_id=trip.id)
    a = TripActivity(trip_id=trip.id, type=t.key, values=values, geo=geo,
                     starts_at=values.get("when") or "")
    # Claim the place first, atomically: of two requests filing it at once (a
    # double tap, a retry) exactly one wins, so it never lands on a trip twice.
    claimed = (session.query(SavedPlace)
               .filter(SavedPlace.id == place.id, SavedPlace.status == places_svc.SAVED)
               .update({"status": places_svc.FILED}, synchronize_session=False))
    if claimed != 1:
        session.rollback()
        raise HTTPException(409, "this place was already filed to a trip")
    session.add(a)
    session.flush()
    tagged = _set_activity_travelers(session, a, body.traveler_ids or [])
    session.commit()
    session.refresh(a)
    return _activity_out(a, tagged)


@router.post("/places/{place_id}/file/{trip_id}", response_model=ActivityOut,
             status_code=201)
def api_place_file(place_id: str, trip_id: str, body: SavedPlaceFileIn,
                   user=Depends(authn.current_user),
                   session: Session = Depends(get_session)):
    """File a saved place to a trip (see `file_place`)."""
    return file_place(session, user, place_id, trip_id, body)


# --------------------------------------------------------------------------- expenses
# SPEC-10 (YONDER-172, D-173–D-177). Trip sharing applies (D-174): reading needs
# view access, every write needs edit access, and inside that a cost is visible
# only to the travellers on it and whoever entered it (D-183) — anything else
# 404s like a missing one. Rules live in app/expenses.py.

class ExpenseShareOut(BaseModel):
    traveler_id: int
    name: str = ""
    amount: str                            # in the expense's currency


class ExpenseCommentOut(BaseModel):
    """A comment on an actual expense (D-185)."""
    id: str
    author_id: str | None = None
    author_name: str = ""
    body: str
    created_at: datetime | None = None
    mine: bool = False                     # the caller wrote it (and can delete it)


class ExpenseOut(BaseModel):
    id: str
    trip_id: str
    kind: str                              # expense | settlement
    title: str
    amount: str                            # decimal string, e.g. "42.50"
    currency: str
    rate: str | None = None                # trip currency per 1 unit; None = unconverted
    rate_source: str | None = None         # same | entered | frankfurter | discover
    rate_date: str | None = None           # the day the rate is for (D-195)
    amount_in_trip_currency: str | None = None
    spent_on: str | None = None
    payer_id: int
    payer_name: str = ""
    split: str                             # equal | exact
    shares: list[ExpenseShareOut]
    category: str
    activity_id: str | None = None
    budget_item_id: str | None = None      # the planned item it fulfils (D-181)
    notes: str | None = None
    owner_id: str | None = None
    created_at: datetime | None = None
    comments: list[ExpenseCommentOut] = []  # the discussion, oldest first (D-185)
    # Where it was logged (YONDER-212). Visible exactly where the expense is (D-183).
    place_lat: float | None = None
    place_lon: float | None = None
    place_label: str | None = None
    place_accuracy_m: float | None = None


class BudgetItemOut(BaseModel):
    """A planned cost (D-178/D-179/D-184): one estimated amount for the travellers
    tagged on it — never multiplied by people or days. `total` is it in the trip
    currency."""
    id: str
    trip_id: str
    title: str
    category: str
    amount: str                            # the estimate, in `currency`
    currency: str
    traveler_ids: list[int] = []           # who it's for (D-184)
    traveler_names: list[str] = []
    start_date: str | None = None          # when it applies, if not the whole trip
    end_date: str | None = None
    total: str | None = None               # in the trip currency; None = no rate
    rate: str | None = None
    rate_source: str | None = None
    rate_date: str | None = None           # D-195
    status: str                            # draft (an agent's suggestion) | kept
    place_key: str | None = None
    activity_id: str | None = None
    notes: str | None = None
    sources: list[dict] = []
    source: str = "api"
    actual: str = "0"                      # actuals logged against it (trip currency)
    owner_id: str | None = None


class CategoryPlanOut(BaseModel):
    category: str
    planned: str                           # kept planned items
    drafts: str                            # agents' suggestions not kept yet
    actual: str                            # actual expenses


class TravelerPlanOut(BaseModel):
    """One traveller's budget beside their share of what was spent (D-184)."""
    traveler_id: int
    name: str = ""
    planned: str
    actual: str


class PlanOut(BaseModel):
    """The budget beside what was actually spent (D-181). Only `actual` is the
    trip's real cost; settle-up uses actual expenses alone (D-178)."""
    people: int
    days: int | None = None                # None = the trip has no dates yet
    planned: str
    drafts: str
    actual: str
    items: list[BudgetItemOut]
    by_category: list[CategoryPlanOut]
    by_traveler: list[TravelerPlanOut] = []
    unconverted: list[str] = []


class BalanceOut(BaseModel):
    traveler_id: int
    name: str = ""
    paid: str                              # expenses they paid (trip currency)
    share: str                             # their part of all expenses
    net: str                               # + is owed money, − owes money


class TransferOut(BaseModel):
    from_id: int
    from_name: str = ""
    to_id: int
    to_name: str = ""
    amount: str


class ExpensesOut(BaseModel):
    currency: str | None = None            # the trip's balance currency (None = no expenses yet)
    total: str                             # trip spend, settlements excluded
    expenses: list[ExpenseOut]
    balances: list[BalanceOut]
    transfers: list[TransferOut]           # the suggested settle-up payments
    unconverted: list[str] = []            # expense ids with no rate (left out of balances)
    categories: list[str] = list(expenses_svc.CATEGORIES)
    plan: PlanOut | None = None            # the planned budget (D-178)
    # The currency picker's order (YONDER-213): destination first, then yours,
    # the trip's and those already used; list every other code after these.
    suggested_currencies: list[str] = []
    default_currency: str | None = None    # what a new expense starts in


class ExpenseIn(BaseModel):
    """Record an expense. `traveler_ids` is who it's split equally between
    ([]/omitted = everyone on the trip); for `split: "exact"` send `shares`
    ({traveler_id: amount}) adding up to `amount`. Leave `rate` blank to use the
    trip's rate for that day (D-195); give it to override."""
    amount: str
    currency: str
    payer_id: int
    title: str = ""
    spent_on: str | None = None            # YYYY-MM-DD
    split: str = "equal"
    traveler_ids: list[int] | None = None
    shares: dict[int, str] | None = None
    category: str = "other"
    rate: str | None = None
    activity_id: str | None = None
    budget_item_id: str | None = None      # log it against a planned item (D-181)
    notes: str | None = None
    source: str = "api"
    # Where it was logged (YONDER-212): the app captures it; optional everywhere.
    place_lat: float | None = None
    place_lon: float | None = None
    place_label: str | None = None
    place_accuracy_m: float | None = None


class ExpensePatch(BaseModel):
    """Edit an expense. Only the fields sent change; changing amount, currency or
    split recomputes the shares. `activity_id: ""` unlinks the timeline item."""
    amount: str | None = None
    currency: str | None = None
    payer_id: int | None = None
    title: str | None = None
    spent_on: str | None = None
    split: str | None = None
    traveler_ids: list[int] | None = None
    shares: dict[int, str] | None = None
    category: str | None = None
    rate: str | None = None
    activity_id: str | None = None
    budget_item_id: str | None = None      # "" unlinks
    notes: str | None = None
    # Replaces the stored location as a whole; send nulls to clear it.
    place_lat: float | None = None
    place_lon: float | None = None
    place_label: str | None = None
    place_accuracy_m: float | None = None


class BudgetItemIn(BaseModel):
    """Plan a cost for the travellers it's for (`traveler_ids`; your own entry
    defaults to you, research must name them). `amount` is the one estimated
    amount for them — a room for two is one cost; nothing is multiplied (D-179).
    `start_date`–`end_date` say when it applies, if not the whole trip."""
    amount: str
    currency: str
    title: str = ""
    category: str = "other"
    traveler_ids: list[int] | None = None
    start_date: str | None = None
    end_date: str | None = None
    rate: str | None = None
    place_key: str | None = None
    activity_id: str | None = None
    notes: str | None = None
    sources: list[dict] | None = None
    source: str = "api"


class BudgetItemPatch(BaseModel):
    """Edit a planned item; editing an agent's draft keeps it. `status: "kept"`
    keeps a draft as it is."""
    amount: str | None = None
    currency: str | None = None
    title: str | None = None
    category: str | None = None
    traveler_ids: list[int] | None = None
    start_date: str | None = None          # "" clears it
    end_date: str | None = None
    rate: str | None = None
    place_key: str | None = None
    activity_id: str | None = None
    notes: str | None = None
    status: str | None = None


class BudgetBatchIn(BaseModel):
    """An agent's budget research (the trip-budget research kit, D-180). Every
    item lands as a draft; `replace` first drops your earlier drafts in the same
    categories. Kept items are never touched."""
    items: list[BudgetItemIn]
    replace: bool = True


class SettlementIn(BaseModel):
    """`from_id` paid `to_id` back. Currency defaults to the trip's."""
    from_id: int
    to_id: int
    amount: str
    currency: str | None = None
    spent_on: str | None = None
    rate: str | None = None
    notes: str | None = None
    source: str = "api"


class ExpenseCurrencyIn(BaseModel):
    currency: str


class ExpenseCommentIn(BaseModel):
    body: str


class RateRefreshOut(BaseModel):
    rate: TripRateOut
    applied: list[str] = []                # your costs that had no rate and now do


def _comments_out(comments, user, users: dict[str, str]) -> list[ExpenseCommentOut]:
    me = getattr(user, "id", None)
    return [ExpenseCommentOut(id=c.id, author_id=c.author_id,
                              author_name=users.get(c.author_id or "", ""), body=c.body,
                              created_at=c.created_at,
                              mine=c.author_id is not None and c.author_id == me)
            for c in comments]


def _user_names(session: Session, ids) -> dict[str, str]:
    """Comment authors by name: their traveller's display name, else username."""
    from app.models import User
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    users = session.query(User).filter(User.id.in_(ids)).all()
    tnames = expenses_svc.names(session, [u.traveler_id for u in users if u.traveler_id])
    return {u.id: tnames.get(u.traveler_id) or u.username for u in users}


def _expense_out(e, shares, nm: dict[int, str], trip_currency: str | None,
                 comments=None) -> ExpenseOut:
    from decimal import ROUND_HALF_UP, Decimal
    converted = None
    if e.rate is not None:
        q = expenses_svc.quantum(trip_currency or e.currency)
        converted = format((Decimal(e.amount) * Decimal(e.rate))
                           .quantize(q, rounding=ROUND_HALF_UP).normalize(), "f")
    return ExpenseOut(
        id=e.id, trip_id=e.trip_id, kind=e.kind, title=e.title, amount=e.amount,
        currency=e.currency, rate=e.rate, rate_source=e.rate_source, rate_date=e.rate_date,
        amount_in_trip_currency=converted, spent_on=e.spent_on, payer_id=e.payer_id,
        payer_name=nm.get(e.payer_id, ""), split=e.split,
        shares=[ExpenseShareOut(traveler_id=s.traveler_id, name=nm.get(s.traveler_id, ""),
                                amount=s.amount) for s in shares],
        category=e.category, activity_id=e.activity_id, budget_item_id=e.budget_item_id,
        notes=e.notes, owner_id=e.owner_id, created_at=e.created_at,
        comments=comments or [], place_lat=e.place_lat, place_lon=e.place_lon,
        place_label=e.place_label, place_accuracy_m=e.place_accuracy_m)


def _one_expense_out(session: Session, trip: Trip, e, user=None) -> ExpenseOut:
    shares = expenses_svc.shares_of(session, e.id)
    nm = expenses_svc.names(session, [e.payer_id, *[s.traveler_id for s in shares]])
    cs = expenses_svc.comments_of(session, [e.id]).get(e.id, [])
    users = _user_names(session, [c.author_id for c in cs])
    return _expense_out(e, shares, nm, trip.expense_currency, _comments_out(cs, user, users))


def expenses_overview(session: Session, trip: Trip, user=None) -> ExpensesOut:
    """What `user` can see of the trip's costs (D-183): the expenses they're on
    or entered, balances and suggested payments from those, and their plan."""
    rows, smap = expenses_svc.visible_expenses(session, trip, user)
    summ = expenses_svc.summarize(session, trip, user)
    cmap = expenses_svc.comments_of(session, [e.id for e in rows])
    users = _user_names(session, [c.author_id for cs in cmap.values() for c in cs])
    ids = {*summ.net, *[e.payer_id for e in rows],
           *[s.traveler_id for ss in smap.values() for s in ss]}
    nm = expenses_svc.names(session, ids)

    def f(d):
        return format(d.normalize(), "f") if d else "0"
    return ExpensesOut(
        currency=summ.currency, total=f(summ.total),
        expenses=[_expense_out(e, smap.get(e.id, []), nm, summ.currency,
                               _comments_out(cmap.get(e.id, []), user, users)) for e in rows],
        balances=[BalanceOut(traveler_id=t, name=nm.get(t, ""),
                             paid=f(summ.paid.get(t)), share=f(summ.share.get(t)),
                             net=f(summ.net.get(t)))
                  for t in sorted(summ.net)],
        transfers=[TransferOut(from_id=a, from_name=nm.get(a, ""), to_id=b,
                               to_name=nm.get(b, ""), amount=f(amt))
                   for a, b, amt in summ.transfers],
        unconverted=summ.unconverted,
        plan=plan_out(session, trip, user, rows),
        suggested_currencies=expenses_svc.suggested_currencies(session, trip, user),
        default_currency=expenses_svc.default_currency(session, trip, user))


def _budget_out(line, actual: str = "0", nm: dict[int, str] | None = None) -> BudgetItemOut:
    b = line.item
    nm = nm or {}
    return BudgetItemOut(
        id=b.id, trip_id=b.trip_id, title=b.title, category=b.category, amount=b.amount,
        currency=b.currency, traveler_ids=line.traveler_ids,
        traveler_names=[nm.get(t, "") for t in line.traveler_ids],
        start_date=b.start_date, end_date=b.end_date,
        total=None if line.total is None else format(line.total.normalize(), "f"),
        rate=b.rate, rate_source=b.rate_source, rate_date=b.rate_date, status=b.status, place_key=b.place_key,
        activity_id=b.activity_id, notes=b.notes, sources=b.sources or [], source=b.source,
        actual=actual, owner_id=b.owner_id)


def plan_out(session: Session, trip: Trip, user=None, expense_rows=None) -> PlanOut:
    from decimal import ROUND_HALF_UP, Decimal
    p = expenses_svc.plan(session, trip, user)
    q = expenses_svc.quantum(p.currency or "")
    nm = expenses_svc.names(session, {*[t for ln in p.lines for t in ln.traveler_ids],
                                      *[t for t, _, _ in p.by_traveler]})
    spent: dict[str, Decimal] = {}
    for e in (expense_rows if expense_rows is not None
              else expenses_svc.visible_expenses(session, trip, user)[0]):
        if e.budget_item_id and e.kind == expenses_svc.EXPENSE and e.rate is not None:
            spent[e.budget_item_id] = spent.get(e.budget_item_id, Decimal(0)) + (
                Decimal(e.amount) * Decimal(e.rate)).quantize(q, rounding=ROUND_HALF_UP)

    def f(d):
        return format(d.normalize(), "f") if d else "0"
    return PlanOut(
        people=p.people, days=p.days, planned=f(p.planned), drafts=f(p.drafts),
        actual=f(p.actual),
        items=[_budget_out(ln, f(spent.get(ln.item.id)), nm) for ln in p.lines],
        by_category=[CategoryPlanOut(category=c, planned=f(a), drafts=f(d), actual=f(x))
                     for c, a, d, x in p.by_category],
        by_traveler=[TravelerPlanOut(traveler_id=t, name=nm.get(t, ""), planned=f(a),
                                     actual=f(x)) for t, a, x in p.by_traveler],
        unconverted=p.unconverted)


def _expense_or_404(session: Session, trip_id: str, expense_id: str, user=None):
    """The expense, if it's on this trip and the caller is on it or entered it
    (D-183); otherwise 404, exactly like a missing one."""
    from app.models import TripExpense
    e = session.get(TripExpense, expense_id)
    if (e is None or e.trip_id != trip_id
            or not expenses_svc.can_see_expense(session, user, e)):
        raise HTTPException(404, "expense not found")
    return e


def _valueerror_400(fn):
    try:
        return fn()
    except ValueError as err:
        raise HTTPException(400, str(err)) from None


@router.get("/trips/{trip_id}/expenses", response_model=ExpensesOut)
def api_expenses(trip_id: str, user=Depends(authn.current_user),
                 session: Session = Depends(get_session)):
    """The trip's costs you're on: expenses, balances and the suggested settle-up
    payments, and the planned budget (D-183)."""
    trip = scope.require_trip(session, user, trip_id)       # 404 if not visible
    return expenses_overview(session, trip, user)


@router.post("/trips/{trip_id}/expenses", response_model=ExpenseOut, status_code=201)
def api_add_expense(trip_id: str, body: ExpenseIn, user=Depends(authn.current_user),
                    session: Session = Depends(get_session)):
    trip = scope.require_trip(session, user, trip_id, edit=True)
    data = expenses_svc.ExpenseInput(**body.model_dump())
    e = _valueerror_400(lambda: expenses_svc.create(session, user, trip, data))
    session.commit()
    return _one_expense_out(session, trip, e, user)


@router.patch("/trips/{trip_id}/expenses/{expense_id}", response_model=ExpenseOut)
def api_update_expense(trip_id: str, expense_id: str, body: ExpensePatch,
                       user=Depends(authn.current_user),
                       session: Session = Depends(get_session)):
    trip = scope.require_trip(session, user, trip_id, edit=True)
    e = _expense_or_404(session, trip.id, expense_id, user)
    _valueerror_400(lambda: expenses_svc.update(session, user, trip, e,
                                                body.model_dump(exclude_unset=True)))
    session.commit()
    return _one_expense_out(session, trip, e, user)


@router.delete("/trips/{trip_id}/expenses/{expense_id}", status_code=204)
def api_delete_expense(trip_id: str, expense_id: str, user=Depends(authn.current_user),
                       session: Session = Depends(get_session)):
    trip = scope.require_trip(session, user, trip_id, edit=True)
    e = _expense_or_404(session, trip.id, expense_id, user)
    expenses_svc.delete(session, e)
    session.commit()
    return None


@router.post("/trips/{trip_id}/settlements", response_model=ExpenseOut, status_code=201)
def api_record_settlement(trip_id: str, body: SettlementIn, user=Depends(authn.current_user),
                          session: Session = Depends(get_session)):
    """Record that one traveller paid another back (D-177)."""
    trip = scope.require_trip(session, user, trip_id, edit=True)
    e = _valueerror_400(lambda: expenses_svc.record_settlement(
        session, user, trip, from_id=body.from_id, to_id=body.to_id, amount=body.amount,
        currency=body.currency, spent_on=body.spent_on, rate=body.rate, notes=body.notes,
        source=body.source))
    session.commit()
    return _one_expense_out(session, trip, e, user)


@router.post("/trips/{trip_id}/rates/{code}/refresh", response_model=RateRefreshOut)
def api_refresh_rate(trip_id: str, code: str, date: str | None = None,
                     user=Depends(authn.current_user),
                     session: Session = Depends(get_session)):
    """Fetch ONE currency's rate for a day (default today) onto the trip's shared
    rate table (YONDER-237/242, D-195), then convert your costs in that currency
    that had no rate yet. Costs that already have a rate keep it. On an ended
    trip it's always today's rate (no backdating). 422 when no rate can be had."""
    from app import rates as rates_svc
    trip = scope.require_trip(session, user, trip_id, edit=True)
    cur = _valueerror_400(lambda: expenses_svc.currency_code(code))
    try:
        row = rates_svc.refresh(session, trip, user.id, cur, date)
    except ValueError as err:
        status = 400 if "not a date" in str(err) else 422
        raise HTTPException(status, str(err)) from None
    applied = expenses_svc.apply_to_unconverted(session, trip, user, cur)
    session.commit()
    return RateRefreshOut(rate=_rate_out(row), applied=applied)


def _rate_out(row) -> TripRateOut:
    return TripRateOut(currency=row.currency, quote=row.quote, rate_date=row.rate_date,
                       rate=row.rate, as_of=row.as_of, source=row.source,
                       fetched_at=row.fetched_at)


@router.put("/trips/{trip_id}/expenses/currency", response_model=ExpensesOut)
def api_set_expense_currency(trip_id: str, body: ExpenseCurrencyIn,
                             user=Depends(authn.current_user),
                             session: Session = Depends(get_session)):
    """Change the currency balances are shown in (D-176, D-195): costs convert
    again from the trip's rate table at their own day. Rates you entered were
    against the old currency, so they are cleared and need entering again."""
    trip = scope.require_trip(session, user, trip_id, edit=True)
    _valueerror_400(lambda: expenses_svc.set_trip_currency(session, user, trip, body.currency))
    session.commit()
    return expenses_overview(session, trip, user)


@router.post("/trips/{trip_id}/expenses/{expense_id}/comments",
             response_model=ExpenseCommentOut, status_code=201)
def api_add_expense_comment(trip_id: str, expense_id: str, body: ExpenseCommentIn,
                            user=Depends(authn.current_user),
                            session: Session = Depends(get_session)):
    """Comment on an expense you're on or entered (D-185). Anyone who can see the
    expense can join the discussion, view-only shares included."""
    trip = scope.require_trip(session, user, trip_id)
    e = _expense_or_404(session, trip.id, expense_id, user)
    c = _valueerror_400(lambda: expenses_svc.add_comment(session, user, e, body.body))
    session.commit()
    return _comments_out([c], user, _user_names(session, [c.author_id]))[0]


@router.delete("/trips/{trip_id}/expenses/{expense_id}/comments/{comment_id}",
               status_code=204)
def api_delete_expense_comment(trip_id: str, expense_id: str, comment_id: str,
                               user=Depends(authn.current_user),
                               session: Session = Depends(get_session)):
    """Delete your own comment."""
    from app.models import TripExpenseComment
    trip = scope.require_trip(session, user, trip_id)
    e = _expense_or_404(session, trip.id, expense_id, user)
    c = session.get(TripExpenseComment, comment_id)
    if c is None or c.expense_id != e.id:
        raise HTTPException(404, "comment not found")
    if not expenses_svc.can_delete_comment(user, c):
        raise HTTPException(403, "you can only delete your own comments")
    session.delete(c)
    session.commit()
    return None



# --------------------------------------------------------------------------- budget (planned)
# SPEC-10 D-178–D-184: the planned side of Costs. Visible to the travellers it's
# for and whoever entered it (D-183).
def _budget_or_404(session: Session, trip_id: str, item_id: str, user=None):
    from app.models import TripBudgetItem
    b = session.get(TripBudgetItem, item_id)
    if (b is None or b.trip_id != trip_id
            or not expenses_svc.can_see_budget(session, user, b)):
        raise HTTPException(404, "planned item not found")
    return b


def _one_budget_out(session: Session, trip: Trip, b, user=None) -> BudgetItemOut:
    # Echo what was just written even if the edit took the caller off it.
    items = plan_out(session, trip, user).items
    hit = next((i for i in items if i.id == b.id), None)
    return hit or next(i for i in plan_out(session, trip, None).items if i.id == b.id)


def save_budget(session: Session, user, trip_id: str, body: BudgetBatchIn, source: str):
    trip = scope.require_trip(session, user, trip_id, edit=True)
    items = [expenses_svc.BudgetInput(**{**i.model_dump(), "source": source})
             for i in body.items]
    out = _valueerror_400(lambda: expenses_svc.save_budget_batch(
        session, user, trip, items, replace=body.replace))
    session.commit()
    return {**out, "plan": plan_out(session, trip, user)}


@router.post("/trips/{trip_id}/budget", response_model=BudgetItemOut, status_code=201)
def api_add_budget_item(trip_id: str, body: BudgetItemIn, user=Depends(authn.current_user),
                        session: Session = Depends(get_session)):
    """Plan a cost yourself (it's kept straight away)."""
    trip = scope.require_trip(session, user, trip_id, edit=True)
    b = _valueerror_400(lambda: expenses_svc.create_budget(
        session, user, trip, expenses_svc.BudgetInput(**body.model_dump())))
    session.commit()
    return _one_budget_out(session, trip, b, user)


@router.post("/trips/{trip_id}/budget/batch")
def api_save_budget(trip_id: str, body: BudgetBatchIn, user=Depends(authn.current_user),
                    session: Session = Depends(get_session)):
    """Save budget research as drafts (see BudgetBatchIn)."""
    return save_budget(session, user, trip_id, body, source="api")


@router.patch("/trips/{trip_id}/budget/{item_id}", response_model=BudgetItemOut)
def api_update_budget_item(trip_id: str, item_id: str, body: BudgetItemPatch,
                           user=Depends(authn.current_user),
                           session: Session = Depends(get_session)):
    trip = scope.require_trip(session, user, trip_id, edit=True)
    b = _budget_or_404(session, trip.id, item_id, user)
    _valueerror_400(lambda: expenses_svc.update_budget(
        session, user, trip, b, body.model_dump(exclude_unset=True)))
    session.commit()
    return _one_budget_out(session, trip, b, user)


@router.delete("/trips/{trip_id}/budget/{item_id}", status_code=204)
def api_delete_budget_item(trip_id: str, item_id: str, user=Depends(authn.current_user),
                           session: Session = Depends(get_session)):
    trip = scope.require_trip(session, user, trip_id, edit=True)
    expenses_svc.delete_budget(session, _budget_or_404(session, trip.id, item_id, user))
    session.commit()
    return None
