"""Google / Apple Maps PLACE link parser (YONDER-200).

Sibling of the directions extractors (maps_url.py, apple_maps_url.py). A place
link is ONE stop — a restaurant, museum, hotel or viewpoint — with no date and
no route. It is parsed offline: the name and coordinates are in the URL itself,
so there is no scraping and no API key (SPEC-8 D-146: core first, extras
optional). Address and category come from a later, optional lookup (the geocoder
or, on iOS, MapKit) and are not this module's job.

Accepted shapes:

- Google `/maps/place/<Name>/@lat,lon,zoom/data=…!3d<lat>!4d<lon>…` — the data
  blob's `!3d/!4d` is the pin itself and beats the `@` viewport centre;
- Google `?api=1&query=<name>&query_place_id=<id>` (`/maps/search/`) and
  `/maps?q=<name>[&ll=lat,lon]`;
- Apple `maps.apple.com/?q=<name>&ll=lat,lon[&address=…]` and
  `/place?name=…&coordinate=lat,lon&address=…`;
- Google `maps.app.goo.gl` short links, but only after `classify` expands them
  (allowlisted hosts only, the same SSRF rule maps_url uses, D-152).

Apple `maps.apple/p/…` short links carry no coordinates (D-88) and are reported
as such rather than guessed at.

`classify` is the one routing decision the capture surface needs: a pasted maps
link is `place`, `directions` or neither. Directions links are never place links
(they need a travel date, RULE-31; a place has none).
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote_plus, urlsplit

from extract import apple_maps_url, maps_url

_GOOGLE_HOST = re.compile(r"^(www\.|maps\.)?google\.[a-z.]{2,6}$")
_PLACE_PATH = re.compile(r"/maps/place/([^/@?#]+)")
_SEARCH_PATH = re.compile(r"/maps/search/([^/@?#]+)")
_AT_COORDS = re.compile(r"@(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)")
# Google's feature id for a place (0x…:0x…), in the data blob as !1s… or as ?ftid=. It is
# a stable identity for a place, so it lets a re-import of the same list recognise it.
_FTID = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.I)
_PIN_COORDS = re.compile(r"!3d(-?\d{1,3}\.\d+)!4d(-?\d{1,3}\.\d+)")
_COORD_PAIR = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def _coords(lat: str | float | None, lon: str | float | None):
    """(lat, lon) floats when both are in range, else (None, None)."""
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None
    if -90 <= la <= 90 and -180 <= lo <= 180:
        return la, lo
    return None, None


def _pair(text: str | None):
    """"41.40,2.17" → (41.40, 2.17); anything else → (None, None)."""
    m = _COORD_PAIR.match(text or "")
    return _coords(m.group(1), m.group(2)) if m else (None, None)


def _name(raw: str | None) -> str | None:
    """A place name from a path segment or query value. A bare coordinate pair
    is a pin with no name, so it is not one."""
    s = unquote_plus(raw or "").strip()
    if not s or _COORD_PAIR.match(s):
        return None
    return s[:300]


def _is_google(url: str) -> bool:
    return bool(_GOOGLE_HOST.match((urlsplit(url).hostname or "").lower()))


def _is_google_short(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "maps.app.goo.gl" or (host in maps_url._SHORT_HOSTS and "/maps" in urlsplit(url).path)


def _place(provider: str, url: str, *, name=None, lat=None, lon=None,
           address=None, place_id=None) -> dict:
    return {"provider": provider, "name": name, "lat": lat, "lon": lon,
            "address": address, "place_id": place_id, "source_url": url}


def _google(url: str) -> dict | None:
    parts = urlsplit(url)
    if "/maps/dir/" in parts.path:
        return None
    q = parse_qs(parts.query)
    if q.get("origin") and q.get("destination"):
        return None
    m = _PLACE_PATH.search(parts.path) or _SEARCH_PATH.search(parts.path)
    name = _name(m.group(1)) if m else _name((q.get("query") or q.get("q") or [None])[0])
    # The data blob's !3d/!4d is the pin; the @ pair is only the map's viewport.
    pin = _PIN_COORDS.search(url) or _AT_COORDS.search(parts.path)
    lat, lon = _coords(*pin.groups()) if pin else (None, None)
    if lat is None:
        for key in ("ll", "query", "q"):
            lat, lon = _pair((q.get(key) or [None])[0])
            if lat is not None:
                break
    ftid = _FTID.search(url)
    place_id = ((q.get("query_place_id") or [None])[0] or (q.get("ftid") or [None])[0]
                or (ftid.group(1).lower() if ftid else None))
    if not (name or lat is not None):
        return None
    return _place("google", url, name=name, lat=lat, lon=lon, place_id=place_id)


def _apple(url: str) -> dict | None:
    if apple_maps_url.is_apple_short_link(url):
        return None
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != "maps.apple.com":
        return None
    q = parse_qs(parts.query)
    if q.get("saddr") or q.get("daddr") or q.get("dirflg"):
        return None

    def first(*keys):
        return next((v[0].strip() for k in keys if (v := q.get(k)) and v[0].strip()), None)

    name = _name(first("name", "q"))
    lat, lon = _pair(first("coordinate", "ll", "sll"))
    if not (name or lat is not None):
        return None
    return _place("apple", url, name=name, lat=lat, lon=lon,
                  address=first("address") and first("address")[:500],
                  place_id=first("place-id", "auid"))


def parse_place_url(url: str) -> dict | None:
    """The place a Maps link points at — `{provider, name, lat, lon, address,
    place_id, source_url}` — or None when it isn't a place link (directions, a
    short link not yet expanded, or an unrelated URL). Offline: no fetching."""
    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return None
    if _is_google(u):
        return _google(u)
    return _apple(u)


def classify(url: str, *, expand: bool = True) -> str | None:
    """`"place"`, `"directions"` or None for a pasted maps link, so the capture
    surface can route it. A Google short link is expanded first (allowlisted
    hosts only) because the short form doesn't say which it is; with
    `expand=False` it classifies as None instead of touching the network."""
    u = (url or "").strip()
    if _is_google_short(u):
        if not expand:
            return None
        u = maps_url._expand_short(u)
        if _is_google_short(u):          # expansion failed — still the short form
            return None
    if parse_place_url(u):
        return "place"
    if maps_url.looks_like_maps_url(u) or apple_maps_url.looks_like_apple_maps_url(u):
        return "directions"
    return None


def is_short_link(url: str) -> bool:
    """True for a Google share short link (not yet expanded)."""
    return _is_google_short((url or "").strip())


def resolved_url(url: str) -> str:
    """The URL a place should be parsed from: a Google short link expanded
    (allowlisted hosts only), anything else untouched."""
    u = (url or "").strip()
    return maps_url._expand_short(u) if _is_google_short(u) else u


# --- category → Rundere ----------------------------------------------------
# One table for every source of a category (D-146: any may be absent): Apple's
# MKPointOfInterestCategory (iOS resolves it on the phone) and OSM tags (the
# server's Photon fallback). Each maps to the Discover topic/kind a saved place
# files under and the timeline activity type it becomes (list_activity_types).
# topic is None for lodging: Discover holds food and events only, so a hotel goes
# straight to the timeline's `lodging` type.
_DINING = {"topic": "food", "activity": "dining"}
_SIGHT = {"topic": "events", "kind": "activity", "activity": "tour"}
_CATEGORIES: dict[str, dict] = {
    # MKPointOfInterestCategory (name without the MKPOICategory prefix)
    "restaurant": {**_DINING, "kind": "restaurant"}, "cafe": {**_DINING, "kind": "cafe"},
    "bakery": {**_DINING, "kind": "bakery"}, "brewery": {**_DINING, "kind": "brewery"},
    "winery": {**_DINING, "kind": "winery"}, "nightlife": {**_DINING, "kind": "bar"},
    "foodmarket": {**_DINING, "kind": "market"},
    "hotel": {"topic": None, "kind": "hotel", "activity": "lodging"},
    "museum": _SIGHT, "nationalpark": _SIGHT, "park": _SIGHT, "beach": _SIGHT,
    "zoo": _SIGHT, "aquarium": _SIGHT, "amusementpark": _SIGHT, "theater": _SIGHT,
    "library": _SIGHT, "stadium": _SIGHT, "university": _SIGHT, "landmark": _SIGHT,
    # OSM key=value
    "amenity=restaurant": {**_DINING, "kind": "restaurant"},
    "amenity=cafe": {**_DINING, "kind": "cafe"}, "amenity=bar": {**_DINING, "kind": "bar"},
    "amenity=pub": {**_DINING, "kind": "pub"},
    "amenity=fast_food": {**_DINING, "kind": "street_food"},
    "amenity=ice_cream": {**_DINING, "kind": "dessert"},
    "craft=brewery": {**_DINING, "kind": "brewery"},
    "craft=distillery": {**_DINING, "kind": "distillery"},
    "shop=bakery": {**_DINING, "kind": "bakery"},
    "tourism=hotel": {"topic": None, "kind": "hotel", "activity": "lodging"},
    "tourism=hostel": {"topic": None, "kind": "hotel", "activity": "lodging"},
    "tourism=guest_house": {"topic": None, "kind": "hotel", "activity": "lodging"},
    "tourism=museum": _SIGHT, "tourism=attraction": _SIGHT, "tourism=viewpoint": _SIGHT,
    "tourism=gallery": _SIGHT, "tourism=zoo": _SIGHT, "historic=monument": _SIGHT,
    "historic=castle": _SIGHT, "leisure=park": _SIGHT,
}
_DEFAULT_CATEGORY = {"topic": "events", "kind": "activity", "activity": "tour"}


def category_for(category: str | None) -> dict:
    """`{topic, kind, activity}` for a category string — an MKPointOfInterest
    Category (`MKPOICategoryRestaurant`, `Restaurant`) or an OSM tag
    (`amenity=restaurant`). Unknown or missing falls back to a generic activity,
    so a place always files somewhere the person can correct."""
    c = (category or "").strip().lower()
    c = re.sub(r"^mk(poi|pointofinterest)category", "", c).replace("_", "") if "=" not in c else c
    return dict(_CATEGORIES.get(c, _DEFAULT_CATEGORY))
