"""Run·dere 4.7 — saved places service (YONDER-200, D-166/D-167).

The one place saving, listing and filing logic lives, so the JSON API (and later
the Capture Hub and MCP) never diverge. A saved place is a Maps link the
person kept before choosing a trip: deterministic parse (extract/place_url.py),
no extraction pipeline, no approval queue (D-167). It is a library entry (SPEC-11):
filing it to a trip builds a timeline activity and records a filing, and the place
stays, ready for other trips (D-212). Removing it keeps a tombstone (D-169).

The library's views read the same rows: search (D-216), the Atlas by continent,
country and city (D-215), map clusters by geohash, places near a trip, and the
person's own lists (D-213).

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

from app import places_geo, scope
from app.models import PlaceList, PlaceListMember, SavedPlace, SavedPlaceFiling
from discover.items import name_key
from extract import place_url

log = logging.getLogger(__name__)

_PROVIDERS = ("google", "apple")
SAVED, FILED, DISMISSED = "saved", "filed", "dismissed"
# What a save did. created/revived put a place in the saved list; the rest left
# it alone and say why. `error` only appears in a batch.
CREATED, REVIVED, DUPLICATE, NEAR_DUPLICATE = "created", "revived", "duplicate", "near_duplicate"
ALREADY_FILED, REMOVED, ERROR = "already_filed", "removed_earlier", "error"
SOMEDAY, MUST = "someday", "must"
PRIORITIES = (SOMEDAY, MUST)
CLIENT, PHOTON, AIRPORT = "client", "photon", "airport"    # where a locality came from
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
    one. Being on a trip doesn't matter any more (D-212)."""
    from fastapi import HTTPException
    p = session.get(SavedPlace, place_id)
    if p is None or not _can_access(user, p) or p.status == DISMISSED:
        raise HTTPException(status_code=404, detail="saved place not found")
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
        # A share sheet's text (name, address, link) carries the link inside it.
        url = place_url.find_maps_link(url) or url
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
        if existing.status != DISMISSED:      # `filed` rows predate D-212: in the library
            return DUPLICATE, existing
        if revive:
            return REVIVED, existing
        return REMOVED, existing
    if not allow_near:
        near = _near(session, owner_id, f, pending)
        if near is not None:
            return NEAR_DUPLICATE, near
    return CREATED, None


def _new_row(owner_id, f: dict, notes, source: str, *, extra: dict | None = None,
             geocoder=None) -> SavedPlace:
    extra = extra or {}
    row = SavedPlace(
        owner_id=owner_id, place_key=f["key"], name_key=f["name_key"], name=f["name"],
        lat=f["lat"], lon=f["lon"], address=f["address"], category=f["category"],
        activity_type=f["activity_type"], provider=f["provider"],
        source_url=f["source_url"], notes=_clean(notes, 5000),
        source=_clean(source, 16) or "api", status=SAVED,
        todo=_clean(extra.get("todo"), 5000),
        priority=extra.get("priority") if extra.get("priority") in PRIORITIES else SOMEDAY)
    refresh(row, client=extra, geocoder=geocoder)
    return row


def _commit_new(session: Session, owner_id, f: dict, notes, source: str, *,
                extra: dict | None = None, geocoder=None):
    """Insert, and if another request got there first (the unique index), return
    the row that won instead of failing: `(row, outcome)`."""
    row = _new_row(owner_id, f, notes, source, extra=extra, geocoder=geocoder)
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
         allow_near_duplicates: bool = False, todo: str | None = None,
         priority: str | None = None, locality: str | None = None,
         region: str | None = None, country_code: str | None = None,
         ) -> tuple[SavedPlace, str]:
    """Save one place for `user`; returns `(place, outcome)`. Pass a Maps `url` for
    the server to parse (Google short links expand through the allowlisted
    helper only), or the details a client already resolved; client fields win over
    the URL parse. The outcome says what happened: `created` and `revived` put it
    in the library; `duplicate` / `near_duplicate` found it already there;
    `removed_earlier` means the person removed it and it was left alone (`revive`
    re-adds it). The locality a client resolved (MapKit) is kept as given; with
    `enrich`, Photon fills it, else the offline guess does (D-215)."""
    f = _with_key(resolve_fields(url=url, name=name, lat=lat, lon=lon, address=address,
                                 category=category, provider=provider,
                                 source_url=source_url, enrich=enrich, geocoder=geocoder))
    owner_id = _owner_id(user)
    outcome, existing = _plan(session, owner_id, f, revive=revive,
                              allow_near=allow_near_duplicates)
    if outcome == CREATED:
        extra = {"todo": todo, "priority": priority, "locality": locality,
                 "region": region, "country_code": country_code}
        return _commit_new(session, owner_id, f, notes, source, extra=extra,
                           geocoder=_reverse_geocoder(geocoder) if enrich else None)
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
                row, outcome = _commit_new(session, owner_id, f, item.get("notes"), source,
                                           extra=item)
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


def _live():
    """The library: everything but removed places (old `filed` rows count, D-212)."""
    return SavedPlace.status != DISMISSED


def list_places(session: Session, user) -> list[SavedPlace]:
    """The whole library, newest first (removed places are tombstones)."""
    return (session.query(SavedPlace).filter(visible(user), _live())
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


_EDITABLE = ("name", "address", "notes", "activity_type", "todo", "priority",
             "visited_on", "locality", "region", "country_code")


def update_place(session: Session, user, place_id: str, **fields) -> SavedPlace:
    """Edit a saved place: its name (a bare pin saves under its coordinates),
    address, notes, what to do there, priority, the day they went, where it is
    in words (the person's correction wins over any lookup, D-215) and the
    activity type it files as. Only the given fields change; a blank name is
    refused, and "" clears a to-do, a visit or a locality. Own-only."""
    import re
    place = require_place(session, user, place_id)
    place_fields = {"locality", "region", "country_code"}
    for key, value in fields.items():
        if key not in _EDITABLE or value is None:
            continue
        if key == "priority":
            if value not in PRIORITIES:
                raise ValueError("priority is someday or must")
        elif key == "visited_on":
            value = value.strip() or None
            if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("visited_on must be YYYY-MM-DD")
        elif key == "country_code":
            value = value.strip().upper() or None
            if value and value not in places_geo.COUNTRIES:
                raise ValueError(f"unknown country code {value!r}")
        elif key in ("locality", "region"):
            value = _clean(value, 120)
        elif key == "todo":
            value = _clean(value, 5000)
        elif key == "name":
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
        if key in place_fields:
            place.locality_source = CLIENT
    refresh(place)
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


# --------------------------------------------------------------------------- the library (SPEC-11)
# Every view below reads the same rows, scoped by `visible`, and none needs more
# than plain SQL (D-216): a few thousand places per person is small enough to
# filter and rank in Python, and it behaves the same on SQLite and Postgres.

def _reverse_geocoder(geocoder=None):
    """The Photon client for locality lookups, or None when Photon is unset
    (D-146): the offline airport guess then stands in."""
    if geocoder is not None:
        return geocoder
    from resolve import place_lookup
    return place_lookup._default_geocoder()


def refresh(place: SavedPlace, *, client: dict | None = None, geocoder=None) -> None:
    """Bring a place's derived fields up to date: its geohash, its locality
    (D-215) and its search text (D-216). Locality comes from, in order, what the
    client sent, Photon when a geocoder is given, then the nearest bundled
    airport. A client or Photon answer is kept; an airport guess is upgraded when
    Photon is available later. Never raises and never commits."""
    client = client or {}
    has_pin = place.lat is not None and place.lon is not None
    place.geohash = places_geo.geohash(place.lat, place.lon) if has_pin else None
    sent = {k: _clean(client.get(k), 120) for k in ("locality", "region", "country_code")}
    if sent["country_code"]:
        sent["country_code"] = sent["country_code"].upper()[:2]
    if sent["locality"] or sent["country_code"]:
        place.locality, place.region = sent["locality"], sent["region"]
        place.country_code, place.locality_source = sent["country_code"], CLIENT
    elif has_pin and place.locality_source in (None, AIRPORT):
        found = None
        if geocoder is not None:
            try:
                found = geocoder.reverse(place.lat, place.lon)
            except Exception:                          # D-146: a lookup never breaks a save
                log.exception("reverse lookup failed; using the offline guess")
            if found:
                place.locality, place.region = found.get("locality"), found.get("region")
                place.country_code, place.locality_source = found.get("country_code"), PHOTON
        if not found and place.locality_source is None:
            guess = places_geo.from_airports(place.lat, place.lon)
            if guess:
                place.locality, place.country_code = guess["locality"], guess["country_code"]
                place.locality_source = AIRPORT
    place.search_text = places_geo.fold(
        place.name, place.address, place.locality, place.region,
        places_geo.country_name(place.country_code), place.category,
        place.notes, place.todo)


def _heal(session: Session, user) -> None:
    """Fill the derived fields of rows saved before the library existed (or by an
    older code path), offline only, the first time a view needs them. The
    backfill command does the same for everyone, with Photon when it is set."""
    stale = (session.query(SavedPlace)
             .filter(visible(user), SavedPlace.search_text == "").all())
    for p in stale:
        refresh(p)
    if stale:
        session.commit()


def backfill(session: Session, *, geocoder=None, upgrade: bool = True) -> dict:
    """Derive locality, geohash and search text for every saved place. Safe to
    re-run: client and Photon localities are kept, and with a geocoder an airport
    guess is upgraded (`upgrade=False` leaves those too). Commits as it goes."""
    gc = _reverse_geocoder(geocoder)
    counts = Counter()
    for p in session.query(SavedPlace).yield_per(200):
        before = (p.locality_source, p.search_text)
        use = gc if (upgrade or p.locality_source is None) else None
        refresh(p, geocoder=use)
        counts[p.locality_source or "none"] += 1
        counts["changed" if (p.locality_source, p.search_text) != before else "unchanged"] += 1
    session.commit()
    return dict(counts)


def _rank(place: SavedPlace, words: list[str]) -> tuple:
    name = places_geo.fold(place.name)
    q = " ".join(words)
    score = 0 if name.startswith(q) else 1 if all(w in name for w in words) else 2
    return (score, 0 if place.priority == MUST else 1, -(place.created_at.timestamp()
                                                          if place.created_at else 0))


def search(session: Session, user, *, q: str | None = None, list_id: str | None = None,
           kind: str | None = None, country: str | None = None, city: str | None = None,
           priority: str | None = None, visited: bool | None = None,
           bbox: tuple[float, float, float, float] | None = None,
           near: tuple[float, float] | None = None, radius_km: float = 25,
           limit: int = 100, offset: int = 0) -> list[SavedPlace]:
    """The library, narrowed. Every filter is optional and they combine:
    - `q`: each word must appear in the name, address, locality, country, notes
      or to-do (accent- and case-blind). Ranked name-prefix first, then name, then
      the rest; *must* places before *someday* ones; newest first.
    - `list_id` (the caller's own list), `kind` (activity type), `country` (ISO
      code), `city` (locality, folded), `priority`, `visited`.
    - `bbox` (south, west, north, east), or `near` (lat, lon) within `radius_km`,
      sorted nearest first.
    Without `q` or `near`, newest first."""
    _heal(session, user)
    query = session.query(SavedPlace).filter(visible(user), _live())
    words = places_geo.fold(q).split() if q else []
    for w in words:
        query = query.filter(SavedPlace.search_text.contains(w, autoescape=True))
    if list_id:
        lst = require_list(session, user, list_id)
        query = query.filter(SavedPlace.id.in_(
            session.query(PlaceListMember.place_id).filter(PlaceListMember.list_id == lst.id)))
    if kind:
        query = query.filter(SavedPlace.activity_type == kind)
    if country:
        query = query.filter(SavedPlace.country_code == country.upper())
    if priority:
        query = query.filter(SavedPlace.priority == priority)
    if visited is not None:
        query = query.filter(SavedPlace.visited_on.isnot(None) if visited
                             else SavedPlace.visited_on.is_(None))
    box = bbox or (places_geo.bbox_around(near[0], near[1], radius_km * 1000) if near else None)
    if box:
        s_, w_, n_, e_ = box
        query = query.filter(SavedPlace.lat.between(s_, n_), SavedPlace.lon.between(w_, e_))
    rows = query.all()
    if city:
        want = places_geo.fold(city)
        rows = [p for p in rows if places_geo.fold(p.locality) == want]
    if near:
        within = [(places_geo.metres(near[0], near[1], p.lat, p.lon), p) for p in rows]
        rows = [p for d, p in sorted(within, key=lambda t: t[0]) if d <= radius_km * 1000]
    elif words:
        rows.sort(key=lambda p: _rank(p, words))
    else:
        rows.sort(key=lambda p: (p.created_at.timestamp() if p.created_at else 0), reverse=True)
    return rows[offset:offset + limit]


UNPLACED = "-"      # the Atlas key for places with no known country (no pin, no lookup)


def atlas(session: Session, user, *, level: str = "continent",
          parent: str | None = None) -> list[dict]:
    """The library grouped for the Atlas (D-215), one level at a time:
    `continent` → `country` (parent = continent code) → `city` (parent =
    country code). Each group is `{key, name, count, must, kinds}`, where
    `kinds` counts activity types, biggest groups first. Places with no country
    group under `UNPLACED` at the top levels, and places in a country with no
    city under `UNPLACED` at the city level."""
    if level not in ("continent", "country", "city"):
        raise ValueError("level is continent, country or city")
    _heal(session, user)
    rows = session.query(SavedPlace).filter(visible(user), _live()).all()
    groups: dict[str, dict] = {}
    for p in rows:
        cont = places_geo.continent_of(p.country_code)
        if level == "continent":
            key, name = (cont, places_geo.CONTINENTS[cont]) if cont else (UNPLACED, "Unplaced")
        elif level == "country":
            if parent and cont != parent and not (parent == UNPLACED and cont is None):
                continue
            key = p.country_code if cont else UNPLACED
            name = places_geo.country_name(key) if cont else "Unplaced"
        else:
            if (p.country_code or "") != (parent or "").upper():
                continue
            key = places_geo.fold(p.locality) or UNPLACED
            name = p.locality or "Elsewhere"
        g = groups.setdefault(key, {"key": key, "name": name, "count": 0, "must": 0,
                                    "kinds": Counter()})
        g["count"] += 1
        g["must"] += p.priority == MUST
        g["kinds"][p.activity_type] += 1
    out = sorted(groups.values(), key=lambda g: (g["key"] == UNPLACED, -g["count"], g["name"]))
    for g in out:
        g["kinds"] = dict(g["kinds"].most_common())
    return out


# Map zoom → geohash cell length for clusters. Past the last zoom, single pins.
_ZOOM_PRECISION = ((2, 1), (4, 2), (7, 3), (9, 4), (12, 5), (14, 6))


def clusters(session: Session, user, *, bbox: tuple[float, float, float, float],
             zoom: int) -> dict:
    """What a map shows for `bbox` at `zoom` (D-216, D-218): `{"clusters": [...]}`
    of `{geohash, lat, lon, count}` (the average pin of the cell) at low zoom,
    or `{"places": [...]}` once zoomed in far enough for single pins. A GROUP BY
    on a geohash prefix, so a world view is a handful of rows, not every pin."""
    from sqlalchemy import func
    _heal(session, user)
    s_, w_, n_, e_ = bbox
    base = (visible(user), _live(), SavedPlace.geohash.isnot(None),
            SavedPlace.lat.between(s_, n_), SavedPlace.lon.between(w_, e_))
    precision = next((p for z, p in _ZOOM_PRECISION if zoom <= z), None)
    if precision is None:
        return {"places": session.query(SavedPlace).filter(*base).all()}
    cell = func.substr(SavedPlace.geohash, 1, precision)
    rows = (session.query(cell, func.count(SavedPlace.id), func.avg(SavedPlace.lat),
                          func.avg(SavedPlace.lon))
            .filter(*base).group_by(cell).all())
    return {"clusters": [{"geohash": c, "count": n, "lat": la, "lon": lo}
                         for c, n, la, lo in rows]}


def nearby(session: Session, user, points: list[tuple[float, float]], *,
           radius_km: float = 25, exclude_trip_id: str | None = None) -> list[SavedPlace]:
    """Library places within `radius_km` of any of `points` (a trip's stops),
    nearest first, each once. With `exclude_trip_id`, places already on that trip
    are left out: what "From your library" offers a trip (D-219)."""
    if not points:
        return []
    _heal(session, user)
    on_trip = set()
    if exclude_trip_id:
        on_trip = {pid for (pid,) in session.query(SavedPlaceFiling.place_id)
                   .filter(SavedPlaceFiling.trip_id == exclude_trip_id)}
    best: dict[str, tuple[float, SavedPlace]] = {}
    for lat, lon in points:
        s_, w_, n_, e_ = places_geo.bbox_around(lat, lon, radius_km * 1000)
        for p in (session.query(SavedPlace)
                  .filter(visible(user), _live(), SavedPlace.lat.between(s_, n_),
                          SavedPlace.lon.between(w_, e_))):
            if p.id in on_trip:
                continue
            d = places_geo.metres(lat, lon, p.lat, p.lon)
            if d <= radius_km * 1000 and (p.id not in best or d < best[p.id][0]):
                best[p.id] = (d, p)
    return [p for _, p in sorted(best.values(), key=lambda t: t[0])]


# --------------------------------------------------------------------------- filings (D-212)

def filings_for(session: Session, place_ids: list[str]) -> dict[str, list[str]]:
    """{place_id: [trip_id, …]} — which trips each place is on."""
    out: dict[str, list[str]] = {}
    if place_ids:
        for f in (session.query(SavedPlaceFiling)
                  .filter(SavedPlaceFiling.place_id.in_(place_ids))
                  .order_by(SavedPlaceFiling.created_at)):
            out.setdefault(f.place_id, []).append(f.trip_id)
    return out


def record_filing(session: Session, place: SavedPlace, trip_id: str, activity_id: str) -> bool:
    """Record that `place` went onto `trip_id`, inside the caller's transaction.
    False when it is already on that trip (checked here, and backed by the
    unique index for two filings at once): the caller answers 409. Flushes,
    never commits."""
    if (session.query(SavedPlaceFiling)
            .filter_by(place_id=place.id, trip_id=trip_id).first()) is not None:
        return False
    session.add(SavedPlaceFiling(owner_id=place.owner_id, place_id=place.id,
                                 trip_id=trip_id, activity_id=activity_id))
    try:
        session.flush()
    except IntegrityError:
        return False
    return True


def unlink_activity(session: Session, activity_id: str) -> None:
    """An activity was deleted: the place it came from is no longer on that trip."""
    session.query(SavedPlaceFiling).filter(SavedPlaceFiling.activity_id == activity_id).delete(
        synchronize_session=False)


# --------------------------------------------------------------------------- lists (D-213)

def require_list(session: Session, user, list_id: str) -> PlaceList:
    """The caller's own list; anyone else's 404s like a missing one."""
    from fastapi import HTTPException
    lst = session.get(PlaceList, list_id)
    if lst is None or not (scope.unrestricted(user)
                           or (user is not None and lst.owner_id == user.id)):
        raise HTTPException(status_code=404, detail="list not found")
    return lst


def _visible_lists(user):
    if scope.unrestricted(user):
        return true()
    if user is None:
        return false()
    return PlaceList.owner_id == user.id


def lists(session: Session, user) -> list[tuple[PlaceList, int]]:
    """The caller's lists in their order, each with how many library places it holds."""
    from sqlalchemy import func
    counts = dict(session.query(PlaceListMember.list_id, func.count(PlaceListMember.id))
                  .join(SavedPlace, SavedPlace.id == PlaceListMember.place_id)
                  .filter(_live()).group_by(PlaceListMember.list_id).all())
    rows = (session.query(PlaceList).filter(_visible_lists(user))
            .order_by(PlaceList.position, PlaceList.created_at).all())
    return [(lst, counts.get(lst.id, 0)) for lst in rows]


def _list_fields(fields: dict) -> dict:
    out = {}
    if "name" in fields and fields["name"] is not None:
        out["name"] = _clean(fields["name"], 120)
        if not out["name"]:
            raise ValueError("a list needs a name")
    for key, n in (("emoji", 16), ("description", 2000)):
        if key in fields and fields[key] is not None:
            out[key] = _clean(fields[key], n)
    if fields.get("position") is not None:
        out["position"] = int(fields["position"])
    return out


def create_list(session: Session, user, *, name: str, emoji: str | None = None,
                description: str | None = None, source: str | None = None) -> PlaceList:
    """A new list at the end of the caller's lists. A list with the same `source`
    (a Google list imported again) is returned instead of made twice."""
    f = _list_fields({"name": name, "emoji": emoji, "description": description})
    owner_id = _owner_id(user)
    src = _clean(source, 160)
    if src:
        same = (session.query(PlaceList)
                .filter(PlaceList.owner_id == owner_id, PlaceList.source == src).first())
        if same is not None:
            return same
    last = (session.query(PlaceList.position).filter(PlaceList.owner_id == owner_id)
            .order_by(PlaceList.position.desc()).first())
    lst = PlaceList(owner_id=owner_id, source=src, position=(last[0] + 1) if last else 0, **f)
    session.add(lst)
    session.commit()
    session.refresh(lst)
    return lst


def update_list(session: Session, user, list_id: str, **fields) -> PlaceList:
    lst = require_list(session, user, list_id)
    for key, value in _list_fields(fields).items():
        setattr(lst, key, value)
    session.commit()
    session.refresh(lst)
    return lst


def delete_list(session: Session, user, list_id: str) -> None:
    """Delete a list. Its places stay in the library (D-213)."""
    lst = require_list(session, user, list_id)
    session.query(PlaceListMember).filter(PlaceListMember.list_id == lst.id).delete(
        synchronize_session=False)
    session.delete(lst)
    session.commit()


def add_to_list(session: Session, user, list_id: str, place_id: str) -> bool:
    """Put one of the caller's places in one of their lists, at the end. True
    when added, False when it was already there. Both must be the caller's."""
    lst = require_list(session, user, list_id)
    place = require_place(session, user, place_id)
    if (session.query(PlaceListMember)
            .filter_by(list_id=lst.id, place_id=place.id).first()) is not None:
        return False
    last = (session.query(PlaceListMember.position).filter(PlaceListMember.list_id == lst.id)
            .order_by(PlaceListMember.position.desc()).first())
    session.add(PlaceListMember(owner_id=lst.owner_id, list_id=lst.id, place_id=place.id,
                                position=(last[0] + 1) if last else 0))
    try:
        session.commit()
    except IntegrityError:                    # the same add, twice at once
        session.rollback()
        return False
    return True


def remove_from_list(session: Session, user, list_id: str, place_id: str) -> None:
    lst = require_list(session, user, list_id)
    session.query(PlaceListMember).filter_by(list_id=lst.id, place_id=place_id).delete(
        synchronize_session=False)
    session.commit()


def lists_for(session: Session, place_ids: list[str]) -> dict[str, list[str]]:
    """{place_id: [list_id, …]} — which lists each place is in."""
    out: dict[str, list[str]] = {}
    if place_ids:
        for m in (session.query(PlaceListMember)
                  .filter(PlaceListMember.place_id.in_(place_ids))
                  .order_by(PlaceListMember.position)):
            out.setdefault(m.place_id, []).append(m.list_id)
    return out
