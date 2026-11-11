"""Run·dere — the Places Library Map lens routes (SPEC-11 phase 3, D-218).

Two small routes under `/places/map`:

- `GET /places/map/data`: what the visible map area shows, from `places.clusters`
  (geohash clusters at low zoom, single pins once zoomed in); with `q`, every place
  the search box matches instead, with their bounds (YONDER-368). Own-only: the service
  scopes it through `places.visible`, so another person's places never appear (D-166).
- `GET /places/map/tiles/{z}/{x}/{y}.png`: one map tile. The browser never talks to a
  tile server (D-218): it asks this route, which uses the same `map_tile_url` and disk
  cache as the PDF maps. The tile template is server config, so no user-supplied URL is
  ever fetched (D-152); z/x/y are range-checked so only real tiles are requested.

This router must be included in `app/main.py` BEFORE `places_web.router`, which owns a
`GET /places/{place_id}` catch-all that would otherwise take `/places/map/...`.
The page itself (the lens tab, `#pl-map`) is `places_web` + `places/_map.html`.
"""
from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app import places as places_svc, scope
from app.config import settings
from app.db import get_session

router = APIRouter(prefix="/places/map")

_MAX_PLACES = 1000          # a map that shows more pins than this is unreadable anyway
_NO_STORE = {"Cache-Control": "private, no-store"}


def map_enabled() -> bool:
    """The Map lens needs a tile template; without one it isn't shown (D-218, D-146)."""
    return bool((settings.map_tile_url or "").strip())


def _num(raw: str | None, name: str, lo: float, hi: float) -> float:
    """A query number, clamped to [lo, hi]. Missing, non-numeric or NaN: 400."""
    try:
        v = float(raw)                          # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} must be a number") from None
    if math.isnan(v) or math.isinf(v):
        raise HTTPException(400, f"{name} must be a number")
    return max(lo, min(hi, v))


def _pin(p) -> dict:
    return {"id": p.id, "name": p.name, "lat": p.lat, "lon": p.lon,
            "activity_type": p.activity_type}


@router.get("/data")
def map_data(request: Request, s: str | None = None, w: str | None = None,
             n: str | None = None, e: str | None = None, zoom: str | None = None,
             q: str | None = None, session: Session = Depends(get_session)):
    if (q or "").strip():
        return _search_data(request, session, q.strip())
    south, north = _num(s, "s", -90, 90), _num(n, "n", -90, 90)
    west, east = _num(w, "w", -180, 180), _num(e, "e", -180, 180)
    z = int(_num(zoom, "zoom", 0, 22))
    if south > north:
        south, north = north, south
    if west > east:                     # a view across the antimeridian: show it all
        west, east = -180.0, 180.0
    found = places_svc.clusters(session, scope.user_of(request),
                                bbox=(south, west, north, east), zoom=z)
    if "places" in found:
        return JSONResponse({"places": [_pin(p) for p in found["places"][:_MAX_PLACES]]},
                            headers=_NO_STORE)
    return JSONResponse(found, headers=_NO_STORE)


def _search_data(request: Request, session: Session, q: str) -> JSONResponse:
    """A search on the Map (YONDER-368): every matching place with a pin, wherever
    it is, and their bounds so the map can fit them. The same search as the list
    (`places_web.search_filters` → `places.search`), so both always agree; own-only
    like the rest (D-166). The browser clusters them itself."""
    from app.places_web import search_filters
    user = scope.user_of(request)
    f, notice = search_filters(session, user, q)
    found = [] if notice else [p for p in places_svc.search(session, user, limit=_MAX_PLACES, **f)
                               if p.lat is not None and p.lon is not None]
    bounds = None
    if found:
        bounds = [[min(p.lat for p in found), min(p.lon for p in found)],
                  [max(p.lat for p in found), max(p.lon for p in found)]]
    return JSONResponse({"places": [_pin(p) for p in found], "bounds": bounds},
                        headers=_NO_STORE)


@router.get("/tiles/{z}/{x}/{y}.png")
def map_tile(z: int, x: int, y: int):
    """A tile, 404 when there's none. A long Cache-Control: tiles change rarely and the
    server keeps them for 30 days anyway. Login is enforced by the app's auth gate."""
    from app.trips import maps
    if not map_enabled():
        return Response(status_code=404)
    data = maps.tile(z, x, y)
    if data is None:
        return Response(status_code=404)
    return Response(data, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=604800"})
