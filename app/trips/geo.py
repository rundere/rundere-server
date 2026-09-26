"""Where a timeline activity is — stored coordinates, not just text.

Every activity carries place text (an address, a venue, an airport code, a
route) and often a Google/Apple Maps link. This module turns that into located
points — and, for a road trip, the road line between them — stored on
`TripActivity.geo` (migration 0029), so maps, the PDF and the app never have to
geocode at read time. The text stays the source of truth: geo is re-derived
whenever the location-relevant values change, and never edited by hand.

Shape (JSON):
    {"points": [{"role": "from"|"to"|"via"|"place", "label": str,
                 "lat": float, "lon": float,
                 "source": "link"|"capture"|"airport"|"photon"}],
     "route":  {"polyline": str (polyline6), "distance_km": float,
                "duration_minutes": int,
                "provider": "valhalla"|"ors"|"osrm"|"capture"},   # road trips only
     "key": str,          # fingerprint of the values it was derived from
     "complete": bool,    # every expected point was found
     "at": ISO timestamp}

Sources, most trusted first: coordinates in the person's own map link, a
capture's already-verified payload, AirTrail's airport reference for airport
codes, then Photon for the rest. Best-effort throughout: nothing here raises
into a save; a point that can't be found is simply missing (complete=False) and
is retried on the next save or by `app.cli locate-activities`.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Callable

from app.trips import types as activity_types

log = logging.getLogger("yonder.trips.geo")

# The values each type is located from, as (role, keys-tried-in-order) — the
# first non-empty key names the point. Two-ended types get from/to; the rest a
# single place. `mapLink` is always part of the fingerprint too.
_POINTS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "flight": (("from", ("from",)), ("to", ("to",))),
    "train": (("from", ("from",)), ("to", ("to",))),
    "drive": (("from", ("from",)), ("to", ("to",))),
    "carRental": (("from", ("pickupLocation",)), ("to", ("dropoffLocation",))),
    "cruise": (("from", ("departurePort",)), ("to", ("arrivalPort",))),
    "lodging": (("place", ("address", "name")),),
    "dining": (("place", ("address", "name")),),
    "tour": (("place", ("location", "name")),),
    "event": (("place", ("venue", "name")),),
    "note": (),
}
_AIRPORT_TYPES = {"flight"}


def fingerprint(atype: str, values: dict) -> str:
    """A short hash of exactly the values that decide where the activity is —
    so editing a confirmation number or a note never re-geocodes it."""
    keys = {"mapLink", "sourceLink", "route"}
    for _, tried in _POINTS.get(atype, ()):
        keys.update(tried)
    if atype in ("lodging", "dining"):
        keys.add("name")
    picked = {k: (values or {}).get(k) or "" for k in sorted(keys)}
    raw = json.dumps([atype, picked], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _query(atype: str, role: str, tried: tuple[str, ...], values: dict) -> str | None:
    """The text to locate one point: a lodging/restaurant is its name AND
    address together when both are given (a chain name alone lands on the
    wrong branch — see resolve/geocode.py)."""
    if atype in ("lodging", "dining") and values.get("address"):
        name = (values.get("name") or "").strip()
        addr = values["address"].strip()
        return addr if not name or name.lower() in addr.lower() else f"{name}, {addr}"
    return next((values[k].strip() for k in tried if (values.get(k) or "").strip()), None)


class Locator:
    """The lookups one save (or one backfill run) shares: Photon, the airport
    reference and the router, created lazily and cached per text, so the same
    airport or hotel is geocoded once."""

    def __init__(self, *, geocoder=None, airport: Callable[[str], object] | None = None,
                 route_url: str | None = None):
        self._geocoder = geocoder
        self._airport = airport
        # None = the configured router chain; a string pins one OSRM ("" = off)
        self.route_url = route_url
        self._cache: dict[str, tuple[float, float, str] | None] = {}
        self.links: dict[str, tuple[list[dict], object]] = {}

    @classmethod
    def default(cls) -> "Locator":
        """Photon + AirTrail airports + the router chain from settings, built
        on first use."""
        loc = cls()
        loc._lazy = True
        return loc

    _lazy = False

    @property
    def geocoder(self):
        if self._geocoder is None and self._lazy:
            from app.config import settings
            from resolve.geocode import PhotonGeocoder
            self._geocoder = PhotonGeocoder(settings.photon_url) if settings.photon_url else False
        return self._geocoder or None

    def airport(self, code: str):
        if self._airport is None and self._lazy:
            try:
                from app.trips.routes import _airport_resolver
                self._airport = _airport_resolver()
            except Exception:
                self._airport = False
        return self._airport(code) if self._airport else None

    def directions(self, link: str):
        """(located stops, estimate) for a directions link, looked up once."""
        if link not in self.links:
            from resolve import routing
            try:
                self.links[link] = routing.estimate_for_link(
                    link, geocoder=self.geocoder, base_url=self.route_url)
            except Exception:
                log.warning("directions lookup failed for %s", link, exc_info=True)
                self.links[link] = ([], None)
        return self.links[link]

    def text(self, text: str, *, airport: bool = False) -> tuple[float, float, str] | None:
        """(lat, lon, source) for place text, or None."""
        key = f"{'A' if airport else 'T'}:{text.strip().lower()}"
        if key in self._cache:
            return self._cache[key]
        hit = None
        code = text.strip().upper()
        if airport and code.isalpha() and len(code) in (3, 4):
            ap = self.airport(code)
            if ap is not None and getattr(ap, "lat", None) is not None:
                hit = (float(ap.lat), float(ap.lon), "airport")
        if hit is None and self.geocoder is not None:
            from resolve.routing import locate
            q = f"{text} airport" if airport and code.isalpha() and len(code) in (3, 4) else text
            # Free text goes to Photon as typed: a full address verifies itself;
            # anything else takes the top hit for exactly what the person wrote.
            place = locate({"raw": q, "name": q}, self.geocoder)
            if place.get("lat") is not None:
                hit = (float(place["lat"]), float(place["lon"]), "photon")
        self._cache[key] = hit
        return hit


def _point(role: str, label: str | None, lat, lon, source: str) -> dict:
    return {"role": role, "label": label or "", "lat": round(float(lat), 6),
            "lon": round(float(lon), 6), "source": source}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def locate_activity(atype: str, values: dict, *, old_geo: dict | None = None,
                    locator: Locator | None = None) -> dict | None:
    """The `geo` for an activity's values. Reuses `old_geo` untouched when the
    location-relevant values haven't changed and it was complete; returns None
    for a type with nothing to locate (a note). Never raises."""
    values = values or {}
    spec = _POINTS.get(atype)
    if not spec:
        return None
    key = fingerprint(atype, values)
    if old_geo and old_geo.get("key") == key and old_geo.get("complete"):
        return old_geo
    loc = locator or Locator.default()
    try:
        return _locate(atype, values, spec, key, loc)
    except Exception:
        log.warning("locating a %s activity failed", atype, exc_info=True)
        return {"points": [], "key": key, "complete": False, "at": _now()}


def _locate(atype, values, spec, key, loc: Locator) -> dict:
    from resolve import routing
    points: list[dict] = []
    route = None
    link = activity_types._maps_link(values)

    stops, est = loc.directions(link) if atype == "drive" and link else ([], None)
    if stops:
        # The person's own directions link: its stops, in order, and the road
        # line through them.
        for i, p in enumerate(stops):
            if p.get("lat") is None:
                continue
            role = "from" if i == 0 else "to" if i == len(stops) - 1 else "via"
            src = "photon" if p.get("geocode_source") == "photon" else "link"
            points.append(_point(role, routing.stop_label(p), p["lat"], p["lon"], src))
        expected = len(stops)
    else:
        pin = routing.link_point(link) if link and len(spec) == 1 else None
        for role, tried in spec:
            q = _query(atype, role, tried, values)
            if pin is not None:
                points.append(_point(role, q, pin[0], pin[1], "link"))
                continue
            if not q:
                continue
            hit = loc.text(q, airport=atype in _AIRPORT_TYPES)
            if hit:
                points.append(_point(role, q, hit[0], hit[1], hit[2]))
        expected = sum(1 for role, tried in spec
                       if pin is not None or _query(atype, role, tried, values))
        est = None
        if atype == "drive" and {p["role"] for p in points} >= {"from", "to"}:
            est = routing.route([(p["lat"], p["lon"]) for p in points],
                                base_url=loc.route_url)

    if atype == "drive" and est is not None:
        route = {"polyline": est.polyline, "distance_km": est.distance_km,
                 "duration_minutes": est.duration_minutes, "provider": est.provider}
    geo = {"points": points, "key": key,
           "complete": bool(points) and len(points) >= expected, "at": _now()}
    if route:
        geo["route"] = route
    return geo


def geo_from_payload(atype: str, values: dict, payload: dict) -> dict | None:
    """The `geo` for an activity committed from a capture, straight from the
    capture's already-located places (and a directions drive's route line) —
    no lookups. None when the payload has no coordinates to offer."""
    from resolve import routing
    t = payload.get("item_type")
    points: list[dict] = []

    def add(role, place):
        if isinstance(place, dict) and place.get("lat") is not None and place.get("lon") is not None:
            label = routing.stop_label(place) if role != "place" else (
                place.get("name") or place.get("raw") or "")
            points.append(_point(role, label, place["lat"], place["lon"], "capture"))

    route = None
    if t == "flight":
        f = payload.get("flight") or {}
        add("from", (f.get("departure") or {}).get("place"))
        add("to", (f.get("arrival") or {}).get("place"))
    elif t == "ground_transport":
        g = payload.get("ground_transport") or {}
        add("from", (g.get("departure") or {}).get("place"))
        for w in g.get("waypoints") or []:
            add("via", (w or {}).get("place"))
        add("to", (g.get("arrival") or {}).get("place"))
        r = g.get("route") or {}
        if r.get("polyline") or r.get("distance_km") is not None:
            route = {"polyline": r.get("polyline"), "distance_km": r.get("distance_km"),
                     "duration_minutes": r.get("duration_minutes"), "provider": "capture"}
    elif t == "lodging":
        add("place", (payload.get("lodging") or {}).get("place"))
    elif t == "event":
        add("place", (payload.get("event") or {}).get("venue"))
    if not points:
        return None
    geo = {"points": points, "key": fingerprint(atype, values), "complete": True,
           "at": _now()}
    if route:
        geo["route"] = route
    return geo


def public_geo(geo: dict | None) -> dict | None:
    """What clients get: the points and the route line, without bookkeeping."""
    if not geo or not geo.get("points"):
        return None
    out = {"points": [{k: p[k] for k in ("role", "label", "lat", "lon")}
                      for p in geo["points"]]}
    if geo.get("route"):
        out["route"] = {k: geo["route"].get(k)
                        for k in ("polyline", "distance_km", "duration_minutes")}
    return out


def backfill(session, *, trip_id: str | None = None, redo: bool = False,
             progress: Callable[[str], None] | None = None) -> dict:
    """Locate existing activities (NULL or incomplete geo; all of them with
    `redo`). One Locator for the whole run, so a shared airport or hotel is
    looked up once. Commits per activity; returns counts."""
    from app.models import TripActivity
    q = session.query(TripActivity)
    if trip_id:
        q = q.filter(TripActivity.trip_id == trip_id)
    loc = Locator.default()
    counts = {"located": 0, "partial": 0, "unlocated": 0, "skipped": 0}
    for a in q.order_by(TripActivity.trip_id, TripActivity.starts_at).all():
        if not redo and a.geo and a.geo.get("complete") \
                and a.geo.get("key") == fingerprint(a.type, a.values or {}):
            counts["skipped"] += 1
            continue
        geo = locate_activity(a.type, a.values or {}, locator=loc)
        a.geo = geo
        session.commit()
        state = ("skipped" if geo is None else "located" if geo.get("complete")
                 else "partial" if geo.get("points") else "unlocated")
        counts[state] += 1
        if progress:
            title = activity_types.get_type(a.type).title(a.values or {})
            progress(f"{state:9} {a.type:9} {title}")
    return counts
