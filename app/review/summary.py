"""Human summaries of a TravelItem — what the booking IS, not what the row is.

The raw admin list shows two UUIDs and a canonical key, which tells an operator
nothing. Everything here answers "what am I looking at?" in the traveller's own
terms: airline and route, hotel and city, show and date.
"""
from __future__ import annotations

_KIND_LABEL = {"flight": "Flight", "lodging": "Hotel", "event": "Event",
               "ground_transport": "Ground", "note": "Note", "unknown": "Unknown"}
_MODE_LABEL = {"train": "Train", "bus": "Bus", "ferry": "Ferry", "car": "Drive",
               "car_rental": "Car rental", "rideshare": "Rideshare",
               "transfer": "Transfer", "other": "Ground"}


def _place(p: dict | None) -> str:
    if not p:
        return "?"
    return (p.get("iata") or p.get("city") or p.get("name")
            or p.get("raw") or "?")


def _date(dt: dict | None) -> str:
    if not dt or not dt.get("local"):
        return ""
    return str(dt["local"])[:10]


def kind_label(payload: dict) -> str:
    t = payload.get("item_type")
    if t == "ground_transport":
        mode = (payload.get("ground_transport") or {}).get("mode")
        return _MODE_LABEL.get(mode, "Ground")
    return _KIND_LABEL.get(t, "Item")


def headline(payload: dict) -> str:
    """One line naming the thing: 'Air Canada AC4826', 'Limmat House Zurich'."""
    t = payload.get("item_type")
    if t == "flight":
        f = payload.get("flight") or {}
        mc = f.get("marketing_carrier") or payload.get("provider") or {}
        carrier = (mc.get("name") or mc.get("iata") or "") if isinstance(mc, dict) else ""
        num = f.get("flight_number") or ""
        code = (mc.get("iata") if isinstance(mc, dict) else "") or ""
        return f"{carrier} {code}{num}".strip() or "Flight"
    if t == "lodging":
        return (payload.get("lodging") or {}).get("property_name") or "Hotel booking"
    if t == "event":
        return (payload.get("event") or {}).get("title") or "Event"
    if t == "ground_transport":
        g = payload.get("ground_transport") or {}
        prov = payload.get("provider") or {}
        name = prov.get("name") if isinstance(prov, dict) else None
        if name:
            return name
        # No operator named in the document: lead with the route, which is what
        # identifies the journey to a person. A bare "Train" says nothing.
        dep, arr = g.get("departure") or {}, g.get("arrival") or {}
        a, b = _place(dep.get("place")), _place(arr.get("place"))
        label = _MODE_LABEL.get(g.get("mode"), "Ground travel")
        if a != "?" and b != "?" and a != b:
            return f"{label}: {a.split(',')[0]} → {b.split(',')[0]}"
        if a != "?":
            return f"{label} from {a.split(',')[0]}"
        return label
    return payload.get("title") or "Item"


def subline(payload: dict) -> str:
    """Route/place and dates: 'Toronto YYZ → Grand Cayman GCM · 30 Jun 2023'."""
    t = payload.get("item_type")
    if t == "flight":
        f = payload.get("flight") or {}
        dep, arr = f.get("departure") or {}, f.get("arrival") or {}
        d = _date(dep.get("time"))
        return f"{_place(dep.get('place'))} → {_place(arr.get('place'))}" + (f" · {d}" if d else "")
    if t == "lodging":
        l = payload.get("lodging") or {}
        where = _place(l.get("place"))
        ci, co = _date(l.get("check_in")), _date(l.get("check_out"))
        span = f"{ci} → {co}" if ci and co else (ci or "")
        return " · ".join(x for x in (where, span) if x and x != "?")
    if t == "event":
        e = payload.get("event") or {}
        where = _place(e.get("venue"))
        return " · ".join(x for x in ((where if where != "?" else ""),
                                      _date(e.get("start"))) if x)
    if t == "ground_transport":
        g = payload.get("ground_transport") or {}
        dep, arr = g.get("departure") or {}, g.get("arrival") or {}
        d = _date(dep.get("time"))
        a, b = _place(dep.get("place")), _place(arr.get("place"))
        route = f"{a} → {b}" if a != b else a
        return " · ".join(x for x in (route, d) if x and x != "?")
    return ""


def reference(payload: dict) -> str:
    ref = payload.get("booking_reference")
    if not ref:
        return ""
    return f"{'PNR' if payload.get('item_type') == 'flight' else 'conf'} {ref}"


# ---------------------------------------------------------------- write preview

_TARGET_LABEL = {
    "airtrail.flight": ("AirTrail", "Log flight"),
    "adventurelog.collection": ("AdventureLog", "New trip"),
    "adventurelog.transportation": ("AdventureLog", "Transportation"),
    "adventurelog.lodging": ("AdventureLog", "Lodging"),
    "adventurelog.note": ("AdventureLog", "Note"),
    "adventurelog.location": ("AdventureLog", "Location"),
    "yonder.activity": ("Run·dere", "Trip activity"),
}


def write_preview(item_payload: dict, resolution: dict,
                  revision: dict | None = None) -> list[dict]:
    """What logging this will actually do, including what it will NOT do.

    Suppressed places are listed explicitly and struck through: the system
    deciding not to write your home airport is invisible otherwise, and
    invisible decisions are the ones that erode trust in an automated write.

    Revision-aware (D-57): an `update` reads "Update …" (the record is revised in
    place, not created again); a cancellation reads "Cancel flight …" / "Mark
    cancelled …" and shows the records it leaves untouched. Default is create.
    """
    from app.committing import _REV_CANCEL_MARK, _REV_UPDATABLE
    out: list[dict] = []
    resolution = resolution or {}
    targets = resolution.get("targets") or []
    coll_name = resolution.get("collection_name")
    has_collection = bool(coll_name or resolution.get("collection_id"))
    minting_here = "adventurelog.collection" in targets
    action = (revision or {}).get("action") or "create"

    # An item that files into a trip it is NOT itself minting — a MATCHED trip,
    # or an ATTACH leg whose trip its sibling mints — must name that trip, or the
    # operator cannot tell WHICH trip it landed in (the whole point of review).
    # Only a minting flight creates one, shown as "New trip" in the loop below;
    # saying "New trip" for a match would signal a broken D-04.
    if has_collection and not minting_here:
        out.append({"system": "AdventureLog", "state": "ref",
                    "text": (f"Files into trip “{coll_name}”" if coll_name
                             else "Files into existing trip")})

    for target in targets:
        system, label = _TARGET_LABEL.get(target, ("", target))

        # Yonder (first-party, 2.2.0): filed onto the linked in-house trip,
        # independent of AirTrail/AdventureLog. Its own create/update/remove line.
        if target == "yonder.activity":
            if action == "supersede":
                out.append({"system": "Run·dere", "state": "cancel",
                            "text": f"Remove {headline(item_payload)} from your Run·dere trip"})
            else:
                verb = "Update" if action == "update" else "Add"
                out.append({"system": "Run·dere",
                            "state": "update" if action == "update" else "new",
                            "text": f"{verb} {headline(item_payload)} on your Run·dere trip"})
            continue

        # A cancellation removes the flight, marks the AdventureLog record, and
        # leaves everything else (locations, trip) as it is (D-57).
        if action == "supersede":
            if target == "airtrail.flight":
                out.append({"system": system, "state": "cancel",
                            "text": f"Cancel flight {headline(item_payload)}".strip()})
            elif target in _REV_CANCEL_MARK:
                out.append({"system": system, "state": "cancel",
                            "text": f"Mark cancelled · {label}"})
            else:
                out.append({"system": system, "state": "skip", "text": label,
                            "why": "Left unchanged by a cancellation."})
            continue

        # An update revises the existing record in place (D-57).
        if action == "update" and target in _REV_UPDATABLE:
            text = (f"Update flight {headline(item_payload)}".strip()
                    if target == "airtrail.flight" else f"Update {label.lower()}")
            out.append({"system": system, "state": "update", "text": text})
            continue

        text = label
        if target == "adventurelog.collection" and coll_name:
            text = f"New trip “{coll_name}”"
        elif target == "airtrail.flight":
            text = f"Log flight {headline(item_payload)}".strip()
        elif target == "adventurelog.location":
            names = [_place(p) for p in _unsuppressed_places(item_payload)]
            text = "Location · " + ", ".join(n for n in names if n != "?") if names else label
        out.append({"system": system, "text": text, "state": "new"})

    # AdventureLog lines only for people who have it connected (D-150, D-192).
    # Items resolved before `integrations` was recorded keep the old wording.
    al_on = "adventurelog" in resolution.get("integrations", ["adventurelog"])
    if al_on:
        for p in _suppressed_places(item_payload):
            out.append({"system": "AdventureLog",
                        "text": f"Location · {_place(p)}",
                        "state": "skip",
                        "why": _suppression_sentence(p)})

    # Nothing is filed until the item is on a Run·dere trip — say so in Run·dere's
    # terms, first (D-148, D-192). An unsettled AdventureLog collection is a
    # second, optional line, and AirTrail is only mentioned when a flight logs there.
    no_trip = ("yonder.activity" not in targets
               and resolution.get("outcome") not in ("ignored",)
               and not resolution.get("within_home_radius")
               and (resolution.get("collection_pending")
                    or resolution.get("outcome") in ("deferred", "needs_review")))
    if no_trip:
        out.append({"system": "Run·dere", "state": "skip", "text": "Not on a trip yet",
                    "why": "Pick a trip above to put it on that trip's timeline."
                           + (" The flight still logs to AirTrail."
                              if "airtrail.flight" in targets else "")})
    if resolution.get("collection_pending") and al_on:
        out.append({"system": "AdventureLog", "state": "skip",
                    "text": "Trip record (optional)",
                    "why": "No AdventureLog collection covers these dates. Pick one "
                           "under AdventureLog to also file it there."})
    return out


def _all_places(payload: dict) -> list[dict]:
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


def _suppressed_places(payload: dict) -> list[dict]:
    return [p for p in _all_places(payload) if p.get("suppressed")]


def _unsuppressed_places(payload: dict) -> list[dict]:
    return [p for p in _all_places(payload) if not p.get("suppressed")]


def _suppression_sentence(place: dict) -> str:
    reason = place.get("suppression_reason")
    where = _place(place)
    if reason == "home":
        return (f"{where} is one of your home airports, so it is never logged as "
                f"a place you visited — only as this journey’s endpoint.")
    if reason == "home_radius":
        return f"{where} is inside your home area, so it isn’t logged as a place you visited."
    return f"{where} isn’t being logged as a location."
