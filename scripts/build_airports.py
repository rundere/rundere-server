"""Rebuild resolve/data/airports.json.gz, Run·dere's own airport reference table
(SPEC-8 D-147, YONDER-190).

    python scripts/build_airports.py                 # fetch mwgg/Airports (MIT)
    python scripts/build_airports.py --src airports.json

Source: https://github.com/mwgg/Airports (MIT; LICENSE kept alongside the
output). Keeps every airport with an IATA code — the code a ticket prints — with
its ICAO code, name, city, ISO country, coordinates and IANA time zone. A row with
no time zone gets one derived offline from its coordinates with timezonefinder,
so every kept airport has one. Refresh it each release (RELEASING.md).

Run once by a person, not at build or boot time: the server never fetches it
(D-146).
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "resolve" / "data" / "airports.json.gz"
SRC = "https://raw.githubusercontent.com/mwgg/Airports/master/airports.json"


def build(raw: dict) -> list[list]:
    from timezonefinder import TimezoneFinder
    tf = TimezoneFinder()
    rows: dict[str, list] = {}
    for icao, a in raw.items():
        iata = (a.get("iata") or "").strip().upper()
        if len(iata) != 3 or not iata.isalpha():
            continue
        try:
            lat, lon = float(a["lat"]), float(a["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        tz = (a.get("tz") or "").strip() or tf.timezone_at(lat=lat, lng=lon)
        row = [iata, (a.get("icao") or icao or "").strip().upper() or None,
               (a.get("name") or "").strip() or None, (a.get("city") or "").strip() or None,
               (a.get("country") or "").strip().upper() or None,
               round(lat, 5), round(lon, 5), tz]
        # Two rows for one IATA code: keep the one with an ICAO code that looks
        # like a real one (4 letters) — the other is usually a heliport or a closed field.
        old = rows.get(iata)
        if old is None or (len(row[1] or "") == 4 and len(old[1] or "") != 4):
            rows[iata] = row
    return [rows[k] for k in sorted(rows)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", help="a local copy of mwgg airports.json")
    args = ap.parse_args()
    if args.src:
        raw = json.loads(Path(args.src).read_text())
    else:
        with urllib.request.urlopen(SRC, timeout=60) as resp:
            raw = json.loads(resp.read())
    rows = build(raw)
    doc = {"source": "mwgg/Airports (MIT)", "fields": ["iata", "icao", "name", "city",
                                                       "country", "lat", "lon", "tz"],
           "airports": rows}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"), ensure_ascii=False)
    missing_tz = sum(1 for r in rows if not r[7])
    print(f"{len(rows)} airports -> {OUT.relative_to(ROOT)} "
          f"({OUT.stat().st_size // 1024} KB, {missing_tz} without a time zone)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
