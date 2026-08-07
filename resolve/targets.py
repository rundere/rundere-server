"""What SHOULD be written — computed at resolve, executed at commit.

Encodes the architectural boundaries (D-22) and AdventureLog's structural
constraints (D-14), so the commit stage never has to make policy decisions.
"""
from __future__ import annotations

# AdventureLog has no orphan path for these: they REQUIRE a collection (RULE-08).
COLLECTION_EXCLUSIVE = {"adventurelog.transportation", "adventurelog.lodging",
                        "adventurelog.note"}


def compute_targets(payload: dict, *, self_on_booking: bool,
                    within_home: bool, has_collection: bool,
                    unsuppressed_places: int,
                    creating_collection: bool = False) -> list[str]:
    """The write set for one item.

    self_on_booking=False means a guest-only flight: AirTrail is excluded
    entirely, enforced here at resolution rather than at commit (D-22/RULE-10).
    """
    if within_home:
        return []                       # never left home; not a trip (RULE-28b)

    item_type = payload.get("item_type")
    targets: list[str] = []

    if item_type == "flight":
        if self_on_booking:
            targets.append("airtrail.flight")       # RULE-10 gate
        targets.append("adventurelog.transportation")
    elif item_type == "ground_transport":
        targets.append("adventurelog.transportation")
    elif item_type == "lodging":
        targets.append("adventurelog.lodging")
    elif item_type == "event":
        targets.append("adventurelog.note")
    elif item_type == "note":
        targets.append("adventurelog.note")

    # One flight writes TWO Locations (departure + arrival); suppressed places
    # (home) contribute none (D-07/D-14).
    if unsuppressed_places:
        targets.append("adventurelog.location")

    # A collection is only a WRITE when this item is the one minting it.
    # Matching an existing trip means referencing it, not creating it — and
    # only flights ever mint (D-04). Getting this wrong made every matched
    # hotel and event advertise "New trip …" in the review queue, which reads
    # exactly like the rule being violated even though the resolver had
    # correctly matched an existing trip.
    if creating_collection and (COLLECTION_EXCLUSIVE & set(targets)):
        targets.append("adventurelog.collection")

    # Stable order, no duplicates.
    seen, out = set(), []
    for t in targets:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def enrich_places_from_airports(payload: dict, airports: dict) -> dict:
    """Fill country/lat/lon/tz on airport Places from AirTrail's own airport
    reference data. Extractors legitimately cannot know an airport's country —
    it is not printed on the ticket — and D-23 needs it to name a collection,
    while the home-radius test needs coordinates. Only ever ADDS missing
    fields; anything the document actually stated wins."""
    if not airports:
        return payload

    def apply(place: dict) -> dict:
        iata = (place.get("iata") or "").upper()
        ref = airports.get(iata)
        if not ref:
            return place
        out = dict(place)
        for field, value in (("country", ref.country), ("lat", ref.lat),
                             ("lon", ref.lon), ("tz", ref.tz)):
            if value is not None and out.get(field) in (None, ""):
                out[field] = value
        if not out.get("name") and ref.name:
            out["name"] = ref.name
        if not out.get("city") and ref.municipality:
            out["city"] = ref.municipality
        return out

    def walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, dict) and "raw" in v and not {"local", "precision"} & set(v):
                    node[k] = apply(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, dict) and "raw" in v and not {"local", "precision"} & set(v):
                    node[i] = apply(v)
                else:
                    walk(v)

    walk(payload)
    return payload


def places_in_payload(payload: dict) -> list[dict]:
    """Every Place object in a payload, in a stable order. A Place is
    identified structurally: it has `raw` and is not a DateTimeLocal."""
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            if "raw" in node and not {"local", "precision"} & set(node):
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return found


def item_date_span(payload: dict) -> tuple[str | None, str | None]:
    """(start, end) local dates for collection matching. Returns the earliest
    and latest date mentioned; None when the payload carries no dates."""
    dates: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if "local" in node and "precision" in node and node.get("local"):
                dates.append(str(node["local"])[:10])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    if not dates:
        return None, None
    return min(dates), max(dates)
