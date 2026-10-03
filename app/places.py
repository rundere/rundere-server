"""Run·dere 4.7 — saved places service (YONDER-200, D-166/D-167).

The one place saving, listing and filing logic lives, so the JSON API (and later
the web capture page and MCP) never diverge. A saved place is a Maps link the
person kept before choosing a trip: deterministic parse (extract/place_url.py),
no extraction pipeline, no approval queue (D-167). Filing it to a trip builds a
timeline activity and consumes the row.

Scoping is own-only (as the journal, D-134) and stricter on one point: no admin
tier, so one person's wishlist is never visible to another. Ownerless rows are
closed to everyone but the auth-off single-user world (YONDER-186).

Bad input raises ValueError (the API maps it to 400); a row that isn't yours
404s exactly like a missing one.
"""
from __future__ import annotations

from sqlalchemy import true
from sqlalchemy.orm import Session

from app import scope
from app.models import SavedPlace
from discover.items import name_key
from extract import place_url

_PROVIDERS = ("google", "apple")


def visible(user):
    """A predicate for `session.query(SavedPlace).filter(...)` — own rows only."""
    if scope.unrestricted(user):
        return true()
    return SavedPlace.owner_id == user.id


def require_place(session: Session, user, place_id: str) -> SavedPlace:
    from fastapi import HTTPException
    p = session.get(SavedPlace, place_id)
    if p is None or not _can_access(user, p):
        raise HTTPException(status_code=404, detail="saved place not found")
    return p


def _can_access(user, place: SavedPlace) -> bool:
    return scope.unrestricted(user) or place.owner_id == user.id


def place_key(provider: str, place_id: str | None, name: str | None,
              lat: float | None, lon: float | None) -> str:
    """Stable identity for re-save detection: the provider's own id when the link
    has one, else name plus coordinates rounded to ~11 m."""
    if place_id:
        return f"{provider}:{place_id}"[:200]
    where = f"{lat:.4f},{lon:.4f}" if lat is not None and lon is not None else ""
    return f"{provider}:{name_key(name)}:{where}"[:200]


def _clean(v, n: int) -> str | None:
    v = (v or "").strip() if isinstance(v, str) else ""
    return v[:n] or None


def _web_url(v, n: int = 2000) -> str | None:
    """Only an http(s) link is kept as a place's link: it is rendered as an href
    (the /places page) and becomes the activity's Map button, so a `javascript:`
    or other scheme from a client must never get that far."""
    v = _clean(v, n)
    return v if v and v.lower().startswith(("http://", "https://")) else None


def _coord(v, lo: float, hi: float) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


def resolve_fields(*, url: str | None = None, name: str | None = None, lat=None,
                   lon=None, address: str | None = None, category: str | None = None,
                   provider: str | None = None, source_url: str | None = None,
                   enrich: bool = False, geocoder=None) -> dict:
    """Everything known about a place, before anything is stored: the URL parse,
    overridden by whatever the client already resolved (iOS MapKit), then — only
    if `enrich` and a verified Photon hit exists — the address and category the
    link didn't carry (D-146: optional, never overrides, never required)."""
    parsed: dict = {}
    if url:
        link = place_url.resolved_url(url)
        parsed = place_url.parse_place_url(link) or {}
        if not parsed:
            raise ValueError("that isn't a Google or Apple Maps place link")
        source_url = source_url or link
    nm = _clean(name, 300) or parsed.get("name")
    la = _coord(lat, -90, 90) if lat is not None else parsed.get("lat")
    lo = _coord(lon, -180, 180) if lon is not None else parsed.get("lon")
    if la is None or lo is None:
        la = lo = None                      # a half pair is no pin
    if not nm and la is None:
        raise ValueError("a place needs a name or coordinates")
    addr = _clean(address, 500) or parsed.get("address")
    cat = _clean(category, 80)
    if enrich and nm and (not addr or not cat):
        from resolve import place_lookup
        found = place_lookup.enrich(nm, la, lo, geocoder=geocoder)
        addr = addr or _clean(found.get("address"), 500)
        cat = cat or _clean(found.get("category"), 80)
    nm = nm or f"{la:.5f}, {lo:.5f}"
    prov = (provider or parsed.get("provider") or "other").lower()
    return {"name": nm, "lat": la, "lon": lo, "address": addr, "category": cat,
            "provider": prov if prov in _PROVIDERS else "other",
            "place_id": parsed.get("place_id"),
            "source_url": _web_url(source_url) or _web_url(parsed.get("source_url")),
            "activity_type": place_url.category_for(cat)["activity"]}


def save(session: Session, user, *, url: str | None = None, name: str | None = None,
         lat=None, lon=None, address: str | None = None, category: str | None = None,
         provider: str | None = None, source_url: str | None = None,
         notes: str | None = None, source: str = "api",
         enrich: bool = False, geocoder=None) -> tuple[SavedPlace, bool]:
    """Save a place for `user`; returns `(place, created)`. Pass a Maps `url` for
    the server to parse (Google short links expand through the allowlisted
    helper only), or the details a client already resolved. Fields the client
    supplies win over what the URL parse found. Saving the same place again
    returns the existing row."""
    f = resolve_fields(url=url, name=name, lat=lat, lon=lon, address=address,
                       category=category, provider=provider, source_url=source_url,
                       enrich=enrich, geocoder=geocoder)
    key = place_key(f["provider"], f["place_id"], f["name"], f["lat"], f["lon"])
    owner_id = user.id if user is not None else None
    existing = (session.query(SavedPlace)
                .filter(SavedPlace.place_key == key, SavedPlace.owner_id == owner_id)
                .first())
    if existing is not None:
        return existing, False
    row = SavedPlace(
        owner_id=owner_id, place_key=key, name=f["name"], lat=f["lat"], lon=f["lon"],
        address=f["address"], category=f["category"], activity_type=f["activity_type"],
        provider=f["provider"], source_url=f["source_url"], notes=_clean(notes, 5000),
        source=_clean(source, 16) or "api")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row, True


def activity_from_link(url: str, *, geocoder=None) -> tuple[str, dict]:
    """The timeline activity (type, values) a pasted Maps link would become, with
    nothing stored — for the in-trip "add by link" form (D-168). The type comes
    from the category when one was found, else a generic Tour the person can
    change before saving. Raises ValueError for a link that isn't a place."""
    f = resolve_fields(url=url, enrich=True, geocoder=geocoder)
    return activity_for(SavedPlace(
        name=f["name"], lat=f["lat"], lon=f["lon"], address=f["address"],
        activity_type=f["activity_type"], source_url=f["source_url"]))


def list_places(session: Session, user) -> list[SavedPlace]:
    return (session.query(SavedPlace).filter(visible(user))
            .order_by(SavedPlace.created_at.desc(), SavedPlace.id).all())


def delete_place(session: Session, user, place_id: str) -> None:
    session.delete(require_place(session, user, place_id))
    session.commit()


def map_link(place: SavedPlace) -> str:
    """The person's own link when they shared one (so the Map button opens their
    app, YONDER-135); otherwise a Google search link from the name and pin."""
    if place.source_url:
        return place.source_url
    from urllib.parse import quote_plus
    q = f"{place.lat},{place.lon}" if place.lat is not None else place.name
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(q)}"


def activity_for(place: SavedPlace, activity_type: str | None = None,
                 notes: str | None = None) -> tuple[str, dict]:
    """The timeline activity (type, values) a saved place becomes. No date: the
    person adds one at filing or later (an undated activity sorts last)."""
    atype = activity_type or place.activity_type or "tour"
    where = place.address or ""
    values = {"name": place.name, "mapLink": map_link(place),
              "sourceLink": place.source_url or "",
              "notes": notes if notes is not None else (place.notes or "")}
    if atype == "tour":
        values["location"] = where
    elif atype in ("dining", "lodging"):
        values["address"] = where
    return atype, {k: v for k, v in values.items() if v}


_EDITABLE = ("name", "address", "notes", "activity_type")


def update_place(session: Session, user, place_id: str, **fields) -> SavedPlace:
    """Edit what a saved place will become: its name (a bare pin saves under its
    coordinates), address, notes and the activity type it files as. Only the
    given fields change; a blank name is refused. Own-only like everything here."""
    place = require_place(session, user, place_id)
    for key, value in fields.items():
        if key not in _EDITABLE or value is None:
            continue
        if key == "name":
            value = _clean(value, 300)
            if not value:
                raise ValueError("a saved place needs a name")
        elif key == "activity_type":
            from app.trips import types as activity_types
            if activity_types.get_type(value).key != value:
                raise ValueError(f"unknown activity type {value!r}")
        else:
            value = _clean(value, 5000 if key == "notes" else 500)
        setattr(place, key, value)
    session.commit()
    session.refresh(place)
    return place


_NEAR_M = 100_000       # a pin within 100 km of a trip's located stops is "on" that trip


def suggest_trip(session: Session, trips: list, place: SavedPlace, today: str | None = None):
    """The id of the trip a saved place most likely belongs to, or None. Only a
    suggestion that pre-selects the picker: filing always waits for the person
    (D-133). Past trips are never suggested. First choice is the trip whose
    located stops (its activities' geo) are nearest the pin, within 100 km; with
    no pin or no match, the trip covering today, else the next one to start."""
    from datetime import date

    from app import journal as journal_svc
    from app.models import TripActivity
    from resolve.place_lookup import _metres
    today = today or date.today().isoformat()
    live = [t for t in trips if not t.end_date or t.end_date >= today]
    if not live:
        return None
    if place.lat is not None and place.lon is not None:
        best: tuple[float, str] | None = None
        ids = {t.id for t in live}
        for a in session.query(TripActivity).filter(TripActivity.trip_id.in_(ids)).all():
            for p in (a.geo or {}).get("points") or []:
                if p.get("lat") is None or p.get("lon") is None:
                    continue
                d = _metres(place.lat, place.lon, p["lat"], p["lon"])
                if d <= _NEAR_M and (best is None or d < best[0]):
                    best = (d, a.trip_id)
        if best:
            return best[1]
    dicts = [{"id": t.id, "name": t.name, "start_date": t.start_date,
              "end_date": t.end_date} for t in live]
    current = journal_svc.default_trip_id(dicts, today)
    if current:
        return current
    upcoming = sorted((t for t in live if t.start_date and t.start_date >= today),
                      key=lambda t: t.start_date)
    return upcoming[0].id if upcoming else None
