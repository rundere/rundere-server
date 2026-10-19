"""Run·dere 4.7 — saved places service (YONDER-200, D-166/D-167).

The one place saving, listing and filing logic lives, so the JSON API (and later
the web capture page and MCP) never diverge. A saved place is a Maps link the
person kept before choosing a trip: deterministic parse (extract/place_url.py),
no extraction pipeline, no approval queue (D-167). Filing it to a trip builds a
timeline activity; the row stays as a tombstone (D-169) so nothing handled is re-added.

Scoping is own-only (as the journal, D-134) and stricter on one point: no admin
tier, so one person's wishlist is never visible to another. Ownerless rows are
closed to everyone but the auth-off single-user world (YONDER-186).

Saving is idempotent and race-safe (D-169, D-170): the same place saved twice,
sequentially or at once, is one row (a unique index backs the check), and a place
that was filed or removed is reported as such instead of being added again — what
lets an agent push a Google list, or the iOS outbox retry, without duplicating.

Bad input raises ValueError (the API maps it to 400); a row that isn't yours
404s exactly like a missing one.
"""
from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import false, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import scope
from app.models import SavedPlace
from discover.items import name_key
from extract import place_url

log = logging.getLogger(__name__)

_PROVIDERS = ("google", "apple")
SAVED, FILED, DISMISSED = "saved", "filed", "dismissed"
# What a save did. created/revived put a place in the saved list; the rest left
# it alone and say why. `error` only appears in a batch.
CREATED, REVIVED, DUPLICATE, NEAR_DUPLICATE = "created", "revived", "duplicate", "near_duplicate"
ALREADY_FILED, REMOVED, ERROR = "already_filed", "removed_earlier", "error"
NEAR_DUP_M = 150            # same name within this distance is the same place
MAX_BATCH = 200             # places per batch call
MAX_ENRICH = 25             # a Photon lookup per item is slow; only small batches


def visible(user):
    """A predicate for `session.query(SavedPlace).filter(...)` — own rows only."""
    if scope.unrestricted(user):
        return true()
    if user is None:                      # sign-in on, nobody signed in (YONDER-293)
        return false()
    return SavedPlace.owner_id == user.id


def require_place(session: Session, user, place_id: str) -> SavedPlace:
    """The caller's saved place. Not theirs, or removed: 404, exactly like a missing
    one. Already filed to a trip: 409, so a stale click or a retry says why."""
    from fastapi import HTTPException
    p = session.get(SavedPlace, place_id)
    if p is None or not _can_access(user, p) or p.status == DISMISSED:
        raise HTTPException(status_code=404, detail="saved place not found")
    if p.status == FILED:
        raise HTTPException(status_code=409, detail="this place was already filed to a trip")
    return p


def _can_access(user, place: SavedPlace) -> bool:
    return scope.unrestricted(user) or (user is not None and place.owner_id == user.id)


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


def _maps_url(v, n: int = 2000) -> str | None:
    """Only a Google/Apple Maps link is kept as a place's link: it is rendered as an
    href (the /places page) and becomes the activity's Map button, so neither a
    `javascript:` link nor any other site's URL gets that far."""
    v = _clean(v, n)
    return v if v and place_url.is_maps_link(v) else None


def _coord(v, lo: float, hi: float) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


def resolve_fields(*, url: str | None = None, name: str | None = None, lat=None,
                   lon=None, address: str | None = None, category: str | None = None,
                   provider: str | None = None, source_url: str | None = None,
                   enrich: bool = False, geocoder=None, expand: bool = True) -> dict:
    """Everything known about a place, before anything is stored: the URL parse,
    overridden by whatever the client already resolved (iOS MapKit), then — only
    if `enrich` and a verified Photon hit exists — the address and category the
    link didn't carry (D-146: optional, never overrides, never required)."""
    parsed: dict = {}
    if url:
        # `expand=False` (batches) never fetches a short link: one network call per
        # item, for a list of hundreds, is a different thing from one paste.
        link = place_url.resolved_url(url) if expand else url.strip()
        parsed = place_url.parse_place_url(link) or {}
        if not parsed and not expand and place_url.is_short_link(link):
            raise ValueError("a short link isn't expanded in a batch; send the full place URL")
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
            "place_id": parsed.get("place_id"), "name_key": name_key(nm),
            "source_url": _maps_url(source_url) or _maps_url(parsed.get("source_url")),
            "activity_type": place_url.category_for(cat)["activity"]}


def _owner_id(user):
    return user.id if user is not None else None


def _find(session: Session, owner_id, key: str, pending=()) -> SavedPlace | None:
    """The owner's row with this identity, whatever its status."""
    for r in pending:
        if r.place_key == key:
            return r
    return (session.query(SavedPlace)
            .filter(SavedPlace.owner_id == owner_id, SavedPlace.place_key == key).first())


def _near(session: Session, owner_id, f: dict, pending=()) -> SavedPlace | None:
    """A row for the same name within NEAR_DUP_M metres: the same place reached by
    another route (a Google list entry and an Apple share of the same café)."""
    if f["lat"] is None or not f["name_key"]:
        return None
    from resolve.place_lookup import _metres
    rows = list(pending) + (session.query(SavedPlace)
                            .filter(SavedPlace.owner_id == owner_id,
                                    SavedPlace.name_key == f["name_key"]).all())
    for r in rows:
        if (r.name_key == f["name_key"] and r.lat is not None
                and _metres(f["lat"], f["lon"], r.lat, r.lon) <= NEAR_DUP_M):
            return r
    return None


def _plan(session: Session, owner_id, f: dict, *, revive: bool, allow_near: bool,
          pending=()) -> tuple[str, SavedPlace | None]:
    """What saving `f` would do, and the existing row it hit — without writing."""
    existing = _find(session, owner_id, f["key"], pending)
    if existing is not None:
        if existing.status == SAVED:
            return DUPLICATE, existing
        if existing.status == DISMISSED and revive:
            return REVIVED, existing
        return (ALREADY_FILED if existing.status == FILED else REMOVED), existing
    if not allow_near:
        near = _near(session, owner_id, f, pending)
        if near is not None:
            return NEAR_DUPLICATE, near
    return CREATED, None


def _new_row(owner_id, f: dict, notes, source: str) -> SavedPlace:
    return SavedPlace(
        owner_id=owner_id, place_key=f["key"], name_key=f["name_key"], name=f["name"],
        lat=f["lat"], lon=f["lon"], address=f["address"], category=f["category"],
        activity_type=f["activity_type"], provider=f["provider"],
        source_url=f["source_url"], notes=_clean(notes, 5000),
        source=_clean(source, 16) or "api", status=SAVED)


def _commit_new(session: Session, owner_id, f: dict, notes, source: str):
    """Insert, and if another request got there first (the unique index), return
    the row that won instead of failing: `(row, outcome)`."""
    row = _new_row(owner_id, f, notes, source)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        winner = _find(session, owner_id, f["key"])
        if winner is None:
            raise
        return winner, DUPLICATE
    session.refresh(row)
    return row, CREATED


def _revive(session: Session, row: SavedPlace) -> SavedPlace:
    row.status = SAVED
    session.commit()
    session.refresh(row)
    return row


def _with_key(f: dict) -> dict:
    f["key"] = place_key(f["provider"], f["place_id"], f["name"], f["lat"], f["lon"])
    return f


def save(session: Session, user, *, url: str | None = None, name: str | None = None,
         lat=None, lon=None, address: str | None = None, category: str | None = None,
         provider: str | None = None, source_url: str | None = None,
         notes: str | None = None, source: str = "api", enrich: bool = False,
         geocoder=None, revive: bool = False,
         allow_near_duplicates: bool = False) -> tuple[SavedPlace, str]:
    """Save one place for `user`; returns `(place, outcome)`. Pass a Maps `url` for
    the server to parse (Google short links expand through the allowlisted
    helper only), or the details a client already resolved; client fields win over
    the URL parse. The outcome says what happened: `created` and `revived` put it
    in the saved list; `duplicate` / `near_duplicate` found it already there;
    `already_filed` and `removed_earlier` mean it was handled before and was left
    alone (`revive` re-adds a removed one, never a filed one)."""
    f = _with_key(resolve_fields(url=url, name=name, lat=lat, lon=lon, address=address,
                                 category=category, provider=provider,
                                 source_url=source_url, enrich=enrich, geocoder=geocoder))
    owner_id = _owner_id(user)
    outcome, existing = _plan(session, owner_id, f, revive=revive,
                              allow_near=allow_near_duplicates)
    if outcome == CREATED:
        return _commit_new(session, owner_id, f, notes, source)
    if outcome == REVIVED:
        return _revive(session, existing), REVIVED
    return existing, outcome


def save_many(session: Session, user, items: list[dict], *, dry_run: bool = False,
              enrich: bool = False, revive: bool = False,
              allow_near_duplicates: bool = False, source: str = "mcp") -> dict:
    """Save a list of places (a Google list, a Takeout export) safely to repeat.
    Each item is judged on its own and committed on its own, so one bad item or a
    lost race never costs the rest, and re-sending the same list changes nothing.
    `dry_run` reports what each item would do and writes nothing, so the list can
    be reviewed first. Items are dicts with the single-save fields. Returns
    `{dry_run, counts, results}`, one result per item, in order."""
    from fastapi import HTTPException
    if not items:
        raise ValueError("no places to save")
    if len(items) > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} places per call; send the rest in another call")
    if enrich and len(items) > MAX_ENRICH:
        raise ValueError(f"address lookups are limited to {MAX_ENRICH} places per call; "
                         "send the details with each place, or use smaller batches")
    owner_id = _owner_id(user)
    pending: list[SavedPlace] = []          # dry run: what earlier items would have added
    results, counts = [], Counter()

    def done(i, outcome, row=None, error=None, name=None):
        r = {"index": i, "outcome": outcome}
        if row is not None:
            r.update(id=row.id, name=row.name)
            if outcome not in (CREATED, REVIVED):
                r["existing_status"] = row.status
        elif name:
            r["name"] = name
        if error:
            r["error"] = error
        results.append(r)
        counts[outcome] += 1

    for i, item in enumerate(items):
        item = item or {}
        try:
            f = _with_key(resolve_fields(
                url=item.get("url"), name=item.get("name"), lat=item.get("lat"),
                lon=item.get("lon"), address=item.get("address"),
                category=item.get("category"), provider=item.get("provider"),
                source_url=item.get("source_url"), enrich=enrich, expand=False))
            outcome, existing = _plan(session, owner_id, f, revive=revive,
                                      allow_near=allow_near_duplicates, pending=pending)
            if dry_run:
                if outcome == CREATED:
                    pending.append(_new_row(owner_id, f, item.get("notes"), source))
                done(i, outcome, existing, name=f["name"])
            elif outcome == CREATED:
                row, outcome = _commit_new(session, owner_id, f, item.get("notes"), source)
                done(i, outcome, row)
            elif outcome == REVIVED:
                done(i, outcome, _revive(session, existing))
            else:
                done(i, outcome, existing)
        except ValueError as exc:
            session.rollback()
            done(i, ERROR, error=str(exc), name=_clean(item.get("name"), 300))
        except HTTPException as exc:                      # not expected; never abort the batch
            session.rollback()
            done(i, ERROR, error=str(exc.detail))
        except Exception:
            session.rollback()
            log.exception("saved-place batch: item %d failed", i)
            done(i, ERROR, error="this item could not be saved; it is safe to retry")
    return {"dry_run": dry_run, "counts": dict(counts), "results": results}


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
    """The places waiting to be filed (filed and removed ones are tombstones)."""
    return (session.query(SavedPlace).filter(visible(user), SavedPlace.status == SAVED)
            .order_by(SavedPlace.created_at.desc(), SavedPlace.id).all())


def delete_place(session: Session, user, place_id: str) -> None:
    """Remove a saved place. It is kept as a tombstone, so re-importing a list
    that still contains it does not bring it back (D-169)."""
    require_place(session, user, place_id).status = DISMISSED
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
