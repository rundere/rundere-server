"""Run·dere — importing saved places from a CSV file (SPEC-11 D-243, YONDER-374).

A real Google Takeout turned out to carry no lists (D-217 is narrowed, not
dropped), so the library first gets a file format of its own. Anyone can fill it
from a spreadsheet or another app:

    name,url,address,lat,lon,list,kind,notes,todo,priority,country

The header row decides the columns, in any order and case-blind. A row needs a
`name` or a Maps `url`. To put a place in two lists, repeat the row.

The file is parsed in memory and never stored (D-153), and nothing is looked up
(D-146): no short link is expanded and no address is geocoded, so a pinless place
waits in the Unsorted view's "Needs a pin" tray. Each row goes through
`places.save_many`, so the batch outcomes, the D-169 identity and the dry run are
the same as `/places/batch`. The web page, `POST /api/v1/places/import` and the
MCP `import_places_csv` all call `run`, and all are own-only (D-166).
"""
from __future__ import annotations

import csv
import io

from sqlalchemy.orm import Session

from app import places as places_svc, places_geo

MAX_BYTES = 2 * 1024 * 1024          # D-243: one file of up to 2 MB …
MAX_ROWS = 5000                      # … and 5,000 places
LIST_SOURCE = "csv:"                 # a list this import made: `csv:<name>`, found again next time

# Each column's accepted spellings, folded. Google's own CSV columns are added here
# once a real "Saved" export has been checked (D-217), not guessed.
COLUMNS = {
    "name": ("name", "place", "place name"),
    "url": ("url", "link", "maps url", "maps link", "google maps url"),
    "address": ("address",),
    "lat": ("lat", "latitude"),
    "lon": ("lon", "lng", "long", "longitude"),
    "list": ("list", "list name"),
    "kind": ("kind", "type"),
    "notes": ("notes", "note"),
    "todo": ("todo", "to do", "to-do"),
    "priority": ("priority",),
    "country": ("country", "country code", "country_code"),
}
_HEADER = {spelling: key for key, spellings in COLUMNS.items() for spelling in spellings}

TEMPLATE = (
    "name,url,address,lat,lon,list,kind,notes,todo,priority,country\n"
    "Harbour Lantern Café,,12 Example Quay,51.50321,-0.11954,Coffee,restaurant,"
    "Flat whites,Try the cardamom bun,must,GB\n"
    "Old Mill Museum,,4 Sample Lane,,,Rainy days,tour,,,someday,GB\n"
    "Pine Ridge Lookout,,,46.20511,7.30112,Hikes,tour,Sunset,,,CH\n"
)


class UnreadableFile(ValueError):
    """The file as a whole can't be read; the message says why, for the person."""


def _fold(v: str) -> str:
    return " ".join(places_geo.fold(v).replace("_", " ").split())


def _dialect(text: str) -> str:
    """Comma, unless the header plainly uses a semicolon or a tab (a spreadsheet
    saved with a European locale writes semicolons)."""
    first = text.split("\n", 1)[0]
    if "," not in first:
        for sep in (";", "\t"):
            if sep in first:
                return sep
    return ","


def parse(data: bytes) -> tuple[list[tuple[int, dict]], list[dict], list[str]]:
    """The file's rows as `(line, fields)`, the rows refused before saving as
    batch-style errors, and warnings about the file. Raises `UnreadableFile` when
    the file itself can't be used."""
    if len(data) > MAX_BYTES:
        raise UnreadableFile(f"the file is over {MAX_BYTES // (1024 * 1024)} MB; split it into smaller files")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UnreadableFile("the file isn't UTF-8 text; save it from your spreadsheet as "
                           "“CSV UTF-8” and try again") from None
    if not text.strip():
        raise UnreadableFile("the file is empty")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=_dialect(text))
    try:
        header = next(reader)
    except csv.Error as exc:
        raise UnreadableFile(f"the header row can't be read as CSV ({exc})") from None
    cols: dict[int, str] = {}
    warnings: list[str] = []
    for i, raw in enumerate(header):
        key = _HEADER.get(_fold(raw))
        if key is None:
            if raw.strip():
                warnings.append(f"column “{raw.strip()[:60]}” isn't one Run·dere knows; it was ignored")
        elif key in cols.values():
            warnings.append(f"column “{raw.strip()[:60]}” repeats {key}; only the first is used")
        else:
            cols[i] = key
    if "name" not in cols.values() and "url" not in cols.values():
        raise UnreadableFile("the header row needs a “name” or a “url” column")
    rows: list[tuple[int, dict]] = []
    errors: list[dict] = []
    try:
        for record in reader:
            line = reader.line_num
            if not any(cell.strip() for cell in record):
                continue                            # a blank line between rows
            if len(rows) + len(errors) >= MAX_ROWS:
                raise UnreadableFile(f"the file has more than {MAX_ROWS:,} places; "
                                   "split it into smaller files")
            fields = {key: record[i].strip() for i, key in cols.items() if i < len(record)}
            if not fields.get("name") and not fields.get("url"):
                errors.append({"line": line, "outcome": places_svc.ERROR,
                               "error": "the row has no name and no url"})
                continue
            rows.append((line, fields))
    except csv.Error as exc:
        raise UnreadableFile(f"line {reader.line_num} can't be read as CSV ({exc})") from None
    if not rows and not errors:
        raise UnreadableFile("the file has a header row but no places")
    return rows, errors, warnings


def _kinds() -> dict[str, str]:
    """Every way a kind can be written (`dining`, `restaurant`, its label) → its key."""
    out = {}
    for o in places_svc.place_type_options():
        for spelling in (o["value"], places_svc.kind_word(o["value"]), o["label"]):
            out[_fold(spelling)] = o["value"]
    return out


def _pin(lat: str, lon: str) -> tuple[float | None, float | None]:
    """A pin from the two cells. Blank, half or 0,0 is no pin: Takeout writes 0,0
    for a place with no known location (D-243). Raises ValueError for a bad number."""
    if not lat and not lon:
        return None, None
    if not lat or not lon:
        return None, None                           # a half pair is no pin (resolve_fields)
    try:
        la, lo = float(lat), float(lon)
    except ValueError:
        raise ValueError("lat and lon must be numbers, like 51.50321 and -0.11954") from None
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        raise ValueError("lat must be between -90 and 90, and lon between -180 and 180")
    if la == 0 and lo == 0:
        return None, None
    return la, lo


def _item(fields: dict, kinds: dict[str, str]) -> dict:
    """One row as a `save_many` item, plus its `kind` and `list`. Raises
    ValueError with a reason the person can act on."""
    la, lo = _pin(fields.get("lat", ""), fields.get("lon", ""))
    kind = None
    if fields.get("kind"):
        kind = kinds.get(_fold(fields["kind"]))
        if kind is None:
            raise ValueError(f"kind “{fields['kind'][:40]}” isn't a place kind; use one of "
                             + ", ".join(sorted({places_svc.kind_word(v) for v in kinds.values()})))
    priority = fields.get("priority", "").lower() or None
    if priority and priority not in places_svc.PRIORITIES:
        raise ValueError("priority must be “must” or “someday”")
    country = fields.get("country", "").upper() or None
    if country and not (len(country) == 2 and country.isalpha()):
        raise ValueError("country must be a two-letter code, like GB or JP")
    return {"name": fields.get("name") or None, "url": fields.get("url") or None,
            "address": fields.get("address") or None, "lat": la, "lon": lo,
            "notes": fields.get("notes") or None, "todo": fields.get("todo") or None,
            "priority": priority, "country_code": country,
            "kind": kind, "list": (fields.get("list") or "")[:120] or None}


def run(session: Session, user, data: bytes, *, dry_run: bool = False) -> dict:
    """Import a CSV file of places for `user` (D-243). Returns
    `{dry_run, rows, counts, results, lists, warnings}`: one result per row, by
    file line, with the batch outcome and the list it joins. A dry run writes
    nothing, not even a list. Raises `UnreadableFile` for a file it can't read."""
    rows, refused, warnings = parse(data)
    kinds = _kinds()
    # The caller's own lists by folded name (D-235), read once.
    mine = {_fold(lst.name): lst for lst, _ in places_svc.lists(session, user)}
    items: list[dict] = []
    lines: list[int] = []
    results: list[dict] = list(refused)
    for line, fields in rows:
        try:
            item = _item(fields, kinds)
        except ValueError as exc:
            results.append({"line": line, "outcome": places_svc.ERROR, "error": str(exc),
                            "name": fields.get("name") or None})
            continue
        items.append(item)
        lines.append(line)

    lists_out: dict[str, dict] = {}

    def list_for(name: str):
        """The caller's list called `name`: an existing one, else a new one (made
        only for real, never in a dry run). `None` id means it would be made."""
        key = _fold(name)
        if key not in lists_out:
            lst = mine.get(key)
            if lst is None and not dry_run:
                lst = places_svc.create_list(session, user, name=name,
                                             source=LIST_SOURCE + name)
                mine[key] = lst
                lists_out[key] = {"name": lst.name, "id": lst.id, "outcome": "created"}
            elif lst is None:
                lists_out[key] = {"name": name, "id": None, "outcome": "would_create"}
            else:
                lists_out[key] = {"name": lst.name, "id": lst.id, "outcome": "existing"}
        return lists_out[key]

    saved = {"results": []}
    if items:
        # One call, so the dry run's `pending` sees the whole file: a row repeated
        # further down is a duplicate in the preview too. Nothing is looked up.
        saved = places_svc.save_many(session, user, items, dry_run=dry_run, enrich=False,
                                     source="csv", max_items=MAX_ROWS)
    for r in saved["results"]:
        item = items[r["index"]]
        out = {k: v for k, v in r.items() if k != "index"}
        out["line"] = lines[r["index"]]
        if item["list"] and r["outcome"] in _SORTABLE:
            lst = list_for(item["list"])
            out["list"] = lst["name"]
            if not dry_run:
                _sort(session, user, r["id"], r["outcome"], item, lst["id"])
        elif item["kind"] and r["outcome"] in _SORTABLE and not dry_run:
            _sort(session, user, r["id"], r["outcome"], item, None)
        results.append(out)
    results.sort(key=lambda r: r["line"])
    counts: dict[str, int] = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    return {"dry_run": dry_run, "rows": len(results), "counts": counts, "results": results,
            "lists": list(lists_out.values()), "warnings": warnings}


# A row that ended up as a live place in the library, so it can join its list
# (the same set as `api_v1._SORTABLE`).
_SORTABLE = (places_svc.CREATED, places_svc.REVIVED, places_svc.DUPLICATE,
             places_svc.NEAR_DUPLICATE)


def _sort(session: Session, user, place_id: str, outcome: str, item: dict,
          list_id: str | None) -> None:
    """The D-236 sorting rule, as `api_v1._sort_saved`: the kind only on a place
    this import added or brought back (a kind the person chose stays,
    YONDER-314), and lists added to, never replaced."""
    if item["kind"] and outcome in (places_svc.CREATED, places_svc.REVIVED):
        places_svc.update_place(session, user, place_id, activity_type=item["kind"])
    if list_id:
        on = places_svc.lists_for(session, [place_id]).get(place_id, [])
        if list_id not in on:
            places_svc.set_lists(session, user, place_id, on + [list_id])
