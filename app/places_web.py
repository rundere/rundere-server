"""Run·dere — the Places Library web page (SPEC-11 phase 2, YONDER-314).

`/places` never shows the whole library as one table. A person arrives through a
**lens**, and each lens shows a small, relevant set:

- **Unsorted:** places in no list and on no trip yet, to sort (not called Inbox:
  that's the captures queue, D-200);
- **Atlas:** continent → country → city boards, with counts, kinds and a small map;
- **Lists:** the person's own lists;
- **All:** everything, newest first.

One search box sits above them all, with chips (`must`, `kind:dining`,
`list:Ramen`, `city:Lisbon`, …, parsed by `places.parse_query`). Clicking a place
opens one panel (`_place_sheet.html`) for everything about it: what it is, where,
what to do there, its lists, its trips, and filing it onto another trip.

Thin over the shared service (`app/places.py`) and `api_v1.file_place`, so the
page, the JSON API and MCP never diverge. Own-only like everything in the library
(D-166). The URL carries the lens and filters, so a view can be bookmarked and
Back works. Read-only on the demo (D-198).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import api_v1, places as places_svc, places_geo, scope
from app.db import get_session

router = APIRouter(prefix="/places")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))
from app.icons import install as _install_icons  # noqa: E402
_install_icons(templates)
templates.env.globals["kind_label"] = places_svc.type_label

LENSES = ("unsorted", "atlas", "lists", "all")
_CHANGED = {"HX-Trigger": "places-changed"}      # the open lens refreshes itself


def _filing_trips(session: Session, user) -> list[dict]:
    from app.capture import _filing_trips as trips
    return [{"id": t.id, "name": t.name,
             "dates": " → ".join(x for x in (t.start_date, t.end_date) if x)}
            for t in trips(session, user)]


def _rows(session: Session, user, places: list) -> list[dict]:
    """Display rows for a set of places, with their lists and trips in bulk."""
    ids = [p.id for p in places]
    on, inl = places_svc.filings_for(session, ids), places_svc.lists_for(session, ids)
    return [{"p": p, "kind": places_svc.type_label(p.activity_type),
             "trips": len(on.get(p.id, [])), "lists": len(inl.get(p.id, []))}
            for p in places]


def _atlas_ctx(session: Session, user, continent: str, country: str, city: str) -> dict:
    """The Atlas at the level the URL asks for, with a breadcrumb."""
    crumbs = [{"label": "World", "href": "/places?lens=atlas"}]
    if country:
        cont = places_geo.continent_of(country)
        if cont:
            crumbs.append({"label": places_geo.CONTINENTS[cont],
                           "href": "/places?" + urlencode({"lens": "atlas", "continent": cont})})
        name = places_geo.country_name(country) or "Unplaced"
        if city:
            crumbs.append({"label": name, "href": "/places?" + urlencode(
                {"lens": "atlas", "country": country})})
            found = places_svc.search(session, user, country=country, city=city, limit=1000) \
                if city != places_svc.UNPLACED else [
                    p for p in places_svc.search(session, user, country=country, limit=1000)
                    if not p.locality]
            title = found[0].locality if found and found[0].locality else (
                "Elsewhere" if city == places_svc.UNPLACED else city)
            found.sort(key=lambda p: (p.priority != places_svc.MUST, p.activity_type, p.name))
            return {"level": "places", "crumbs": crumbs, "title": title,
                    "rows": _rows(session, user, found)}
        return {"level": "city", "crumbs": crumbs, "title": name, "country": country,
                "boards": places_svc.atlas(session, user, level="city", parent=country)}
    if continent:
        title = places_geo.CONTINENTS.get(continent, "Unplaced")
        return {"level": "country", "crumbs": crumbs, "title": title,
                "boards": places_svc.atlas(session, user, level="country", parent=continent)}
    return {"level": "continent", "crumbs": [], "title": "World",
            "boards": places_svc.atlas(session, user)}


def _body_ctx(request: Request, session: Session) -> dict:
    """Everything the lens area shows, from the URL alone."""
    user = scope.user_of(request)
    qp = request.query_params
    lens = qp.get("lens") or ""
    q = (qp.get("q") or "").strip()
    unsorted = places_svc.unsorted(session, user)
    ctx = {"request": request, "q": q, "lens": lens, "unsorted_count": len(unsorted),
           "kinds": places_svc.place_type_options(), "err": qp.get("err", "")}
    if q:                                           # a search overrides the lens
        f = places_svc.parse_query(q)
        name = f.pop("list_name", None)
        if name:
            lst = places_svc.list_by_name(session, user, name)
            f["list_id"] = lst.id if lst else None
            if lst is None:
                return {**ctx, "lens": "search", "rows": [],
                        "notice": f"You have no list called “{name}”."}
        found = places_svc.search(session, user, limit=200, **f)
        return {**ctx, "lens": "search", "rows": _rows(session, user, found)}
    lists = places_svc.lists(session, user)
    if not lens:                                    # first visit: sort if there's any
        lens = "unsorted" if unsorted else "atlas"
    ctx["lens"] = lens
    if lens == "unsorted":
        pinless = [p for p in unsorted if p.lat is None]
        return {**ctx, "rows": _rows(session, user, [p for p in unsorted if p.lat is not None]),
                "pinless": _rows(session, user, pinless)}
    if lens == "atlas":
        return {**ctx, "atlas": _atlas_ctx(session, user, qp.get("continent", ""),
                                           qp.get("country", "").upper(), qp.get("city", ""))}
    if lens == "lists":
        list_id = qp.get("list", "")
        if list_id:
            lst = places_svc.require_list(session, user, list_id)
            found = places_svc.search(session, user, list_id=lst.id, limit=1000)
            return {**ctx, "list": lst, "rows": _rows(session, user, found)}
        return {**ctx, "lists": lists}
    return {**ctx, "lens": "all",
            "rows": _rows(session, user, places_svc.list_places(session, user))}


def _is_partial(request: Request) -> bool:
    return request.headers.get("hx-request") == "true" and \
        request.headers.get("hx-target") == "pl-body"


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def places_page(request: Request, session: Session = Depends(get_session)):
    ctx = _body_ctx(request, session)
    if _is_partial(request):
        return templates.TemplateResponse(request, "places/_body.html", ctx)
    return templates.TemplateResponse(request, "places/index.html", ctx)


# --------------------------------------------------------------------------- one place
def _sheet(request: Request, session: Session, place_id: str, notice: dict | None = None,
           headers: dict | None = None):
    user = scope.user_of(request)
    p = places_svc.require_place(session, user, place_id)
    fileable = api_v1.fileable_trips(session, user)
    on = places_svc.filings_for(session, [p.id]).get(p.id, [])
    from app.models import Trip
    trips_on = [{"id": t.id, "name": t.name} for t in
                (session.get(Trip, tid) for tid in on) if t is not None]
    member = set(places_svc.lists_for(session, [p.id]).get(p.id, []))
    ctx = {"p": p, "kinds": places_svc.place_type_options(),
           "kind": places_svc.type_label(p.activity_type),
           "country": places_geo.country_name(p.country_code),
           "map_link": places_svc.map_link(p), "trips": _filing_trips(session, user),
           "suggested": places_svc.suggest_trip(session, fileable, p),
           "trips_on": trips_on, "lists": [(lst, lst.id in member) for lst, _ in
                                           places_svc.lists(session, user)],
           "notice": notice}
    return templates.TemplateResponse(request, "places/_place_sheet.html", ctx,
                                      headers=headers or {})


@router.get("/{place_id}", response_class=HTMLResponse)
def place_sheet(place_id: str, request: Request, session: Session = Depends(get_session)):
    return _sheet(request, session, place_id)


_FIELDS = ("name", "activity_type", "todo", "notes", "priority", "visited_on", "address",
           "locality", "country_code")


@router.post("/{place_id}/update", response_class=HTMLResponse)
async def place_update(place_id: str, request: Request,
                       session: Session = Depends(get_session)):
    """Save whichever fields the panel sent (one per change, or the whole form)."""
    form = await request.form()
    fields = {k: str(form[k]) for k in _FIELDS if k in form}
    if "priority" in form and form.get("priority") not in places_svc.PRIORITIES:
        fields["priority"] = places_svc.MUST if form.get("priority") in ("on", "1") \
            else places_svc.SOMEDAY
    user = scope.user_of(request)
    try:
        places_svc.update_place(session, user, place_id, **fields)
    except ValueError as exc:
        session.rollback()
        return _sheet(request, session, place_id, {"ok": False, "message": str(exc)})
    message, ok = "Saved.", True
    if any(k in fields for k in places_svc.ADDRESS_FIELDS):
        # A corrected address places a pin the place doesn't have yet (YONDER-314).
        outcome = places_svc.pin_from_address(session, user, place_id)
        said = places_svc.pin_message(outcome)
        if said:
            message, ok = f"Saved. {said}", outcome == places_svc.PINNED
    return _sheet(request, session, place_id, {"ok": ok, "message": message},
                  headers=_CHANGED)


@router.post("/{place_id}/pin", response_class=HTMLResponse)
def place_pin(place_id: str, request: Request, session: Session = Depends(get_session)):
    """Move the pin to the place's address, when the person asks (it already had one)."""
    outcome = places_svc.pin_from_address(session, scope.user_of(request), place_id,
                                          move=True)
    said = places_svc.pin_message(outcome) or "There's no address to look up."
    return _sheet(request, session, place_id,
                  {"ok": outcome in (places_svc.PINNED, places_svc.MOVED), "message": said},
                  headers=_CHANGED if outcome in (places_svc.PINNED, places_svc.MOVED) else None)


@router.post("/{place_id}/lists", response_class=HTMLResponse)
def place_lists(place_id: str, request: Request, list_id: str = Form(""),
                on: str = Form(""), new_list: str = Form(""),
                session: Session = Depends(get_session)):
    """Put the place in a list, take it out, or make a new list with it in."""
    user = scope.user_of(request)
    try:
        if new_list.strip():
            list_id = places_svc.create_list(session, user, name=new_list).id
            on = "1"
        if on == "1":
            places_svc.add_to_list(session, user, list_id, place_id)
        else:
            places_svc.remove_from_list(session, user, list_id, place_id)
    except ValueError as exc:
        session.rollback()
        return _sheet(request, session, place_id, {"ok": False, "message": str(exc)})
    return _sheet(request, session, place_id, headers=_CHANGED)


@router.post("/{place_id}/file", response_class=HTMLResponse)
def places_file(place_id: str, request: Request, trip_id: str = Form(""),
                starts_at: str = Form(""), session: Session = Depends(get_session)):
    """Put the place on a trip, dated or not (D-210); it stays in the library (D-212)."""
    user = scope.user_of(request)
    if not trip_id:
        return _sheet(request, session, place_id,
                      {"ok": False, "message": "Pick a trip to add it to."})
    try:
        a = api_v1.file_place(session, user, place_id, trip_id,
                              api_v1.SavedPlaceFileIn(starts_at=starts_at.strip() or None))
    except HTTPException as exc:
        session.rollback()
        if exc.status_code == 404 and "place" in str(exc.detail):
            raise
        return _sheet(request, session, place_id, {"ok": False, "message": str(exc.detail)})
    when = f" on {a.starts_at[:10]}" if a.starts_at else " (no date yet)"
    return _sheet(request, session, place_id, {
        "ok": True, "message": f"Added “{a.title or 'place'}” to the trip{when}.",
        "trip_id": trip_id}, headers=_CHANGED)


@router.post("/{place_id}/rename", response_class=HTMLResponse)
def places_rename(place_id: str, request: Request, name: str = Form(""),
                  session: Session = Depends(get_session)):
    try:
        places_svc.update_place(session, scope.user_of(request), place_id, name=name)
    except ValueError as exc:
        session.rollback()
        return _sheet(request, session, place_id, {"ok": False, "message": str(exc)})
    return _sheet(request, session, place_id, headers=_CHANGED)


@router.post("/{place_id}/delete", response_class=HTMLResponse)
def places_delete(place_id: str, request: Request, session: Session = Depends(get_session)):
    """Remove a place (a tombstone, D-169). Closes the panel; the lens refreshes."""
    try:
        places_svc.delete_place(session, scope.user_of(request), place_id)
    except HTTPException:
        session.rollback()
    return HTMLResponse("", headers=_CHANGED)


# --------------------------------------------------------------------------- lists
@router.post("/lists", response_class=HTMLResponse)
def lists_create(request: Request, name: str = Form(""), session: Session = Depends(get_session)):
    try:
        lst = places_svc.create_list(session, scope.user_of(request), name=name)
    except ValueError as exc:
        return RedirectResponse("/places?" + urlencode({"lens": "lists", "err": str(exc)}),
                                status_code=303)
    return RedirectResponse(f"/places?lens=lists&list={lst.id}", status_code=303)


@router.post("/lists/{list_id}", response_class=HTMLResponse)
def lists_rename(list_id: str, request: Request, name: str = Form(""),
                 description: str = Form(""), session: Session = Depends(get_session)):
    try:
        places_svc.update_list(session, scope.user_of(request), list_id, name=name,
                               description=description)
    except ValueError as exc:
        return RedirectResponse("/places?" + urlencode(
            {"lens": "lists", "list": list_id, "err": str(exc)}), status_code=303)
    return RedirectResponse(f"/places?lens=lists&list={list_id}", status_code=303)


@router.post("/lists/{list_id}/delete", response_class=HTMLResponse)
def lists_delete(list_id: str, request: Request, session: Session = Depends(get_session)):
    """Delete a list; its places stay in the library (D-213)."""
    places_svc.delete_list(session, scope.user_of(request), list_id)
    return RedirectResponse("/places?lens=lists", status_code=303)


# --------------------------------------------------------------------------- Atlas maps
_THUMB_W, _THUMB_H = 280, 120


def _thumb_dir() -> Path:
    from app.config import settings
    base = Path(settings.map_tile_cache_dir or Path(settings.artifacts_dir) / "tile-cache")
    return base / "place-thumbs"


@router.get("/atlas/map.png")
def atlas_map(request: Request, country: str = "", city: str = "", continent: str = "",
              session: Session = Depends(get_session)):
    """A small static map of one Atlas board's places (D-218: the PDF maps' own
    renderer and tile cache, no client-side map). Cached by the pins it shows, so
    it's drawn again only when they change. Own-only: drawn from the caller's
    places, and never shared between people. 404 when there's nothing to draw."""
    from app.trips import maps
    user = scope.user_of(request)
    places = places_svc.search(session, user, country=country.upper() or None,
                               city=city or None, limit=1000)
    if continent and not country:
        places = [p for p in places_svc.search(session, user, limit=5000)
                  if places_geo.continent_of(p.country_code) == continent]
    pins = sorted((round(p.lat, 4), round(p.lon, 4), p.activity_type)
                  for p in places if p.lat is not None)[:300]
    if not pins:
        return Response(status_code=404)
    key = hashlib.sha256(repr((getattr(user, "id", None), pins)).encode()).hexdigest()[:32]
    path = _thumb_dir() / f"{key}.png"
    headers = {"Cache-Control": "private, max-age=86400"}
    if path.exists():
        return Response(path.read_bytes(), media_type="image/png", headers=headers)
    img = maps.render_map([maps.Marker(lat, lon, colour=maps.KIND_COLOURS.get(kind,
                                                                        maps.DEFAULT_COLOUR))
                           for lat, lon, kind in pins], [], width=_THUMB_W, height=_THUMB_H)
    if img is None:
        return Response(status_code=404)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(img.data)
    except OSError:                                  # a read-only cache is no reason to fail
        pass
    return Response(img.data, media_type=img.mime, headers=headers)
