"""Resolve stage: TravelItems with status.resolve=pending -> resolved.

Per item, in order:
  1. canonical_key + alternates (A5)      -> item_keys, dedup lookup
  2. revision decision vs any prior item sharing a key (D-13)
  3. home annotation + suppression (D-07)
  4. collection resolution ladder (A6) against live AdventureLog collections
  5. write targets (D-14/D-22)
  6. rules engine gate (D-25 enablement from rule_settings)

A rule violation never silently proceeds: the item goes needs_review with the
violations recorded. no_change items are retained (OQ-03) but write nothing.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

import rules as rules_module
from adapters.targets.adventurelog import AdventureLogClient, AdventureLogError
from app.models import ItemKey, RuleSetting, TravelItem
from extract.normalize import normalize_payload
from resolve import keys as keys_mod
from resolve import revision as rev_mod
from resolve.collections import (CollectionRef, Resolution, collection_name,
                                 resolve_collection)
from resolve.home import (HomeProfileRef, annotate_place, match_place,
                          within_home_radius)
from resolve.identity import Identity, resolve_travelers
from resolve.geocode import PhotonGeocoder, apply_geocode
from resolve.targets import (compute_targets, enrich_places_from_airports,
                             item_date_span, places_in_payload)

log = logging.getLogger("yonder.resolving")

# Targets that need NO AdventureLog collection, so they stay live even while the
# AdventureLog trip is unresolved: a flight's AirTrail record, and the in-house
# Yonder activity (filed under a Yonder trip, an entirely separate system).
# Keeping these live lets a capture reach AirTrail / Yonder even when its
# AdventureLog trip is still unresolved (owner, 2026-08-10 — backfilling old legs
# that may never map cleanly to an AdventureLog trip).
_COLLECTION_INDEPENDENT_TARGETS = {"airtrail.flight", "yonder.activity"}


def _collection_reason(coll) -> str:
    """The review-reason code for a clean item whose only open question is which
    trip it belongs to: several candidates -> pick one; otherwise none matched."""
    if coll is not None and getattr(coll, "candidate_collections", None):
        return "ambiguous_collection"
    return "no_collection_match"


def _auto_link_yonder_trip(session: Session, item: TravelItem,
                           start: str | None, end: str | None) -> None:
    """Link the item to a Yonder trip when EXACTLY ONE trip's dates contain it
    (owner, 2026-08-10: auto-link on resolve when unambiguous).

    Independent of AdventureLog — Yonder trips are their own system. Never
    overrides a human: a manual link OR unlink in the review picker pins the
    item (review.trip_pinned) and the resolver then leaves the link alone. Two or
    more matching trips are left for the operator to pick (like the AL ladder,
    the resolver never guesses between overlapping trips)."""
    if item.trip_id or (item.review or {}).get("trip_pinned"):
        return
    if not (start and end):
        return
    from app.models import Trip
    matches = [t for t in session.query(Trip).all()
               if t.start_date and t.end_date
               and t.start_date <= start and end <= t.end_date]
    if len(matches) == 1:
        item.trip_id = matches[0].id
        log.info("auto-linked item %s to Yonder trip %s", item.id, matches[0].id)


def load_home_profiles(session: Session) -> list[HomeProfileRef]:
    from app.models import HomeProfile
    return [HomeProfileRef(
        id=str(p.id), kind=p.kind, label=p.label, iata=p.iata,
        lat=p.lat, lon=p.lon, radius_km=p.radius_km or 50.0,
        country=p.country, effective_from=p.effective_from,
        effective_to=p.effective_to)
        for p in session.query(HomeProfile).all()]


def _capturer_identity(session: Session, item: TravelItem,
                       identities: list[Identity]) -> Identity | None:
    """The traveller of the user who captured this item (4.1) — who a booking
    is assumed to be for when it names no one. None for an open capture or a
    user with no traveller linked (the caller then falls back to `self`)."""
    if not item.owner_id:
        return None
    from app.models import User
    user = session.get(User, item.owner_id)
    if user is None or user.traveler_id is None:
        return None
    return next((i for i in identities if i.id == str(user.traveler_id)), None)


def load_identities(session: Session) -> list[Identity]:
    from app.models import TravelerIdentity
    return [Identity(id=str(t.id), display_name=t.display_name, kind=t.kind,
                     aliases=tuple(t.aliases or []),
                     airtrail_user_id=t.airtrail_user_id)
            for t in session.query(TravelerIdentity).all()]


def load_enablement(session: Session) -> dict[str, str]:
    return {r.rule_id: r.mode for r in session.query(RuleSetting).all()
            if r.rule_id in rules_module.RULES}


def _annotate_places(payload: dict, profiles: list[HomeProfileRef],
                     on_date: str | None) -> tuple[dict, list[dict]]:
    """Rewrite every Place in-place with home fields. Returns (payload, places)."""
    def walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, dict) and "raw" in v and not {"local", "precision"} & set(v):
                    node[k] = annotate_place(v, profiles, on_date)
                else:
                    walk(v)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, dict) and "raw" in v and not {"local", "precision"} & set(v):
                    node[i] = annotate_place(v, profiles, on_date)
                else:
                    walk(v)

    walk(payload)
    return payload, places_in_payload(payload)


def _find_prior(session: Session, item: TravelItem,
                key_set: frozenset[str]) -> tuple[TravelItem | None, str | None]:
    """The EARLIER stored item sharing any key with this one (A5: identity is
    key-set intersection, not primary equality).

    "Earlier" is load-bearing, not incidental: resolution must be re-runnable
    after the resolver improves, and a symmetric match would let the original
    `create` find one of its own duplicates on a second pass and demote itself
    to `no_change` — leaving a booking that nothing writes. Anchoring on
    created_at makes the outcome identical however many times it runs.
    """
    rows = (session.query(ItemKey)
            .filter(ItemKey.key.in_(list(key_set)))
            .filter(ItemKey.item_id != item.id).all())
    candidates = []
    for row in rows:
        prior = session.get(TravelItem, row.item_id)
        if prior is None or not prior.extraction:
            continue
        if (_as_utc(prior.created_at) and _as_utc(item.created_at)
                and _as_utc(prior.created_at) >= _as_utc(item.created_at)):
            continue                      # same-or-later: not a predecessor
        candidates.append((prior, row.key))
    if not candidates:
        return None, None
    # Most recent predecessor: an update chains onto the latest version, not
    # the original.
    candidates.sort(key=lambda c: _as_utc(c[0].created_at))
    return candidates[-1]


def _as_utc(dt):
    """Coerce a datetime to tz-aware UTC. Postgres returns tz-aware timestamps,
    but SQLite (and any naive value) returns naive ones, and comparing the two
    raises — so normalise before any created_at comparison."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_local(s) -> datetime | None:
    """A DateTimeLocal `local` (date or datetime) as a naive datetime, for
    comparing dwell times. Never raises."""
    if not s:
        return None
    s = str(s)
    try:
        return datetime.fromisoformat(s[:16] if "T" in s else s[:10])
    except ValueError:
        return None


def _gap_hours(arrive, depart_next) -> float | None:
    a, b = _parse_local(arrive), _parse_local(depart_next)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 3600.0


def _leg_facts(payload: dict, profiles: list[HomeProfileRef]) -> dict | None:
    """Home/route/time facts for one flight leg, used to group a journey.

    Home status is computed HERE via match_place, never trusted from the
    payload's own `is_home` flag. resolve_pending commits one item at a time,
    so a sibling leg queried mid-batch (e.g. the return flight of a pair
    captured together) may not have been through its own _annotate_places yet
    -- its stored `is_home` reads False/absent even though it plainly IS the
    home arrival. That let an envelope search skip right past the real return
    leg and snap onto whatever LATER, already-resolved home-arrival flight it
    found next (owner bug report, 2026-08-11: a same-day outbound/return pair
    minted into two different trips, one of them months away). Recomputing
    from the raw place independent of resolve order fixes that regardless of
    which leg processes first.
    """
    f = payload.get("flight") or {}
    dep, arr = f.get("departure") or {}, f.get("arrival") or {}
    dep_place, arr_place = dep.get("place") or {}, arr.get("place") or {}
    d_local = str((dep.get("time") or {}).get("local") or "")
    a_local = str((arr.get("time") or {}).get("local") or "")
    d_date = d_local[:10]
    a_date = a_local[:10] or d_date
    if not d_date:
        return None
    return {"from_home": match_place(dep_place, profiles, d_date)[0] is not None,
            "to_home": match_place(arr_place, profiles, a_date)[0] is not None,
            "dep": d_date, "arr": a_date,
            # Full timestamp for ORDERING: connecting legs routinely share a
            # date, and ordering them by anything else (a uuid, insertion
            # order) can put the middle leg before the departure from home,
            # which then looks like an unclosed journey.
            "order": d_local or d_date,
            # Arrival time + country, for the DESTINATION heuristic: the trip is
            # named after where the traveller dwells longest, not a layover.
            "arr_order": a_local or a_date,
            "arr_country": arr_place.get("country")}


def _trip_envelope(session: Session, item: TravelItem, payload: dict,
                   profiles: list[HomeProfileRef]) -> tuple[str, str] | None:
    """The home-to-home span containing this flight, or None if it isn't closed.

    A trip is bounded by home (owner, 2026-08-03): flying AWAY from home is
    half a journey, and until a flight brings you back the system genuinely
    cannot know when the trip ends. Minting from an open-ended departure would
    invent an end date, and every later leg that fell outside the invented
    range would mint ANOTHER trip for the same journey.

    So: walk the flight legs chronologically, take the last home-departure at
    or before this leg and the first home-arrival at or after it. Both present
    => a closed trip whose real boundaries are known. Otherwise => None, and
    the caller defers until the return leg arrives.
    """
    here = _leg_facts(payload, profiles)
    if not here:
        return None

    legs = [(here["order"], here, item.id)]
    for sib in (session.query(TravelItem)
                .filter(TravelItem.item_type == "flight")
                .filter(TravelItem.id != item.id).all()):
        facts = _leg_facts((sib.extraction or {}).get("payload") or {}, profiles)
        if facts:
            legs.append((facts["order"], facts, sib.id))
    legs.sort(key=lambda x: (x[0], str(x[2])))
    idx = next(i for i, (_, f, iid) in enumerate(legs) if iid == item.id)

    start = next((legs[i][1]["dep"] for i in range(idx, -1, -1)
                  if legs[i][1]["from_home"]), None)
    end = next((legs[i][1]["arr"] for i in range(idx, len(legs))
                if legs[i][1]["to_home"]), None)
    if start and end and start <= end:
        return start, end
    return None


def _journey_destination(members: list) -> str | None:
    """The ISO country of a journey's DESTINATION — the non-home airport where
    the traveller dwells longest. A trip through Munich to Bangalore stays ~1 day
    in Munich (a layover) and ~3 weeks in Bangalore, so the biggest gap between a
    leg's arrival and the next leg's departure sits at the real destination. This
    is why naming the trip from the first leg's arrival country was wrong: it
    picked the layover. Ties and missing times fall back to the last non-home
    arrival before the traveller comes home.
    """
    best_gap, dest = -1.0, None
    for i in range(len(members) - 1):
        f = members[i][1]
        gap = _gap_hours(f.get("arr_order"), members[i + 1][1].get("order"))
        if (gap is not None and gap > best_gap
                and not f["to_home"] and f.get("arr_country")):
            best_gap, dest = gap, f["arr_country"]
    if dest is None:
        for (_, f, _iid) in reversed(members):
            if not f["to_home"] and f.get("arr_country"):
                dest = f["arr_country"]
                break
    return dest


def _journey_minter(session: Session, item: TravelItem, payload: dict,
                    profiles: list[HomeProfileRef]
                    ) -> tuple[tuple[str, str], str, str | None] | None:
    """For a flight in a CLOSED home-to-home journey, return
    (envelope, minter_id, destination_country): the single leg that MINTS the
    shared trip (the earliest, so its siblings ATTACH), plus the journey's
    destination country computed ONCE — every leg derives the same value, so the
    whole journey agrees on one trip name (owner: resolver picks one minter,
    2026-08-05). None when the journey is not closed (defer until the return).
    """
    here = _leg_facts(payload, profiles)
    if not here:
        return None
    legs = [(here["order"], here, item.id)]
    for sib in (session.query(TravelItem)
                .filter(TravelItem.item_type == "flight")
                .filter(TravelItem.id != item.id).all()):
        facts = _leg_facts((sib.extraction or {}).get("payload") or {}, profiles)
        if facts:
            legs.append((facts["order"], facts, sib.id))
    legs.sort(key=lambda x: (x[0], str(x[2])))
    idx = next(i for i, (_, f, iid) in enumerate(legs) if iid == item.id)
    start = next((legs[i][1]["dep"] for i in range(idx, -1, -1)
                  if legs[i][1]["from_home"]), None)
    end = next((legs[i][1]["arr"] for i in range(idx, len(legs))
                if legs[i][1]["to_home"]), None)
    if not (start and end and start <= end):
        return None
    # Members: every leg whose departure falls inside the closed envelope. The
    # earliest is the minter; the same computation on any member yields the same
    # minter and the same destination, so the legs agree without coordinating.
    members = [(o, f, iid) for (o, f, iid) in legs if start <= f["dep"] <= end]
    minter_id = members[0][2] if members else item.id
    return (start, end), minter_id, _journey_destination(members)


_MAPS_LATLON = re.compile(r"!1d(-?\d+\.\d+)!2d(-?\d+\.\d+)")
_MAPS_COORD_RAW = re.compile(r"^\s*(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)\s*$")


def _apply_maps_coords(payload: dict) -> dict:
    """Give a google_maps drive's stops their exact coordinates from the URL, so
    the geocoder need not (and usually cannot) locate Google's abbreviated
    formatted addresses ("100 … Pkwy, … ON Z9Z 9Z9"). Two sources, in order of
    trust: a bare-coordinate stop's own `raw`, then the `!1d<lon>!2d<lat>` pairs
    Google embeds in `route.source_url` (route order). Coords live at RESOLVE, not
    extraction — the extraction Place schema has no coord fields (D-61)."""
    gt = payload.get("ground_transport") or {}
    route = gt.get("route") or {}
    if route.get("provider") != "google_maps":
        return payload
    stops = [gt.get("departure"), *(gt.get("waypoints") or []), gt.get("arrival")]
    stops = [m for m in stops if isinstance(m, dict) and isinstance(m.get("place"), dict)]
    for m in stops:                                  # 1) a stop that IS a coordinate
        p = m["place"]
        cm = _MAPS_COORD_RAW.match(str(p.get("raw") or ""))
        if cm and p.get("lat") is None:
            p["lat"], p["lon"] = float(cm.group(1)), float(cm.group(2))
    pairs = _MAPS_LATLON.findall(str(route.get("source_url") or ""))
    if len(pairs) == len(stops):                     # 2) embedded per-stop coords
        for m, (lon, lat) in zip(stops, pairs):
            p = m["place"]
            if p.get("lat") is None:
                p["lat"], p["lon"] = float(lat), float(lon)
    return payload


def _geocode_places(payload: dict, geocoder: PhotonGeocoder) -> dict:
    """Locate non-airport places. Only fills blanks, and only on a result
    consistent with what the document said (see resolve/geocode.py)."""
    def walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, dict) and "raw" in v and not {"local", "precision"} & set(v):
                    if v.get("lat") is None and not v.get("iata"):
                        node[k] = apply_geocode(v, geocoder.geocode_place(v))
                else:
                    walk(v)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, dict) and "raw" in v and not {"local", "precision"} & set(v):
                    if v.get("lat") is None and not v.get("iata"):
                        node[i] = apply_geocode(v, geocoder.geocode_place(v))
                else:
                    walk(v)
    walk(payload)
    return payload


def resolve_item(session: Session, item: TravelItem,
                 profiles: list[HomeProfileRef],
                 collections: list[CollectionRef],
                 enablement: dict[str, str],
                 airports: dict | None = None,
                 geocoder: PhotonGeocoder | None = None,
                 country_names: dict[str, str] | None = None,
                 identities: list[Identity] | None = None) -> str:
    """Resolve one item; returns the new status.resolve value."""
    now = datetime.now(timezone.utc).isoformat()
    payload = (item.extraction or {}).get("payload") or {}
    if not payload:
        item.status = {**(item.status or {}), "resolve": "skipped"}
        return "skipped"
    # Deterministic normalization is idempotent, so applying it here also
    # repairs items extracted before a normalization rule existed — no need to
    # re-run the LLM over stored payloads.
    payload = normalize_payload(payload)
    # A google_maps drive already knows its stops' exact coords (in the URL);
    # apply them up front — the road key is built from the endpoint coordinates,
    # so this must happen BEFORE keying, not just before geocoding (D-61).
    payload = _apply_maps_coords(payload)

    cc = (item.source or {}).get("capture_context") or {}

    # ---- 1. keys
    key_result = keys_mod.keys_for(payload, cc)
    if key_result is None:
        item.review = {**(item.review or {}), "required": True,
                       "reasons": [keys_mod.unkeyable_reason(payload, cc)]}
        item.resolution = {"ran_at": now, "outcome": "needs_review",
                           "match_attempted": False}
        item.status = {**(item.status or {}), "resolve": "needs_review"}
        return "needs_review"

    # ---- 2. revision vs prior
    prior_item, _ = _find_prior(session, item, key_result.all_keys())
    prior_ref = None
    prior_payload = None
    if prior_item is not None:
        prior_payload = (prior_item.extraction or {}).get("payload")
        prior_rev = prior_item.revision or {}
        prior_ref = rev_mod.PriorItem(
            id=prior_item.id,
            content_hash=prior_rev.get("content_hash") or rev_mod.content_hash(prior_payload or {}),
            document_timestamp=(prior_item.source or {}).get("document_timestamp"),
            revision=prior_rev.get("revision", 1),
            field_locks=tuple(prior_rev.get("field_locks") or []),
            has_target_record=bool(prior_item.commit))
    decision = rev_mod.decide(
        payload, prior_ref,
        document_timestamp=(item.source or {}).get("document_timestamp"),
        prior_payload=prior_payload)

    # ---- 3. locate, then home-annotate
    start, end = item_date_span(payload)
    # Airports first from AirTrail's own reference data (free, exact), then
    # Photon for everything else it cannot cover — hotels, venues, stations.
    if airports:
        payload = enrich_places_from_airports(payload, airports)
    if geocoder is not None:
        payload = _geocode_places(payload, geocoder)
    payload, places = _annotate_places(payload, profiles, start)
    home_only = within_home_radius(places)
    unsuppressed = sum(1 for p in places if not p.get("suppressed"))

    # In-house Yonder trip auto-link (independent of the AdventureLog ladder
    # below): a real journey unambiguously inside one Yonder trip gets linked
    # here; home-only movement isn't a trip, so skip it.
    if not home_only:
        _auto_link_yonder_trip(session, item, start, end)

    # ---- 4. collection ladder (A6). A flight in a CLOSED home-to-home journey
    # belongs to ONE trip shared by every leg: the earliest leg MINTS it and its
    # siblings ATTACH, instead of each leg minting its own (owner: resolver picks
    # one minter, 2026-08-05). _journey_minter names that single minter.
    is_flight = payload.get("item_type") == "flight"
    journey = (_journey_minter(session, item, payload, profiles)
               if (is_flight and not home_only) else None)
    envelope = journey[0] if journey else None
    is_minter = bool(journey) and journey[1] == item.id
    journey_dest = journey[2] if journey else None
    anchor_eligible = bool(journey)
    mint_group = None
    coll = None
    if not home_only and start and end:
        mint_country = None
        if anchor_eligible:
            # Name the trip after the JOURNEY's destination — the turnaround the
            # traveller dwells at — computed ONCE for every leg, so a layover
            # country (Munich on the way to India) never wins and no two legs of
            # one journey disagree on the name. Fall back to this leg's own
            # arrival country only if the journey destination is unknown.
            code = journey_dest
            if not code:
                for p in reversed(places):
                    if not p.get("is_home") and p.get("country"):
                        code = p["country"]
                        break
            if code:
                # Name the trip the way AdventureLog names countries, not by raw
                # ISO code ("Cayman Islands", not "KY").
                code = str(code).upper()
                mint_country = (country_names or {}).get(code, code)
        coll = resolve_collection(
            item_type=payload.get("item_type") or "note",
            item_start=start, item_end=end, collections=collections,
            capture_collection_id=cc.get("collection_id"),
            today=now[:10], mint_country=mint_country,
            # Mint across the whole home-to-home journey, not this one leg.
            mint_start=envelope[0] if envelope else None,
            mint_end=envelope[1] if envelope else None,
            can_mint=is_minter)            # only the designated minter creates it
        if journey:
            # Both minter and attach legs carry the group key so the commit stage
            # can join an attach leg to the collection the minter creates.
            mint_group = f"{envelope[0]}..{envelope[1]}"
            # A non-minter leg with no EXISTING collection match would defer as if
            # it were a lone open journey; instead attach it to the sibling
            # minter's (to-be-created) trip. It computes the same trip name from
            # the same destination + envelope, so the two never disagree.
            if not is_minter and coll.outcome == "deferred" and mint_country:
                coll = Resolution(
                    collection_action="created", outcome="resolved",
                    collection_name=collection_name(mint_country, envelope[0],
                                                    envelope[1]),
                    match_attempted=True)

    # Yonder trip minting mirrors the AdventureLog ladder (owner, 2026-08-12):
    # whenever this leg is minting OR attaching to a NEW AdventureLog trip and no
    # EXISTING Yonder trip already covers it (_auto_link_yonder_trip above found
    # no unambiguous match), a Yonder trip should exist too — the two systems
    # track the same journey and must not disagree on whether a trip exists. The
    # attach-leg override just above also sets collection_action == "created", so
    # this single check already covers both legs of the journey without having to
    # special-case the minter vs. attach split again.
    mint_yonder_trip = bool(
        coll and coll.collection_action == "created" and not item.trip_id)

    # ---- 5. who travelled — FLIGHTS ONLY (D-22/RULE-10). Traveller identity
    # exists solely to keep guest-only flights out of AirTrail; nothing else
    # writes to AirTrail, and AdventureLog does not care who was on a booking.
    # Running this on events/lodging/ground would demand "confirm who
    # travelled" for a dinner invite, which is meaningless.
    needs_traveler_review = False
    self_on_booking = True
    if payload.get("item_type") == "flight":
        if identities:
            tr = resolve_travelers(payload, identities,
                                   default=_capturer_identity(session, item, identities))
            payload["travelers"] = tr.travelers
            self_on_booking = tr.self_on_booking
            needs_traveler_review = tr.inferred
        else:
            # No identities configured yet: assume self rather than excluding
            # every flight, but flag it so RULE-11's notify half is satisfied.
            needs_traveler_review = not (payload.get("travelers") or [])

    # ---- 6. targets
    has_collection = bool(coll and (coll.collection_id or coll.collection_name))
    targets = compute_targets(
        payload, self_on_booking=self_on_booking, within_home=home_only,
        has_collection=has_collection, unsuppressed_places=unsuppressed,
        # Only the minter writes the collection; an attach leg references it.
        creating_collection=bool(coll and coll.collection_action == "created"
                                 and is_minter))
    if decision.action == "no_change":
        targets = []                           # nothing to write (D-13)

    # Yonder is a first-party write target (2.2.0): a capture linked to an
    # in-house Yonder trip is filed there as a TripActivity, independent of
    # AdventureLog. Add it whenever the item has a trip (manual pick or the
    # unambiguous auto-link above), OR the commit stage is about to mint one
    # (mint_yonder_trip) — the trip doesn't exist yet, but it will by the time
    # this target is written — and there is actually something to write.
    if ((item.trip_id or mint_yonder_trip) and not home_only
            and decision.action != "no_change"):
        targets = targets + ["yonder.activity"]

    outcome = "ignored" if home_only else (coll.outcome if coll else "needs_review")
    if decision.review_required:
        outcome = "needs_review"

    # A flight's AirTrail write needs no trip, so an unsettled collection must not
    # block it (owner, 2026-08-10): when the PAYLOAD is clean and only the TRIP is
    # unresolved (deferred / ambiguous / mint-blocked), keep the collection-
    # INDEPENDENT targets (the AirTrail flight) live and let the item be approved;
    # the AdventureLog records that DO need a trip wait for a match or a manual
    # pick. `collection_pending` marks this so the tail sets an approvable-but-
    # flagged review instead of the old reasonless block.
    #
    # Otherwise an item is only WRITABLE when it actually resolved: a review or
    # ignored item with nothing trip-independent to write advertises NO targets,
    # or the commit would attempt a write that cannot succeed — e.g. an
    # AdventureLog transportation with no collection (RULE-08).
    collection_pending = (not home_only and not decision.review_required
                          and outcome in ("deferred", "needs_review"))
    if collection_pending:
        targets = [t for t in targets if t in _COLLECTION_INDEPENDENT_TARGETS]
    elif outcome != "resolved":
        targets = []

    resolution = {
        "ran_at": now,
        "outcome": outcome,
        "collection_action": (coll.collection_action if coll else "none"),
        "collection_id": (coll.collection_id if coll else None),
        "collection_name": (coll.collection_name if coll else None),
        "candidate_collections": list(coll.candidate_collections) if coll else [],
        "match_attempted": bool(coll.match_attempted) if coll else False,
        "anchor_eligible": anchor_eligible and not home_only,
        "within_home_radius": home_only,
        "self_on_booking": self_on_booking,
        "targets": targets,
    }
    if collection_pending:
        # The payload is fine; only the trip is unsettled. Carried so the review
        # UI can say so (and, later, offer a trip picker) and the commit stage can
        # log the AirTrail flight while withholding the trip-bound records.
        resolution["collection_pending"] = True
    if mint_group:
        # Shared journey key: the commit stage joins an attach leg to the trip
        # its minter sibling creates, and it records which leg is the minter.
        resolution["mint_group"] = mint_group
        resolution["is_minter"] = is_minter
    if mint_yonder_trip:
        # Tells the commit stage to mint (minter) or borrow (attach leg, via
        # mint_group) a Yonder trip alongside the AdventureLog collection.
        resolution["mint_yonder_trip"] = True
    if is_minter and coll and coll.collection_action == "created" and envelope:
        # The commit stage mints the collection, and D-34 says a trip spans the
        # whole home-to-home journey, not this one leg — so persist that span
        # now rather than have the committer re-derive it without the session.
        resolution["collection_start"], resolution["collection_end"] = envelope
    if coll and coll.deferred_until:
        resolution["deferred_until"] = coll.deferred_until
    if coll and coll.deferred_reason:
        resolution["deferred_reason"] = coll.deferred_reason
    if decision.action == "supersede" and prior_item is not None:
        resolution["supersedes"] = prior_item.id

    # ---- write back, then gate on rules
    item.canonical_key = key_result.primary
    item.extraction = {**(item.extraction or {}), "payload": payload}
    item.resolution = resolution
    # Preserve locks a human set in the review UI: they are the record of a
    # human decision, and resolution must never quietly drop them (RULE-15
    # depends on them surviving every later pass).
    revision = decision.as_dict()
    existing_locks = set((item.revision or {}).get("field_locks") or [])
    if existing_locks:
        revision["field_locks"] = sorted(existing_locks | set(revision.get("field_locks") or []))
    item.revision = revision
    if decision.review_required:
        item.review = {
            **(item.review or {}), "required": True,
            "reasons": ["stale_document" if decision.stale else "locked_field_conflict"]}
    elif needs_traveler_review and payload.get("item_type") == "flight":
        # Policy is assume-self AND NOTIFY (D-22); RULE-11 checks that the
        # notification half actually happened rather than trusting it.
        item.review = {**(item.review or {}), "required": True,
                       "reasons": ["missing_passenger_list"]}

    violations = rules_module.check_item(_as_rule_shape(item), enablement)
    blocking = rules_module.blocking(violations)
    if blocking:
        # review.reasons is a CLOSED enum in the schema; the readable sentence
        # is derived from the code at render time (app/review/reasons.py) and
        # the precise rule text is re-derived on demand in the detail view.
        item.review = {**(item.review or {}), "required": True,
                       "reasons": rules_module.review_reasons_for(blocking)}
        item.resolution = {**resolution, "outcome": "needs_review", "targets": []}
        item.status = {**(item.status or {}), "resolve": "needs_review"}
        log.warning("item %s -> needs_review: %s", item.id,
                    "; ".join(str(v) for v in blocking)[:300])
        return "needs_review"

    # ---- collection unsettled, payload clean: approvable-but-flagged, never a
    # reasonless block. A flight we can still log to AirTrail comes to you as
    # APPROVABLE (status ok) with a soft reason naming the trip problem — it logs
    # to AirTrail on approve, and the AdventureLog trip record follows once a trip
    # is picked. An item with nothing to write without a trip stays blocked, but
    # now SAYS why: the invisible "needs_review with empty reasons" deadlock (in
    # 'Needs you', no note, Approve silently refused) is gone.
    if collection_pending:
        writable = bool(targets)              # the collection-independent subset
        reasons = [_collection_reason(coll)]
        if needs_traveler_review and payload.get("item_type") == "flight":
            reasons.append("missing_passenger_list")
        item.review = {**(item.review or {}),
                       "required": writable or outcome == "needs_review",
                       "reasons": reasons}
        if writable:
            # It resolves for the purpose of the AirTrail write; the pending trip
            # lives in `collection_pending` + `candidate_collections`.
            resolution = {**resolution, "outcome": "resolved"}
            item.resolution = resolution
        state = "ok" if writable else ("deferred" if outcome == "deferred"
                                       else "needs_review")
        item.status = {**(item.status or {}), "resolve": state}
        for attr in ("extraction", "resolution", "revision", "review", "status"):
            flag_modified(item, attr)
        _sync_keys(session, item, key_result)
        return state

    # Clean resolution: clear any STALE machine review state left over from a
    # previous pass (e.g. a passenger flag that a resolver fix has since made
    # moot), or the item would keep showing "needs you" though it is fine now.
    # A human's own decision (action_taken) and edits are preserved — only the
    # auto-generated `required`/`reasons` are cleared.
    review = dict(item.review or {})
    if review.get("required") or review.get("reasons"):
        review["required"] = False
        review["reasons"] = []
        item.review = review

    state = {"resolved": "ok", "deferred": "deferred",
             "ignored": "skipped", "needs_review": "needs_review"}.get(outcome, "ok")
    item.status = {**(item.status or {}), "resolve": state}
    for attr in ("extraction", "resolution", "revision", "review", "status"):
        flag_modified(item, attr)
    _sync_keys(session, item, key_result)
    return state


def _as_rule_shape(item: TravelItem) -> dict:
    """rules.py reads the canonical TravelItem shape, not the ORM object."""
    return {"id": item.id, "canonical_key": item.canonical_key,
            "item_type": item.item_type, "source": item.source,
            "extraction": item.extraction, "resolution": item.resolution,
            "revision": item.revision, "review": item.review,
            "commit": item.commit, "status": item.status,
            "batch_id": item.batch_id, "segment_index": item.segment_index,
            "segment_count": item.segment_count}


def _sync_keys(session: Session, item: TravelItem, key_result) -> None:
    """item_keys rows for this item. `key` is globally unique, so a key already
    owned by another item is left alone — that is the dedup signal, not a bug."""
    existing = {r.key for r in session.query(ItemKey).filter_by(item_id=item.id).all()}
    taken = {r.key for r in session.query(ItemKey)
             .filter(ItemKey.key.in_(list(key_result.all_keys()))).all()}
    for key in key_result.all_keys():
        if key in existing or key in taken:
            continue
        session.add(ItemKey(item_id=item.id, key=key,
                            kind="primary" if key == key_result.primary else "alternate"))


def resolve_pending(session: Session, adventurelog: AdventureLogClient | None,
                    limit: int = 0, airtrail=None,
                    geocoder: PhotonGeocoder | None = None,
                    redo: bool = False, progress=None) -> dict:
    """Resolve every pending item. Per-item commit so one failure never blocks
    the rest and a crash mid-run loses nothing.

    redo=True re-resolves items that already resolved — for when the resolver
    itself improves. Safe to repeat: resolution is deterministic and
    _find_prior anchors on created_at, so the same input yields the same
    outcome however many passes run. Items a human has already acted on are
    left alone; their decision is not the resolver's to overwrite.
    """
    profiles = load_home_profiles(session)
    enablement = load_enablement(session)
    identities = load_identities(session)
    collections: list[CollectionRef] = []
    if adventurelog is not None:
        try:
            collections = [CollectionRef(id=c.id, name=c.name,
                                         start_date=c.start_date or "",
                                         end_date=c.end_date or "")
                           for c in adventurelog.list_collections()
                           if c.start_date and c.end_date]
        except AdventureLogError as ex:
            log.error("could not list AdventureLog collections: %s", ex)
            raise

    # AirTrail's own airport table gives country/coords/tz for every airport
    # the user has flown — free, exact, and no second dataset to maintain.
    # Non-fatal: without it, airport places stay unlocated and more items
    # land in review rather than being mislocated.
    country_names: dict[str, str] = {}
    if adventurelog is not None:
        try:
            country_names = adventurelog.country_names()
        except Exception as ex:          # falls back to raw ISO codes
            log.warning("could not load AdventureLog country names: %s", ex)

    airports: dict = {}
    if airtrail is not None:
        try:
            airports = airtrail.airport_reference()
            log.info("airport reference: %d airports from AirTrail", len(airports))
        except Exception as ex:
            log.warning("could not load AirTrail airport reference: %s", ex)

    # JSON path comparison differs between JSONB and SQLite's JSON, and this
    # table stays small (homelab scale), so filter in Python rather than carry
    # a dialect-specific query that only one of the two environments exercises.
    # Oldest first: a predecessor must resolve before the duplicates that
    # chain onto it.
    candidates = (session.query(TravelItem)
                  .order_by(TravelItem.created_at).all())
    def _eligible(i):
        state = (i.status or {}).get("resolve")
        if state == "pending":
            return True
        if not redo:
            return False
        # Re-resolve items a person EDITED or APPROVED: their edits live in the
        # payload and are protected by field locks, while targets, collection
        # match and rule outcomes are DERIVED — recomputing them is the entire
        # point of --redo, and skipping them leaves stale conclusions from a
        # resolver version that has since been corrected.
        # `ignored` is different: it is a standing decision to exclude the item,
        # and re-resolving would silently resurrect it.
        return (i.review or {}).get("action_taken") != "ignored"

    pending = [i for i in candidates if _eligible(i)]
    if limit:
        pending = pending[:limit]

    total = len(pending)
    summary: dict[str, int] = {}
    for idx, item in enumerate(pending):
        if progress:
            progress("resolving", idx, total,
                     str(item.canonical_key or item.id)[:70])
        try:
            state = resolve_item(session, item, profiles, collections, enablement,
                                 airports=airports, geocoder=geocoder,
                                 country_names=country_names,
                                 identities=identities)
            session.commit()
        except Exception as ex:                       # never poison the whole run
            session.rollback()
            log.exception("resolve failed for item %s", item.id)
            item.status = {**(item.status or {}), "resolve": "failed"}
            item.review = {**(item.review or {}), "required": True,
                           "reasons": [f"resolver error: {ex}"]}
            session.commit()
            state = "failed"
        summary[state] = summary.get(state, 0) + 1
    if progress:
        progress("resolving", total, total, "")
    return summary
