"""Run·dere — the Places Library Map lens routes (SPEC-11 phase 3, D-218).

Two small routes under `/places/map`:

- `GET /places/map/data`: what the visible map area shows, from `places.clusters`
  (geohash clusters at low zoom, single pins once zoomed in). Own-only: the service
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


@router.get("/data")
def map_data(request: Request, s: str | None = None, w: str | None = None,
             n: str | None = None, e: str | None = None, zoom: str | None = None,
             session: Session = Depends(get_session)):
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
        return JSONResponse({"places": [
            {"id": p.id, "name": p.name, "lat": p.lat, "lon": p.lon,
             "activity_type": p.activity_type}
            for p in found["places"][:_MAX_PLACES]]}, headers=_NO_STORE)
    return JSONResponse(found, headers=_NO_STORE)


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
