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

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.models import (DiscoveryExclusion, DiscoveryFinding, DiscoveryRun, Trip,
                        TripActivity, TravelerIdentity, TripTraveler)
from app.trips import types as activity_types

log = logging.getLogger("yonder.trips")
router = APIRouter(prefix="/trips")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# Let Trips templates {% include %} the shared global header (_appheader.html)
# that lives with the review templates, so all surfaces keep one header.
from jinja2 import ChoiceLoader, FileSystemLoader  # noqa: E402

templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(Path(__file__).parent / "templates")),
    FileSystemLoader(str(Path(__file__).parent.parent / "review" / "templates")),
])

STATUS_LABELS = [("inprogress", "In progress"), ("upcoming", "Upcoming"),
                 ("past", "Past")]
STATUS_FILTERS = [("all", "All"), ("inprogress", "In progress"),
                  ("upcoming", "Upcoming"), ("past", "Past")]

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


def _trip_display(t: Trip) -> dict:
    notes = (t.notes or "").strip()
    return {
        "id": t.id, "name": t.name,
        "start_display": fmt_date(t.start_date) or "—",
        "end_display": fmt_date(t.end_date) or "—",
        "notes_preview": notes or "—",
    }


def _list_context(session: Session, q: str, status: str) -> dict:
    today = date.today().isoformat()
    trips = session.query(Trip).all()
    ql = q.lower().strip()
    if ql:
        trips = [t for t in trips if ql in (t.name or "").lower()]
    annotated = [(t, trip_status(t, today)) for t in trips]
    if status != "all":
        annotated = [(t, s) for (t, s) in annotated if s == status]

    groups = []
    for key, label in STATUS_LABELS:
        members = [t for (t, s) in annotated if s == key]
        # Upcoming/in-progress ascend by start; past descends (most recent first).
        members.sort(key=lambda t: (t.start_date or ""), reverse=(key == "past"))
        if members:
            groups.append({"label": label,
                           "trips": [_trip_display(t) for t in members]})

    total = session.query(Trip).count()
    return {
        "groups": groups,
        "has_visible": bool(groups),
        "has_any": total > 0,
        "search_no_results": total > 0 and not groups,
        "q": q, "status": status,
        "status_filters": [{"key": k, "label": l, "checked": status == k}
                           for k, l in STATUS_FILTERS],
    }


def _activity_display(a: TripActivity) -> dict:
    t = activity_types.get_type(a.type)
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
    }


def _detail_context(session: Session, trip: Trip) -> dict:
    acts = list(trip_activities(session, trip.id))
    # Sort by the denormalized starts_at; '' (undated) sorts to the end.
    acts.sort(key=lambda a: (a.starts_at or "￿"))
    day_groups: list[dict] = []
    index: dict[str, dict] = {}
    for a in acts:
        key = (a.starts_at or "")[:10] or "_unscheduled"
        grp = index.get(key)
        if grp is None:
            grp = {"label": "Date not set" if key == "_unscheduled"
                   else fmt_date(key), "activities": []}
            index[key] = grp
            day_groups.append(grp)
        grp["activities"].append(_activity_display(a))
    notes = (trip.notes or "").strip()
    return {
        "trip": {
            "id": trip.id, "name": trip.name,
            "start_display": fmt_date(trip.start_date) or "—",
            "end_display": fmt_date(trip.end_date) or "—",
            "notes": notes, "has_notes": bool(notes),
            "adventurelog_collection_id": trip.adventurelog_collection_id,
            "adventurelog_url": _collection_url(trip.adventurelog_collection_id),
        },
        "day_groups": day_groups,
        "has_activities": bool(acts),
        "linked_captures": _linked_captures(session, trip.id),
        **_push_state(trip),
        **_travelers_context(session, trip.id),
        **_discovery_context(session, trip.id),
        **_packing_context(session, trip.id),
    }


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


def _traveler_view(t: TravelerIdentity, role: str) -> dict:
    return {"id": t.id, "name": t.display_name, "kind": t.kind, "role": role,
            "role_label": _ROLE_LABELS.get(role, role.title()),
            "nationality": t.nationality, "generic": bool(t.generic)}


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
    travelers = [_traveler_view(idents[tid], role)
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


def _adventurelog():
    """The AdventureLog client, or None when the deployment hasn't configured
    one — same seam as review's _target_clients."""
    if not settings.adventurelog_api_key:
        return None
    from adapters.targets.adventurelog import AdventureLogClient
    return AdventureLogClient(settings.adventurelog_url,
                              settings.adventurelog_api_key)


def _airtrail():
    """AirTrail client, or None when unconfigured — same seam as _adventurelog."""
    if not settings.airtrail_token:
        return None
    from adapters.targets.airtrail import AirTrailClient
    return AirTrailClient(settings.airtrail_url, settings.airtrail_token)


def _photon():
    from resolve.geocode import PhotonGeocoder
    return PhotonGeocoder(settings.photon_url)


def _airport_resolver():
    """A complete `(code) -> Airport | None` lookup for location derivation
    (discover/places.py). AirTrail's flown-airport reference is checked first
    (free, in-memory), then its complete read-only airport DB (D-50) so a
    never-before-flown destination airport still resolves. Both are optional;
    with neither configured, airport codes simply drop and geocoding carries the
    rest — same graceful degradation as the push path's _resolve_coords."""
    airports: dict = {}
    at = _airtrail()
    if at is not None:
        try:
            airports = at.airport_reference()
        except Exception:
            airports = {}
    db = None
    if settings.airtrail_db_url:
        from adapters.targets.airtrail_db import AirTrailDB
        db = AirTrailDB(settings.airtrail_db_url)

    def resolve(code: str):
        key = (code or "").strip().upper()
        if not key:
            return None
        ap = airports.get(key)
        if ap is None:
            ap = next((a for a in airports.values()
                       if a.icao and a.icao.upper() == key), None)
        if ap is None and db is not None:
            try:
                ap = db.airport(key)
            except Exception:
                ap = None
        return ap

    return resolve


def _collection_url(collection_id: str | None) -> str | None:
    """Browser-facing link to a collection, or None when either the public base
    isn't set or the trip isn't linked."""
    base = (settings.adventurelog_public_url or "").rstrip("/")
    if not (base and collection_id):
        return None
    return f"{base}/collections/{collection_id}"


def _push_state(trip: Trip) -> dict:
    """What the detail header shows for AdventureLog: a link if already pushed,
    a Push button if we can, or a disabled button + reason if we can't yet."""
    linked = bool(trip.adventurelog_collection_id)
    configured = bool(settings.adventurelog_api_key)
    has_dates = bool(trip.start_date and trip.end_date)
    reason = ""
    if not linked:
        if not configured:
            reason = "AdventureLog isn't configured on this server."
        elif not has_dates:
            reason = "Add a start and end date first — AdventureLog needs both."
    return {
        "al_linked": linked,
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


# ----------------------------------------------------------------- list / detail

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", status: str = "all",
          session: Session = Depends(get_session)):
    ctx = {"request": request, **_list_context(session, q, status)}
    return templates.TemplateResponse(request, "list.html", ctx)


@router.get("/list", response_class=HTMLResponse)
def list_partial(request: Request, q: str = "", status: str = "all",
                 session: Session = Depends(get_session)):
    """Groups-only fragment for the live search + status filter."""
    ctx = {"request": request, **_list_context(session, q, status)}
    return templates.TemplateResponse(request, "_groups.html", ctx)


@router.get("/close", response_class=HTMLResponse)
def close_modal():
    """Clear the modal slot (HTMX has no built-in 'remove', so swap empty)."""
    return HTMLResponse("")


@router.get("/new", response_class=HTMLResponse)
def new_trip(request: Request, next: str = "/trips"):
    return templates.TemplateResponse(request, "_trip_modal.html", {
        "request": request, "mode": "create", "title": "New trip",
        "action": "/trips", "next": next,
        "form": {"name": "", "start": "", "end": "", "notes": "",
                 "collection_id": ""}, "error": None})


@router.get("/{trip_id}", response_class=HTMLResponse)
def detail(trip_id: str, request: Request,
           session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("<p class='bp-empty'>That trip no longer exists.</p>",
                            status_code=404)
    ctx = {"request": request, **_detail_context(session, trip)}
    return templates.TemplateResponse(request, "detail.html", ctx)


@router.get("/{trip_id}/derived-locations", response_class=HTMLResponse)
def derived_locations(trip_id: str, request: Request,
                      session: Session = Depends(get_session)):
    """The "What Yonder found" viewer (D-68): show the destination set Discovery
    DERIVED from this trip's own activities (D-62) — with per-place provenance
    and the places that couldn't be verified — so the operator sees the
    pipeline's starting point rather than guessing at it. On-demand modal, read
    from the same discover.places output a run would use, so it doubles as a
    pre-flight check."""
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_derived_locations.html",
                                      _derived_locations_ctx(request, session, trip))


def _excluded_keys(session: Session, trip_id: str) -> set[str]:
    return {e.place_key for e in session.query(DiscoveryExclusion)
            .filter(DiscoveryExclusion.trip_id == trip_id).all()}


def _derived_locations_ctx(request: Request, session: Session, trip: Trip) -> dict:
    from app.resolving import load_home_profiles
    from discover.home_filter import home_country_set, is_home_place
    from discover.places import derive_locations
    acts = trip_activities(session, trip.id)
    result = derive_locations(acts, resolve_airport=_airport_resolver(),
                              geocoder=_photon())
    profiles = load_home_profiles(session)
    hc = home_country_set(result.places, profiles, trip.start_date)
    excluded = _excluded_keys(session, trip.id)
    places = [{
        "label": p.label, "city": p.city, "country": p.country,
        "lat": p.lat, "lon": p.lon, "key": p.place_key,
        "sources": [f"{pr['activity_type']} · {pr['field']}"
                    for pr in p.provenance],
        "is_home": is_home_place(p, profiles, trip.start_date),
        "home_country": bool(p.country and p.country.upper() in hc),
        "excluded": p.place_key in excluded,
    } for p in result.places]
    return {
        "request": request, "trip_id": trip.id, "trip_name": trip.name,
        "places": places, "countries": result.countries,
        "dropped": result.dropped,
        "has_home": any(p["is_home"] or p["home_country"] for p in places),
    }


@router.post("/{trip_id}/derived-locations/toggle", response_class=HTMLResponse)
def toggle_excluded_place(trip_id: str, place_key: str = Form(""),
                          request: Request = None,
                          session: Session = Depends(get_session)):
    """Exclude/include a derived place, then re-render the viewer. Excluding also
    drops that place's DRAFT findings for instant feedback; country-scope findings
    reconcile on the next run (the runner prunes stale drafts)."""
    trip = session.get(Trip, trip_id)
    if trip is None or not place_key:
        return HTMLResponse("", status_code=404)
    existing = (session.query(DiscoveryExclusion)
                .filter(DiscoveryExclusion.trip_id == trip_id,
                        DiscoveryExclusion.place_key == place_key).first())
    if existing is not None:
        session.delete(existing)
    else:
        session.add(DiscoveryExclusion(trip_id=trip_id, place_key=place_key))
        # Instant feedback: remove this place's own draft findings now.
        for f in (session.query(DiscoveryFinding)
                  .filter(DiscoveryFinding.trip_id == trip_id,
                          DiscoveryFinding.place_key == place_key,
                          DiscoveryFinding.status == "draft").all()):
            session.delete(f)
    session.commit()
    return templates.TemplateResponse(request, "_derived_locations.html",
                                      _derived_locations_ctx(request, session, trip))


# --------------------------------------------------------------- discovery (3.0)

TOPIC_ORDER = ["weather", "currency", "plugs", "visa", "safety", "events",
               "food", "language", "language_regional", "language_local", "packing"]
TOPIC_LABELS = {
    "weather": "Weather", "currency": "Currency", "plugs": "Plugs & voltage",
    "visa": "Visa & vaccination", "safety": "Safety & transport",
    "events": "Events & activities", "food": "Food & drink", "language": "Language",
    "language_regional": "Regional language", "language_local": "Local phrases & register",
    "packing": "Packing",
}
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


def _finding_view(f: DiscoveryFinding) -> dict:
    data = f.data or {}
    return {
        "id": f.id, "topic": f.topic, "place_key": f.place_key,
        "scope": f.scope, "status": f.status, "confidence": f.confidence,
        "body": _render_md(f.body_md), "sources": f.sources or [],
        "is_currency": f.topic == "currency",
        "is_language": f.topic == "language",
        # Language findings aren't per-finding re-runnable: national detection is
        # a pure function of country (re-run can only clobber a manual Languages
        # pick), and the regional/local overlays key on the trip's places. The
        # Languages picker is the tool for those; other topics keep Re-run.
        "rerunnable": not f.topic.startswith("language"),
        "has_rates": bool(data.get("rates")),
    }


def _run_view(run: DiscoveryRun) -> dict:
    return {"status": run.status, "phase": run.phase, "done": run.done,
            "total": run.total,
            "when": run.finished_at.strftime("%Y-%m-%d %H:%M")
            if run.finished_at else ""}


def _discovery_context(session: Session, trip_id: str) -> dict:
    findings = (session.query(DiscoveryFinding)
                .filter(DiscoveryFinding.trip_id == trip_id).all())
    grouped: dict[str, list] = {}
    for f in findings:
        grouped.setdefault(f.topic, []).append(f)
    topic_groups = []
    for topic in TOPIC_ORDER:
        fs = grouped.get(topic)
        if fs:
            fs.sort(key=lambda x: x.place_key or "")
            topic_groups.append({"topic": topic,
                                 "label": TOPIC_LABELS.get(topic, topic),
                                 "findings": [_finding_view(f) for f in fs]})
    last = (session.query(DiscoveryRun)
            .filter(DiscoveryRun.trip_id == trip_id)
            .order_by(DiscoveryRun.started_at.desc()).first())
    running = last is not None and last.status == "running"
    run_progress = None
    if running:
        pct = int(100 * last.done / last.total) if last.total else 0
        run_progress = {"phase": last.phase or "starting", "done": last.done,
                        "total": last.total, "pct": pct}
    return {"discovery_topics": topic_groups,
            "discovery_has_findings": bool(findings),
            "discovery_last": _run_view(last) if last else None,
            "discovery_running": running,
            "discovery_run": run_progress}


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
           **_discovery_context(session, trip.id)}
    return templates.TemplateResponse(request, "_discovery_section.html", ctx)


@router.post("/{trip_id}/discover", response_class=HTMLResponse)
def run_discovery_route(trip_id: str, request: Request,
                        session: Session = Depends(get_session)):
    """Launch a Discovery pass — BOTH tiers on a background worker (§6.1) — and
    return the self-polling section. The deterministic findings land in the first
    few seconds; the LLM findings stream in as each resolver finishes. A run is
    already in flight for this trip? Just show its panel (one run per trip)."""
    from app.resolving import load_home_profiles
    from app.trips import discovery_jobs as dj
    from datetime import date as _date
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    if dj.running_run(session, trip_id) is None:
        profile = _discovery_profile_row(session)
        ident = _self_identity(session)
        dj.launch(trip_id, resolve_airport=_airport_resolver(), geocoder=_photon(),
                  det_resolvers=_resolvers(), llm_resolvers=_llm_resolvers(),
                  home_profiles=load_home_profiles(session),
                  excluded=_excluded_keys(session, trip_id),
                  discovery_profile=(profile.values if profile else None),
                  nationality=(ident.nationality if ident else None),
                  base_currencies=settings.base_currency_list,
                  today=_date.today().isoformat())
    return _discovery_section(request, session, trip)


@router.get("/{trip_id}/discover/status", response_class=HTMLResponse)
def discovery_status_route(trip_id: str, request: Request,
                           session: Session = Depends(get_session)):
    """Poll target for the running-run panel. Reading it also settles an orphaned
    run (restart → failed). When the run is no longer `running`, the returned
    section carries no poll trigger, so the panel stops on its own."""
    from app.trips import discovery_jobs as dj
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    dj.running_run(session, trip_id)          # settle orphans as a side effect
    return _discovery_section(request, session, trip)


def _finding_or_404(session: Session, trip_id: str, fid: str) -> DiscoveryFinding | None:
    f = session.get(DiscoveryFinding, fid)
    return f if (f is not None and f.trip_id == trip_id) else None


@router.post("/{trip_id}/finding/{fid}/rerun", response_class=HTMLResponse)
def rerun_finding_route(trip_id: str, fid: str,
                        session: Session = Depends(get_session)):
    """Regenerate this one finding (the per-topic re-run). Re-fetches live topics
    (weather/currency) and overrides a manual edit — an explicit fresh copy."""
    from discover.runner import rerun_finding
    f = _finding_or_404(session, trip_id, fid)
    if f is None:
        return HTMLResponse("", status_code=404)
    # Language findings aren't re-runnable (see _finding_view) — never let a
    # stale/forged POST overwrite a manual Languages pick.
    if f.topic.startswith("language"):
        return _redirect(f"/trips/{trip_id}")
    rerun_finding(session, f, resolve_airport=_airport_resolver(),
                  geocoder=_photon(), resolvers=_resolvers(),
                  base_currencies=settings.base_currency_list)
    return _redirect(f"/trips/{trip_id}")


@router.get("/{trip_id}/finding/{fid}/edit", response_class=HTMLResponse)
def edit_finding(trip_id: str, fid: str, request: Request,
                 session: Session = Depends(get_session)):
    f = _finding_or_404(session, trip_id, fid)
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
    return templates.TemplateResponse(request, "_finding_edit.html", {
        "request": request, "trip_id": trip_id, "fid": fid,
        "topic_label": TOPIC_LABELS.get(f.topic, f.topic),
        "place_key": f.place_key, "body": f.body_md or ""})


@router.post("/{trip_id}/finding/{fid}/edit", response_class=HTMLResponse)
def save_finding(trip_id: str, fid: str, body: str = Form(""),
                 langs: list[str] = Form(default=[]),
                 session: Session = Depends(get_session)):
    f = _finding_or_404(session, trip_id, fid)
    if f is None:
        return HTMLResponse("", status_code=404)
    if f.topic == "language":
        # Rebuild the phrasebook from the picked languages (a manual override,
        # not "official"); marking edited protects it from a whole-trip re-run.
        from discover.resolvers.language import build_language_body
        from discover import langdata
        label = _bold_label(f.body_md) or f.place_key
        f.body_md, f.data, f.confidence = build_language_body(
            label, langs, official=False, phrasebook=langdata.phrasebook(session))
    else:
        f.body_md = body
    f.status = "edited"
    session.commit()
    return _redirect(f"/trips/{trip_id}")


@router.get("/{trip_id}/finding/{fid}/calculator", response_class=HTMLResponse)
def currency_calculator(trip_id: str, fid: str, request: Request,
                        session: Session = Depends(get_session)):
    """The per-trip currency calculator (D-65). Computes client-side from the
    STORED rate snapshot — no live call — pivoting every currency through CAD so
    any pair converts, including local↔local on a multi-country trip."""
    f = _finding_or_404(session, trip_id, fid)
    if f is None:
        return HTMLResponse("", status_code=404)
    cur_findings = (session.query(DiscoveryFinding)
                    .filter(DiscoveryFinding.trip_id == trip_id,
                            DiscoveryFinding.topic == "currency").all())
    # rates[code][base] = units of `code` per 1 `base` (from the snapshots).
    rates: dict[str, dict[str, float]] = {}
    as_of = None
    for cf in cur_findings:
        d = cf.data or {}
        as_of = as_of or d.get("date")
        for code, per in (d.get("rates") or {}).items():
            rates[code] = {b.replace("per_", ""): v for b, v in per.items()}
    # Pivot table: units of each currency per 1 CAD.
    per_cad: dict[str, float] = {"CAD": 1.0}
    usd_per_cad = None
    for code, br in rates.items():
        if br.get("CAD"):
            per_cad[code] = br["CAD"]
        if usd_per_cad is None and br.get("CAD") and br.get("USD"):
            usd_per_cad = br["CAD"] / br["USD"]
    if rates.get("USD", {}).get("CAD"):
        usd_per_cad = rates["USD"]["CAD"]
    if usd_per_cad:
        per_cad["USD"] = usd_per_cad
    # Dropdown order: bases first, then destination currencies.
    bases = [b for b in settings.base_currency_list if b in per_cad]
    dests = sorted(c for c in per_cad if c not in bases)
    currencies = bases + dests
    import json as _json
    return templates.TemplateResponse(request, "_calculator.html", {
        "request": request, "trip_id": trip_id,
        "currencies": currencies, "per_cad_json": Markup(_json.dumps(per_cad)),
        "as_of": as_of or "—",
        "default_from": bases[0] if bases else (currencies[0] if currencies else ""),
        "default_to": dests[0] if dests else (currencies[-1] if currencies else ""),
    })


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


def _packing_context(session: Session, trip_id: str) -> dict:
    items = (session.query(PackingItem)
             .filter(PackingItem.trip_id == trip_id).all())
    plan = (session.query(PackingPlan)
            .filter(PackingPlan.trip_id == trip_id)
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
def generate_packing_plan_route(trip_id: str,
                                session: Session = Depends(get_session)):
    """Generate (or re-generate) the Packing Plan and reload the detail. Preserves
    your ticked-off / removed items."""
    from app.resolving import load_home_profiles
    from planning.runner import generate_plan
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    generate_plan(session, trip, resolve_airport=_airport_resolver(),
                  geocoder=_photon(), home_profiles=load_home_profiles(session))
    return _redirect(f"/trips/{trip_id}")


def _current_archetype_ids(session: Session, trip_id: str) -> list[str]:
    plan = (session.query(PackingPlan).filter(PackingPlan.trip_id == trip_id)
            .order_by(PackingPlan.generated_at.desc()).first())
    return [a.get("id") for a in ((plan.archetypes if plan else None) or [])
            if a.get("id")]


def _regenerate_with_archetypes(request: Request, session: Session, trip: Trip,
                                ids: list[str]) -> HTMLResponse:
    from app.resolving import load_home_profiles
    from planning.runner import generate_plan
    generate_plan(session, trip, resolve_airport=_airport_resolver(),
                  geocoder=_photon(), home_profiles=load_home_profiles(session),
                  manual_archetypes=ids)
    return templates.TemplateResponse(request, "_packing_section.html",
        {"request": request, "trip": {"id": trip.id},
         **_packing_context(session, trip.id)})


@router.post("/{trip_id}/pack/archetypes/add", response_class=HTMLResponse)
def add_archetype_route(trip_id: str, request: Request, archetype_id: str = Form(""),
                        session: Session = Depends(get_session)):
    """Add a trip type to the packing plan and re-generate with the operator's
    explicit set (auto-detection stays as the starting suggestion)."""
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    valid = {a["id"] for a in _pack_all_archetypes()}
    ids = _current_archetype_ids(session, trip_id)
    if archetype_id in valid and archetype_id not in ids:
        ids.append(archetype_id)
    return _regenerate_with_archetypes(request, session, trip, ids)


@router.post("/{trip_id}/pack/archetypes/remove", response_class=HTMLResponse)
def remove_archetype_route(trip_id: str, request: Request, archetype_id: str = Form(""),
                           session: Session = Depends(get_session)):
    """Remove a trip type and re-generate. Removing the last one resets to
    auto-detection on the next generate."""
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    ids = [i for i in _current_archetype_ids(session, trip_id) if i != archetype_id]
    return _regenerate_with_archetypes(request, session, trip, ids)


def _pack_all_archetypes():
    from planning.engine import all_archetypes
    return all_archetypes()


def _pack_count_oob(session: Session, trip_id: str, category: str) -> str:
    """An out-of-band swap for a category's packed/total badge, so an inline
    row change keeps the header count honest without a page reload."""
    rows = (session.query(PackingItem)
            .filter(PackingItem.trip_id == trip_id,
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
    return HTMLResponse(row_html + _pack_count_oob(session, trip_id, i.category))


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
    if i is None or i.trip_id != trip_id:
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
    if i is None or i.trip_id != trip_id:
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
    if i is None or i.trip_id != trip_id:
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
    if i is None or i.trip_id != trip_id:
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
        trip = session.get(Trip, trip_id)
        return templates.TemplateResponse(request, "_packing_section.html",
                                          {"request": request, "trip": trip,
                                           **_packing_context(session, trip_id)})
    row_html = "" if action == "remove" else templates.env.get_template(
        "_packing_item.html").render(
            trip_id=trip_id, i=_packing_item_view(i, _bag_label_lookup(session)),
            packing_bag_options=_bag_options(session))
    return HTMLResponse(row_html + _pack_count_oob(session, trip_id, category))


@router.post("/{trip_id}/pack-item/add", response_class=HTMLResponse)
def packing_item_add(trip_id: str, request: Request, category: str = Form(...),
                     label: str = Form(...), quantity: int = Form(1),
                     bag: str = Form("checked"),
                     session: Session = Depends(get_session)):
    """Add a custom line to a category — the other half of a line-item edit
    ("remove the jeans, add a pair of slacks instead") that doesn't fit into
    editing an existing row. Never touched by a re-generate (`edited=True`)."""
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    label = label.strip()[:128]
    if (category not in PACK_CAT_LABELS or not label
            or bag not in _bag_valid_values(session)):
        return HTMLResponse("", status_code=400)
    item = PackingItem(trip_id=trip_id, item_key=f"custom:{uuid.uuid4().hex}",
                       label=label, category=category,
                       quantity=max(1, min(99, quantity)), bag=bag,
                       source="custom", status="draft", edited=True)
    session.add(item)
    session.commit()
    return templates.TemplateResponse(request, "_packing_section.html",
        {"request": request, "trip": trip, **_packing_context(session, trip_id)})


def _profile_row(session: Session) -> PackingProfile | None:
    return (session.query(PackingProfile)
            .order_by(PackingProfile.created_at).first())


def _profile_form_ctx(request: Request, session: Session, saved: bool = False) -> dict:
    from planning.engine import default_profile
    from planning import catalog
    row = _profile_row(session)
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
        "request": request, "saved": saved,
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
    row = _profile_row(session)
    if row is None:
        session.add(PackingProfile(values=values))
    else:
        row.values = values
    session.commit()
    # Re-render the page with a saved banner; the plan applies the new profile on
    # the next Re-generate.
    return templates.TemplateResponse(request, "packing_profile.html",
                                      _profile_form_ctx(request, session, saved=True))


# ---- Discovery Profile editor (3.2, SPEC-3.2 §11.1) ------------------------

def _discovery_profile_row(session: Session) -> DiscoveryProfile | None:
    """Single-user for now (D-89): the first row is the deployment-wide default,
    same convention as _profile_row for packing."""
    return (session.query(DiscoveryProfile)
            .order_by(DiscoveryProfile.created_at).first())


def _discovery_form_ctx(request: Request, session: Session,
                        saved: bool = False) -> dict:
    row = _discovery_profile_row(session)
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
        "request": request, "saved": saved,
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
    }
    row = _discovery_profile_row(session)
    if row is None:
        session.add(DiscoveryProfile(values=values))
    else:
        row.values = values
    session.commit()
    return templates.TemplateResponse(request, "discovery_profile.html",
                                      _discovery_form_ctx(request, session, saved=True))


@router.get("/{trip_id}/edit", response_class=HTMLResponse)
def edit_trip(trip_id: str, request: Request, next: str = "",
              session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_trip_modal.html", {
        "request": request, "mode": "edit", "title": "Edit trip",
        "action": f"/trips/{trip.id}", "next": next or f"/trips/{trip.id}",
        "form": {"name": trip.name, "start": trip.start_date or "",
                 "end": trip.end_date or "", "notes": trip.notes or "",
                 "collection_id": trip.adventurelog_collection_id or ""},
        "error": None})


# ----------------------------------------------------------------- trip writes

def _save_trip_fields(trip: Trip, name: str, start: str, end: str, notes: str,
                      collection_id: str = ""):
    trip.name = name.strip()
    trip.start_date = start.strip() or None
    trip.end_date = end.strip() or None
    trip.notes = notes.strip() or None
    trip.adventurelog_collection_id = collection_id.strip() or None


@router.post("", response_class=HTMLResponse)
def create_trip(request: Request, name: str = Form(""), start: str = Form(""),
                end: str = Form(""), notes: str = Form(""),
                adventurelog_collection_id: str = Form(""),
                next: str = Form("/trips"),
                session: Session = Depends(get_session)):
    if not name.strip():
        return templates.TemplateResponse(request, "_trip_modal.html", {
            "request": request, "mode": "create", "title": "New trip",
            "action": "/trips", "next": next,
            "form": {"name": name, "start": start, "end": end, "notes": notes,
                     "collection_id": adventurelog_collection_id},
            "error": "A trip needs a name."})
    trip = Trip(source="manual")
    _save_trip_fields(trip, name, start, end, notes, adventurelog_collection_id)
    session.add(trip)
    session.flush()                       # need trip.id for the self-traveller link
    _auto_link_self(session, trip.id)     # §11.2 — you're on your own trip by default
    session.commit()
    return _redirect("/trips")


@router.post("/{trip_id}", response_class=HTMLResponse)
def update_trip(trip_id: str, request: Request, name: str = Form(""),
                start: str = Form(""), end: str = Form(""), notes: str = Form(""),
                adventurelog_collection_id: str = Form(""),
                next: str = Form(""), session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    dest = next or f"/trips/{trip.id}"
    if not name.strip():
        return templates.TemplateResponse(request, "_trip_modal.html", {
            "request": request, "mode": "edit", "title": "Edit trip",
            "action": f"/trips/{trip.id}", "next": dest,
            "form": {"name": name, "start": start, "end": end, "notes": notes,
                     "collection_id": adventurelog_collection_id},
            "error": "A trip needs a name."})
    _save_trip_fields(trip, name, start, end, notes, adventurelog_collection_id)
    session.commit()
    return _redirect(dest)


@router.post("/{trip_id}/delete", response_class=HTMLResponse)
def delete_trip(trip_id: str, session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is not None:
        # Explicit child delete: SQLite in tests does not enforce ON DELETE
        # CASCADE unless PRAGMA foreign_keys is on, so never rely on the DB to
        # sweep the activities.
        session.query(TripActivity).filter(
            TripActivity.trip_id == trip_id).delete(synchronize_session=False)
        session.query(TripTraveler).filter(
            TripTraveler.trip_id == trip_id).delete(synchronize_session=False)
        # Clear the soft link from any captured items filed into this trip
        # (2.1.2) — there's no DB FK to null it for us (migration 0008).
        from app.models import TravelItem
        session.query(TravelItem).filter(
            TravelItem.trip_id == trip_id).update({"trip_id": None},
                                                  synchronize_session=False)
        session.delete(trip)
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
    trip = session.get(Trip, trip_id)
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
    trip = session.get(Trip, trip_id)
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
    """Drop a traveller from the trip roster. Swaps just the section."""
    row = session.get(TripTraveler,
                      {"trip_id": trip_id, "traveler_id": traveler_id})
    if row is not None:
        session.delete(row)
        session.commit()
    return _travelers_section(request, session, trip_id)


def _push_msg(text: str) -> HTMLResponse:
    """Inline failure notice swapped next to the Push button (a success answers
    with an HX-Redirect that reloads the detail showing the new link)."""
    return HTMLResponse(f'<span class="bp-push-err">{text}</span>')


# Every Yonder activity type becomes a Transportation, except lodging (its own
# AdventureLog object) and tour/event/dining/note, which fall through to a
# Note — AdventureLog has no generic "activity" object to hold those.
_AL_TRANSPORT_TYPE = {
    "flight": "plane", "train": "train", "drive": "car",
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
    link = v.get("sourceLink") or None
    detail_lines = [f"Confirmation: {v['confirmation']}"] if v.get("confirmation") else []
    if v.get("notes"):
        detail_lines.append(v["notes"])
    description = "\n\n".join(detail_lines) or None

    if activity.type in _AL_TRANSPORT_TYPE:
        name = f"{title} – {subtitle}" if subtitle else title
        origin = v.get("from") or v.get("pickupLocation") or v.get("departurePort")
        dest = v.get("to") or v.get("dropoffLocation") or v.get("arrivalPort")
        origin_lat, origin_lon = _resolve_coords(origin, airports, photon, coord_cache)
        dest_lat, dest_lon = _resolve_coords(dest, airports, photon, coord_cache)
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
        body = {
            "name": v.get("name") or title, "check_in": starts, "check_out": ends,
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


def _push_activities(client, collection_id: str, trip_id: str,
                     activities: list[TripActivity]) -> None:
    """Best-effort: the collection is already created and linked by the time
    this runs, so one bad activity logs and moves on rather than losing the
    ones that would otherwise succeed."""
    airtrail = _airtrail()
    airports = {}
    if airtrail is not None:
        try:
            airports = airtrail.airport_reference()
        except Exception:
            log.warning("push-adventurelog: AirTrail airport reference unavailable "
                       "for trip %s, falling back to Photon for every endpoint", trip_id)
    photon = _photon()
    coord_cache: dict = {}
    create = {"transportation": client.create_transportation,
             "lodging": client.create_lodging, "note": client.create_note}
    failed = 0
    for activity in activities:
        kind, body = _al_body_for_activity(activity, collection_id,
                                           airports, photon, coord_cache)
        try:
            status, parsed, raw = create[kind](body)
            if status >= 400:
                failed += 1
                log.warning("push-adventurelog: %s for trip %s rejected (HTTP %s): %s",
                           kind, trip_id, status, raw[:300])
        except Exception:
            failed += 1
            log.exception("push-adventurelog: %s failed for trip %s", kind, trip_id)
    if failed:
        log.warning("push-adventurelog: %d/%d activities failed for trip %s",
                    failed, len(activities), trip_id)


@router.post("/{trip_id}/push-adventurelog", response_class=HTMLResponse)
def push_adventurelog(trip_id: str, session: Session = Depends(get_session)):
    """Create a PUBLIC AdventureLog collection from this trip and link it — the
    collection ONLY, empty. Pushing the trip's activities and discovery findings
    into it is done afterwards from the managed sync page (/adventurelog), which
    deduplicates against what's already there. (Before 3.8 this route also did a
    one-shot activity push; that moved to the sync page so even the first push is
    selective and dup-safe.)

    Public by default so the collection can be direct-linked from outside
    AdventureLog. Idempotent on the collection itself: a trip already linked is
    left alone — we never create a second collection for the same trip."""
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    if trip.adventurelog_collection_id:
        return _redirect(f"/trips/{trip_id}")           # already linked
    client = _adventurelog()
    if client is None:
        return _push_msg("AdventureLog isn't configured on this server.")
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
                  "sourceLink": rec.get("link") or ""}
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
                  "sourceLink": rec.get("link") or ""}
    values = {k: v for k, v in values.items() if v}
    return TripActivity(trip_id=trip_id, type=ytype, values=values,
                        starts_at=values.get("when") or "")


def _al_airports_photon():
    airtrail = _airtrail()
    airports = {}
    if airtrail is not None:
        try:
            airports = airtrail.airport_reference()
        except Exception:
            log.warning("adventurelog-sync: AirTrail airport reference unavailable")
    return airports, _photon(), {}


def _al_sync_ctx(session: Session, trip: Trip, client) -> dict:
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
    for f in (session.query(DiscoveryFinding)
              .filter(DiscoveryFinding.trip_id == trip.id).all()):
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
            "adventurelog_url": _collection_url(cid)}


@router.get("/{trip_id}/adventurelog", response_class=HTMLResponse)
def adventurelog_sync_page(trip_id: str, request: Request,
                           session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is None or not trip.adventurelog_collection_id:
        return _redirect(f"/trips/{trip_id}")
    client = _adventurelog()
    if client is None:
        return HTMLResponse("AdventureLog isn't configured on this server.",
                            status_code=503)
    ctx = {"request": request, **_al_sync_ctx(session, trip, client)}
    return templates.TemplateResponse(request, "adventurelog.html", ctx)


def _build_push_plan(session, trip, item_ids, collection_id, airports, photon, cache):
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
            if f is None or f.trip_id != trip.id:
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
    trip = session.get(Trip, trip_id)
    if trip is None or not trip.adventurelog_collection_id:
        return HTMLResponse("", status_code=404)
    client = _adventurelog()
    if client is None:
        return _push_msg("AdventureLog isn't configured on this server.")
    cid = trip.adventurelog_collection_id
    index = _al_collection_index(client, cid)
    airports, photon, cache = _al_airports_photon()
    plan = _build_push_plan(session, trip, item_ids, cid, airports, photon, cache)

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
    trip = session.get(Trip, trip_id)
    if trip is None or not trip.adventurelog_collection_id:
        return HTMLResponse("", status_code=404)
    client = _adventurelog()
    if client is None:
        return _push_msg("AdventureLog isn't configured on this server.")
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
                        action: str, atype: str, values: dict) -> dict:
    t = activity_types.get_type(atype)
    return {
        "request": request, "mode": mode,
        "title": "New activity" if mode == "create" else "Edit activity",
        "action": action, "trip_id": trip_id,
        "type": t.key, "type_options": activity_types.type_options(),
        "fields": _modal_fields(t, values),
    }


def _modal_fields(t: activity_types.ActivityType, values: dict) -> list[dict]:
    return [{"key": f["key"], "label": f["label"], "type": f["type"],
             "is_textarea": f["type"] == "textarea",
             "value": values.get(f["key"], "")} for f in t.fields]


@router.get("/{trip_id}/activity/new", response_class=HTMLResponse)
def new_activity(trip_id: str, request: Request,
                 type: str = activity_types.DEFAULT_TYPE):
    return templates.TemplateResponse(request, "_activity_modal.html",
        _activity_modal_ctx(request, trip_id, mode="create",
                            action=f"/trips/{trip_id}/activity",
                            atype=type, values={}))


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
    if a is None or a.trip_id != trip_id:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_activity_modal.html",
        _activity_modal_ctx(request, trip_id, mode="edit",
                            action=f"/trips/{trip_id}/activity/{a.id}",
                            atype=a.type, values=a.values or {}))


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
    trip = session.get(Trip, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    form = await request.form()
    t = activity_types.get_type(form.get("activity_kind") or activity_types.DEFAULT_TYPE)
    values = _collect_values(t, form)
    a = TripActivity(trip_id=trip_id, type=t.key, values=values,
                     starts_at=values.get("when") or "")
    session.add(a)
    session.commit()
    return _redirect(f"/trips/{trip_id}")


@router.post("/{trip_id}/activity/{activity_id}", response_class=HTMLResponse)
async def update_activity(trip_id: str, activity_id: str, request: Request,
                          session: Session = Depends(get_session)):
    a = session.get(TripActivity, activity_id)
    if a is None or a.trip_id != trip_id:
        return HTMLResponse("", status_code=404)
    form = await request.form()
    t = activity_types.get_type(form.get("activity_kind") or a.type)
    values = _collect_values(t, form)
    a.type = t.key
    a.values = values
    a.starts_at = values.get("when") or ""
    session.commit()
    return _redirect(f"/trips/{trip_id}")


@router.post("/{trip_id}/activity/{activity_id}/delete", response_class=HTMLResponse)
def delete_activity(trip_id: str, activity_id: str,
                    session: Session = Depends(get_session)):
    a = session.get(TripActivity, activity_id)
    if a is not None and a.trip_id == trip_id:
        session.delete(a)
        session.commit()
    return _redirect(f"/trips/{trip_id}")
