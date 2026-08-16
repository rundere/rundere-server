"""Cultural norms lookup (SPEC-3.1-planning.md §4; culture.json).

A country resolves to its own entry, else its region default, else a permissive
blank. `summarise` merges the norms across a whole trip's countries into the few
signals the engine's cultural pass needs: modest (coverage), shoes_off (favour
slip-on), and the union of cultural add-items.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data" / "culture.json"


@lru_cache(maxsize=1)
def _doc() -> dict:
    with open(_DATA, encoding="utf-8") as fh:
        return json.load(fh)


def for_country(country: str | None) -> dict:
    if not country:
        return {}
    cc = country.upper()
    countries = _doc()["countries"]
    if cc in countries:
        return countries[cc]
    for region in _doc()["region_defaults"].values():
        if cc in region.get("countries", []):
            return {k: v for k, v in region.items() if k != "countries"}
    return {}


def summarise(countries: list[str]) -> dict:
    """Merge cultural norms across a trip's countries → the engine's signals."""
    modest = False
    shoes_off = False
    add_items: list[str] = []
    notes: list[dict] = []
    for cc in countries:
        entry = for_country(cc)
        if not entry:
            continue
        modest = modest or bool(entry.get("modest"))
        shoes_off = shoes_off or bool(entry.get("shoes_off"))
        for item in entry.get("add_items", []):
            if item not in add_items:
                add_items.append(item)
        if entry.get("note"):
            notes.append({"country": cc, "note": entry["note"]})
    return {"modest": modest, "shoes_off": shoes_off,
            "add_items": add_items, "notes": notes}
