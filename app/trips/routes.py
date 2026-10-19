"""Trips — Yonder 2.0's in-house trip manager (D-18 lineage: server-rendered
HTMX + Jinja, no build step, nothing extra to deploy).

The design's client-side component is translated here to plain endpoints: the
list and timeline are GET renders; create/edit/delete post and answer with an
`HX-Redirect` back to the page they belong on (a management surface used a few
times a week does not need optimistic partial juggling — a clean reload is
simpler and never leaves a stale half-swapped view). Only the live search /
filter and the modals swap partials.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from app import places as places_svc, scope
from app.config import settings
from app.db import get_session
from app.models import (DiscoveryExclusion, DiscoveryFinding, DiscoveryRun, Trip,
                        TripActivity, TravelerIdentity, TripTraveler)
from app.trips import types as activity_types
from app.trips.enrich import prepare_activity
from app.trips import who

log = logging.getLogger("yonder.trips")
router = APIRouter(prefix="/trips")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
from app.icons import install as _install_icons  # noqa: E402
_install_icons(templates)
# Let Trips templates {% include %} the shared global header (_appheader.html)
# that lives with the review templates, so all surfaces keep one header.
from jinja2 import ChoiceLoader, FileSystemLoader  # noqa: E402

templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(Path(__file__).parent / "templates")),
    FileSystemLoader(str(Path(__file__).parent.parent / "review" / "templates")),
])

STATUS_LABELS = [("inprogress", "In progress"), ("upcoming", "Upcoming"),
                 ("past", "Past")]
# The list's two tabs. "Upcoming" also holds in-progress (and undated) trips;
# "Past" is grouped by year so a long history stays scannable.
TRIP_TABS = [("upcoming", "Upcoming"), ("past", "Past")]

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
           "Oct", "Nov", "Dec"]


# ----------------------------------------------------------------- formatting

def _parse_dt(s: str | None) -> datetime | None:
    """Parse a `datetime-local`/ISO value tolerantly (date-only ok)."""
    if not s:
        return None
    raw = str(s).strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:len(fmt) + 2] if fmt.endswith("S") else raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _fmt_ampm(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{h}:{dt.minute:02d} {ampm}"


def fmt_date(iso: str | None) -> str:
    """'2026-08-15' -> 'Aug 15, 2026'. Accepts a full datetime too."""
    dt = _parse_dt(iso)
    return f"{_MONTHS[dt.month - 1]} {dt.day}, {dt.year}" if dt else ""


def fmt_datetime(iso: str | None) -> str:
    dt = _parse_dt(iso)
    if not dt:
        return ""
    if "T" not in str(iso):                      # date-only, no clock time
        return fmt_date(iso)
    return f"{_MONTHS[dt.month - 1]} {dt.day}, {_fmt_ampm(dt)}"


def fmt_time_only(iso: str | None) -> str:
    dt = _parse_dt(iso)
    if not dt or "T" not in str(iso):
        return ""
    return _fmt_ampm(dt)


def fmt_date_short(iso: str | None) -> str:
    dt = _parse_dt(iso)
    return f"{_MONTHS[dt.month - 1]} {dt.day}" if dt else ""


# ----------------------------------------------------------------- status

def trip_status(t: Trip, today: str) -> str:
    if t.end_date and t.end_date < today:
        return "past"
    if t.start_date and t.start_date > today:
        return "upcoming"
    return "inprogress"


_IATA = re.compile(r"^[A-Z]{3}$")


def trip_route_codes(activities, limit: int | None = 4) -> list[str]:
    """The airport codes a trip's flights visit, in flight order and without repeats
    (LHR, NRT, KIX) — what the design's trip card shows as mono codes. Only real
    3-letter codes; a free-text place never becomes a code. `limit=None` = all."""
    codes: list[str] = []
    for a in sorted((a for a in activities if a.type == "flight"),
                    key=lambda a: a.starts_at or ""):
        for key in ("from", "to"):
            c = str((a.values or {}).get(key) or "").strip().upper()
            if _IATA.match(c) and c not in codes:
                codes.append(c)
    return codes if limit is None else codes[:limit]


def trip_when(t: Trip, today: str) -> str:
    """Relative timing for a trip card: "In progress", "in 12 days", "ended 3 days ago"."""
    st = trip_status(t, today)
    if st == "inprogress":
        return "In progress" if t.start_date else "No dates"
    ref = t.start_date if st == "upcoming" else (t.end_date or t.start_date)
    try:
        days = abs((date.fromisoformat(ref[:10]) - date.fromisoformat(today)).days)
    except (TypeError, ValueError):
        return ""
    unit = "day" if days == 1 else "days"
    return f"in {days} {unit}" if st == "upcoming" else f"ended {days} {unit} ago"


def packing_progress(session: Session, trip_ids, user=None) -> dict[str, dict]:
    """{trip_id: {packed, total, pct}} for the viewer's own packing lists (D-123: packing
    is per-user, so a shared trip never shows someone else's progress). Trips with no
    packing items are absent. `omitted` and `removed` lines don't count toward total."""
    ids = list(trip_ids)
    if not ids:
        return {}
    uid = user.id if user else None
    rows = (session.query(PackingItem.trip_id, PackingItem.status)
            .filter(PackingItem.trip_id.in_(ids), PackingItem.user_id == uid,
                    PackingItem.status.in_(("draft", "packed"))).all())
    out: dict[str, dict] = {}
    for tid, status in rows:
        d = out.setdefault(tid, {"packed": 0, "total": 0, "pct": 0})
        d["total"] += 1
        d["packed"] += status == "packed"
    for d in out.values():
        d["pct"] = round(100 * d["packed"] / d["total"])
    return out


def _trip_display(t: Trip, activities=(), today: str | None = None, packing=None) -> dict:
    notes = (t.notes or "").strip()
    today = today or date.today().isoformat()
    return {
        "id": t.id, "name": t.name,
        "start_display": fmt_date(t.start_date) or "—",
        "end_display": fmt_date(t.end_date) or "—",
        "notes_preview": notes or "—",
        "codes": trip_route_codes(activities),
        "when": trip_when(t, today),
        "item_count": len(activities),
        "packing": packing,
        "range_short": " – ".join(x for x in (fmt_date_short(t.start_date),
                                              fmt_date_short(t.end_date)) if x),
    }


def _list_context(session: Session, q: str, status: str, user=None) -> dict:
    from app import clock
    today = clock.today_for(session, user)          # the viewer's own date (D-187)
    tab = "past" if status == "past" else "upcoming"   # always default: Upcoming
    # Phase 2: only trips the user may see (own + open + shared); no-op when
    # auth is off or the user is admin.
    trips = session.query(Trip).filter(scope.trip_filter(session, user)).all()
    total = len(trips)
    ql = q.lower().strip()
    if ql:
        trips = [t for t in trips if ql in (t.name or "").lower()]
    annotated = [(t, trip_status(t, today)) for t in trips]
    in_tab = {k: [(t, st) for (t, st) in annotated if (st == "past") == (k == "past")]
              for k, _ in TRIP_TABS}

    acts_by_trip: dict[str, list] = {}
    if trips:
        for a in (session.query(TripActivity)
                  .filter(TripActivity.trip_id.in_([t.id for t in trips])).all()):
            acts_by_trip.setdefault(a.trip_id, []).append(a)

    pack = packing_progress(session, [t.id for t in trips], user)

    def disp(t):
        d = _trip_display(t, acts_by_trip.get(t.id, ()), today, pack.get(t.id))
        # The card's pencil only where the edit would be accepted: not a
        # view-only share (D-122), not a demo server (D-198, YONDER-261).
        d["can_edit"] = scope.can_edit_trip(session, user, t)
        return d

    groups = []
    if tab == "upcoming":
        for key, label in STATUS_LABELS:
            if key == "past":
                continue
            # In progress first, then upcoming — both ascending by start.
            members = sorted((t for (t, st) in in_tab[tab] if st == key),
                             key=lambda t: t.start_date or "")
            if members:
                groups.append({"label": label,
                               "trips": [disp(t) for t in members]})
    else:
        # Most recent first, one group per year.
        members = sorted((t for (t, _) in in_tab[tab]),
                         key=lambda t: t.start_date or t.end_date or "", reverse=True)
        for t in members:
            year = (t.start_date or t.end_date or "")[:4] or "Undated"
            if not groups or groups[-1]["label"] != year:
                groups.append({"label": year, "trips": []})
            groups[-1]["trips"].append(disp(t))

    other = "past" if tab == "upcoming" else "upcoming"
    return {
        "groups": groups,
        "has_visible": bool(groups),
        "has_any": total > 0,
        "search_no_results": total > 0 and not groups,
        "q": q, "status": tab,
        "tabs": [{"key": k, "label": l, "count": len(in_tab[k]), "active": k == tab}
                 for k, l in TRIP_TABS],
        # When a search misses in this tab but hits in the other, say so.
        "other_tab": {"key": other, "label": dict(TRIP_TABS)[other],
                      "count": len(in_tab[other])},
    }


def _activity_display(a: TripActivity, tagged: list[int] | None = None,
                      names: dict[int, str] | None = None) -> dict:
    """`tagged` is who this activity is for ([]/None = everyone) and `names`
    the trip's roster (id -> name). Names are only shown when the activity is
    for part of a multi-traveller trip."""
    t = activity_types.get_type(a.type)
    names = names or {}
    who_ids = [tid for tid in (tagged or []) if tid in names]
    v = a.values or {}
    when = v.get("when") or ""
    end_when = v.get("endWhen") or ""
    multi_day = bool(when and end_when and when[:10] != end_when[:10])
    return {
        "id": a.id, "type": a.type, "type_label": t.label,
        "title": t.title(v), "subtitle": t.subtitle(v),
        "time_label": fmt_time_only(when),
        "multi_day": multi_day,
        "span_label": ("→ " + fmt_date_short(end_when)) if multi_day else "",
        "fields": activity_types.detail_fields(a.type, v),
        "map_url": activity_types.map_url(a.type, v),
        # The typed start ("Check-in: Apr 10, 3:00 PM") — the PDF pairs it with
        # the end field when there is one.
        "start_label": activity_types.time_labels(a.type)[0] or "Start",
        "start_display": fmt_datetime(when),
        "has_end": bool(end_when),
        "traveler_ids": who_ids,
        "traveler_names": [names[tid] for tid in who_ids],
        "geo": a.geo,                    # located points / route line (geo.py)
    }


def _roster_names(session: Session, trip_id: str) -> dict[int, str]:
    ids = who.roster_ids(session, trip_id)
    if not ids:
        return {}
    return {t.id: t.display_name for t in session.query(TravelerIdentity)
            .filter(TravelerIdentity.id.in_(ids)).all()}


def _sharing_context(session: Session, trip: Trip, user=None) -> dict:
    """Phase 2 (D-121/D-122): the "Sharing" panel data. None when auth is off.
    Two tiers: travellers-on-the-trip who are users get FULL access implicitly;
    explicit shares are VIEW-ONLY for non-travellers. Owner/admin manage shares."""
    if user is None:
        return {"sharing": None}
    from app.models import TripMember, TripTraveler, User
    # Explicit view-only shares.
    member_ids = {m.user_id for m in
                  session.query(TripMember).filter_by(trip_id=trip.id).all()}
    members = [u for u in (session.get(User, mid) for mid in member_ids) if u]
    # Travellers on this trip who are users → implicit full access.
    roster_tids = {r.traveler_id for r in
                   session.query(TripTraveler).filter_by(trip_id=trip.id).all()}
    traveller_users = (session.query(User).filter(User.traveler_id.in_(roster_tids)).all()
                       if roster_tids else [])
    traveller_user_ids = {u.id for u in traveller_users}
    owner = session.get(User, trip.owner_id) if trip.owner_id else None
    from app import demo
    # Not on a demo server (D-198, YONDER-261): the share would be refused.
    can_share = bool(user.is_admin or (trip.owner_id and trip.owner_id == user.id)) \
        and not demo.read_only()
    # An explicit share only makes sense for a user who isn't already the owner,
    # a traveller (full access already), or shared.
    add_choices = [u for u in session.query(User).order_by(User.username).all()
                   if u.id != trip.owner_id and u.id not in traveller_user_ids
                   and u.id not in member_ids]
    return {"sharing": {
        "owner": owner.username if owner else None,
        "is_open": trip.owner_id is None,
        "travellers": [u.username for u in traveller_users if u.id != trip.owner_id],
        "members": [{"id": u.id, "username": u.username} for u in members],
        "can_share": can_share,
        "add_choices": [{"id": u.id, "username": u.username} for u in add_choices],
        # YONDER-186/165: an ownerless trip is admin-only until an admin gives
        # it an owner.
        "owner_choices": ([{"id": u.id, "username": u.username}
                           for u in session.query(User).filter(User.is_active.is_(True))
                           .order_by(User.username).all()]
                          if trip.owner_id is None and user.is_admin else []),
    }}


def timeline_day_groups(session: Session, trip: Trip) -> list[dict]:
    """The timeline as day groups of display-ready activities — shared by the
    trip page and the PDF export."""
    acts = list(trip_activities(session, trip.id))
    # Sort by the denormalized starts_at; '' (undated) sorts to the end.
    acts.sort(key=lambda a: (a.starts_at or "￿"))
    names = _roster_names(session, trip.id)
    tags = who.activity_map(session, [a.id for a in acts]) if len(names) > 1 else {}
    day_groups: list[dict] = []
    index: dict[str, dict] = {}
    for a in acts:
        key = (a.starts_at or "")[:10] or "_unscheduled"
        grp = index.get(key)
        if grp is None:
            grp = {"label": "Date not set" if key == "_unscheduled"
                   else fmt_date(key), "day": "" if key == "_unscheduled" else key,
                   "activities": []}
            index[key] = grp
            day_groups.append(grp)
        grp["activities"].append(_activity_display(a, tags.get(a.id), names))
    return day_groups


def _detail_context(session: Session, trip: Trip, user=None) -> dict:
    al = _al_clients(session, user)
    day_groups = timeline_day_groups(session, trip)
    acts = [a for g in day_groups for a in g["activities"]]
    notes = (trip.notes or "").strip()
    # D-199 (YONDER-185): a view-only share sees the trip without its edit
    # controls, packing or travellers' profile details. The server still refuses
    # its writes (scope.trip_or_none); this only stops offering them.
    my_role = scope.trip_role(session, user, trip) or "viewer"
    return {
        "my_role": my_role,
        "view_only": my_role == "viewer",
        # No edit controls: a viewer, or anyone on a demo server (D-198), where
        # can_edit_trip is always false but every tab still shows.
        "read_only": not scope.can_edit_trip(session, user, trip),
        "trip": {
            "id": trip.id, "name": trip.name,
            "start_display": fmt_date(trip.start_date) or "—",
            "end_display": fmt_date(trip.end_date) or "—",
            "notes": notes, "has_notes": bool(notes),
            "adventurelog_collection_id": trip.adventurelog_collection_id,
            "adventurelog_url": _collection_url(trip.adventurelog_collection_id,
                                                al.public_url),
        },
        "day_groups": day_groups,
        "has_activities": bool(acts),
        "next_activity_id": _next_activity_id(session, trip, user),
        "inbox_waiting": _inbox_waiting(session, trip, user),
        "linked_captures": _linked_captures(session, trip.id),
        **_push_state(trip, al),
        **_travelers_context(session, trip.id),
        **_discovery_context(session, trip.id, user.id if user else None),
        **_packing_context(session, trip.id, user.id if user else None),
        **_sharing_context(session, trip, user),
        **_costs_context(session, trip, user),
        "journal": _journal_context(session, trip, user),
    }


def _inbox_waiting(session: Session, trip: Trip, user) -> int:
    """D-200: the viewer's captures filed to this trip and still in the Inbox, for
    the header's "N waiting in Inbox" link (the Inbox stays where you approve)."""
    from app.review.routes import waiting_for_trip
    try:
        return waiting_for_trip(session, user, trip.id)
    except Exception:                       # pragma: no cover - a hint, never a failure
        return 0


def _next_activity_id(session: Session, trip: Trip, user) -> str | None:
    """The activity holding the trip's next moment for the viewer (D-172), whose
    node on the timeline is lit (YONDER-262, design README §6). The same rule and
    clock as Home's Next-up card, so the two always agree."""
    from app import clock
    from app.dashboard import viewer_moments
    from app.trips import nextup
    try:
        moment, _ = viewer_moments(session, [trip], nextup.now_key(clock.now_for(session, user)),
                                   user)
    except Exception:                       # pragma: no cover - decoration only
        return None
    return moment.activity.id if moment else None


def _journal_context(session: Session, trip: Trip, user) -> list[dict]:
    """The viewer's OWN journal entries linked to this trip (YONDER-215),
    grouped by the day they're about, newest first. Own-only, stricter than the
    Journal page's filter: sharing a trip never shows anyone else's journal —
    an admin's or a co-traveller's (D-134). Auth off (no user) is the single
    owner and sees every entry."""
    from app.models import JournalEntry
    q = session.query(JournalEntry).filter(JournalEntry.trip_id == trip.id)
    if user is not None:
        q = q.filter(JournalEntry.owner_id == user.id)
    groups: list[dict] = []
    for e in q.order_by(JournalEntry.entry_date.desc(), JournalEntry.captured_at.desc()):
        if not groups or groups[-1]["date"] != e.entry_date:
            groups.append({"date": e.entry_date, "display": fmt_date(e.entry_date) or e.entry_date,
                           "entries": []})
        groups[-1]["entries"].append({
            "id": e.id, "body": e.body or "",
            "time": e.captured_at.strftime("%H:%M") if e.captured_at else "",
            "place": e.place_label or "",
            "tags": (e.values or {}).get("tags") or []})
    return groups


def _costs_context(session: Session, trip: Trip, user) -> dict:
    from app.trips.costs import costs_context       # costs.py imports this module
    return costs_context(session, trip, user)


def _linked_captures(session: Session, trip_id: str) -> list[dict]:
    """Captured review items linked to this Yonder trip (2.1.2). Read-only here —
    the link is made and unmade in the review queue's trip picker."""
    from app.models import TravelItem
    from app.review import summary as rs
    rows = (session.query(TravelItem)
            .filter(TravelItem.trip_id == trip_id)
            .order_by(TravelItem.created_at).all())
    out = []
    for c in rows:
        payload = (c.extraction or {}).get("payload") or {}
        out.append({"id": c.id, "kind": rs.kind_label(payload),
                    "headline": rs.headline(payload), "subline": rs.subline(payload)})
    return out


# --------------------------------------------------------------- travelers (3.2)
# Who is on this trip (D-94): a structured TripTraveler roster replacing the old
# free-text `traveling_with` hint. Discovery reads it for each person's profile
# and the `self` traveller's nationality (visa resolver).

_ROLE_LABELS = {"self": "You", "companion": "Companion"}


def _traveler_view(t: TravelerIdentity, role: str, has_account: bool = False) -> dict:
    return {"id": t.id, "name": t.display_name, "kind": t.kind, "role": role,
            "role_label": _ROLE_LABELS.get(role, role.title()),
            "nationality": t.nationality, "generic": bool(t.generic),
            # D-199: can this person sign in (a linked account), or are they a
            # guest who is only on the roster?
            "has_account": has_account}


def discovery_travelers(session: Session, trip_id: str) -> list[TravelerIdentity]:
    """The trip's travellers that discovery/planning should CONSIDER (D-100):
    every roster member except generic guests, which are roster placeholders
    carrying no profile or nationality. Future resolvers read the roster through
    this helper so a guest name never leaks into a recommendation."""
    rows = (session.query(TripTraveler)
            .filter(TripTraveler.trip_id == trip_id).all())
    ids = [r.traveler_id for r in rows]
    if not ids:
        return []
    return (session.query(TravelerIdentity)
            .filter(TravelerIdentity.id.in_(ids),
                    TravelerIdentity.generic.is_(False))
            .order_by(TravelerIdentity.display_name).all())


def _viewer_uid(request) -> str | None:
    """Whose Discover/Packing this is (4.1): the signed-in user; None with auth
    off (the single-user mode those rows use)."""
    u = scope.user_of(request)
    return u.id if u else None


def _viewer_traveler(session: Session, request) -> TravelerIdentity | None:
    """The viewing person's traveller — their nationality, base currency and
    discovery profile shape their Discover tab. Auth off: the `self` traveller.
    A signed-in user with no traveller linked: None."""
    return _traveler_of(session, scope.user_of(request))


def _traveler_of(session: Session, user) -> TravelerIdentity | None:
    """_viewer_traveler for a known user (or None = auth off) — the API's way in."""
    if user is None:
        return _self_identity(session)
    tid = getattr(user, "traveler_id", None)
    return session.get(TravelerIdentity, tid) if tid is not None else None


def _self_identity(session: Session) -> TravelerIdentity | None:
    return (session.query(TravelerIdentity)
            .filter(TravelerIdentity.kind == "self")
            .order_by(TravelerIdentity.id).first())


def _auto_link_self(session: Session, trip_id: str) -> None:
    """Link the `self` traveller to a trip on creation (§11.2). No-op when there
    is no self identity yet, or the link already exists."""
    ident = _self_identity(session)
    if ident is None:
        return
    if session.get(TripTraveler, {"trip_id": trip_id,
                                  "traveler_id": ident.id}) is None:
        session.add(TripTraveler(trip_id=trip_id, traveler_id=ident.id,
                                 role="self"))


def _travelers_context(session: Session, trip_id: str) -> dict:
    rows = (session.query(TripTraveler)
            .filter(TripTraveler.trip_id == trip_id).all())
    roles = {r.traveler_id: r.role for r in rows}
    idents = ({t.id: t for t in session.query(TravelerIdentity)
               .filter(TravelerIdentity.id.in_(roles)).all()} if roles else {})
    # self first, then companions alphabetically. Orphan rows (identity deleted)
    # are skipped silently.
    from app.models import User
    with_login = ({row[0] for row in session.query(User.traveler_id)
                   .filter(User.traveler_id.in_(roles)).all()} if roles else set())
    travelers = [_traveler_view(idents[tid], role, tid in with_login)
                 for tid, role in roles.items() if tid in idents]
    travelers.sort(key=lambda v: (v["role"] != "self", v["name"].lower()))
    avail_q = session.query(TravelerIdentity)
    if roles:
        avail_q = avail_q.filter(~TravelerIdentity.id.in_(roles))
    available = avail_q.order_by(TravelerIdentity.display_name).all()
    return {
        "trip_id": trip_id,
        "travelers": travelers,
        "travelers_available": [{"id": t.id, "name": t.display_name}
                                for t in available],
        "travelers_manage_url": "/admin/traveler-identity/list",
    }


def _al_clients(session: Session, user):
    """The acting person's own integration clients (SPEC-8 D-150) — the trip
    page's AdventureLog push / sync / pull use their connection, never a
    server-wide one."""
    from app import integrations
    return integrations.clients_for_user(session, getattr(user, "id", None))


def _adventurelog(session: Session, request: Request):
    """The acting person's AdventureLog client, or None when they haven't
    connected AdventureLog (Settings → Connections)."""
    return _al_clients(session, scope.user_of(request)).adventurelog


def _photon():
    from app import demo
    if demo.enabled():
        # D-198: a demo server places its sample trips from the seed's own
        # table; it never reaches a geocoder.
        from app.demo_seed import SeedGeocoder
        return SeedGeocoder()
    from resolve.geocode import PhotonGeocoder
    return PhotonGeocoder(settings.photon_url)


def _airport_resolver():
    """A `(code) -> Airport | None` lookup for location derivation
    (discover/places.py): Run·dere's own airport table (D-147), which needs no
    integration and covers every IATA airport. An unknown code drops and
    geocoding carries the rest."""
    from resolve import airports as airports_mod
    return airports_mod.lookup


def _collection_url(collection_id: str | None, base: str | None) -> str | None:
    """Browser-facing link to a collection on the viewer's AdventureLog (their
    connection's browser address), or None when either isn't there."""
    base = (base or "").rstrip("/")
    if not (base and collection_id):
        return None
    return f"{base}/collections/{collection_id}"


def _push_state(trip: Trip, clients) -> dict:
    """What the detail header shows for AdventureLog: a link if already pushed,
    a Push button if the viewer has an AdventureLog connection (D-146: the whole
    menu stays hidden for everyone else), or a disabled button + reason."""
    linked = bool(trip.adventurelog_collection_id)
    configured = clients.adventurelog is not None
    has_dates = bool(trip.start_date and trip.end_date)
    reason = ""
    if not linked:
        if not configured:
            reason = _NOT_CONNECTED
        elif not has_dates:
            reason = "Add a start and end date first — AdventureLog needs both."
    return {
        "al_linked": linked,
        "al_connected": configured,
        "al_show_push": configured and not linked,
        "al_can_push": configured and not linked and has_dates,
        "al_push_reason": reason,
    }


def trip_activities(session: Session, trip_id: str):
    return session.query(TripActivity).filter(
        TripActivity.trip_id == trip_id).all()


def _redirect(url: str) -> HTMLResponse:
    """HTMX client-side navigation — the post's answer is 'go here now'."""
    resp = HTMLResponse("")
    resp.headers["HX-Redirect"] = url
    return resp


def _refresh() -> HTMLResponse:
    """Reload the page the action came from, keeping its #section (4.5: a
    Discover action returns to the same section and city — a redirect that only
    changed the #fragment wouldn't reload at all)."""
    resp = HTMLResponse("")
    resp.headers["HX-Refresh"] = "true"
    return resp


# ----------------------------------------------------------------- list / detail

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", status: str = "upcoming",
          session: Session = Depends(get_session)):
    ctx = {"request": request, **_list_context(session, q, status, scope.user_of(request))}
    return templates.TemplateResponse(request, "list.html", ctx)


@router.get("/list", response_class=HTMLResponse)
def list_partial(request: Request, q: str = "", status: str = "upcoming",
                 session: Session = Depends(get_session)):
    """Tabs + groups fragment for the live search and the Upcoming/Past tabs."""
    ctx = {"request": request, **_list_context(session, q, status, scope.user_of(request))}
    return templates.TemplateResponse(request, "_groups.html", ctx)


@router.get("/close", response_class=HTMLResponse)
def close_modal():
    """Clear the modal slot (HTMX has no built-in 'remove', so swap empty)."""
    return HTMLResponse("")


def _show_al_field(session: Session, request: Request) -> bool:
    """The trip form's AdventureLog collection field is for people who connected
    AdventureLog (D-146: an optional extra, never asked of anyone else)."""
    return _adventurelog(session, request) is not None


@router.get("/new", response_class=HTMLResponse)
def new_trip(request: Request, next: str = "/trips",
             session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "_trip_modal.html", {
        "request": request, "mode": "create", "title": "New trip",
        "action": "/trips", "next": next, "show_al": _show_al_field(session, request),
        "form": {"name": "", "start": "", "end": "", "notes": "",
                 "collection_id": ""}, "error": None})


@router.get("/{trip_id}", response_class=HTMLResponse)
def detail(trip_id: str, request: Request,
           session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("<p class='bp-empty'>That trip no longer exists.</p>",
                            status_code=404)
    ctx = {"request": request, **_detail_context(session, trip, scope.user_of(request))}
    return templates.TemplateResponse(request, "detail.html", ctx)


@router.get("/{trip_id}/timeline.pdf")
def timeline_pdf(trip_id: str, request: Request, download: bool = False,
                 session: Session = Depends(get_session)):
    """The Timeline tab as a shareable PDF itinerary (same visibility rules as
    the trip page)."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("<p class='bp-empty'>That trip no longer exists.</p>",
                            status_code=404)
    from app.trips import pdf
    return pdf.pdf_response(session, trip, download=download)


@router.post("/{trip_id}/share", response_class=HTMLResponse)
def share_trip(trip_id: str, request: Request, user_id: str = Form(""),
               session: Session = Depends(get_session)):
    """Share this trip with another user (owner/admin only). Member = full access."""
    trip = scope.trip_or_none(session, request, trip_id)
    me = scope.user_of(request)
    if trip is None or me is None:
        return HTMLResponse("", status_code=404)
    if not (me.is_admin or trip.owner_id == me.id):
        return HTMLResponse("<p>Only the trip owner can share it.</p>", status_code=403)
    from app.models import TripMember, User
    target = session.get(User, user_id) if user_id else None
    if (target and target.id != trip.owner_id and session.query(TripMember)
            .filter_by(trip_id=trip.id, user_id=target.id).count() == 0):
        session.add(TripMember(trip_id=trip.id, user_id=target.id))
        session.commit()
    return _redirect(f"/trips/{trip.id}")


@router.post("/{trip_id}/owner", response_class=HTMLResponse)
def assign_owner(trip_id: str, request: Request, user_id: str = Form(""),
                 session: Session = Depends(get_session)):
    """Give an ownerless trip an owner (admin only, YONDER-165). Only for trips
    with NO owner — changing an existing owner isn't offered."""
    trip = scope.trip_or_none(session, request, trip_id)
    me = scope.user_of(request)
    if trip is None or me is None:
        return HTMLResponse("", status_code=404)
    if not me.is_admin:
        return HTMLResponse("<p>Only an admin can assign a trip's owner.</p>", status_code=403)
    if trip.owner_id is not None:
        return HTMLResponse("<p>This trip already has an owner.</p>", status_code=409)
    from app.models import User
    target = session.get(User, user_id) if user_id else None
    if target is None or not target.is_active:
        return HTMLResponse("<p>Pick an active user.</p>", status_code=400)
    trip.owner_id = target.id
    session.commit()
    log.info("trip %s assigned to owner %s by admin %s", trip.id, target.username, me.username)
    return _redirect(f"/trips/{trip.id}")


@router.post("/{trip_id}/unshare", response_class=HTMLResponse)
def unshare_trip(trip_id: str, request: Request, user_id: str = Form(""),
                 session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    me = scope.user_of(request)
    if trip is None or me is None:
        return HTMLResponse("", status_code=404)
    if not (me.is_admin or trip.owner_id == me.id):
        return HTMLResponse("<p>Only the trip owner can change sharing.</p>", status_code=403)
    from app.models import TripMember
    session.query(TripMember).filter_by(trip_id=trip.id, user_id=user_id).delete()
    session.commit()
    return _redirect(f"/trips/{trip.id}")


@router.get("/{trip_id}/derived-locations", response_class=HTMLResponse)
def derived_locations(trip_id: str, request: Request,
                      session: Session = Depends(get_session)):
    """The Refine Discovery sheet (D-68; renamed from "What Yonder found",
    YONDER-264): show the destination set Discovery
    DERIVED from this trip's own activities (D-62) — with per-place provenance
    and the places that couldn't be verified — so the operator sees the
    pipeline's starting point rather than guessing at it. On-demand modal, read
    from the same discover.places output a run would use, so it doubles as a
    pre-flight check."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_derived_locations.html",
                                      _derived_locations_ctx(request, session, trip))


def _excluded_keys(session: Session, trip_id: str, uid: str | None) -> set[str]:
    """Places this person chose not to research on this trip (per person, 4.1)."""
    return {e.place_key for e in session.query(DiscoveryExclusion)
            .filter(DiscoveryExclusion.trip_id == trip_id,
                    DiscoveryExclusion.user_id == uid).all()}


def discovery_places(session: Session, trip: Trip, uid: str | None) -> dict:
    """The places Discovery will research on this trip for one person (D-62/D-68):
    derived from the trip's own activities, each with where it came from, whether
    it's home or in the home country (skipped or narrowed), and whether this person
    excluded it (4.1: per person). Shared by the web sheet, /api/v1 and MCP
    (YONDER-264)."""
    from app.resolving import load_homes_for_user
    from discover.home_filter import home_country_set, is_home_place
    from discover.places import derive_locations
    acts = trip_activities(session, trip.id)
    result = derive_locations(acts, resolve_airport=_airport_resolver(),
                              geocoder=_photon())
    profiles = load_homes_for_user(session, uid)
    hc = home_country_set(result.places, profiles, trip.start_date)
    excluded = _excluded_keys(session, trip.id, uid)
    places = [{
        "label": p.label, "city": p.city, "country": p.country,
        "lat": p.lat, "lon": p.lon, "key": p.place_key,
        "sources": [f"{pr['activity_type']} · {pr['field']}"
                    for pr in p.provenance],
        "is_home": is_home_place(p, profiles, trip.start_date),
        "home_country": bool(p.country and p.country.upper() in hc),
        "excluded": p.place_key in excluded,
    } for p in result.places]
    return {"places": places, "countries": result.countries, "dropped": result.dropped}


def set_place_excluded(session: Session, trip_id: str, uid: str | None, place_key: str,
                       excluded: bool) -> None:
    """Exclude (or include again) one place from this person's Discovery on a trip.
    Idempotent. Excluding also drops that place's DRAFT findings for instant
    feedback; country-scope findings reconcile on the next run (the runner prunes
    stale drafts)."""
    existing = (session.query(DiscoveryExclusion)
                .filter(DiscoveryExclusion.trip_id == trip_id,
                        DiscoveryExclusion.user_id == uid,
                        DiscoveryExclusion.place_key == place_key).first())
    if not excluded:
        if existing is not None:
            session.delete(existing)
        session.commit()
        return
    if existing is None:
        session.add(DiscoveryExclusion(trip_id=trip_id, user_id=uid, place_key=place_key))
    for f in (session.query(DiscoveryFinding)
              .filter(DiscoveryFinding.trip_id == trip_id,
                      DiscoveryFinding.user_id == uid,
                      DiscoveryFinding.place_key == place_key,
                      DiscoveryFinding.status == "draft").all()):
        from discover import items as discovery_items
        discovery_items.delete_for_finding(session, f.id)
        session.delete(f)
    session.commit()


def _derived_locations_ctx(request: Request, session: Session, trip: Trip) -> dict:
    # Discover is per person (4.1): the viewer's homes and exclusions.
    found = discovery_places(session, trip, _viewer_uid(request))
    return {
        "request": request, "trip_id": trip.id, "trip_name": trip.name, **found,
        "has_home": any(p["is_home"] or p["home_country"] for p in found["places"]),
    }


@router.post("/{trip_id}/derived-locations/toggle", response_class=HTMLResponse)
def toggle_excluded_place(trip_id: str, place_key: str = Form(""),
                          request: Request = None,
                          session: Session = Depends(get_session)):
    """Exclude/include a derived place, then re-render the viewer. Excluding also
    drops that place's DRAFT findings for instant feedback; country-scope findings
    reconcile on the next run (the runner prunes stale drafts)."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None or not place_key:
        return HTMLResponse("", status_code=404)
    uid = _viewer_uid(request)
    now_excluded = place_key in _excluded_keys(session, trip_id, uid)
    set_place_excluded(session, trip_id, uid, place_key, not now_excluded)
    return templates.TemplateResponse(request, "_derived_locations.html",
                                      _derived_locations_ctx(request, session, trip))


# --------------------------------------------------------------- discovery (3.0)

TOPIC_ORDER = ["weather", "currency", "plugs", "visa", "safety", "events",
               "food", "language", "language_regional", "language_local", "packing",
               "research"]
TOPIC_LABELS = {
    "weather": "Weather", "currency": "Currency", "plugs": "Plugs & voltage",
    "visa": "Visa & vaccination", "safety": "Safety & transport",
    "events": "Events & activities", "food": "Food & drink", "language": "Language",
    "language_regional": "Regional language", "language_local": "Local phrases & register",
    "packing": "Packing",
    # YONDER-108: research saved by an AI agent over MCP/API (`save_research`,
    # POST /api/v1/trips/{id}/findings) — the caller's own, reviewed like any finding.
    "research": "Research (AI agents)",
}
def _iso_or_none(value: str | None) -> str | None:
    """The ISO 4217 code, or None for anything no currency picker would offer."""
    from app.expenses import currency_code
    try:
        return currency_code(value)
    except ValueError:
        return None


_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _bold_label(body_md: str | None) -> str:
    """The leading **bold** place label a finding body opens with (e.g.
    "Geneva, CH"), used to re-title a regenerated language finding."""
    m = _MD_BOLD.match((body_md or "").lstrip())
    return m.group(1) if m else ""


def _render_md(text: str | None) -> Markup:
    """Minimal, safe render of a finding's body: escape first, then a tiny
    subset — **bold**, "### " headings, "> " callouts, "- " bullets, line
    breaks. No markdown dependency; the raw markdown is what later crosses to an
    AdventureLog Note (the LLM resolvers lean on headings/callouts, so the
    renderer grew to match them)."""
    if not text:
        return Markup("")
    # Stash [text](http…url) links before escaping so their href survives intact,
    # then re-insert them as safe anchors at the end (href + text escaped).
    links: list[tuple[str, str]] = []

    def _stash(m):
        links.append((m.group(1), m.group(2)))
        return f"\x00L{len(links) - 1}\x00"

    html = str(escape(_MD_LINK.sub(_stash, text)))
    html = _MD_BOLD.sub(r"<strong>\1</strong>", html)
    out = []
    for line in html.split("\n"):
        stripped = line.lstrip()
        quote = stripped.startswith("&gt;")          # '>' escaped to '&gt;'
        if quote:
            stripped = stripped[4:].lstrip()
        if stripped.startswith("### "):
            content = f'<span class="yn-fh">{stripped[4:]}</span>'
        elif stripped.startswith("- "):
            content = "• " + stripped[2:]
        elif quote:
            content = stripped
        else:
            content = line
        if quote:
            content = f'<span class="yn-fq">{content}</span>'
        out.append(content)
    joined = "<br>".join(out)
    for i, (label, url) in enumerate(links):
        anchor = (f'<a href="{escape(url)}" target="_blank" rel="noopener">'
                  f'{escape(label)}</a>')
        joined = joined.replace(f"\x00L{i}\x00", anchor)
    return Markup(joined)


def _finding_view(f: DiscoveryFinding, ended: bool = False) -> dict:
    from discover import attribution as _attribution
    from discover import policy as _policy
    data = f.data or {}
    return {
        "id": f.id, "topic": f.topic, "place_key": f.place_key,
        "scope": f.scope, "status": f.status, "confidence": f.confidence,
        "body": _render_md(f.body_md), "sources": f.sources or [],
        # The data licence's required credit (Open-Meteo, CC BY 4.0), shown
        # once per section by _discovery_section.html — not tucked in Sources.
        "attribution": _attribution.for_finding(f.resolver),
        "is_currency": f.topic == "currency",
        # 4.5 Food/Events: the city write-up shown above the tables.
        "summary": _render_md(data.get("summary")) if data.get("summary") else None,
        "dishes": [d for d in (data.get("local_dishes") or []) if isinstance(d, str)],
        "warnings": [w for w in (data.get("allergy_warnings") or []) if isinstance(w, str)],
        "currency": data.get("currency") or "",
        "rates_date": data.get("date") or "",
        "parts": _safety_parts(data) if f.topic == "safety" else None,
        "is_language": f.topic == "language",
        # Language findings aren't per-finding re-runnable: national detection is
        # a pure function of country (re-run can only clobber a manual Languages
        # pick), and the regional/local overlays key on the trip's places. The
        # Languages picker is the tool for those; other topics keep Re-run.
        # Saved research (YONDER-108) has no resolver to re-run either.
        # A past trip runs nothing (D-157), so no finding offers a re-run either.
        "rerunnable": (not f.topic.startswith("language") and f.topic != "research"
                       and not ended),
        "ended": ended,
        "has_rates": bool(data.get("rates")),
        "lockable": _policy.is_lockable(f.topic),
        "locked": _policy.is_locked(f),
        "external": _policy.is_external(f),
        "agent": _policy.agent_label(f),
        # Superseded versions of saved research (YONDER-108), newest first.
        "history": [{"when": (h.get("generated_at") or "")[:10], "status": h.get("status"),
                     "agent": h.get("agent"),
                     "body": _render_md(h.get("body_md")), "sources": h.get("sources") or []}
                    for h in (data.get("history") or []) if isinstance(h, dict)],
    }


_KIND_LABELS = {
    "restaurant": "Restaurants", "cafe": "Cafés", "bar": "Bars", "pub": "Pubs",
    "brewery": "Breweries", "distillery": "Distilleries", "winery": "Wineries",
    "market": "Markets", "bakery": "Bakeries", "dessert": "Desserts",
    "street_food": "Street food",
}


def _when_label(i) -> str:
    """"Oct 14" / "Oct 14 – 16" / the source's own wording."""
    def fmt(v):
        try:
            d = datetime.strptime(v[:10], "%Y-%m-%d")
        except (TypeError, ValueError):
            return None
        out = d.strftime("%b %-d")
        return out + (f", {v[11:16]}" if len(v) > 10 else "")
    start, end = fmt(i.starts_on), fmt(i.ends_on)
    if start and end and end != start:
        return f"{start} – {end}"
    return start or (i.when_text or "")


def _item_view(i, on_timeline: dict) -> dict:
    from discover import items as disc_items
    src = next((s.get("url") for s in (i.sources or []) if s.get("url")), None)
    act = on_timeline.get(i.activity_id) if i.activity_id else None
    extra = i.extra or {}
    return {
        "id": i.id, "name": i.name, "kind": i.kind,
        "kind_label": _KIND_LABELS.get(i.kind, i.kind.replace("_", " ").title()),
        "neighbourhood": i.neighbourhood, "address": i.address,
        "what_to_get": i.what_to_get,
        "price": "$" * i.price_tier if i.price_tier else "",
        "when": _when_label(i), "sort_when": i.starts_on or "9999",
        "venue": i.venue, "details": i.details, "duration": i.duration,
        "booking": i.booking, "website": i.website, "source_url": src,
        "map_url": disc_items.map_url(i), "hidden": bool(i.hidden),
        "hidden_reason": (i.hidden_reason or disc_items.DEFAULT_HIDE_REASON) if i.hidden else None,
        "heads_up": bool(extra.get("heads_up")), "local_specialty": bool(extra.get("local_specialty")),
        "agent": i.agent if i.origin == "agent" else None,
        "on_timeline": act,   # {"when": "Oct 14"} or None
    }


def _item_cities(session: Session, trip_id: str, uid: str | None,
                 views: dict[str, list]) -> dict[str, list]:
    """Food & Drink / Events & Activities as cities (4.5): each city's finding
    (summary, status) plus its items grouped into tables. Keyed by section."""
    from app.models import DiscoveryItem
    rows = (session.query(DiscoveryItem)
            .filter(DiscoveryItem.trip_id == trip_id, DiscoveryItem.user_id == uid)
            .order_by(DiscoveryItem.name).all())
    act_ids = [r.activity_id for r in rows if r.activity_id]
    on_tl = {}
    for a in (session.query(TripActivity).filter(TripActivity.id.in_(act_ids)).all()
              if act_ids else []):
        on_tl[a.id] = {"when": _when_label(SimpleNamespace(starts_on=a.starts_at or None,
                                                           ends_on=None, when_text=""))}
    by_finding: dict[str, list] = {}
    for r in rows:
        by_finding.setdefault(r.finding_id, []).append(_item_view(r, on_tl))
    out: dict[str, list] = {}
    for topic in ("food", "events"):
        cities = []
        for f in views.get(topic, []):
            its = by_finding.get(f["id"], [])
            shown = [i for i in its if not i["hidden"]]
            if topic == "food":
                order = list(_KIND_LABELS)
                groups = [{"label": _KIND_LABELS[k], "kind": k,
                           "rows": [i for i in its if i["kind"] == k]} for k in order]
                groups += [{"label": "Other", "kind": "other",
                            "rows": [i for i in its if i["kind"] not in _KIND_LABELS]}]
            else:
                dated = sorted([i for i in its if i["kind"] == "event" and not i["heads_up"]],
                               key=lambda i: i["sort_when"])
                groups = [
                    {"label": "Heads-up while you're there", "kind": "heads_up",
                     "rows": [i for i in its if i["heads_up"]]},
                    {"label": "Events", "kind": "event", "rows": dated},
                    {"label": "Activities", "kind": "activity",
                     "rows": [i for i in its if i["kind"] == "activity"]},
                ]
            groups = [g for g in groups if g["rows"]]
            srcs, seen = [], set()
            for s in (f["sources"] or []) + [{"url": i["source_url"], "title": i["name"]}
                                             for i in its if i["source_url"]]:
                u = (s or {}).get("url")
                if u and u not in seen:
                    seen.add(u)
                    srcs.append({"url": u, "title": s.get("title") or u,
                                 "domain": urlparse(u).netloc.removeprefix("www.")})
            srcs.sort(key=lambda s: s["domain"])
            cities.append({"finding": f, "key": f["place_key"],
                           "label": f["place_key"].split(":", 1)[-1],
                           "groups": groups, "count": len(shown),
                           "hidden": len(its) - len(shown), "sources": srcs})
        out[topic] = cities
    return out


def _safety_parts(data: dict) -> dict | None:
    """Safety & Transport as headed subsections (4.5, YONDER-168) from the
    structured data the local resolver stores. None for a finding without it
    (e.g. an agent's markdown) — that one renders its text as before."""
    safety = data.get("safety") if isinstance(data.get("safety"), dict) else None
    transport = data.get("transport") if isinstance(data.get("transport"), dict) else None
    if not (safety or transport):
        return None

    def strs(xs):
        return [x.strip() for x in (xs or []) if isinstance(x, str) and x.strip()]

    out = {"safety": [], "transport": [], "single_source": False}
    if safety:
        if (safety.get("summary") or "").strip():
            out["safety"].append({"h": "Summary", "text": safety["summary"].strip()})
        adv = [{"text": a.get("text", "").strip(), "url": a.get("source_url")}
               for a in (safety.get("advisories") or [])
               if isinstance(a, dict) and (a.get("text") or "").strip()]
        if adv:
            out["safety"].append({"h": "Advisories", "links": adv})
        if strs(safety.get("scams")):
            out["safety"].append({"h": "Scams to know", "list": strs(safety.get("scams"))})
        out["single_source"] = safety.get("corroborated") is False
    if transport:
        for key, h in (("summary", "Getting around"), ("public_transit", "Public transit"),
                       ("from_airport", "From the airport")):
            if isinstance(transport.get(key), str) and transport[key].strip():
                out["transport"].append({"h": h, "text": transport[key].strip()})
        for key, h in (("ride_hailing", "Ride-hailing that works here"),
                       ("apps", "Apps to install"), ("tips", "Tips")):
            if strs(transport.get(key)):
                out["transport"].append({"h": h, "list": strs(transport.get(key))})
    return out if (out["safety"] or out["transport"]) else None


def _run_view(run: DiscoveryRun) -> dict:
    return {"status": run.status, "phase": run.phase, "done": run.done,
            "total": run.total,
            "when": run.finished_at.strftime("%Y-%m-%d %H:%M")
            if run.finished_at else ""}


def _discovery_context(session: Session, trip_id: str, uid: str | None) -> dict:
    """The Discover tab for ONE person (4.1): their own findings and runs."""
    findings = (session.query(DiscoveryFinding)
                .filter(DiscoveryFinding.trip_id == trip_id,
                        DiscoveryFinding.user_id == uid).all())
    from discover import sections as disc_sections
    from discover.runner import trip_has_ended
    ended = trip_has_ended(session.get(Trip, trip_id))
    grouped: dict[str, list] = {}
    for f in findings:
        grouped.setdefault(f.topic, []).append(f)
    views: dict[str, list] = {}
    topic_groups = []
    for topic in TOPIC_ORDER:
        fs = grouped.get(topic)
        if fs:
            fs.sort(key=lambda x: x.place_key or "")
            views[topic] = [_finding_view(f, ended) for f in fs]
            topic_groups.append({"topic": topic,
                                 "label": TOPIC_LABELS.get(topic, topic),
                                 "findings": views[topic]})
    # 4.5: the currency calculator lives in the trip header (YONDER-166).
    currencies = [{"id": v["id"], "code": v["currency"], "place_key": v["place_key"]}
                  for v in views.get("currency", []) if v["has_rates"]]
    last = (session.query(DiscoveryRun)
            .filter(DiscoveryRun.trip_id == trip_id, DiscoveryRun.user_id == uid)
            .order_by(DiscoveryRun.started_at.desc()).first())
    running = last is not None and last.status == "running"
    run_progress = None
    if running:
        pct = int(100 * last.done / last.total) if last.total else 0
        run_progress = {"phase": last.phase or "starting", "done": last.done,
                        "total": last.total, "pct": pct}
    secs = disc_sections.build(views)
    cities = _item_cities(session, trip_id, uid, views)
    from discover.weather_summary import summarize as weather_summary
    weather = weather_summary([((f.place_key or "").split(":", 1)[-1], f.data)
                               for f in grouped.get("weather", [])])
    for s in secs:
        if s["key"] in cities:
            s["cities"] = cities[s["key"]]
        if s["key"] == "weather":
            s["summary"] = weather
    return {"discovery_topics": topic_groups,
            "discovery_sections": secs,
            "discovery_default_section": disc_sections.DEFAULT_SECTION,
            "trip_currencies": currencies,
            "discovery_has_findings": bool(findings),
            "discovery_ended": ended,
            "discovery_last": _run_view(last) if last else None,
            "discovery_running": running,
            "discovery_run": run_progress,
            "hide_reasons": _hide_reasons()}


def _hide_reasons():
    from discover.items import HIDE_REASONS
    return HIDE_REASONS


def _resolvers():
    from discover.resolvers import build_deterministic
    return build_deterministic(
        frankfurter_url=settings.frankfurter_url,
        open_meteo_url=settings.open_meteo_url,
        open_meteo_archive_url=settings.open_meteo_archive_url)


def _llm_resolvers():
    from discover.resolvers import build_llm
    return build_llm(searxng_url=settings.searxng_url, ollama_url=settings.ollama_url,
                     model=settings.ollama_model, timeout=settings.llm_timeout_seconds)


def _discovery_section(request: Request, session: Session, trip: Trip) -> HTMLResponse:
    ctx = {"request": request, "trip": {"id": trip.id},
           **_discovery_context(session, trip.id, _viewer_uid(request))}
    return templates.TemplateResponse(request, "_discovery_section.html", ctx)


def _base_currencies(traveler: TravelerIdentity | None) -> tuple[str, ...]:
    """Base currencies the currency resolver + calculator convert FROM (D-65),
    for ONE person (4.1 — Discover is per person):
      1. their EXPLICIT base currency from their travel profile
         (TravelerIdentity.base_currency) — not derived from nationality or home;
      2. then the deployment's ALWAYS-ON base currencies
         (TX_DISCOVERY_BASE_CURRENCIES), for reference (e.g. USD for Cuba).
    With no pick, just the deployment set."""
    codes: list[str] = []
    c = ((traveler.base_currency if traveler else None) or "").strip().upper()
    if c:
        codes.append(c)
    for c in settings.base_currency_list:          # always-on deployment extras
        if c not in codes:
            codes.append(c)
    return tuple(codes)


@router.post("/{trip_id}/discover", response_class=HTMLResponse)
def run_discovery_route(trip_id: str, request: Request,
                        session: Session = Depends(get_session)):
    """Launch a Discovery pass — BOTH tiers on a background worker (§6.1) — and
    return the self-polling section. The deterministic findings land in the first
    few seconds; the LLM findings stream in as each resolver finishes. A run is
    already in flight? Just show its panel (one run per person per trip).

    Per person (4.1): the run uses the VIEWER's discovery profile, nationality,
    base currency, homes and excluded places, and its findings are theirs."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    start_discovery(session, trip, scope.user_of(request))   # ended: the section says why
    return _discovery_section(request, session, trip)


def discovery_topics() -> set[str]:
    """Every topic a run can refresh: the deterministic and LLM resolvers'."""
    return {r.topic for r in [*_resolvers(), *_llm_resolvers()]}


def start_discovery(session: Session, trip: Trip, user, *, topic: str | None = None,
                    place_key: str | None = None) -> bool:
    """Launch `user`'s Discovery run on a trip they can see — the ONE launch the
    web button and /api/v1 (YONDER-238) share. `user` None = auth off. False when
    the trip has ended (D-157: refused server-side, not just a hidden button);
    True otherwise, including when their run is already in flight (one run per
    person per trip — no second run is started). `topic`/`place_key` narrow it
    to a targeted refresh (discovery_jobs.launch); a whole-trip or targeted run
    respects locked, rejected and agent-written findings the same way."""
    from app.resolving import load_homes_for_user
    from app.trips import discovery_jobs as dj
    from datetime import date as _date
    from discover.runner import trip_has_ended
    if trip_has_ended(trip):
        return False
    uid = user.id if user else None
    if dj.running_run(session, trip.id, uid) is None:
        ident = _traveler_of(session, user)
        profile = _discovery_profile_row(session, ident, uid is None)
        dj.launch(trip.id, resolve_airport=_airport_resolver(), geocoder=_photon(),
                  det_resolvers=_resolvers(), llm_resolvers=_llm_resolvers(),
                  home_profiles=load_homes_for_user(session, uid),
                  excluded=_excluded_keys(session, trip.id, uid),
                  discovery_profile=(profile.values if profile else None),
                  nationality=(ident.nationality if ident else None),
                  base_currencies=_base_currencies(ident),
                  today=_date.today().isoformat(), user_id=uid,
                  topic=topic, place_key=place_key)
    return True


@router.get("/{trip_id}/discover/status", response_class=HTMLResponse)
def discovery_status_route(trip_id: str, request: Request,
                           session: Session = Depends(get_session)):
    """Poll target for the running-run panel. Reading it also settles an orphaned
    run (restart → failed). When the run is no longer `running`, the returned
    section carries no poll trigger, so the panel stops on its own."""
    from app.trips import discovery_jobs as dj
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    dj.running_run(session, trip_id, _viewer_uid(request))   # settle orphans
    return _discovery_section(request, session, trip)


def _finding_or_404(session: Session, request: Request, trip_id: str,
                    fid: str) -> DiscoveryFinding | None:
    """A finding on a trip the viewer can access, and only their OWN (4.1).
    Anything else reads as missing."""
    f = session.get(DiscoveryFinding, fid)
    if f is None or f.trip_id != trip_id or f.user_id != _viewer_uid(request):
        return None
    if scope.trip_or_none(session, request, trip_id) is None:
        return None
    return f


def _item_or_none(session: Session, request: Request, trip_id: str, item_id: str):
    """A Discover item on a trip the viewer can access, and only their OWN."""
    from app.models import DiscoveryItem
    if not item_id:
        return None
    i = session.get(DiscoveryItem, item_id)
    if i is None or i.trip_id != trip_id or i.user_id != _viewer_uid(request):
        return None
    if scope.trip_or_none(session, request, trip_id) is None:
        return None
    return i


@router.get("/{trip_id}/discovery-item/{item_id}/to-timeline", response_class=HTMLResponse)
def item_to_timeline_form(trip_id: str, item_id: str, request: Request,
                          session: Session = Depends(get_session)):
    """The activity form, filled in from a Discover item (4.5): Restaurant for
    food, Event on its date, Tour / activity otherwise. The person sets the time."""
    from discover import items as disc_items
    i = _item_or_none(session, request, trip_id, item_id)
    if i is None:
        return HTMLResponse("", status_code=404)
    atype, values = disc_items.activity_for(i)
    ctx = _activity_modal_ctx(request, trip_id, mode="create",
                              action=f"/trips/{trip_id}/activity", atype=atype,
                              values=values, session=session)
    ctx.update(title=f"Add to timeline — {i.name}", discovery_item_id=i.id)
    return templates.TemplateResponse(request, "_activity_modal.html", ctx)


@router.post("/{trip_id}/discovery-item/{item_id}/hide", response_class=HTMLResponse)
def item_hide(trip_id: str, item_id: str, request: Request, hidden: str = Form("1"),
              reason: str = Form(""), session: Session = Depends(get_session)):
    """Hide (or bring back) a Discover row. Hidden rows stay hidden when Discover
    or an agent refreshes the city. The reason defaults to "not my thing" (what
    agents read as a dislike); "closed" and "been there" say nothing about taste."""
    from discover import items as disc_items
    i = _item_or_none(session, request, trip_id, item_id)
    if i is None:
        return HTMLResponse("", status_code=404)
    if hidden == "1":
        disc_items.hide(i, reason or None)
    else:
        disc_items.unhide(i)
    session.commit()
    return _discovery_section(request, session, scope.trip_or_none(session, request, trip_id))


@router.post("/{trip_id}/finding/{fid}/rerun", response_class=HTMLResponse)
def rerun_finding_route(trip_id: str, fid: str, request: Request,
                        session: Session = Depends(get_session)):
    """Regenerate this one finding (the per-topic re-run). Re-fetches live topics
    (weather/currency) and overrides a manual edit — an explicit fresh copy."""
    from discover.runner import rerun_finding, trip_has_ended
    f = _finding_or_404(session, request, trip_id, fid)
    if f is None:
        return HTMLResponse("", status_code=404)
    if trip_has_ended(session.get(Trip, trip_id)):         # D-157; reload shows the reason
        return _refresh()
    # Language findings aren't re-runnable (see _finding_view) — never let a
    # stale/forged POST overwrite a manual Languages pick.
    if f.topic.startswith("language") or f.topic == "research":
        return _refresh()
    rerun_finding(session, f, resolve_airport=_airport_resolver(),
                  geocoder=_photon(), resolvers=_resolvers(),
                  base_currencies=_base_currencies(_viewer_traveler(session, request)))
    return _refresh()


@router.get("/{trip_id}/finding/{fid}/edit", response_class=HTMLResponse)
def edit_finding(trip_id: str, fid: str, request: Request,
                 session: Session = Depends(get_session)):
    f = _finding_or_404(session, request, trip_id, fid)
    if f is None:
        return HTMLResponse("", status_code=404)
    # Language uses a structured picker, not free text (you pick from the vetted
    # list) — every other topic edits as markdown.
    if f.topic == "language":
        from discover.resolvers.language import supported_languages
        from discover import langdata
        chosen = {l.get("code") for l in (f.data or {}).get("languages", [])}
        options = [{"code": c, "name": n, "checked": c in chosen}
                   for c, n in supported_languages(langdata.phrasebook(session))]
        return templates.TemplateResponse(request, "_language_edit.html", {
            "request": request, "trip_id": trip_id, "fid": fid,
            "place_key": f.place_key, "options": options})
    # Currency edits the actual conversion numbers (data.rates), not just the
    # markdown — a text-only edit would leave the calculator computing from the
    # stale snapshot. No resolved currency code (Babel couldn't map the country)
    # → nothing structured to edit, fall through to the markdown editor.
    data = f.data or {}
    if f.topic == "currency" and data.get("currency"):
        code = data["currency"]
        per = (data.get("rates") or {}).get(code, {})
        bases = data.get("bases") or list(settings.base_currency_list)
        rows = [{"base": b, "value": per.get(f"per_{b}")} for b in bases]
        return templates.TemplateResponse(request, "_currency_edit.html", {
            "request": request, "trip_id": trip_id, "fid": fid,
            "code": code, "place_key": f.place_key, "rows": rows})
    # Food/Events (4.5): the text you edit is the city summary above the tables;
    # the rows are edited in place, so the old generated list isn't offered.
    item_topic = f.topic in ("food", "events")
    return templates.TemplateResponse(request, "_finding_edit.html", {
        "request": request, "trip_id": trip_id, "fid": fid,
        "topic_label": ("City summary · " if item_topic else "")
                       + TOPIC_LABELS.get(f.topic, f.topic),
        "place_key": f.place_key,
        "body": (data.get("summary") or "") if item_topic else (f.body_md or "")})


@router.post("/{trip_id}/finding/{fid}/edit", response_class=HTMLResponse)
def save_finding(trip_id: str, fid: str, request: Request, body: str = Form(""),
                 langs: list[str] = Form(default=[]),
                 cur_base: list[str] = Form(default=[]),
                 cur_rate: list[str] = Form(default=[]),
                 session: Session = Depends(get_session)):
    f = _finding_or_404(session, request, trip_id, fid)
    if f is None:
        return HTMLResponse("", status_code=404)
    kept_history = (f.data or {}).get("history") if isinstance(f.data, dict) else None
    if f.topic == "language":
        # Rebuild the phrasebook from the picked languages (a manual override,
        # not "official"); marking edited protects it from a whole-trip re-run.
        from discover.resolvers.language import build_language_body
        from discover import langdata
        label = _bold_label(f.body_md) or f.place_key
        f.body_md, f.data, f.confidence = build_language_body(
            label, langs, official=False, phrasebook=langdata.phrasebook(session))
    elif f.topic == "currency" and (f.data or {}).get("currency"):
        # Rewrite the stored rate snapshot (units of `code` per 1 base) that the
        # calculator reads — the whole point of the fix — then regenerate the body
        # from it so text and math never diverge.
        from discover.resolvers.currency import render_currency_body
        from datetime import date as _date
        data = dict(f.data or {})
        code = data["currency"]
        per_base: dict[str, float] = {}
        for b, v in zip(cur_base, cur_rate):
            b, v = _iso_or_none(b), (v or "").strip()
            if b is None or not v:            # real ISO 4217 codes only (YONDER-293)
                continue
            try:
                fv = float(v)
            except ValueError:
                continue
            if fv > 0:
                per_base[b] = fv
        bases = [b for b in (_iso_or_none(x) for x in cur_base) if b is not None]
        as_of = _date.today().isoformat()
        data["date"] = as_of
        data["bases"] = bases
        data["rates"] = ({code: {f"per_{b}": per_base[b] for b in per_base}}
                         if per_base else {})
        f.data = data
        f.body_md = render_currency_body(
            _bold_label(f.body_md) or f.place_key, code,
            data.get("currency_name") or code, bases, per_base, as_of,
            bool(data.get("usd_useful")), source_note="manually edited")
        f.confidence = "high" if per_base else "flagged"
    elif f.topic in ("food", "events"):
        f.data = {**(f.data or {}), "summary": body.strip()}
        f.body_md = f"**{_bold_label(f.body_md) or f.place_key}**\n\n{body.strip()}"
    else:
        f.body_md = body
    if kept_history and not (f.data or {}).get("history"):
        f.data = {**(f.data or {}), "history": kept_history}   # an edit keeps earlier versions
    f.status = "edited"          # on a lockable topic this also LOCKS it (discover/policy.py)
    session.commit()
    return _refresh()


@router.post("/{trip_id}/finding/{fid}/lock", response_class=HTMLResponse)
def lock_finding(trip_id: str, fid: str, request: Request, lock: str = Form("1"),
                 session: Session = Depends(get_session)):
    """Lock (approve) or unlock a finding on a LOCKABLE topic (plugs, visa,
    languages). Locked: neither a local Discover run nor an AI agent overwrites it.
    Unlocking returns it to draft, so the next run may refresh it."""
    from discover import policy
    f = _finding_or_404(session, request, trip_id, fid)
    if f is None or scope.trip_or_none(session, request, trip_id) is None:
        return HTMLResponse("", status_code=404)
    if not policy.is_lockable(f.topic):
        return HTMLResponse("This topic can't be locked — it's refreshed on every run.",
                            status_code=400)
    f.status = "approved" if lock == "1" else "draft"
    session.commit()
    return _refresh()


@router.get("/{trip_id}/finding/{fid}/calculator", response_class=HTMLResponse)
def currency_calculator(trip_id: str, fid: str, request: Request,
                        session: Session = Depends(get_session)):
    """The per-trip currency calculator (D-65). Computes client-side from the
    STORED rate snapshot — no live call — pivoting every currency through one home
    base so any pair converts, including local↔local on a multi-country trip. The
    pivot is chosen from the trip's own base currencies (no hardcoded CAD), so a
    non-Canadian traveller's calculator works too.

    D-195 (owner, 2026-10-13): a currency on the trip's shared rate table uses
    its LATEST stored rate there, over the Discover snapshot; still no live
    call. "Update rates for today" (calculator_refresh) appears when a rate
    shown is older than today."""
    f = _finding_or_404(session, request, trip_id, fid)
    if f is None:
        return HTMLResponse("", status_code=404)
    trip = session.get(Trip, trip_id)
    cur_findings = (session.query(DiscoveryFinding)
                    .filter(DiscoveryFinding.trip_id == trip_id,
                            DiscoveryFinding.user_id == f.user_id,
                            DiscoveryFinding.topic == "currency").all())
    # rates[code][base] = units of `code` per 1 `base` (from the snapshots).
    rates: dict[str, dict[str, float]] = {}
    as_of = None
    for cf in cur_findings:
        d = cf.data or {}
        as_of = as_of or d.get("date")
        for code, per in (d.get("rates") or {}).items():
            rates[code] = {b.replace("per_", ""): v for b, v in per.items()}
    # The home base currencies the snapshots quote (what we convert FROM). Pick
    # the pivot as the base the most currencies quote, so the table stays
    # connected even after a manual edit trimmed a base from one finding.
    all_bases: list[str] = []
    for br in rates.values():
        for b in br:
            if b not in all_bases:
                all_bases.append(b)
    ordered_bases = ([b for b in settings.base_currency_list if b in all_bases]
                     + [b for b in all_bases if b not in settings.base_currency_list])
    pivot = max(ordered_bases, default=None,
                key=lambda B: sum(1 for br in rates.values() if br.get(B)))
    # per_pivot[X] = units of X per 1 pivot, so the client converts any pair as
    # amount / per_pivot[from] * per_pivot[to].
    per_pivot: dict[str, float] = {}
    if pivot is not None:
        per_pivot[pivot] = 1.0
        for code, br in rates.items():
            if br.get(pivot):
                per_pivot[code] = br[pivot]                  # code per 1 pivot
        for code, br in rates.items():                       # other home bases
            p = br.get(pivot)
            for b, v in br.items():
                if b not in per_pivot and p and v:
                    per_pivot[b] = p / v                     # b per 1 pivot
    # The trip's shared rate table wins for the currencies it has (D-195).
    from app import rates as rates_svc
    q = trip.expense_currency if trip else None
    latest = rates_svc.latest(session, trip) if trip else {}
    if latest and q not in per_pivot:
        bridge = next((c for c in latest if c in per_pivot), None)
        if bridge is not None:                  # q per pivot, via a shared currency
            per_pivot[q] = per_pivot[bridge] * float(latest[bridge].rate)
        elif not per_pivot:
            per_pivot[q] = 1.0                  # no snapshot at all: pivot on q
    trip_dates: dict[str, str] = {}
    if q in per_pivot:
        for code, row in latest.items():
            per_pivot[code] = per_pivot[q] / float(row.rate)   # code per 1 pivot
            trip_dates[code] = row.as_of or row.rate_date
        if q not in ordered_bases:
            ordered_bases.insert(0, q)
    # Dropdown order: home bases first, then destination currencies.
    bases = [b for b in ordered_bases if b in per_pivot]
    dests = sorted(c for c in per_pivot if c not in bases)
    currencies = bases + dests
    today = rates_svc.today()
    shown = [c for c in currencies if c != q]
    stale = bool(q) and any(trip_dates.get(c, "") < today for c in shown)
    import json as _json
    return templates.TemplateResponse(request, "_calculator.html", {
        "request": request, "trip_id": trip_id, "fid": fid,
        # Inside a <script>: escape <, > and & so no value can close the tag
        # (YONDER-293; currency codes were free text from the edit form).
        "currencies": currencies, "per_pivot_json": Markup(
            _json.dumps(per_pivot).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026")),
        "as_of": as_of or "—",
        "trip_dates": sorted(trip_dates.items()), "trip_currency": q,
        "can_update": stale and scope.can_edit_trip(session, scope.user_of(request), trip),
        "default_from": bases[0] if bases else (currencies[0] if currencies else ""),
        "default_to": dests[0] if dests else (currencies[-1] if currencies else ""),
    })


@router.post("/{trip_id}/finding/{fid}/calculator/update-rates", response_class=HTMLResponse)
def calculator_refresh(trip_id: str, fid: str, request: Request,
                       session: Session = Depends(get_session)):
    """"Update rates for today" (owner, 2026-10-13): fetch today's rate onto the
    trip's shared table for every currency the calculator shows, then redraw
    it. The person asked, so this is the one live call; costs keep their rates
    (D-195). Needs edit access — the table is the trip's, not theirs."""
    from app import rates as rates_svc
    f = _finding_or_404(session, request, trip_id, fid)
    trip = session.get(Trip, trip_id)
    if f is None or trip is None:
        return HTMLResponse("", status_code=404)
    if not trip.expense_currency:         # view-only was already refused (trip_or_none)
        return HTMLResponse("", status_code=400)
    codes = set()
    for cf in (session.query(DiscoveryFinding)
               .filter(DiscoveryFinding.trip_id == trip_id,
                       DiscoveryFinding.user_id == f.user_id,
                       DiscoveryFinding.topic == "currency").all()):
        codes.update((cf.data or {}).get("rates") or {})
    codes.update(rates_svc.latest(session, trip))
    for code in sorted(c for c in codes if c != trip.expense_currency):
        try:
            rates_svc.refresh(session, trip, getattr(scope.user_of(request), "id", None),
                              code)
        except ValueError:
            pass                    # not quoted / feed down: keep the stored rate
    session.commit()
    return currency_calculator(trip_id, fid, request, session)


# ----------------------------------------------------------------- planning (3.1)

from app.models import (PackingItem, PackingPlan, PackingProfile,   # noqa: E402
                        DiscoveryProfile, Bag)

# The Packing Profile form — closed vocabularies rendered as dropdowns/checkboxes
# (D-86) so the owner never hand-writes JSON. label pairs are (value, human text).
PROFILE_SELECTS = {
    "bag_mode": [("one_bag", "One bag only"), ("carry_on", "Carry-on"),
                 ("checked", "Checked + carry-on")],
    "laundry_comfort": [("none", "No laundry — pack the whole trip"),
                        ("hand_wash", "Hand wash on the trip"),
                        ("machine", "Machine / laundromat")],
    "packing_style": [("minimalist", "Minimalist"), ("balanced", "Balanced"),
                      ("prepared", "Prepared for anything")],
    "climate_preference": [("runs_cold", "I run cold"), ("neutral", "Neutral"),
                           ("runs_hot", "I run hot")],
    "cultural_strictness": [("strict", "Follow local norms strictly"),
                            ("flexible", "Flexible — suggest, don't enforce")],
    "clothing_variant": [("neutral", "Neutral"), ("masc", "Masculine cuts"),
                         ("fem", "Feminine cuts")],
}
PROFILE_CONSTRAINTS = [("no_heels", "No heels"),
                       ("modest_dress", "Always dress modestly"),
                       ("business_casual_only", "Business-casual only")]
_PACK_CAT_ORDER_FULL = ["clothing", "footwear", "toiletries", "tech", "documents",
                        "health", "activity"]

PACK_CAT_ORDER = ["documents", "health", "clothing", "footwear", "toiletries",
                  "tech", "activity"]
PACK_CAT_LABELS = {"documents": "Documents", "health": "Health",
                   "clothing": "Clothing", "footwear": "Footwear",
                   "toiletries": "Toiletries", "tech": "Tech",
                   "activity": "Activity gear"}

# ---- Discovery Profile vocabularies (3.2, SPEC-3.2 §2.2) --------------------
# Constraint-first (D-87): dietary/dislikes/budget/mobility SUBTRACT or bound;
# drink/activity interests ADD search queries. Closed vocabularies map to the
# resolvers' SearXNG query templates; dietary allows custom strings (D-93) but
# the form offers the standard set as checkboxes.
DISCOVERY_DIETARY = [("vegetarian", "Vegetarian"), ("vegan", "Vegan"),
                     ("halal", "Halal"), ("kosher", "Kosher"),
                     ("gluten_free", "Gluten-free"), ("dairy_free", "Dairy-free")]
DISCOVERY_DRINKS = [("craft_beer", "Craft beer"), ("distilleries", "Distilleries"),
                    ("wineries", "Wineries"), ("coffee", "Coffee"), ("tea", "Tea"),
                    ("non_alcoholic", "Non-alcoholic")]
DISCOVERY_ACTIVITIES = [("markets", "Markets"), ("hidden_gems", "Hidden gems"),
                        ("museums", "Museums"), ("outdoor", "Outdoor"),
                        ("historical", "Historical"), ("nightlife", "Nightlife"),
                        ("sports", "Sports"), ("shopping", "Shopping"),
                        ("wellness", "Wellness"), ("cooking_classes", "Cooking classes"),
                        ("live_music", "Live music"), ("architecture", "Architecture")]
# Budget ceiling (D-88): $/$$/$$$/$$$$, 0 = no limit. Integer, a ceiling not a floor.
DISCOVERY_BUDGET = [(0, "No limit"), (1, "$"), (2, "$$"), (3, "$$$"), (4, "$$$$")]
DISCOVERY_MOBILITY = [("no_limits", "No limits"),
                      ("moderate", "Moderate — some walking is fine"),
                      ("limited", "Limited — minimal walking or stairs"),
                      ("wheelchair", "Wheelchair accessible only")]


DISCOVERY_NOTES_MAX = 2000


def _discovery_default() -> dict:
    """A blank profile — "show me everything, no constraints" (SPEC-3.2 §2.2)."""
    return {
        "food": {"dietary_restrictions": [], "cuisine_dislikes": [],
                 "allergies": "", "budget_ceiling": 0},
        "drink": {"interests": []},
        "activity": {"interests": []},
        "practical": {"mobility": "no_limits"},
    }


PACK_BAG_OPTS = [("backpack", "backpack"), ("carry_on", "carry-on"),
                 ("checked", "checked")]
_PACK_BAG_LABELS = dict(PACK_BAG_OPTS)


def _bag_inventory(session: Session) -> list[Bag]:
    return (session.query(Bag).filter(Bag.enabled.is_(True))
            .order_by(Bag.kind, Bag.label).all())


def _bag_option_label(b: Bag) -> str:
    return f"{b.label} · {b.capacity}" if b.capacity else b.label


def _bag_options(session: Session) -> list[dict]:
    """The per-item bag dropdown's choices: the owner's real bag inventory
    (Admin → Bag Inventory) once they have one, else the generic carry-on /
    checked / backpack categories — the zero-setup default, same DB-is-truth
    -else-fallback pattern as the packing catalog (D-84)."""
    rows = _bag_inventory(session)
    if rows:
        return [{"value": b.id, "label": _bag_option_label(b)} for b in rows]
    return [{"value": v, "label": t} for v, t in PACK_BAG_OPTS]


def _bag_valid_values(session: Session) -> set[str]:
    """Everything a `bag` field may legally be set to: the generic values
    (always valid, so old rows/tests keep working even once an inventory
    exists) plus any currently-enabled Bag id."""
    return set(_PACK_BAG_LABELS) | {b.id for b in _bag_inventory(session)}


def _bag_label_lookup(session: Session) -> dict[str, str]:
    """id/generic-value -> display label, for rendering an item's CURRENT
    bag even if it's since been disabled/deleted, or is a legacy generic
    value no longer offered once a real inventory exists — never silently
    mislabel a row just because it fell out of the active option list."""
    lookup = dict(_PACK_BAG_LABELS)
    for b in session.query(Bag).all():           # incl. disabled: still label it
        lookup[b.id] = _bag_option_label(b)
    return lookup


def _packing_item_view(i: PackingItem, bag_labels: dict[str, str]) -> dict:
    return {"id": i.id, "label": i.label, "category": i.category,
            "quantity": i.quantity, "bag": i.bag,
            "bag_label": bag_labels.get(i.bag, i.bag or "checked"),
            "reason": i.reason, "source": i.source, "status": i.status,
            "swap_for": i.swap_for, "packed": i.status == "packed",
            "edited": i.edited}


def _pack_identity(request):
    """(user_id, traveler_id) for the viewing user — packing is per-user (D-123).
    Both None when auth is off → the legacy shared plan."""
    u = scope.user_of(request)
    return (u.id if u else None), (getattr(u, "traveler_id", None) if u else None)


def _packing_context(session: Session, trip_id: str, user_id=None) -> dict:
    items = (session.query(PackingItem)
             .filter(PackingItem.trip_id == trip_id,
                     PackingItem.user_id == user_id).all())
    plan = (session.query(PackingPlan)
            .filter(PackingPlan.trip_id == trip_id, PackingPlan.user_id == user_id)
            .order_by(PackingPlan.generated_at.desc()).first())
    from planning.engine import all_archetypes as _all_archetypes
    _all_arch = _all_archetypes()
    chosen_arch = (plan.archetypes if plan else None) or []
    chosen_ids = {a.get("id") for a in chosen_arch}
    included = [i for i in items if i.status in ("draft", "packed")]
    omitted = [i for i in items if i.status == "omitted"]
    bag_labels = _bag_label_lookup(session)
    grouped: dict[str, list] = {}
    for i in included:
        grouped.setdefault(i.category, []).append(i)
    cats = []
    for cat in PACK_CAT_ORDER:
        rows = grouped.get(cat)
        if rows:
            rows.sort(key=lambda x: x.label)
            # NB: key is "rows", not "items" — Jinja's `grp.items` would resolve
            # to the dict's built-in items() method, not this list.
            cats.append({"category": cat, "label": PACK_CAT_LABELS.get(cat, cat),
                         "rows": [_packing_item_view(r, bag_labels) for r in rows],
                         "packed": sum(1 for r in rows if r.status == "packed"),
                         "total": len(rows)})
    return {
        "packing_cats": cats,
        "packing_omitted": [_packing_item_view(i, bag_labels) for i in omitted],
        "packing_bag_options": _bag_options(session),
        "packing_has": bool(included),
        "packing_warnings": (plan.warnings if plan else None) or [],
        "packing_tasks": (plan.tasks if plan else None) or [],
        "packing_archetypes": chosen_arch,
        "packing_archetype_options": [a for a in _all_arch
                                      if a["id"] not in chosen_ids],
        "packing_manual": bool(plan and plan.manual_archetypes),
        "packing_when": plan.generated_at.strftime("%Y-%m-%d %H:%M")
        if plan and plan.generated_at else "",
        "packing_failed": bool(plan and plan.status == "failed"),
    }


@router.post("/{trip_id}/pack", response_class=HTMLResponse)
def generate_packing_plan_route(trip_id: str, request: Request,
                                session: Session = Depends(get_session)):
    """Generate (or re-generate) the Packing Plan and reload the detail. Preserves
    your ticked-off / removed items."""
    from app.resolving import load_homes_for_user
    from planning.runner import generate_plan
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    uid, tid = _pack_identity(request)
    generate_plan(session, trip, resolve_airport=_airport_resolver(),
                  geocoder=_photon(), home_profiles=load_homes_for_user(session, uid),
                  user_id=uid, traveler_id=tid)
    return _redirect(f"/trips/{trip_id}")


def _current_archetype_ids(session: Session, trip_id: str, user_id=None) -> list[str]:
    plan = (session.query(PackingPlan)
            .filter(PackingPlan.trip_id == trip_id, PackingPlan.user_id == user_id)
            .order_by(PackingPlan.generated_at.desc()).first())
    return [a.get("id") for a in ((plan.archetypes if plan else None) or [])
            if a.get("id")]


def _regenerate_with_archetypes(request: Request, session: Session, trip: Trip,
                                ids: list[str]) -> HTMLResponse:
    from app.resolving import load_homes_for_user
    from planning.runner import generate_plan
    uid, tid = _pack_identity(request)
    generate_plan(session, trip, resolve_airport=_airport_resolver(),
                  geocoder=_photon(), home_profiles=load_homes_for_user(session, uid),
                  manual_archetypes=ids, user_id=uid, traveler_id=tid)
    return templates.TemplateResponse(request, "_packing_section.html",
        {"request": request, "trip": {"id": trip.id},
         **_packing_context(session, trip.id, uid)})


@router.post("/{trip_id}/pack/archetypes/add", response_class=HTMLResponse)
def add_archetype_route(trip_id: str, request: Request, archetype_id: str = Form(""),
                        session: Session = Depends(get_session)):
    """Add a trip type to the packing plan and re-generate with the operator's
    explicit set (auto-detection stays as the starting suggestion)."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    valid = {a["id"] for a in _pack_all_archetypes()}
    ids = _current_archetype_ids(session, trip_id, _pack_identity(request)[0])
    if archetype_id in valid and archetype_id not in ids:
        ids.append(archetype_id)
    return _regenerate_with_archetypes(request, session, trip, ids)


@router.post("/{trip_id}/pack/archetypes/remove", response_class=HTMLResponse)
def remove_archetype_route(trip_id: str, request: Request, archetype_id: str = Form(""),
                           session: Session = Depends(get_session)):
    """Remove a trip type and re-generate. Removing the last one resets to
    auto-detection on the next generate."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    ids = [i for i in _current_archetype_ids(session, trip_id, _pack_identity(request)[0]) if i != archetype_id]
    return _regenerate_with_archetypes(request, session, trip, ids)


def _pack_all_archetypes():
    from planning.engine import all_archetypes
    return all_archetypes()


def _pack_count_oob(session: Session, trip_id: str, category: str, user_id=None) -> str:
    """An out-of-band swap for a category's packed/total badge, so an inline
    row change keeps the header count honest without a page reload. Scoped to the
    user's own items (D-123)."""
    rows = (session.query(PackingItem)
            .filter(PackingItem.trip_id == trip_id,
                    PackingItem.user_id == user_id,
                    PackingItem.category == category,
                    PackingItem.status.in_(("draft", "packed"))).all())
    packed = sum(1 for r in rows if r.status == "packed")
    return (f'<span class="bp-group-count" id="pc-{category}" '
            f'hx-swap-oob="true">{packed}/{len(rows)}</span>')


def _pack_item_row(request: Request, trip_id: str, session: Session,
                   i: PackingItem) -> HTMLResponse:
    row_html = templates.env.get_template("_packing_item.html").render(
        trip_id=trip_id, i=_packing_item_view(i, _bag_label_lookup(session)),
        packing_bag_options=_bag_options(session))
    return HTMLResponse(row_html + _pack_count_oob(session, trip_id, i.category, i.user_id))


# NB: these three (qty/bag/label) MUST be registered before the generic
# .../{action} route below — FastAPI/Starlette match routes in registration
# order, and .../{item_id}/{action} is shaped identically to .../{item_id}/qty,
# so it would swallow these requests first (as "qty"/"bag"/"label" not being a
# recognized action → a 400) if it came first.
@router.post("/{trip_id}/pack-item/{item_id}/qty", response_class=HTMLResponse)
def packing_item_qty(trip_id: str, item_id: str, request: Request,
                     quantity: int = Form(...),
                     session: Session = Depends(get_session)):
    """A line-item edit (not a master-catalog change): tweak how many of this
    item this trip needs — "2 pairs of jeans" doesn't always mean 2. Marks the
    row `edited` so a later re-generate never overwrites it back."""
    i = session.get(PackingItem, item_id)
    uid = _pack_identity(request)[0]
    if i is None or i.trip_id != trip_id or i.user_id != uid:
        return HTMLResponse("", status_code=404)
    i.quantity = max(1, min(99, quantity))
    i.edited = True
    session.commit()
    return _pack_item_row(request, trip_id, session, i)


@router.post("/{trip_id}/pack-item/{item_id}/bag", response_class=HTMLResponse)
def packing_item_bag(trip_id: str, item_id: str, request: Request,
                     bag: str = Form(...), session: Session = Depends(get_session)):
    """Which bag this line goes in — a specific bag from the owner's inventory
    (Admin → Bag Inventory) once they have one, else the generic backpack /
    carry-on / checked categories — set per item, per trip, independent of the
    catalog's default load-out."""
    i = session.get(PackingItem, item_id)
    uid = _pack_identity(request)[0]
    if i is None or i.trip_id != trip_id or i.user_id != uid:
        return HTMLResponse("", status_code=404)
    if bag not in _bag_valid_values(session):
        return HTMLResponse("", status_code=400)
    i.bag = bag
    i.edited = True
    session.commit()
    return _pack_item_row(request, trip_id, session, i)


@router.post("/{trip_id}/pack-item/{item_id}/label", response_class=HTMLResponse)
def packing_item_label(trip_id: str, item_id: str, request: Request,
                       label: str = Form(...),
                       session: Session = Depends(get_session)):
    """Rename a line in place — e.g. swap "Jeans" for "Slacks" without touching
    the master packing catalog."""
    i = session.get(PackingItem, item_id)
    uid = _pack_identity(request)[0]
    if i is None or i.trip_id != trip_id or i.user_id != uid:
        return HTMLResponse("", status_code=404)
    label = label.strip()[:128]
    if not label:
        return HTMLResponse("", status_code=400)
    i.label = label
    i.edited = True
    session.commit()
    return _pack_item_row(request, trip_id, session, i)


@router.post("/{trip_id}/pack-item/{item_id}/{action}", response_class=HTMLResponse)
def packing_item_action(trip_id: str, item_id: str, action: str, request: Request,
                        session: Session = Depends(get_session)):
    """Toggle one packing item and answer INLINE — no page reload (D-85). pack /
    unpack / remove swap just the row (+ the category count out-of-band); keep
    (bring back an omitted item, which crosses sections) re-renders the section."""
    i = session.get(PackingItem, item_id)
    uid = _pack_identity(request)[0]
    if i is None or i.trip_id != trip_id or i.user_id != uid:
        return HTMLResponse("", status_code=404)
    if action == "pack":
        i.status = "packed"
    elif action == "unpack":
        i.status = "draft"
    elif action == "remove":
        i.status = "removed"
    elif action in ("restore", "keep"):        # keep = bring back an omitted item
        i.status = "draft"
    else:
        return HTMLResponse("", status_code=400)
    category = i.category
    session.commit()

    if action in ("keep", "restore"):
        trip = scope.trip_or_none(session, request, trip_id)
        return templates.TemplateResponse(request, "_packing_section.html",
                                          {"request": request, "trip": trip,
                                           **_packing_context(session, trip_id, uid)})
    row_html = "" if action == "remove" else templates.env.get_template(
        "_packing_item.html").render(
            trip_id=trip_id, i=_packing_item_view(i, _bag_label_lookup(session)),
            packing_bag_options=_bag_options(session))
    return HTMLResponse(row_html + _pack_count_oob(session, trip_id, category, uid))


@router.post("/{trip_id}/pack-item/add", response_class=HTMLResponse)
def packing_item_add(trip_id: str, request: Request, category: str = Form(...),
                     label: str = Form(...), quantity: int = Form(1),
                     bag: str = Form("checked"),
                     session: Session = Depends(get_session)):
    """Add a custom line to a category — the other half of a line-item edit
    ("remove the jeans, add a pair of slacks instead") that doesn't fit into
    editing an existing row. Never touched by a re-generate (`edited=True`)."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    label = label.strip()[:128]
    if (category not in PACK_CAT_LABELS or not label
            or bag not in _bag_valid_values(session)):
        return HTMLResponse("", status_code=400)
    uid = _pack_identity(request)[0]
    item = PackingItem(trip_id=trip_id, user_id=uid,
                       item_key=f"custom:{uuid.uuid4().hex}",
                       label=label, category=category,
                       quantity=max(1, min(99, quantity)), bag=bag,
                       source="custom", status="draft", edited=True)
    session.add(item)
    session.commit()
    return templates.TemplateResponse(request, "_packing_section.html",
        {"request": request, "trip": trip, **_packing_context(session, trip_id, uid)})


def _profile_row(session: Session, traveler_id=None, owner: bool = False):
    """The packing profile to DISPLAY for one person (D-123, 4.1) — their own;
    never another person's. See planning.runner.profile_row."""
    from planning.runner import profile_row
    return profile_row(session, traveler_id, owner=owner)


def _profile_form_ctx(request: Request, session: Session, saved: bool = False) -> dict:
    from planning.engine import default_profile
    from planning import catalog
    from app.acting import acting_for
    acting = acting_for(session, request)
    tid = acting.traveler.id if acting.traveler else None
    row = _profile_row(session, tid, owner=acting.owner_mode)
    values = {**default_profile(), **((row.values if row else None) or {})}
    bag = values.get("bag") or {}
    items = catalog.items(session)
    by_cat: dict[str, list] = {}
    for iid, it in items.items():
        by_cat.setdefault(it.get("category", "activity"), []).append(
            (iid, it.get("label", iid)))
    chosen_must = set(values.get("must_have_items") or [])
    groups = []
    for cat in _PACK_CAT_ORDER_FULL:
        rows = sorted(by_cat.get(cat, []), key=lambda p: p[1])
        if rows:
            groups.append({"label": PACK_CAT_LABELS.get(cat, cat),
                           "rows": [{"id": i, "label": l, "checked": i in chosen_must}
                                    for i, l in rows]})

    def opts(field):
        cur = values.get(field)
        return [{"value": v, "label": t, "selected": v == cur}
                for v, t in PROFILE_SELECTS[field]]
    chosen_cons = set(values.get("style_constraints") or [])
    return {
        "request": request, "saved": saved, "acting": acting,
        "no_traveller": tid is None and not acting.owner_mode,
        "bag_opts": [{"value": v, "label": t,
                      "selected": v == bag.get("mode", "carry_on")}
                     for v, t in PROFILE_SELECTS["bag_mode"]],
        "bag_weight_kg": bag.get("weight_kg", 10),
        "laundry_opts": opts("laundry_comfort"), "style_opts": opts("packing_style"),
        "climate_opts": opts("climate_preference"),
        "strictness_opts": opts("cultural_strictness"),
        "variant_opts": opts("clothing_variant"),
        "constraints": [{"value": v, "label": t, "checked": v in chosen_cons}
                        for v, t in PROFILE_CONSTRAINTS],
        "must_groups": groups,
    }


@router.get("/settings/packing-profile", response_class=HTMLResponse)
def edit_packing_profile(request: Request, session: Session = Depends(get_session)):
    """The structured Packing Profile editor (D-86) — a standalone page in the
    Administer nav (a global preference, like Home Profiles), dropdowns +
    checkboxes, no JSON. 2-segment static path so `/{trip_id}` doesn't capture it."""
    return templates.TemplateResponse(request, "packing_profile.html",
                                      _profile_form_ctx(request, session))


@router.post("/settings/packing-profile", response_class=HTMLResponse)
def save_packing_profile(request: Request,
                         bag_mode: str = Form("carry_on"),
                         bag_weight_kg: str = Form("10"),
                         laundry_comfort: str = Form("none"),
                         packing_style: str = Form("balanced"),
                         climate_preference: str = Form("neutral"),
                         cultural_strictness: str = Form("strict"),
                         clothing_variant: str = Form("neutral"),
                         style_constraints: list[str] = Form(default=[]),
                         must_have_items: list[str] = Form(default=[]),
                         session: Session = Depends(get_session)):
    try:
        weight = int(float(bag_weight_kg))
    except (TypeError, ValueError):
        weight = 10
    values = {
        "bag": {"mode": bag_mode, "weight_kg": weight},
        "laundry_comfort": laundry_comfort, "packing_style": packing_style,
        "climate_preference": climate_preference,
        "cultural_strictness": cultural_strictness,
        "clothing_variant": clothing_variant,
        "style_constraints": style_constraints,
        "must_have_items": must_have_items,
    }
    # Save to the CURRENT user's OWN profile (D-123: tied to their traveller),
    # creating it if missing — never overwrite another traveller's or the shared
    # default when this user has their own.
    from app.acting import acting_for, audit_if_other
    acting = acting_for(session, request)          # self, or an admin acting for someone
    traveler_id = acting.traveler.id if acting.traveler else None
    if traveler_id is None and not acting.owner_mode:
        # Signed in but no traveller linked: nowhere of their own to save it —
        # and never into the owner's unassigned row.
        return templates.TemplateResponse(request, "packing_profile.html",
                                          _profile_form_ctx(request, session))
    audit_if_other(session, request, acting, "packing profile")
    own = _profile_row(session, traveler_id, owner=acting.owner_mode)
    if own is None:
        session.add(PackingProfile(values=values, traveler_id=traveler_id))
    else:
        own.traveler_id = traveler_id   # claims a pre-4.1 unassigned row for self
        own.values = values
    session.commit()
    # Re-render the page with a saved banner; the plan applies the new profile on
    # the next Re-generate.
    return templates.TemplateResponse(request, "packing_profile.html",
                                      _profile_form_ctx(request, session, saved=True))


def _zones() -> list[str]:
    from app.clock import zone_names
    return zone_names()


def _currency_options() -> list[tuple[str, str]]:
    """(code, "CAD — Canadian Dollar") for every ISO-4217 currency Babel knows,
    for the travel-profile base-currency picker. Sorted by code."""
    from babel.numbers import list_currencies, get_currency_name
    opts = []
    for c in list_currencies():
        try:
            name = get_currency_name(c, locale="en")
        except Exception:
            name = c
        opts.append((c, f"{c} — {name}"))
    return sorted(opts)


@router.get("/settings/travel-profile", response_class=HTMLResponse)
def edit_travel_profile(request: Request, session: Session = Depends(get_session)):
    """A user edits THEIR OWN traveller's personal attributes (D-124): nationality
    (for visa research), base currency (for discovery currency conversion),
    name-on-ticket aliases, AirTrail mapping, notes. Not in the admin tables —
    travel is personal. An admin may edit it FOR a traveller with no account or
    one whose user shares with admins (4.1, app/acting.py)."""
    from app.acting import acting_for
    acting = acting_for(session, request)
    trav = acting.traveler
    return templates.TemplateResponse(request, "travel_profile.html",
        {"request": request, "trav": trav, "no_traveller": trav is None,
         "acting": acting, "currencies": _currency_options(), "zones": _zones(),
         "saved": False})


@router.post("/settings/travel-profile", response_class=HTMLResponse)
def save_travel_profile(request: Request, nationality: str = Form(""),
                        base_currency: str = Form(""), timezone: str = Form(""),
                        aliases: str = Form(""), airtrail_user_id: str = Form(""),
                        notes: str = Form(""), session: Session = Depends(get_session)):
    from sqlalchemy.orm.attributes import flag_modified
    from babel.numbers import list_currencies
    from app.acting import acting_for, audit_if_other
    acting = acting_for(session, request)
    trav = acting.traveler
    if trav is None:
        return templates.TemplateResponse(request, "travel_profile.html",
            {"request": request, "trav": None, "no_traveller": True, "acting": acting,
             "currencies": _currency_options(), "zones": _zones(), "saved": False})
    audit_if_other(session, request, acting, "travel profile")
    trav.nationality = (nationality or "").strip().upper()[:2] or None
    bc = (base_currency or "").strip().upper()
    trav.base_currency = bc if bc in set(list_currencies()) else None
    from app.clock import valid_zone
    trav.timezone = valid_zone(timezone)            # their own "now" (YONDER-219, D-187)
    trav.aliases = [a.strip() for a in (aliases or "").splitlines() if a.strip()]
    flag_modified(trav, "aliases")
    trav.airtrail_user_id = (airtrail_user_id or "").strip() or None
    trav.notes = (notes or "").strip()[:512] or None
    session.commit()
    session.refresh(trav)
    return templates.TemplateResponse(request, "travel_profile.html",
        {"request": request, "trav": trav, "no_traveller": False, "acting": acting,
         "currencies": _currency_options(), "zones": _zones(), "saved": True})


# ---- Discovery Profile editor (3.2, SPEC-3.2 §11.1) ------------------------

def _discovery_profile_row(session: Session, traveler: TravelerIdentity | None,
                           owner: bool = False) -> DiscoveryProfile | None:
    """ONE person's discovery profile (4.1, D-95: exactly one per person). The
    row on their traveller; for the `self` traveller, a pre-4.1 unassigned row
    counts too (it was the owner's). Nobody else ever sees another person's
    tastes — no traveller, or no row yet, means a blank profile. `owner` (auth
    off, single-user): with no traveller at all, the unassigned row."""
    if traveler is None:
        if not owner:
            return None
        return (session.query(DiscoveryProfile)
                .filter(DiscoveryProfile.traveler_id.is_(None))
                .order_by(DiscoveryProfile.created_at).first())
    own = (session.query(DiscoveryProfile)
           .filter(DiscoveryProfile.traveler_id == traveler.id)
           .order_by(DiscoveryProfile.created_at).first())
    if own is not None or traveler.kind != "self":
        return own
    return (session.query(DiscoveryProfile)
            .filter(DiscoveryProfile.traveler_id.is_(None))
            .order_by(DiscoveryProfile.created_at).first())


def discovery_profile_public(session: Session, traveler: TravelerIdentity | None,
                             owner: bool = False, user_id: str | None = None) -> dict:
    """ONE person's discovery profile for an AI agent (4.5, MCP/API): the same
    preferences the local Discover run shapes its research with, each value
    with its label, their free-text notes for agents, the rows they hid as
    "not my thing" (`user_id`'s, across trips), and how to apply it all. Only
    ever the caller's own."""
    from discover.items import dislikes as hidden_dislikes
    row = _discovery_profile_row(session, traveler, owner)
    d = _discovery_default()
    v = {**d, **((row.values if row else None) or {})}
    food = {**d["food"], **(v.get("food") or {})}
    drink = {**d["drink"], **(v.get("drink") or {})}
    activity = {**d["activity"], **(v.get("activity") or {})}
    practical = {**d["practical"], **(v.get("practical") or {})}
    diet_l, drink_l, act_l = (dict(DISCOVERY_DIETARY), dict(DISCOVERY_DRINKS),
                              dict(DISCOVERY_ACTIVITIES))
    budget = food.get("budget_ceiling") or 0
    mob = practical.get("mobility") or "no_limits"

    def tagged(keys, labels):
        return [{"key": k, "label": labels.get(k, k)} for k in keys or []]

    out = {
        "food": {
            "dietary_restrictions": tagged(food.get("dietary_restrictions"), diet_l),
            "avoid_cuisines": list(food.get("cuisine_dislikes") or []),
            "allergies": food.get("allergies") or "",
            "budget_ceiling": {"tier": budget,
                               "label": dict(DISCOVERY_BUDGET).get(budget, "No limit")},
        },
        "drink": {"interests": tagged(drink.get("interests"), drink_l)},
        "activity": {"interests": tagged(activity.get("interests"), act_l)},
        "practical": {"mobility": {"key": mob,
                                   "label": dict(DISCOVERY_MOBILITY).get(mob, mob)}},
        "travel": {"nationality": traveler.nationality if traveler else None,
                   "base_currency": traveler.base_currency if traveler else None},
        "notes": (v.get("notes") or "").strip(),
        "not_my_thing": hidden_dislikes(session, user_id),
    }
    out["blank"] = row is None or not any([
        food.get("dietary_restrictions"), food.get("cuisine_dislikes"),
        food.get("allergies"), budget, drink.get("interests"),
        activity.get("interests"), mob != "no_limits", out["notes"]])
    out["how_to_use"] = (
        "Constraint-first: every recommendation must fit the dietary restrictions; "
        "never recommend the avoided cuisines; stay at or under the budget ceiling "
        "(1–4, $–$$$$; 0 = no limit); flag — don't exclude — anything that may "
        "involve a listed allergen. Interests ADD places (breweries, museums, …) on "
        "top of the usual mix; they never narrow it. Respect mobility for activities. "
        "`notes` is the person in their own words — likes, dislikes, past "
        "favourites; follow it like the rest. `not_my_thing` lists places and "
        "events they hid as not their thing: don't suggest those again, and steer "
        "away from similar ones. If `blank` is true, ask the person what they like "
        "and want to avoid before researching. Never infer tastes from nationality.")
    return out


def _discovery_form_ctx(request: Request, session: Session,
                        saved: bool = False) -> dict:
    from app.acting import acting_for
    acting = acting_for(session, request)           # self, or an admin acting for someone
    trav, owner = acting.traveler, acting.owner_mode
    row = _discovery_profile_row(session, trav, owner)
    d = _discovery_default()
    v = {**d, **((row.values if row else None) or {})}
    food = {**d["food"], **(v.get("food") or {})}
    drink = {**d["drink"], **(v.get("drink") or {})}
    activity = {**d["activity"], **(v.get("activity") or {})}
    practical = {**d["practical"], **(v.get("practical") or {})}
    diet = set(food.get("dietary_restrictions") or [])
    drinks = set(drink.get("interests") or [])
    acts = set(activity.get("interests") or [])
    budget = food.get("budget_ceiling") or 0
    mob = practical.get("mobility") or "no_limits"
    return {
        "request": request, "saved": saved, "acting": acting,
        "no_traveller": trav is None and not owner,
        "dietary": [{"value": k, "label": t, "checked": k in diet}
                    for k, t in DISCOVERY_DIETARY],
        "cuisine_dislikes": ", ".join(food.get("cuisine_dislikes") or []),
        "allergies": food.get("allergies") or "",
        "budget_opts": [{"value": k, "label": t, "checked": k == budget}
                        for k, t in DISCOVERY_BUDGET],
        "drinks": [{"value": k, "label": t, "checked": k in drinks}
                   for k, t in DISCOVERY_DRINKS],
        "activities": [{"value": k, "label": t, "checked": k in acts}
                       for k, t in DISCOVERY_ACTIVITIES],
        "mobility_opts": [{"value": k, "label": t, "selected": k == mob}
                          for k, t in DISCOVERY_MOBILITY],
        "notes": v.get("notes") or "",
    }


@router.get("/settings/discovery-profile", response_class=HTMLResponse)
def edit_discovery_profile(request: Request, session: Session = Depends(get_session)):
    """The Discovery Profile editor (D-89) — peer of the Packing Profile page,
    linked from the Administer nav. Constraint-first preferences that shape what
    the food/activity resolvers search for. 2-segment static path so `/{trip_id}`
    doesn't capture it."""
    return templates.TemplateResponse(request, "discovery_profile.html",
                                      _discovery_form_ctx(request, session))


@router.post("/settings/discovery-profile", response_class=HTMLResponse)
def save_discovery_profile(request: Request,
                           dietary_restrictions: list[str] = Form(default=[]),
                           cuisine_dislikes: str = Form(""),
                           allergies: str = Form(""),
                           budget_ceiling: str = Form("0"),
                           drink_interests: list[str] = Form(default=[]),
                           activity_interests: list[str] = Form(default=[]),
                           mobility: str = Form("no_limits"),
                           notes: str = Form(""),
                           session: Session = Depends(get_session)):
    try:
        budget = int(budget_ceiling)
    except (TypeError, ValueError):
        budget = 0
    if budget not in (0, 1, 2, 3, 4):
        budget = 0
    # Interests are closed vocabularies (they map to query templates) — filter to
    # known keys so the blob stays clean. Dietary allows custom strings (D-93), so
    # it is not filtered. Cuisine dislikes are free text, split on commas.
    drinks_ok = {k for k, _ in DISCOVERY_DRINKS}
    acts_ok = {k for k, _ in DISCOVERY_ACTIVITIES}
    mob_ok = {k for k, _ in DISCOVERY_MOBILITY}
    dislikes = [c.strip() for c in cuisine_dislikes.split(",") if c.strip()]
    values = {
        "food": {
            "dietary_restrictions": [d.strip() for d in dietary_restrictions if d.strip()],
            "cuisine_dislikes": dislikes,
            "allergies": allergies.strip(),
            "budget_ceiling": budget,
        },
        "drink": {"interests": [x for x in drink_interests if x in drinks_ok]},
        "activity": {"interests": [x for x in activity_interests if x in acts_ok]},
        "practical": {"mobility": mobility if mobility in mob_ok else "no_limits"},
        "notes": notes.strip()[:DISCOVERY_NOTES_MAX],   # for agents (4.5); free text
    }
    from app.acting import acting_for, audit_if_other
    acting = acting_for(session, request)
    trav, owner = acting.traveler, acting.owner_mode
    if trav is None and not owner:   # signed in, no traveller: nowhere to keep it
        return templates.TemplateResponse(request, "discovery_profile.html",
                                          _discovery_form_ctx(request, session))
    audit_if_other(session, request, acting, "discovery profile")
    tid = trav.id if trav else None
    row = _discovery_profile_row(session, trav, owner)
    if row is None:
        session.add(DiscoveryProfile(traveler_id=tid, values=values))
    else:
        row.traveler_id = tid        # claims a pre-4.1 unassigned row for self
        row.values = values
    session.commit()
    return templates.TemplateResponse(request, "discovery_profile.html",
                                      _discovery_form_ctx(request, session, saved=True))


@router.get("/{trip_id}/edit", response_class=HTMLResponse)
def edit_trip(trip_id: str, request: Request, next: str = "",
              session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_trip_modal.html", {
        "request": request, "mode": "edit", "title": "Edit trip",
        "action": f"/trips/{trip.id}", "next": next or f"/trips/{trip.id}",
        "show_al": _show_al_field(session, request),
        "form": {"name": trip.name, "start": trip.start_date or "",
                 "end": trip.end_date or "", "notes": trip.notes or "",
                 "collection_id": trip.adventurelog_collection_id or ""},
        "error": None})


# ----------------------------------------------------------------- trip writes

def _save_trip_fields(trip: Trip, name: str, start: str, end: str, notes: str,
                      collection_id: str | None = None):
    trip.name = name.strip()
    trip.start_date = start.strip() or None
    trip.end_date = end.strip() or None
    trip.notes = notes.strip() or None
    if collection_id is not None:       # None: the form didn't show the field
        trip.adventurelog_collection_id = collection_id.strip() or None


@router.post("", response_class=HTMLResponse)
def create_trip(request: Request, name: str = Form(""), start: str = Form(""),
                end: str = Form(""), notes: str = Form(""),
                adventurelog_collection_id: str | None = Form(None),
                next: str = Form("/trips"),
                session: Session = Depends(get_session)):
    if not name.strip():
        return templates.TemplateResponse(request, "_trip_modal.html", {
            "request": request, "mode": "create", "title": "New trip",
            "action": "/trips", "next": next,
            "show_al": adventurelog_collection_id is not None,
            "form": {"name": name, "start": start, "end": end, "notes": notes,
                     "collection_id": adventurelog_collection_id or ""},
            "error": "A trip needs a name."})
    trip = Trip(source="manual")
    # Phase 2 (D-121): a hand-made trip is owned by its creator. NULL when auth
    # is off (no user) — an open trip, consistent with pre-4.0 behaviour.
    _u = scope.user_of(request)
    trip.owner_id = _u.id if _u else None
    _save_trip_fields(trip, name, start, end, notes, adventurelog_collection_id)
    session.add(trip)
    session.flush()                       # need trip.id for the self-traveller link
    _auto_link_self(session, trip.id)     # §11.2 — you're on your own trip by default
    session.commit()
    _recheck_waiting(session, trip, _u)
    return _redirect("/trips")


@router.post("/{trip_id}", response_class=HTMLResponse)
def update_trip(trip_id: str, request: Request, name: str = Form(""),
                start: str = Form(""), end: str = Form(""), notes: str = Form(""),
                adventurelog_collection_id: str | None = Form(None),
                next: str = Form(""), session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    from app.authn_routes import _safe_next
    dest = _safe_next(next) if next else f"/trips/{trip.id}"      # local paths only
    if not name.strip():
        return templates.TemplateResponse(request, "_trip_modal.html", {
            "request": request, "mode": "edit", "title": "Edit trip",
            "action": f"/trips/{trip.id}", "next": dest,
            "show_al": adventurelog_collection_id is not None,
            "form": {"name": name, "start": start, "end": end, "notes": notes,
                     "collection_id": adventurelog_collection_id or ""},
            "error": "A trip needs a name."})
    dates_before = (trip.start_date, trip.end_date)
    _save_trip_fields(trip, name, start, end, notes, adventurelog_collection_id)
    session.commit()
    if (trip.start_date, trip.end_date) != dates_before:
        _recheck_waiting(session, trip, scope.user_of(request))
    return _redirect(dest)


def _recheck_waiting(session: Session, trip: Trip, user) -> None:
    """A new or re-dated trip may be the one a waiting capture needs
    (YONDER-222). Best-effort: the trip is already saved, so a resolver
    failure here is logged, never shown as a failed save."""
    from app.review.service import recheck_for_trip
    try:
        recheck_for_trip(session, trip, user)
    except Exception:
        session.rollback()
        log.warning("re-checking captures for trip %s failed", trip.id, exc_info=True)


@router.post("/{trip_id}/delete", response_class=HTMLResponse)
def delete_trip(trip_id: str, request: Request, session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is not None:
        # Everything that only exists for this trip goes with it; captures and
        # journal entries are kept and unlinked (app/trips/purge.py).
        from app.trips.purge import purge_trip
        purge_trip(session, trip)
        session.commit()
    return _redirect("/trips")


def _travelers_section(request: Request, session: Session, trip_id: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_travelers_section.html",
        {"request": request, **_travelers_context(session, trip_id)})


@router.post("/{trip_id}/travelers", response_class=HTMLResponse)
def add_traveler(trip_id: str, request: Request, traveler_id: str = Form(""),
                 session: Session = Depends(get_session)):
    """Add an existing TravelerIdentity to the trip roster as a companion (the
    `self` traveller auto-links on creation). Idempotent — a duplicate add is a
    no-op — and swaps just the Who's-traveling section."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    try:
        tid = int(traveler_id)
    except (TypeError, ValueError):
        tid = 0
    ident = session.get(TravelerIdentity, tid) if tid else None
    if ident is not None and session.get(
            TripTraveler, {"trip_id": trip_id, "traveler_id": tid}) is None:
        role = "self" if ident.kind == "self" else "companion"
        session.add(TripTraveler(trip_id=trip_id, traveler_id=tid, role=role))
        session.commit()
    return _travelers_section(request, session, trip_id)


@router.post("/{trip_id}/travelers/new", response_class=HTMLResponse)
def add_guest(trip_id: str, request: Request, name: str = Form(""),
              session: Session = Depends(get_session)):
    """Quick-add a generic guest by name — mint a minimal TravelerIdentity
    (kind=guest, no profile, no nationality) and link it as a companion (§11.2's
    "+ Add new"). A guest needs no Discovery/Packing profile; those tables are
    optional and hang off the identity later if ever wanted (D-94). Reuses an
    existing identity of the same name rather than duplicating it (display_name
    is unique). Blank name is a no-op."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    label = name.strip()
    if label:
        ident = (session.query(TravelerIdentity)
                 .filter(TravelerIdentity.display_name == label).first())
        if ident is None:
            # generic=True: a placeholder person, ignored by discovery/planning
            # (D-100). Reusing an existing identity never flips its flag.
            ident = TravelerIdentity(display_name=label, kind="guest",
                                     generic=True)
            session.add(ident)
            session.flush()               # need ident.id for the link
        if session.get(TripTraveler, {"trip_id": trip_id,
                                      "traveler_id": ident.id}) is None:
            role = "self" if ident.kind == "self" else "companion"
            session.add(TripTraveler(trip_id=trip_id, traveler_id=ident.id,
                                     role=role))
        session.commit()
    return _travelers_section(request, session, trip_id)


@router.post("/{trip_id}/travelers/{traveler_id}/remove", response_class=HTMLResponse)
def remove_traveler(trip_id: str, traveler_id: int, request: Request,
                    session: Session = Depends(get_session)):
    """Drop a traveller from the trip roster. Swaps just the section. Needs edit
    access to the trip, like adding one (YONDER-293: it had no check at all)."""
    if scope.trip_or_none(session, request, trip_id) is None:
        return HTMLResponse("", status_code=404)
    row = session.get(TripTraveler,
                      {"trip_id": trip_id, "traveler_id": traveler_id})
    if row is not None:
        session.delete(row)
        who.remove_from_trip(session, trip_id, traveler_id)
        session.commit()
    return _travelers_section(request, session, trip_id)


_NOT_CONNECTED = ("Connect your AdventureLog first (Settings → Connections).")


def _push_msg(text: str) -> HTMLResponse:
    """Inline failure notice swapped next to the Push button (a success answers
    with an HX-Redirect that reloads the detail showing the new link)."""
    return HTMLResponse(f'<span class="bp-push-err">{text}</span>')


# Every Yonder activity type becomes a Transportation, except lodging (its own
# AdventureLog object) and tour/event/dining/note, which fall through to a
# Note — AdventureLog has no generic "activity" object to hold those.
_AL_TRANSPORT_TYPE = {
    "flight": "plane", "train": "train", "drive": "car", "taxi": "car",
    "carRental": "car", "cruise": "boat",
}


def _al_iso(value: str | None) -> str | None:
    """A stored `when`/`endWhen` value (bare "YYYY-MM-DD" or datetime-local
    "YYYY-MM-DDTHH:MM") as AdventureLog's explicit-UTC ISO string.

    TripActivity carries no per-activity timezone (RULE D-06's naive-time
    guard doesn't apply here — there's no source document to hold a zone), so
    — consistent with the rest of the adapter layer (D-06's Photon-adjacent
    convention: a naive value is always sent as explicit UTC, never guessed
    at) — this treats the stored value as UTC rather than inventing one."""
    if not value:
        return None
    if len(value) == 10:                                 # "YYYY-MM-DD"
        return f"{value}T00:00:00Z"
    if value.endswith("Z"):
        return value
    return f"{value}:00Z" if len(value) == 16 else f"{value}Z"


def _resolve_coords(place: str | None, airports: dict, photon,
                    cache: dict) -> tuple[float | None, float | None]:
    """Best-effort (lat, lon) for a transportation endpoint.

    `place` is usually an IATA/ICAO code (flight/train) but may be free text
    (a cruise port, a car-rental address) — AirTrail's flown-airport reference
    is checked first (free, exact, already loaded), Photon covers everything
    else, including an airport AirTrail has never seen flown (same fallback
    chain as the home-profile airport lookup in app/review/home.py). `cache`
    is the caller's dict, reused across every activity in one push so the same
    airport isn't geocoded twice."""
    if not place:
        return None, None
    key = place.strip().upper()
    if key in cache:
        return cache[key]
    coords = (None, None)
    airport = airports.get(key)
    if airport is None:
        airport = next((a for a in airports.values()
                        if a.icao and a.icao.upper() == key), None)
    if airport is not None:
        coords = (airport.lat, airport.lon)
    else:
        try:
            looks_like_code = key.isalpha() and len(key) in (3, 4)
            result = photon.geocode_freeform(f"{place} airport" if looks_like_code else place)
        except Exception:
            result = None
        if result is not None:
            coords = (result.lat, result.lon)
    cache[key] = coords
    return coords


# AdventureLog's Lodging.type enum. A free-text lodging type only crosses over
# when it matches one (else omitted, so AL never 400s on an unknown value).
_AL_LODGING_TYPES = {"hotel", "hostel", "resort", "bnb", "campground", "cabin",
                     "apartment", "house", "villa", "motel", "other"}
# Common synonyms → the canonical AL value.
_AL_LODGING_ALIASES = {"airbnb": "bnb", "b&b": "bnb", "bed and breakfast": "bnb",
                       "guesthouse": "hotel", "ryokan": "other", "camping": "campground",
                       "lodge": "cabin", "inn": "hotel", "flat": "apartment"}


def _al_lodging_type(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().lower()
    key = _AL_LODGING_ALIASES.get(key, key)
    return key if key in _AL_LODGING_TYPES else None


def _al_body_for_activity(activity: TripActivity, collection_id: str,
                          airports: dict, photon, coord_cache: dict) -> tuple[str, dict]:
    """Map one TripActivity onto the AdventureLog object it becomes.

    Returns (kind, body) where kind is "transportation" | "lodging" | "note" —
    the three create_* methods on AdventureLogClient. `is_public: True`
    matches the collection push_adventurelog always creates as public; every
    child object must agree or AdventureLog's own validation rejects it."""
    v = activity.values or {}
    t = activity_types.get_type(activity.type)
    title = t.title(v) or "Untitled"
    subtitle = t.subtitle(v)
    starts = _al_iso(v.get("when"))
    ends = _al_iso(v.get("endWhen")) or starts
    # AdventureLog's link is a URL field: the Source when it is a link (it may be
    # plain text, e.g. an email subject), else the map link.
    link = next((u for u in (v.get("sourceLink"), v.get("mapLink"))
                 if (u or "").strip().lower().startswith(("http://", "https://"))), None)
    detail_lines = [f"Confirmation: {v['confirmation']}"] if v.get("confirmation") else []
    if v.get("notes"):
        detail_lines.append(v["notes"])
    description = "\n\n".join(detail_lines) or None

    if activity.type in _AL_TRANSPORT_TYPE:
        name = f"{title} – {subtitle}" if subtitle else title
        origin = v.get("from") or v.get("pickupLocation") or v.get("departurePort")
        dest = v.get("to") or v.get("dropoffLocation") or v.get("arrivalPort")
        # Stored coordinates first (app/trips/geo.py); look up only what's missing.
        stored = {p["role"]: (p["lat"], p["lon"])
                  for p in ((activity.geo or {}).get("points") or [])}
        origin_lat, origin_lon = stored.get("from") or _resolve_coords(
            origin, airports, photon, coord_cache)
        dest_lat, dest_lon = stored.get("to") or _resolve_coords(
            dest, airports, photon, coord_cache)
        body = {
            "type": _AL_TRANSPORT_TYPE[activity.type], "name": name,
            "date": starts, "end_date": ends,
            "from_location": origin, "to_location": dest,
            "start_code": v.get("from"), "end_code": v.get("to"),
            "origin_latitude": origin_lat, "origin_longitude": origin_lon,
            "destination_latitude": dest_lat, "destination_longitude": dest_lon,
            "link": link, "description": description,
            "is_public": True, "collection": collection_id,
        }
        if activity.type == "flight":
            body["flight_number"] = v.get("flightNumber")
        return "transportation", {k: v for k, v in body.items() if v is not None}

    if activity.type == "lodging":
        place = next((p for p in ((activity.geo or {}).get("points") or [])
                      if p.get("role") == "place"), {})
        body = {
            "name": v.get("name") or title, "check_in": starts, "check_out": ends,
            "latitude": place.get("lat"), "longitude": place.get("lon"),
            "location": v.get("address"), "reservation_number": v.get("confirmation"),
            "link": link, "description": v.get("notes") or None,
            "type": _al_lodging_type(v.get("type")),
            "is_public": True, "collection": collection_id,
        }
        return "lodging", {k: v for k, v in body.items() if v is not None}

    content = "\n\n".join(p for p in (subtitle, v.get("text"), description) if p) or None
    body = {
        "name": title, "content": content, "links": [link] if link else None,
        "date": (v.get("when") or "")[:10] or None,
        "is_public": True, "collection": collection_id,
    }
    return "note", {k: v for k, v in body.items() if v is not None}


@router.post("/{trip_id}/push-adventurelog", response_class=HTMLResponse)
def push_adventurelog(trip_id: str, request: Request, session: Session = Depends(get_session)):
    """Create a PUBLIC AdventureLog collection from this trip and link it — the
    collection ONLY, empty. Pushing the trip's activities and discovery findings
    into it is done afterwards from the managed sync page (/adventurelog), which
    deduplicates against what's already there. (Before 3.8 this route also did a
    one-shot activity push; that moved to the sync page so even the first push is
    selective and dup-safe.)

    Public by default so the collection can be direct-linked from outside
    AdventureLog. Idempotent on the collection itself: a trip already linked is
    left alone — we never create a second collection for the same trip."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    if trip.adventurelog_collection_id:
        return _redirect(f"/trips/{trip_id}")           # already linked
    client = _adventurelog(session, request)
    if client is None:
        return _push_msg(_NOT_CONNECTED)
    if not (trip.start_date and trip.end_date):
        return _push_msg("Add a start and end date first — AdventureLog needs both.")
    try:
        status, parsed, raw = client.create_collection(
            trip.name, trip.start_date, trip.end_date, is_public=True)
    except Exception as ex:                              # network/adapter error
        return _push_msg(f"AdventureLog request failed: {ex}")
    if status >= 400 or not isinstance(parsed, dict) or not parsed.get("id"):
        return _push_msg(f"AdventureLog rejected the collection (HTTP {status}).")
    collection_id = str(parsed["id"])
    trip.adventurelog_collection_id = collection_id
    trip.source = trip.source or "manual"
    session.commit()
    return _redirect(f"/trips/{trip_id}")


# ------------------------------------------------- AdventureLog sync (3.8, push/pull)
#
# Ownership markers, embedded in an object's description (transport/lodging) or
# content (note), let a re-push MATCH what Yonder already wrote instead of
# duplicating it (AdventureLog has no server-side dedup). A finding keys on its
# topic+place; an activity keys on its stable id. The two prefixes also keep a
# Yonder-pushed note distinct from a trip-prep-skill note for the same topic.
_MARKER_RE = re.compile(r"<!--\s*(yonder:[^\s>]+)\s*-->")
# AdventureLog transport enum → the Yonder Timeline type, for PULL. Lossy on the
# way back ("car" was drive OR carRental on push) — car defaults to drive.
_AL_TO_YONDER_TRANSPORT = {"plane": "flight", "train": "train", "car": "drive",
                           "boat": "cruise", "bus": "drive", "bike": "drive",
                           "walking": "drive", "other": "drive"}


def _activity_marker(activity_id: str) -> str:
    return f"yonder:activity:{activity_id}"


def _discovery_marker(topic: str, place_key: str) -> str:
    return f"yonder:discovery:{topic}:{place_key}"


def _activity_target_kind(atype: str) -> str:
    """The AdventureLog object a Yonder activity type becomes."""
    if atype in _AL_TRANSPORT_TYPE:
        return "transportation"
    if atype == "lodging":
        return "lodging"
    return "note"


def _inject_marker(kind: str, body: dict, marker: str) -> dict:
    """Stamp the ownership marker into the object's text field so a later push can
    find it. Notes carry it in `content`, transport/lodging in `description`."""
    field = "content" if kind == "note" else "description"
    existing = (body.get(field) or "").strip()
    body[field] = f"{existing}\n\n<!-- {marker} -->".strip() if existing \
        else f"<!-- {marker} -->"
    return body


def _finding_note_body(finding: DiscoveryFinding, collection_id: str) -> dict:
    """One discovery finding → an AdventureLog Note body (SPEC-3.2 §8)."""
    name = f"{TOPIC_LABELS.get(finding.topic, finding.topic)} — {finding.place_key}"
    return {"name": name, "content": finding.body_md or "",
            "is_public": True, "collection": collection_id}


def _al_collection_index(client, collection_id: str) -> dict:
    """Scan the collection's embedded objects for Yonder ownership markers →
    {marker: {"kind", "id", "name"}}. The basis for dedup on push."""
    idx: dict[str, dict] = {}
    try:
        status, data, raw = client.get_collection(collection_id)
    except Exception:
        return idx
    if not isinstance(data, dict):
        return idx
    for arr_key, kind, field in (("transportations", "transportation", "description"),
                                 ("lodging", "lodging", "description"),
                                 ("notes", "note", "content")):
        for rec in (data.get(arr_key) or []):
            if not isinstance(rec, dict):
                continue
            m = _MARKER_RE.search(rec.get(field) or "")
            if m:
                idx[m.group(1)] = {"kind": kind, "id": str(rec.get("id")),
                                   "name": rec.get("name") or ""}
    return idx


def _from_al_iso(value: str | None) -> str:
    """AdventureLog explicit-UTC ISO → the Yonder `when` shape ("YYYY-MM-DD" or
    "YYYY-MM-DDTHH:MM"), dropping a midnight time."""
    if not value:
        return ""
    v = value.replace("Z", "")
    if "T" not in v:
        return v[:10]
    date_p, time_p = v[:10], v[11:16]
    return date_p if time_p in ("", "00:00") else f"{date_p}T{time_p}"


def _al_link_values(link: str | None) -> dict:
    """An AdventureLog record's link lands in the Map field when it is a Maps
    link (so it drives the Map button), else in Source."""
    link = (link or "").strip()
    if not link:
        return {}
    return {"mapLink": link} if activity_types._is_maps_url(link) else {"sourceLink": link}


def _al_record_to_activity(rec: dict, kind: str, trip_id: str) -> TripActivity:
    """Map an AdventureLog transportation/lodging record onto a Yonder TripActivity
    (a Timeline item). Notes have no Timeline equivalent and are never pulled."""
    def _clean(text):
        return _MARKER_RE.sub("", text or "").strip()
    if kind == "transportation":
        ytype = _AL_TO_YONDER_TRANSPORT.get((rec.get("type") or "").lower(), "drive")
        values = {"from": rec.get("start_code") or rec.get("from_location") or "",
                  "to": rec.get("end_code") or rec.get("to_location") or "",
                  "when": _from_al_iso(rec.get("date")),
                  "endWhen": _from_al_iso(rec.get("end_date")),
                  "confirmation": rec.get("reservation_number") or "",
                  "notes": _clean(rec.get("description")),
                  **_al_link_values(rec.get("link"))}
        if ytype == "flight" and rec.get("flight_number"):
            values["flightNumber"] = rec["flight_number"]
    else:                                                # lodging
        ytype = "lodging"
        values = {"name": rec.get("name") or "",
                  "when": _from_al_iso(rec.get("check_in")),
                  "endWhen": _from_al_iso(rec.get("check_out")),
                  "address": rec.get("location") or "",
                  "confirmation": rec.get("reservation_number") or "",
                  "type": rec.get("type") or "", "notes": _clean(rec.get("description")),
                  **_al_link_values(rec.get("link"))}
    values = {k: v for k, v in values.items() if v}
    return TripActivity(trip_id=trip_id, type=ytype, values=values,
                        starts_at=values.get("when") or "")


def _al_airports_photon():
    from resolve import airports as airports_mod
    return airports_mod.reference(), _photon(), {}      # Run·dere's own table (D-147)


def _al_sync_ctx(session: Session, trip: Trip, client, uid: str | None = None,
                 public_url: str | None = None) -> dict:
    """Everything the sync page renders: pushable Yonder items (with a per-item
    'already there?' flag) and pullable AdventureLog records."""
    cid = trip.adventurelog_collection_id
    index = _al_collection_index(client, cid)
    push_items = []
    for a in (session.query(TripActivity).filter(TripActivity.trip_id == trip.id)
              .order_by(TripActivity.starts_at).all()):
        t = activity_types.get_type(a.type)
        kind = _activity_target_kind(a.type)
        push_items.append({"id": f"activity:{a.id}", "source": "Timeline",
                           "label": t.title(a.values or {}) or a.type,
                           "kind": kind.title(),
                           "exists": _activity_marker(a.id) in index})
    # Only the viewer's own Discover findings (4.1: Discover is per person).
    for f in (session.query(DiscoveryFinding)
              .filter(DiscoveryFinding.trip_id == trip.id,
                      DiscoveryFinding.user_id == uid).all()):
        push_items.append({"id": f"discovery:{f.id}", "source": "Discovery",
                           "label": f"{TOPIC_LABELS.get(f.topic, f.topic)} — {f.place_key}",
                           "kind": "Note",
                           "exists": _discovery_marker(f.topic, f.place_key) in index})
    pull_rows = []
    try:
        status, data, raw = client.get_collection(cid)
    except Exception:
        data = None
    if isinstance(data, dict):
        for rec in (data.get("transportations") or []):
            pull_rows.append({"id": f"transportation:{rec.get('id')}",
                              "kind": "Transportation", "name": rec.get("name") or "—",
                              "date": (rec.get("date") or "")[:10],
                              "detail": f"{rec.get('from_location') or '?'} → "
                                        f"{rec.get('to_location') or '?'}"})
        for rec in (data.get("lodging") or []):
            pull_rows.append({"id": f"lodging:{rec.get('id')}", "kind": "Lodging",
                              "name": rec.get("name") or "—",
                              "date": (rec.get("check_in") or "")[:10],
                              "detail": rec.get("location") or ""})
    return {"trip": {"id": trip.id, "name": trip.name},
            "push_items": push_items, "pull_rows": pull_rows,
            "adventurelog_url": _collection_url(cid, public_url)}


@router.get("/{trip_id}/adventurelog", response_class=HTMLResponse)
def adventurelog_sync_page(trip_id: str, request: Request,
                           session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None or not trip.adventurelog_collection_id:
        return _redirect(f"/trips/{trip_id}")
    clients = _al_clients(session, scope.user_of(request))
    client = clients.adventurelog
    if client is None:
        # A page, not a bare string (YONDER-202): the shared layout with the way back.
        return templates.TemplateResponse(
            request, "adventurelog.html",
            {"request": request, "trip": {"id": trip.id, "name": trip.name},
             "not_connected": _NOT_CONNECTED}, status_code=403)
    ctx = {"request": request, **_al_sync_ctx(session, trip, client, _viewer_uid(request),
                                              public_url=clients.public_url)}
    return templates.TemplateResponse(request, "adventurelog.html", ctx)


def _build_push_plan(session, trip, item_ids, collection_id, airports, photon, cache,
                     uid=None):
    """(kind, body, marker, is_note, label) for each selected item, markers stamped."""
    plan = []
    for raw in item_ids:
        source, _, ident = raw.partition(":")
        if source == "activity":
            a = session.get(TripActivity, ident)
            if a is None or a.trip_id != trip.id:
                continue
            kind, body = _al_body_for_activity(a, collection_id, airports, photon, cache)
            marker = _activity_marker(a.id)
            label = activity_types.get_type(a.type).title(a.values or {}) or a.type
        elif source == "discovery":
            f = session.get(DiscoveryFinding, ident)
            if f is None or f.trip_id != trip.id or f.user_id != uid:
                continue
            kind, body = "note", _finding_note_body(f, collection_id)
            marker = _discovery_marker(f.topic, f.place_key)
            label = f"{TOPIC_LABELS.get(f.topic, f.topic)} — {f.place_key}"
        else:
            continue
        _inject_marker(kind, body, marker)
        plan.append({"kind": kind, "body": body, "marker": marker,
                     "is_note": kind == "note", "label": label})
    return plan


@router.post("/{trip_id}/adventurelog/push", response_class=HTMLResponse)
def adventurelog_push(trip_id: str, request: Request,
                      item_ids: list[str] = Form(default=[]),
                      confirm: str = Form(""),
                      session: Session = Depends(get_session)):
    """Push selected Timeline activities + discovery findings into the collection,
    deduplicated by ownership marker. Transport/lodging update in place silently on
    a match; a Note that matches an existing note needs an explicit confirm (the
    warning panel) before it overwrites."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None or not trip.adventurelog_collection_id:
        return HTMLResponse("", status_code=404)
    client = _adventurelog(session, request)
    if client is None:
        return _push_msg(_NOT_CONNECTED)
    cid = trip.adventurelog_collection_id
    index = _al_collection_index(client, cid)
    airports, photon, cache = _al_airports_photon()
    plan = _build_push_plan(session, trip, item_ids, cid, airports, photon, cache,
                            uid=_viewer_uid(request))

    # Note collisions need confirmation unless already confirmed.
    collisions = [{"label": p["label"], "existing": index[p["marker"]]["name"]}
                  for p in plan if p["is_note"] and p["marker"] in index]
    if collisions and confirm != "1":
        return templates.TemplateResponse(request, "_al_push_confirm.html", {
            "request": request, "trip_id": trip_id, "collisions": collisions,
            "item_ids": item_ids})

    created = updated = failed = 0
    creators = {"transportation": client.create_transportation,
                "lodging": client.create_lodging, "note": client.create_note}
    updaters = {"transportation": client.update_transportation,
                "lodging": client.update_lodging, "note": client.update_note}
    for p in plan:
        hit = index.get(p["marker"])
        try:
            if hit:
                status, parsed, raw = updaters[p["kind"]](hit["id"], p["body"])
            else:
                status, parsed, raw = creators[p["kind"]](p["body"])
            if status >= 400:
                failed += 1
                log.warning("adventurelog-push: %s rejected (HTTP %s): %s",
                            p["kind"], status, raw[:200])
            elif hit:
                updated += 1
            else:
                created += 1
        except Exception:
            failed += 1
            log.exception("adventurelog-push: %s failed for trip %s", p["kind"], trip_id)
    return templates.TemplateResponse(request, "_al_sync_result.html", {
        "request": request, "trip_id": trip_id, "action": "Push",
        "created": created, "updated": updated, "failed": failed,
        "total": len(plan)})


@router.post("/{trip_id}/adventurelog/pull", response_class=HTMLResponse)
def adventurelog_pull(trip_id: str, request: Request,
                      record_ids: list[str] = Form(default=[]),
                      session: Session = Depends(get_session)):
    """Pull selected AdventureLog transport/lodging records into the Yonder
    Timeline as TripActivities. No dedup — Yonder is first-party (owner decision);
    re-pulling a record just adds another Timeline item."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None or not trip.adventurelog_collection_id:
        return HTMLResponse("", status_code=404)
    client = _adventurelog(session, request)
    if client is None:
        return _push_msg(_NOT_CONNECTED)
    try:
        status, data, raw = client.get_collection(trip.adventurelog_collection_id)
    except Exception as ex:
        return _push_msg(f"AdventureLog request failed: {ex}")
    by_id = {}
    if isinstance(data, dict):
        for rec in (data.get("transportations") or []):
            by_id[f"transportation:{rec.get('id')}"] = ("transportation", rec)
        for rec in (data.get("lodging") or []):
            by_id[f"lodging:{rec.get('id')}"] = ("lodging", rec)
    pulled = 0
    for rid in record_ids:
        hit = by_id.get(rid)
        if hit is None:
            continue
        kind, rec = hit
        session.add(_al_record_to_activity(rec, kind, trip.id))
        pulled += 1
    session.commit()
    return templates.TemplateResponse(request, "_al_sync_result.html", {
        "request": request, "trip_id": trip_id, "action": "Pull",
        "created": pulled, "updated": 0, "failed": 0, "total": len(record_ids)})


# ----------------------------------------------------------------- activities

def _activity_modal_ctx(request: Request, trip_id: str, *, mode: str,
                        action: str, atype: str, values: dict,
                        session: Session | None = None,
                        tagged: list[int] | None = None) -> dict:
    t = activity_types.get_type(atype)
    names = _roster_names(session, trip_id) if session is not None else {}
    # "Who is this for" only appears once the trip has more than one traveller;
    # an untagged activity (everyone) shows every box ticked.
    chosen = set(tagged or []) & set(names) or set(names)
    return {
        "travelers": ([{"id": tid, "name": n, "checked": tid in chosen}
                       for tid, n in sorted(names.items(), key=lambda kv: kv[1].lower())]
                      if len(names) > 1 else []),
        "request": request, "mode": mode,
        "title": "New activity" if mode == "create" else "Edit activity",
        "action": action, "trip_id": trip_id,
        "type": t.key, "type_options": activity_types.type_options(),
        "fields": _modal_fields(t, values),
    }


def _modal_fields(t: activity_types.ActivityType, values: dict) -> list[dict]:
    return [{"key": f["key"], "label": f["label"], "type": f["type"],
             "is_textarea": f["type"] == "textarea", "hint": f.get("hint", ""),
             "value": values.get(f["key"], "")} for f in t.fields]


@router.get("/{trip_id}/activity/new", response_class=HTMLResponse)
def new_activity(trip_id: str, request: Request,
                 type: str = activity_types.DEFAULT_TYPE,
                 session: Session = Depends(get_session)):
    if scope.trip_or_none(session, request, trip_id) is None:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_activity_modal.html",
        _activity_modal_ctx(request, trip_id, mode="create",
                            action=f"/trips/{trip_id}/activity",
                            atype=type, values={}, session=session))


@router.get("/{trip_id}/activity/from-link", response_class=HTMLResponse)
def activity_from_link(trip_id: str, request: Request, link: str = "",
                       session: Session = Depends(get_session)):
    """Add by Maps link (YONDER-200, D-168): a pasted Google/Apple place link
    re-renders the new-activity form with the type, name, address and Map link
    filled in. Nothing is saved here, so the person checks the type (a generic
    Tour when the category is unknown) and sets the time before saving."""
    if scope.trip_or_none(session, request, trip_id) is None:
        return HTMLResponse("", status_code=404)
    link = (link or "").strip()
    try:
        if not link:
            raise ValueError("")                  # nothing pasted: a blank form, no error
        atype, values = places_svc.activity_from_link(link)
    except ValueError as exc:
        ctx = _activity_modal_ctx(request, trip_id, mode="create",
                                  action=f"/trips/{trip_id}/activity",
                                  atype=activity_types.DEFAULT_TYPE, values={},
                                  session=session)
        ctx.update(link_value=link, link_error=str(exc))
        return templates.TemplateResponse(request, "_activity_modal.html", ctx)
    ctx = _activity_modal_ctx(request, trip_id, mode="create",
                              action=f"/trips/{trip_id}/activity", atype=atype,
                              values=values, session=session)
    ctx.update(link_value=link, from_link=True)
    return templates.TemplateResponse(request, "_activity_modal.html", ctx)


@router.get("/{trip_id}/activity/fields", response_class=HTMLResponse)
def activity_fields(trip_id: str, request: Request,
                    activity_kind: str = activity_types.DEFAULT_TYPE):
    """Re-render just the dynamic field block when the type dropdown changes.
    Switching type starts from empty inputs (as in the design) — the contract
    for what a flight vs a hotel carries is different, so old values would be
    meaningless under the new type. The selector is `activity_kind`, NOT `type`,
    because a detail field (lodging's sub-type) is itself named `type` and would
    otherwise shadow the selector in the posted form (Starlette .get() returns the
    LAST duplicate) — silently saving every manual lodging as a flight."""
    t = activity_types.get_type(activity_kind)
    return templates.TemplateResponse(request, "_activity_fields.html", {
        "request": request, "fields": _modal_fields(t, {})})


@router.get("/{trip_id}/activity/{activity_id}/edit", response_class=HTMLResponse)
def edit_activity(trip_id: str, activity_id: str, request: Request,
                  session: Session = Depends(get_session)):
    a = session.get(TripActivity, activity_id)
    if (a is None or a.trip_id != trip_id
            or scope.trip_or_none(session, request, trip_id) is None):
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_activity_modal.html",
        _activity_modal_ctx(request, trip_id, mode="edit",
                            action=f"/trips/{trip_id}/activity/{a.id}",
                            atype=a.type, values=a.values or {}, session=session,
                            tagged=who.activity_map(session, [a.id]).get(a.id)))


def _collect_values(t: activity_types.ActivityType, form) -> dict:
    """Pull only this type's declared fields out of the posted form."""
    out = {}
    for f in t.fields:
        raw = (form.get(f["key"]) or "").strip()
        if raw:
            out[f["key"]] = raw
    return out


@router.post("/{trip_id}/activity", response_class=HTMLResponse)
async def create_activity(trip_id: str, request: Request,
                          session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    form = await request.form()
    t = activity_types.get_type(form.get("activity_kind") or activity_types.DEFAULT_TYPE)
    values, geo = prepare_activity(t.key, _collect_values(t, form),
                                   session=session, trip_id=trip_id)
    a = TripActivity(trip_id=trip_id, type=t.key, values=values, geo=geo,
                     starts_at=values.get("when") or "")
    session.add(a)
    session.flush()
    if "travelers_shown" in form:
        who.set_travelers(session, a.id, trip_id, form.getlist("traveler_ids"))
    item = _item_or_none(session, request, trip_id, form.get("discovery_item_id") or "")
    if item is not None:
        item.activity_id = a.id          # the Discover row now shows "On timeline"
    session.commit()
    if item is not None:
        return _refresh()            # stay on the Discover section + city it came from
    return _redirect(f"/trips/{trip_id}")


@router.post("/{trip_id}/activity/{activity_id}", response_class=HTMLResponse)
async def update_activity(trip_id: str, activity_id: str, request: Request,
                          session: Session = Depends(get_session)):
    a = session.get(TripActivity, activity_id)
    if (a is None or a.trip_id != trip_id
            or scope.trip_or_none(session, request, trip_id) is None):
        return HTMLResponse("", status_code=404)
    form = await request.form()
    t = activity_types.get_type(form.get("activity_kind") or a.type)
    values, geo = prepare_activity(t.key, _collect_values(t, form),
                                   old_geo=a.geo if a.type == t.key else None,
                                   session=session, trip_id=trip_id, activity_id=a.id)
    a.type = t.key
    a.values = values
    a.geo = geo
    a.starts_at = values.get("when") or ""
    # Only touch the tags when the form showed the picker (a multi-traveller
    # trip); a solo trip's form never posts them, so nothing is cleared.
    if "travelers_shown" in form:
        who.set_travelers(session, a.id, trip_id, form.getlist("traveler_ids"))
    session.commit()
    return _redirect(f"/trips/{trip_id}")


@router.post("/{trip_id}/activity/{activity_id}/delete", response_class=HTMLResponse)
def delete_activity(trip_id: str, activity_id: str, request: Request,
                    session: Session = Depends(get_session)):
    a = session.get(TripActivity, activity_id)
    if (a is not None and a.trip_id == trip_id
            and scope.trip_or_none(session, request, trip_id) is not None):
        who.clear_activity(session, a.id)
        from discover import items as discovery_items
        discovery_items.unlink_activity(session, a.id)
        from app import expenses as expenses_svc
        expenses_svc.unlink_activity(session, a.id)   # SPEC-10: keep the expense, unlinked
        session.delete(a)
        session.commit()
    return _redirect(f"/trips/{trip_id}")
