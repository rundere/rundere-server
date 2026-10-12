"""AirTrail target adapter.

Facts confirmed by probing the live API, not assumptions:
  - auth is Authorization: Bearer
  - the flight list is /api/flight/list (NOT /api/flights, which 404s)
  - envelope is {"success": true, "flights": [...]}
  - flightNumber is COMBINED ("AC9338"); the airline is a separate object
  - AirTrail resolves airports server-side: from/to are rich objects keyed by
    fromId/toId, carrying iata, icao, lat, lon, tz, country, municipality
  - flights carry a native datePrecision field (all existing rows = "day")

Read side only for now. The commit stage adds writes, and must send flights
ONE AT A TIME — AirTrail aborts an entire batch on a single bad record (D-06).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class AirTrailError(Exception):
    pass


def _read_capped(resp, max_bytes: int | None) -> str:
    """The body as text, refusing one larger than `max_bytes` (D-152)."""
    if max_bytes is None:
        return resp.read().decode("utf-8", errors="replace")
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise OSError(f"response larger than {max_bytes} bytes")
    return data.decode("utf-8", errors="replace")


def _digits(value: str) -> str:
    """"AC9338" / "8721" / "AC 8721" -> "8721". AirTrail stores the carrier
    prefix inside flightNumber (A4 Q1), so comparing raw strings would miss
    duplicates that differ only in formatting."""
    return "".join(c for c in str(value or "") if c.isdigit()).lstrip("0")


@dataclass(frozen=True)
class Airport:
    """AirTrail's own airport reference data, reused rather than duplicated."""
    iata: str | None
    icao: str | None
    name: str | None
    municipality: str | None
    country: str | None          # ISO alpha-2
    lat: float | None
    lon: float | None
    tz: str | None
    id: int | None = None        # AirTrail's internal airport id; save wants it


@dataclass(frozen=True)
class Airline:
    """AirTrail's own airline reference, harvested from existing flights — the
    save endpoint wants the resolved airline object (with its id), not a code."""
    id: int | None
    iata: str | None
    icao: str | None
    name: str | None


class AirTrailClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30, *,
                 opener=None, max_bytes: int | None = None):
        self.base = (base_url or "").rstrip("/")
        self.token = token
        self.timeout = timeout
        # A per-user connection passes the SSRF-guarded opener and a response
        # size cap (app/netpolicy.py, D-152); the operator's own defaults don't.
        self._open = opener.open if opener is not None else urllib.request.urlopen
        self.max_bytes = max_bytes

    def __repr__(self) -> str:                  # never the token (D-151)
        return f"AirTrailClient({self.base!r})"

    def _request(self, method: str, path: str,
                 body: dict | None = None) -> tuple[int, dict | list | None, str]:
        url = path if path.startswith("http") else f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
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
            raise AirTrailError(f"{method} {url}: {ex}") from ex
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        return status, parsed, raw

    def list_flights(self) -> list[dict]:
        status, parsed, raw = self._request("GET", "/api/flight/list")
        if status >= 400:
            raise AirTrailError(f"list_flights: HTTP {status}: {raw[:300]}")
        if not isinstance(parsed, dict) or "flights" not in parsed:
            raise AirTrailError(f"unexpected envelope: {raw[:200]}")
        return parsed["flights"] or []

    def airport_reference(self) -> dict[str, Airport]:
        """{IATA: Airport} harvested from existing flights.

        AirTrail exposes no airport endpoint, but every flight embeds fully
        resolved from/to airport objects — so the airports the user actually
        flies are all obtainable from data we are already authorized to read,
        with no second airport database to bundle or keep in sync. An airport
        never flown before simply is not in the map, and the caller falls back
        to review rather than guessing.
        """
        out: dict[str, Airport] = {}
        for f in self.list_flights():
            for side in ("from", "to"):
                a = f.get(side) or {}
                iata = a.get("iata")
                if not iata or iata in out:
                    continue
                out[iata] = Airport(
                    iata=iata, icao=a.get("icao"), name=a.get("name"),
                    municipality=a.get("municipality"), country=a.get("country"),
                    lat=a.get("lat"), lon=a.get("lon"), tz=a.get("tz"),
                    id=a.get("id"))
        return out

    def airline_reference(self, flights: list[dict] | None = None) -> dict[str, Airline]:
        """{IATA: Airline} harvested from existing flights. Same reasoning as
        airport_reference: the save endpoint wants the resolved airline object
        (with its id), and every flight already embeds one, so there is no
        airline dataset to bundle. An airline never flown is not here, and the
        committer defers rather than guessing an id."""
        out: dict[str, Airline] = {}
        for f in (self.list_flights() if flights is None else flights):
            a = f.get("airline") or {}
            iata = a.get("iata")
            if not iata or iata in out:
                continue
            out[iata] = Airline(id=a.get("id"), iata=iata,
                                icao=a.get("icao"), name=a.get("name"))
        return out

    def lookup_airport(self, code: str) -> Airport | None:
        """Authoritative airport data for an IATA/ICAO code, from AirTrail's own
        records. AirTrail has a full airport table but no stable REST lookup
        for it (the tRPC route uses superjson and is fragile to couple to), so
        this reads the airports embedded in existing flights — which is every
        airport the user has flown, home airport included. A code never flown
        is not here; the caller falls back to Photon (D-22 clean seam)."""
        code = (code or "").strip().upper()
        if not code:
            return None
        ref = self.airport_reference()
        if code in ref:
            return ref[code]
        for a in ref.values():                 # allow ICAO too
            if a.icao and a.icao.upper() == code:
                return a
        return None

    def find_duplicate(self, *, carrier_iata: str | None, flight_number: str,
                       departure_date: str, origin_iata: str | None,
                       destination_iata: str | None,
                       flights: list[dict] | None = None) -> dict | None:
        """An already-logged flight matching this one, or None.

        AirTrail has no per-item identity we can query, and it will happily
        store the same flight twice — so the check must happen HERE, before any
        write. Matching is deliberately exact on the four things that identify a
        leg (carrier, number, calendar date, route) and ignores everything that
        legitimately differs between two records of the same flight: seat,
        aircraft, gate, times revised by a schedule change.

        `flights` lets a batch commit fetch the list once instead of per item.
        """
        want_num = _digits(flight_number)
        rows = self.list_flights() if flights is None else flights
        for f in rows:
            airline = f.get("airline") or {}
            got_num = _digits(str(f.get("flightNumber") or ""))
            if got_num != want_num:
                continue
            if carrier_iata and airline.get("iata") \
                    and airline["iata"].upper() != carrier_iata.upper():
                continue
            if str(f.get("date") or "")[:10] != str(departure_date)[:10]:
                continue
            frm = (f.get("from") or {}).get("iata")
            to = (f.get("to") or {}).get("iata")
            if origin_iata and frm and frm.upper() != origin_iata.upper():
                continue
            if destination_iata and to and to.upper() != destination_iata.upper():
                continue
            return f
        return None

    def save_flight(self, body: dict) -> tuple[int, dict | list | None, str]:
        """Create a flight via POST /api/flight/save (the app's own save route;
        `/api/flights` does not exist). Returns the verbatim (status, parsed,
        raw) so the committer can store the exchange (D-19); it does NOT raise on
        an HTTP error, because a 4xx body is evidence the operator needs to see,
        not an exception to swallow. AirTrail stores one flight per seat, so a
        self-only booking carries exactly one seat."""
        return self._request("POST", "/api/flight/save", body=body)

    def update_flight(self, flight_id, body: dict) -> tuple[int, dict | list | None, str]:
        """Update a flight in place. /api/flight/save is an UPSERT: with an `id`
        in the body it fetches the flight, checks the caller has a seat on it,
        then runs updateFlightPrimitiveWithConnection (verified by reading the
        server build 2026-08-06). So an update is a save that carries the id —
        the same body build as a create, plus the existing flight's id."""
        return self._request("POST", "/api/flight/save", body={**body, "id": flight_id})

    def delete_flight(self, flight_id) -> tuple[int, dict | list | None, str]:
        """Delete a flight by id: POST /api/flight/delete {"id": ...}. The server
        checks the caller has a seat, then deletes; a cancelled booking was never
        flown, so it should not remain in the flown log (D-57)."""
        return self._request("POST", "/api/flight/delete", body={"id": flight_id})

    def health(self) -> bool:
        try:
            status, _, _ = self._request("GET", "/api/flight/list")
        except AirTrailError:
            return False
        return status < 400
