"""AdventureLog target adapter.

Facts confirmed by probing the live API, not assumptions:
  - auth header is X-API-Key (Bearer returns 400)
  - DRF-style paths WITH trailing slashes
  - country is ISO alpha-2 uppercase
  - date-only Transportation is ACCEPTED but silently stored as T00:00:00Z,
    so the adapter must mark any time it synthesized (D-06 stays intact)
  - naive datetimes are interpreted as UTC -> always send explicit Z
  - NO server-side Location dedup -> the resolver must match before creating
  - running version v0.12.1 (order= collision fixed)

This module currently implements the READ side, which the A6 collection ladder
needs. Write methods land with the commit stage; keeping the seam here means
the resolver never talks HTTP itself.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


class AdventureLogError(Exception):
    pass


def _read_capped(resp, max_bytes: int | None) -> str:
    """The body as text, refusing one larger than `max_bytes` (D-152)."""
    if max_bytes is None:
        return resp.read().decode("utf-8", errors="replace")
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise OSError(f"response larger than {max_bytes} bytes")
    return data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class Collection:
    id: str
    name: str
    start_date: str | None
    end_date: str | None


class AdventureLogClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30, *,
                 opener=None, max_bytes: int | None = None):
        self.base = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # A per-user connection passes the SSRF-guarded opener and a response
        # size cap (app/netpolicy.py, D-152); the operator's own defaults don't.
        self._open = opener.open if opener is not None else urllib.request.urlopen
        self.max_bytes = max_bytes

    def __repr__(self) -> str:                  # never the key (D-151)
        return f"AdventureLogClient({self.base!r})"

    def _on_base(self, url: str) -> str:
        """A server-built absolute URL as a path under self.base."""
        parts = urllib.parse.urlsplit(url)
        path = parts.path
        prefix = urllib.parse.urlsplit(self.base).path.rstrip("/")
        if prefix and path.startswith(prefix + "/"):
            path = path[len(prefix):]
        return path + (f"?{parts.query}" if parts.query else "")

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | None, str]:
        url = path if path.startswith("http") else f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-API-Key": self.api_key,          # A4: Bearer -> 400
            "Content-Type": "application/json",
        })
        try:
            with self._open(req, timeout=self.timeout) as resp:
                raw = _read_capped(resp, self.max_bytes)
                status = resp.status
        except urllib.error.HTTPError as ex:
            raw = _read_capped(ex, self.max_bytes)
            status = ex.code
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            raise AdventureLogError(f"{method} {url}: {ex}") from ex
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        return status, parsed, raw

    # ---------------------------------------------------------------- read

    def list_collections(self, max_pages: int = 20) -> list[Collection]:
        """Every collection, following DRF pagination. This is what the A6
        ladder matches an item's date span against."""
        out: list[Collection] = []
        path = "/api/collections/"
        for _ in range(max_pages):
            status, parsed, raw = self._request("GET", path)
            if status >= 400:
                raise AdventureLogError(f"list_collections: HTTP {status}: {raw[:300]}")
            if isinstance(parsed, dict):
                rows = parsed.get("results") or []
                nxt = parsed.get("next")
            else:
                rows, nxt = (parsed or []), None
            for r in rows:
                if not isinstance(r, dict) or not r.get("id"):
                    continue
                out.append(Collection(
                    id=str(r["id"]), name=str(r.get("name") or ""),
                    start_date=(r.get("start_date") or None),
                    end_date=(r.get("end_date") or None)))
            if not nxt:
                break
            # Follow pagination on the configured server only (the key goes
            # with every request): keep the next link's path and query.
            path = self._on_base(nxt)
        return out

    def country_names(self) -> dict[str, str]:
        """{ISO alpha-2: display name} from AdventureLog's own country table.

        Collection names go INTO AdventureLog (D-23: "{country} — {Month Year}"),
        so the country should be spelled the way AdventureLog spells it. Using
        the raw code produced "AW — July 2031" instead of "Aruba —
        July 2031" on the first real run.
        """
        out: dict[str, str] = {}
        path = "/api/countries/"
        for _ in range(20):
            status, parsed, raw = self._request("GET", path)
            if status >= 400:
                raise AdventureLogError(f"country_names: HTTP {status}: {raw[:200]}")
            if isinstance(parsed, dict):
                rows, nxt = parsed.get("results") or [], parsed.get("next")
            else:
                rows, nxt = (parsed or []), None
            for r in rows:
                if isinstance(r, dict) and r.get("country_code") and r.get("name"):
                    out[str(r["country_code"]).upper()] = str(r["name"])
            if not nxt:
                break
            # Follow pagination on the configured server only (the key goes
            # with every request): keep the next link's path and query.
            path = self._on_base(nxt)
        return out

    def find_locations_near(self, query: str, lat: float | None, lon: float | None,
                            radius_km: float = 0.5) -> list[dict]:
        """A4 Q6: AdventureLog does NOT dedup Locations server-side, so the
        resolver must look before creating or every trip accumulates duplicate
        airports/places.

        IMPORTANT (verified live 2026-08-05): `/api/locations/?search=` is a
        NO-OP on this AdventureLog version — it returns the full, UNFILTERED list
        (count stays at the total for any term), so the old dedup never actually
        matched and would have created duplicates next to the user's 7000+ Swarm
        locations. The working endpoint is the dedicated `/api/search/?query=`,
        which returns a `locations` array. We keep the client-side proximity
        filter (search relevance is loose — bare 'LAS' also matches Spanish
        'las …' places 14000 km away) and return candidates NEAREST-first."""
        from resolve.home import haversine_km
        q = urllib.parse.urlencode({"query": query})
        status, parsed, raw = self._request("GET", f"/api/search/?{q}")
        if status >= 400:
            raise AdventureLogError(f"find_locations_near: HTTP {status}: {raw[:300]}")
        rows = parsed.get("locations") if isinstance(parsed, dict) else (parsed or [])
        scored = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            if lat is None or lon is None:
                scored.append((0.0, r))
                continue
            try:
                rlat, rlon = float(r["latitude"]), float(r["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            dist = haversine_km(lat, lon, rlat, rlon)
            if dist <= radius_km:
                scored.append((dist, r))
        scored.sort(key=lambda t: t[0])
        return [r for _, r in scored]

    # ---------------------------------------------------------------- write
    #
    # Every write returns the verbatim (status, parsed, raw) so the committer
    # can store the exchange (D-19). None of them raise on an HTTP 4xx/5xx: the
    # body is the evidence the operator needs, not an exception to swallow. Paths
    # keep DRF trailing slashes; country is ISO alpha-2 upper; datetimes must be
    # explicit UTC "Z" (A4 — naive is read as UTC).

    def create_collection(self, name: str, start_date: str, end_date: str,
                          *, is_public: bool = False
                          ) -> tuple[int, dict | list | None, str]:
        # is_public defaults False so the commit-mint path is unchanged; the
        # Yonder "Push to AdventureLog" path passes True — a private collection
        # cannot be direct-linked to from outside AdventureLog.
        body = {"name": name, "start_date": start_date, "end_date": end_date}
        if is_public:
            body["is_public"] = True
        return self._request("POST", "/api/collections/", body=body)

    def create_location(self, *, name: str, latitude: float | None,
                        longitude: float | None, address: str | None = None,
                        city: str | None = None, region: str | None = None,
                        country: str | None = None,
                        collection_id: str | None = None,
                        ) -> tuple[int, dict | list | None, str]:
        body: dict = {"name": name}
        if latitude is not None:
            body["latitude"] = latitude
        if longitude is not None:
            body["longitude"] = longitude
        if address:
            body["location"] = address
        if city:
            body["city"] = city
        if region:
            body["region"] = region
        if country:
            body["country"] = str(country).upper()
        if collection_id:                       # locations join via M2M
            body["collections"] = [collection_id]
        return self._request("POST", "/api/locations/", body=body)

    def get_location(self, location_id: str) -> tuple[int, dict | list | None, str]:
        return self._request("GET", f"/api/locations/{location_id}/")

    def get_collection(self, collection_id: str) -> tuple[int, dict | list | None, str]:
        """A collection's detail, which embeds its transportations/lodging/notes
        arrays. The commit stage matches against these to avoid re-creating a
        record it already wrote when the DB's own commit record was lost (D-60):
        AdventureLog has no server-side dedup for these types (A4 Q6), so the
        check must happen HERE before every create — the same discipline the
        flight (AirTrail find_duplicate) and location (proximity) writes follow."""
        return self._request("GET", f"/api/collections/{collection_id}/")

    def set_location_collections(self, location_id: str,
                                 collection_ids: list) -> tuple[int, dict | list | None, str]:
        """PATCH a Location's collections M2M. The caller passes the FULL desired
        set (existing ∪ new) so this only ever ADDS — it can never drop a link to
        another trip. Locations are M2M with collections (A4 Q1), which is what
        lets one airport belong to every trip that passes through it."""
        return self._request("PATCH", f"/api/locations/{location_id}/",
                             body={"collections": list(collection_ids)})

    def create_transportation(self, body: dict) -> tuple[int, dict | list | None, str]:
        return self._request("POST", "/api/transportations/", body=body)

    def create_lodging(self, body: dict) -> tuple[int, dict | list | None, str]:
        return self._request("POST", "/api/lodging/", body=body)

    def create_note(self, body: dict) -> tuple[int, dict | list | None, str]:
        return self._request("POST", "/api/notes/", body=body)

    # ---- update / delete (D-57) -------------------------------------------
    # PATCH is partial (verified live 2026-08-06: sending only the changed keys
    # returns 200 and persists), so an update sends the whole rebuilt body and
    # AdventureLog keeps any field the payload does not mention. DELETE returns
    # 204 and a subsequent GET 404s. Same DRF record path for every type.

    def update_transportation(self, rid: str, body: dict) -> tuple[int, dict | list | None, str]:
        return self._request("PATCH", f"/api/transportations/{rid}/", body=body)

    def delete_transportation(self, rid: str) -> tuple[int, dict | list | None, str]:
        return self._request("DELETE", f"/api/transportations/{rid}/")

    def update_lodging(self, rid: str, body: dict) -> tuple[int, dict | list | None, str]:
        return self._request("PATCH", f"/api/lodging/{rid}/", body=body)

    def delete_lodging(self, rid: str) -> tuple[int, dict | list | None, str]:
        return self._request("DELETE", f"/api/lodging/{rid}/")

    def update_note(self, rid: str, body: dict) -> tuple[int, dict | list | None, str]:
        return self._request("PATCH", f"/api/notes/{rid}/", body=body)

    def delete_note(self, rid: str) -> tuple[int, dict | list | None, str]:
        return self._request("DELETE", f"/api/notes/{rid}/")

    def health(self) -> bool:
        try:
            status, _, _ = self._request("GET", "/api/collections/")
        except AdventureLogError:
            return False
        return status < 400
