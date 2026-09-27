"""Discovery items — the rows inside Food & Drink and Events & Activities
(Run·dere 4.5, Discovery 2.0, YONDER-147).

A finding stays the city's write-up; its places, events and activities become
DiscoveryItem rows so each can be sorted, hidden, edited and added to the
timeline. Two halves:

- `rows_from_finding(topic, data)` is pure: it turns what a resolver already
  stores in `finding.data` (food `places`, events `impacts`/`notable`) into item
  dicts. The migration's backfill and every local run use it, so both agree.
- `sync_items(...)` writes those rows for one finding. Rows are matched by name
  (case/space-insensitive); a matched row keeps what the person did with it —
  hidden, edited, added to the timeline — and edited rows keep their fields.
  Rows the new result no longer has are dropped, unless the person added them
  to the timeline or edited them.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

FOOD_KINDS = ("restaurant", "cafe", "bar", "pub", "brewery", "distillery", "winery",
              "market", "bakery", "dessert", "street_food")
EVENT_KINDS = ("event", "activity")
ITEM_TOPICS = ("food", "events")

# Fields a sync may overwrite on an untouched row (never id/links/flags).
_CONTENT_FIELDS = ("kind", "name", "neighbourhood", "address", "lat", "lon", "website",
                   "what_to_get", "price_tier", "starts_on", "ends_on", "when_text",
                   "venue", "details", "duration", "booking", "sources", "extra")

_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def name_key(name: str | None) -> str:
    """Match key: case-, space- and accent-insensitive ("Café" == "cafe")."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.strip().lower())


def _clean(v, n: int | None = None):
    if not isinstance(v, str):
        return None
    v = v.strip()
    if not v:
        return None
    return v[:n] if n else v


def _source(url) -> list | None:
    url = _clean(url, 2000)
    return [{"url": url}] if url and url.startswith(("http://", "https://")) else None


def _dates(text: str | None) -> tuple[str | None, str | None]:
    """ISO dates written in a free-text range ("2026-10-12 to 2026-10-14")."""
    found = _ISO_DATE.findall(text or "")
    if not found:
        return None, None
    return found[0], (found[-1] if len(found) > 1 else None)


def _food_rows(data: dict) -> list[dict]:
    rows = []
    for p in data.get("places") or []:
        if not isinstance(p, dict) or not _clean(p.get("name")):
            continue
        kind = (p.get("type") or "restaurant").strip().lower()
        tier = p.get("price_tier")
        extra = {}
        if p.get("local_specialty"):
            extra["local_specialty"] = True
        rows.append({
            "kind": kind if kind in FOOD_KINDS else "restaurant",
            "name": _clean(p["name"], 300),
            "neighbourhood": _clean(p.get("neighborhood") or p.get("neighbourhood"), 200),
            "address": _clean(p.get("address"), 500),
            "what_to_get": _clean(p.get("what_to_get")),
            "price_tier": tier if isinstance(tier, int) and 1 <= tier <= 4 else None,
            "details": _clean(p.get("why")),
            "website": _clean(p.get("website"), 2000),
            "sources": _source(p.get("source_url")),
            "extra": extra or None,
        })
    return rows


def _event_rows(data: dict) -> list[dict]:
    rows = []
    groups = (("impacts", True), ("notable", False))
    for key, is_impact in groups:
        for e in data.get(key) or []:
            if not isinstance(e, dict) or not _clean(e.get("name")):
                continue
            when = _clean(e.get("date_range"), 200)
            start, end = _dates(when)
            extra = {}
            if is_impact:
                extra["heads_up"] = True        # affects travel (strike, closure, crowds)
            if _clean(e.get("matched_interest")):
                extra["interest"] = e["matched_interest"].strip()
            rows.append({
                # A dated item is an event; an undated one is something to do.
                "kind": "event" if (when or is_impact) else "activity",
                "name": _clean(e["name"], 300),
                "when_text": when,
                "starts_on": start,
                "ends_on": end,
                "details": _clean(e.get("effect") if is_impact else e.get("why")),
                "address": _clean(e.get("address"), 500),
                "venue": _clean(e.get("venue"), 300),
                "sources": _source(e.get("source_url")),
                "extra": extra or None,
            })
    return rows


def rows_from_finding(topic: str, data) -> list[dict]:
    """The item rows a resolver's stored `data` describes (pure; no DB)."""
    if not isinstance(data, dict):
        return []
    if topic == "food":
        return _food_rows(data)
    if topic == "events":
        return _event_rows(data)
    return []


def _dedupe(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        k = name_key(r.get("name"))
        if k and k not in seen:
            seen.add(k)
            out.append(r)
    return out


def sync_items(session, finding, rows: list[dict], *, origin: str = "local",
               agent: str | None = None) -> None:
    """Make `finding`'s items match `rows`, keeping what the person did."""
    from app.models import DiscoveryItem

    now = datetime.now(timezone.utc)
    session.flush()                     # sessions run with autoflush off (app/db.py)
    existing = {name_key(i.name): i for i in
                session.query(DiscoveryItem).filter_by(finding_id=finding.id).all()}
    keep = set()
    for r in _dedupe(rows):
        k = name_key(r["name"])
        item = existing.get(k)
        if item is None:
            item = DiscoveryItem(finding_id=finding.id, trip_id=finding.trip_id,
                                 user_id=finding.user_id, topic=finding.topic,
                                 place_key=finding.place_key, created_at=now)
            session.add(item)
        if not item.edited:
            for f in _CONTENT_FIELDS:
                setattr(item, f, r.get(f))
            item.origin, item.agent = origin, agent
            item.updated_at = now
        keep.add(k)
    for k, item in existing.items():
        if k not in keep and not (item.activity_id or item.edited):
            session.delete(item)


def sync_from_finding(session, finding) -> None:
    """Re-derive a local finding's items from its stored data."""
    if finding.topic in ITEM_TOPICS:
        if finding.id is None:
            session.flush()
        sync_items(session, finding, rows_from_finding(finding.topic, finding.data))


def delete_for_finding(session, finding_id: str) -> None:
    from app.models import DiscoveryItem
    session.query(DiscoveryItem).filter_by(finding_id=finding_id).delete()


def unlink_activity(session, activity_id: str) -> None:
    """A timeline activity was deleted: items added as it are off the timeline."""
    from app.models import DiscoveryItem
    session.query(DiscoveryItem).filter(DiscoveryItem.activity_id == activity_id).update(
        {"activity_id": None}, synchronize_session=False)


def item_dict(i) -> dict:
    """The public shape of an item (API, MCP, templates)."""
    return {"id": i.id, "finding_id": i.finding_id, "topic": i.topic,
            "place_key": i.place_key, "kind": i.kind, "name": i.name,
            "neighbourhood": i.neighbourhood, "address": i.address,
            "lat": i.lat, "lon": i.lon, "website": i.website,
            "what_to_get": i.what_to_get, "price_tier": i.price_tier,
            "starts_on": i.starts_on, "ends_on": i.ends_on, "when_text": i.when_text,
            "venue": i.venue, "details": i.details, "duration": i.duration,
            "booking": i.booking, "sources": i.sources or [], "extra": i.extra or {},
            "origin": i.origin, "agent": i.agent, "hidden": bool(i.hidden),
            "edited": bool(i.edited), "activity_id": i.activity_id}
