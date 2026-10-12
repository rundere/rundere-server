"""Run·dere's own airport reference data (SPEC-8 D-147, YONDER-190).

A bundled table (resolve/data/airports.json.gz, built from mwgg/Airports by
scripts/build_airports.py) gives every IATA airport its ICAO code, name, city,
country, coordinates and IANA time zone, so a flight capture resolves its
airports — and keeps its time zones — with no AirTrail at all (D-146).

AirTrail is only a gap-filler: when the capture owner has an AirTrail
connection, airports from their flown history that the table lacks are added
(`reference(flown)`). AirTrail's internal airport ids, which its flight-save
endpoint needs, still come from AirTrail itself (app/committing.py).
"""
from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path

from adapters.targets.airtrail import Airport

_DATA = Path(__file__).parent / "data" / "airports.json.gz"


@lru_cache(maxsize=1)
def bundled() -> dict[str, Airport]:
    """{IATA: Airport} from the bundled table. Read once per process; an
    unreadable file degrades to {} (lookups then fall back to Photon)."""
    try:
        with gzip.open(_DATA, "rt", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {r[0]: Airport(iata=r[0], icao=r[1], name=r[2], municipality=r[3],
                          country=r[4], lat=r[5], lon=r[6], tz=r[7])
            for r in doc.get("airports") or []}


@lru_cache(maxsize=1)
def _by_icao() -> dict[str, Airport]:
    return {a.icao: a for a in bundled().values() if a.icao}


def lookup(code: str | None) -> Airport | None:
    """One airport by IATA (3 letters) or ICAO (4) code, or None."""
    key = (code or "").strip().upper()
    if not key:
        return None
    return bundled().get(key) or _by_icao().get(key)


def reference(flown: dict | None = None) -> dict[str, Airport]:
    """{IATA: Airport} for place enrichment: the bundled table first, then any
    airport from an AirTrail flown history that the table doesn't have (D-147)."""
    out = dict(bundled())
    for code, a in (flown or {}).items():
        out.setdefault(code, a)
    return out
