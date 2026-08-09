"""Read-only lookups against AirTrail's OWN Postgres `airport` / `airline`
reference tables (D-50).

The flown-flight reference in `airtrail.py` only knows airports already in the
history, so a flight to a never-before-flown airport (MUC/BLR/DEL on a first
India trip) has no internal `id` — and `/api/flight/save` needs `from.id`. These
tables are AirTrail's complete airport/airline database and carry the id, coords
and tz for every airport, so one read resolves any of them.

This deliberately couples to AirTrail's schema — exactly what the owner's n8n
workflow did (`SELECT ... FROM airport WHERE iata = ...`) — but it is READ-ONLY
and OPTIONAL: with `TX_AIRTRAIL_DB_URL` unset the committer falls back to the
flown reference and simply refuses a never-flown airport rather than mis-logging
it. Never writes; a lookup failure degrades to None, never an exception at the
call site.
"""
from __future__ import annotations

import logging

from adapters.targets.airtrail import Airline, Airport

log = logging.getLogger("yonder.airtrail-db")


class AirTrailDB:
    def __init__(self, dsn: str):
        # psycopg wants a plain libpq DSN; tolerate a SQLAlchemy-style prefix.
        self.dsn = (dsn or "").replace("postgresql+psycopg://", "postgresql://")

    def _one(self, sql: str, params: tuple):
        import psycopg
        with psycopg.connect(self.dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()

    def airport(self, code: str) -> Airport | None:
        code = (code or "").strip().upper()
        if not code:
            return None
        try:
            row = self._one(
                "SELECT id, iata, icao, name, municipality, country, lat, lon, tz "
                "FROM airport WHERE iata = %s OR icao = %s LIMIT 1", (code, code))
        except Exception as ex:                     # never break the commit
            log.warning("AirTrail DB airport lookup failed for %s: %s", code, ex)
            return None
        if not row:
            return None
        return Airport(id=row[0], iata=row[1], icao=row[2], name=row[3],
                       municipality=row[4], country=row[5], lat=row[6],
                       lon=row[7], tz=row[8])

    def airline(self, code: str) -> Airline | None:
        code = (code or "").strip().upper()
        if not code:
            return None
        try:
            row = self._one(
                "SELECT id, iata, icao, name FROM airline "
                "WHERE iata = %s OR icao = %s LIMIT 1", (code, code))
        except Exception as ex:
            log.warning("AirTrail DB airline lookup failed for %s: %s", code, ex)
            return None
        if not row:
            return None
        return Airline(id=row[0], iata=row[1], icao=row[2], name=row[3])

    def health(self) -> bool:
        try:
            return self._one("SELECT 1", ()) is not None
        except Exception:
            return False
