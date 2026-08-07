#!/usr/bin/env python3
"""Business-rule engine (Task 4 port of tests/validate.py check_rules /
check_batch_rules — logic preserved verbatim; see D-25 for the toggle design).

Three layers:

  REGISTRY   — RULES: every rule's id, group, summary, criticality. The single
               source of truth. RULES.md is GENERATED from it: `python3 rules.py
               --docs > RULES.md`. In Phase 0 the registry also seeds the
               `rule_settings` DB table (rule_id, mode, note, updated_at).
  CHECKS     — the ported validate.py logic, emitting (rule_id, message) pairs.
               Never edit a check without its registry entry and a sample case.
  ENGINE     — check_item()/check_batch() take an `enablement` map
               {rule_id: mode} (in production: loaded from rule_settings;
               in tests: passed directly). Modes:
                 enforce — violation blocks (default for every rule)
                 audit   — violation recorded but non-blocking (trial a
                           disable without losing visibility)
                 off     — rule skipped entirely
               Unknown rule ids in the map raise: a typo must not silently
               leave a rule enforced/disabled.

Rules never run on a schema-invalid object (pipeline order) — that gate is
validation.py (real `jsonschema`), not this module.
"""
from __future__ import annotations

import collections
import re
import sys
from dataclasses import dataclass

MODES = ("enforce", "audit", "off")


# ================================================================= registry

@dataclass(frozen=True)
class Rule:
    id: str
    group: str
    summary: str
    critical: bool = False      # encodes a past real-world failure; disabling
                                # one must log loudly in the app
    scope: str = "item"         # item | batch


_G_TEMPORAL = "Temporal integrity"
_G_COLLECTION = "Collection model"
_G_IDENTITY = "Identity & scope"
_G_UPDATE = "Update decisions"
_G_BATCH = "Batch integrity"
_G_COMMIT = "Commit graph"
_G_HOME = "Home suppression"
_G_ROAD = "Road trips"
_G_CAPTURE = "Capture context"

_GROUP_NOTES = {
    _G_TEMPORAL: "Encode the TripIt→AirTrail import failure. Do not weaken.",
    _G_COLLECTION: "AdventureLog structural constraints and the minting rule (D-04).",
    _G_IDENTITY: "Who travelled, and what AirTrail is allowed to see (D-22).",
    _G_UPDATE: "The create/update/no_change/conflict ladder (D-13).",
    _G_BATCH: "A dropped leg errors nowhere else (D-12).",
    _G_COMMIT: "Per-record AdventureLog status and dependencies (D-14).",
    _G_HOME: "Home is never a tracked Location (D-07).",
    _G_ROAD: "Route data is intent, not actuals (D-22).",
    _G_CAPTURE: "Human-supplied metadata for dateless sources (D-05).",
}

_R = [
    Rule("RULE-01", _G_TEMPORAL, "Arrival/end precedes departure/start — AirTrail aborts the entire batch on this", critical=True),
    Rule("RULE-02", _G_TEMPORAL, "Identical start and end timestamp — the exact TripIt midnight-stub failure", critical=True),
    Rule("RULE-03", _G_TEMPORAL, "Flight place has no IATA/ICAO — AirTrail resolves airports by code; lands in unknownAirports", critical=True),
    Rule("RULE-04", _G_TEMPORAL, "Flight time precision is not full datetime — must go to review, never be defaulted", critical=True),
    Rule("RULE-05", _G_TEMPORAL, "Lodging check_out precedes check_in", critical=True),
    Rule("RULE-06", _G_TEMPORAL, "Zero-night stay (check_in == check_out) — verify before writing", critical=True),
    Rule("RULE-07", _G_TEMPORAL, "IANA timezone with two slashes — AirTrail's regex rejects it; map to backward-compatible alias", critical=True),
    Rule("RULE-08", _G_COLLECTION, "Collection-exclusive target (transportation/lodging/note) resolved with no collection_id"),
    Rule("RULE-08b", _G_COLLECTION, "Collection-exclusive commit record with no collection record or collection_id"),
    Rule("RULE-09", _G_COLLECTION, "collection_action=created on a non-flight — only flights mint collections (D-04)"),
    Rule("RULE-09b", _G_COLLECTION, "anchor_eligible=true on a non-flight — only flights anchor trips"),
    Rule("RULE-30", _G_COLLECTION, "Minted a collection while candidate collections matched — mint only if none exists"),
    Rule("RULE-30b", _G_COLLECTION, "Minted without match_attempted=true — an erroring ladder must not silently mint duplicates"),
    Rule("RULE-10", _G_IDENTITY, "Guest-only flight (self_on_booking=false) targeted at AirTrail"),
    Rule("RULE-11", _G_IDENTITY, "Traveler inferred from an absent passenger list without review.required (assume self AND notify)"),
    Rule("RULE-12", _G_IDENTITY, "Cancellation resolved without supersedes/duplicate_of — nothing was actually cancelled"),
    Rule("RULE-13", _G_UPDATE, "action=update with no prior_item_id — this is a create"),
    Rule("RULE-13b", _G_UPDATE, "action=update but no prior target record_id exists — downgrade to create or the write duplicates"),
    Rule("RULE-14", _G_UPDATE, "Stale document (older than stored revision) about to write — must yield conflict"),
    Rule("RULE-15", _G_UPDATE, "Update would overwrite human-edited (locked) fields — action must be conflict"),
    Rule("RULE-15b", _G_UPDATE, "action=conflict without review.required"),
    Rule("RULE-16", _G_BATCH, "declared_segment_count differs from items carried — a leg was dropped without erroring", critical=True, scope="batch"),
    Rule("RULE-16b", _G_BATCH, "Item artifact sha256 differs from the batch artifact", scope="batch"),
    Rule("RULE-16c", _G_BATCH, "Item batch_id does not match the batch", scope="batch"),
    Rule("RULE-16d", _G_BATCH, "Item segment_count differs from the batch size", scope="batch"),
    Rule("RULE-17", _G_BATCH, "segment_index values are not a contiguous 0..n-1 range", scope="batch"),
    Rule("RULE-17b", _G_BATCH, "Multiple booking_group_id values across flight segments from one document", scope="batch"),
    Rule("RULE-17c", _G_BATCH, "Duplicate canonical_key within one batch — the same leg extracted twice", scope="batch"),
    Rule("RULE-22", _G_BATCH, "batch_status=complete while items are failed/needs_review/deferred — must be partial", scope="batch"),
    Rule("RULE-18", _G_COMMIT, "action=update with empty changed_fields — should be no_change"),
    Rule("RULE-18b", _G_COMMIT, "action=no_change but content_hash differs from prior — a real change is being dropped"),
    Rule("RULE-19", _G_COMMIT, "Duplicate AdventureLog target key within an item"),
    Rule("RULE-20", _G_COMMIT, "depends_on references a key not in the target set"),
    Rule("RULE-20b", _G_COMMIT, "Record is ok while its dependency is not"),
    Rule("RULE-21", _G_COMMIT, "Per-type status rollup rosier than its worst record — partial failure would be invisible"),
    Rule("RULE-23", _G_HOME, "Home place not suppressed — home is never written to AdventureLog as a Location"),
    Rule("RULE-23b", _G_HOME, "AdventureLog location record exists for a home place — suppress the write, not just the flag"),
    Rule("RULE-24", _G_HOME, "is_home=true with no home_ref"),
    Rule("RULE-24b", _G_HOME, "suppressed with no suppression_reason"),
    Rule("RULE-26", _G_ROAD, "Waypoints not in chronological order"),
    Rule("RULE-26b", _G_ROAD, "Waypoint precedes departure"),
    Rule("RULE-26c", _G_ROAD, "Waypoint follows arrival"),
    Rule("RULE-27", _G_ROAD, "Road trip anchor_eligible without route.distance_km — home-radius test cannot run"),
    Rule("RULE-27b", _G_ROAD, "route.is_estimate=false — directions data is planning-time only; Dawarich owns GPS actuals"),
    Rule("RULE-28", _G_ROAD, "within_home_radius=true but outcome is not ignored/needs_review — a local drive is not a trip"),
    Rule("RULE-28b", _G_ROAD, "within_home_radius=true but write targets are set"),
    Rule("RULE-29", _G_ROAD, "Collection minted by an item marked anchor_eligible=false"),
    Rule("RULE-29b", _G_ROAD, "anchor_eligible and within_home_radius are mutually exclusive"),
    Rule("RULE-31", _G_CAPTURE, "maps_url capture without capture_context.travel_date — a directions URL carries no date"),
    Rule("RULE-32", _G_CAPTURE, "maps_url resolved without a user-selected collection (D-05)"),
    Rule("RULE-33", _G_CAPTURE, "capture_required_fields entry unsupplied and review.required not set"),
    Rule("RULE-34", _G_CAPTURE, "maps_url date with origin other than capture_context — nothing in the URL supplies it"),
    Rule("RULE-34b", _G_CAPTURE, "Date origin=inferred is never committable (D-06)"),
    Rule("RULE-34c", _G_CAPTURE, "origin=capture_context but capture_context supplied no date"),
    Rule("RULE-35", _G_CAPTURE, "collection_action=user_selected but capture_context supplies no collection_id"),
    Rule("RULE-35b", _G_CAPTURE, "Resolver assigned a different collection than the human selected — never override"),
    Rule("RULE-35c", _G_CAPTURE, "Human selected a collection but the resolver minted a new one"),
]

RULES: dict[str, Rule] = {r.id: r for r in _R}
GROUPS: tuple[str, ...] = tuple(_GROUP_NOTES)


# ---------------------------------------------------------------- review codes
# TravelItem.review.reasons is a CLOSED enum in the canonical schema
# (additionalProperties: false, no free-text field), so a rule violation has to
# be stored as one of these codes. The human sentence shown in the review queue
# is derived from the code at render time — see app/review/reasons.py.
# Anything unmapped falls back to `low_confidence`, which reads as "something is
# off here, look at it" rather than pretending to a specific diagnosis.
REVIEW_REASON_FOR_RULE: dict[str, str] = {
    "RULE-01": "conflicting_version", "RULE-02": "missing_time",
    "RULE-03": "geocode_failed",      "RULE-04": "missing_time",
    "RULE-05": "conflicting_version", "RULE-06": "low_confidence",
    "RULE-07": "missing_timezone",
    "RULE-08": "no_collection_match", "RULE-08b": "no_collection_match",
    "RULE-09": "ambiguous_collection", "RULE-09b": "ambiguous_collection",
    "RULE-30": "ambiguous_collection", "RULE-30b": "ambiguous_collection",
    "RULE-10": "low_confidence",      "RULE-11": "missing_passenger_list",
    "RULE-12": "cancellation_no_match",
    "RULE-13": "conflicting_version", "RULE-13b": "conflicting_version",
    "RULE-14": "stale_document",
    "RULE-15": "locked_field_conflict", "RULE-15b": "locked_field_conflict",
    "RULE-16": "segment_count_mismatch", "RULE-16b": "segment_count_mismatch",
    "RULE-16c": "segment_count_mismatch", "RULE-16d": "segment_count_mismatch",
    "RULE-17": "segment_count_mismatch", "RULE-17b": "segment_count_mismatch",
    "RULE-17c": "segment_count_mismatch", "RULE-22": "partial_commit",
    "RULE-18": "conflicting_version", "RULE-18b": "conflicting_version",
    "RULE-19": "partial_commit",      "RULE-20": "partial_commit",
    "RULE-20b": "partial_commit",     "RULE-21": "partial_commit",
    "RULE-23": "low_confidence",      "RULE-23b": "low_confidence",
    "RULE-24": "low_confidence",      "RULE-24b": "low_confidence",
    "RULE-26": "waypoint_ambiguous",  "RULE-26b": "waypoint_ambiguous",
    "RULE-26c": "waypoint_ambiguous", "RULE-27": "route_unparsed",
    "RULE-27b": "route_unparsed",     "RULE-28": "home_only_movement",
    "RULE-28b": "home_only_movement", "RULE-29": "ambiguous_collection",
    "RULE-29b": "home_only_movement",
    "RULE-31": "missing_travel_date", "RULE-32": "collection_not_selected",
    "RULE-33": "missing_capture_context",
    "RULE-34": "missing_capture_context", "RULE-34b": "missing_capture_context",
    "RULE-34c": "missing_capture_context",
    "RULE-35": "collection_not_selected",
    "RULE-35b": "collection_not_selected", "RULE-35c": "collection_not_selected",
}


def review_reasons_for(violations) -> list[str]:
    """Schema-valid review.reasons codes for a set of violations, deduped and
    ordered by first appearance so the most relevant reads first."""
    out: list[str] = []
    for v in violations:
        code = REVIEW_REASON_FOR_RULE.get(getattr(v, "rule_id", v), "low_confidence")
        if code not in out:
            out.append(code)
    return out


# ================================================================= engine

@dataclass(frozen=True)
class Violation:
    rule_id: str
    message: str
    mode: str = "enforce"       # enforce | audit (off never yields a Violation)

    def __str__(self):          # legacy string shape: "RULE-XX message"
        return f"{self.rule_id} {self.message}"

    @property
    def blocking(self) -> bool:
        return self.mode == "enforce"


def _apply(raw: list[tuple[str, str]],
           enablement: dict[str, str] | None) -> list[Violation]:
    en = dict(enablement or {})
    unknown = set(en) - set(RULES)
    if unknown:
        raise ValueError(f"enablement names unknown rule(s): {sorted(unknown)}")
    bad = {k: v for k, v in en.items() if v not in MODES}
    if bad:
        raise ValueError(f"invalid mode(s): {bad} — expected one of {MODES}")
    out = []
    for rid, msg in raw:
        mode = en.get(rid, "enforce")
        if mode == "off":
            continue
        out.append(Violation(rid, msg, mode))
    return out


def check_item(item: dict, enablement: dict[str, str] | None = None) -> list[Violation]:
    return _apply(_item_violations(item), enablement)


def check_batch(batch: dict, enablement: dict[str, str] | None = None) -> list[Violation]:
    """Batch-level checks only; run check_item() per item as well."""
    return _apply(_batch_violations(batch), enablement)


def blocking(violations: list[Violation]) -> list[Violation]:
    return [v for v in violations if v.blocking]


# ======================================================== checks (the port)
# Ported verbatim from tests/validate.py check_rules()/check_batch_rules().
# The ONLY change: e.append(f"RULE-XX ...") became e(("RULE-XX", f"...")).

TWO_SLASH_TZ = re.compile(r"^[^/]+/[^/]+/[^/]+$")


def _dt_key(d):
    """Sortable key for a DateTimeLocal, ignoring tz. None if not comparable."""
    if not d or d.get("precision") != "datetime":
        return None
    return d["local"]


def _item_violations(item: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    e = out.append
    p = (item.get("extraction") or {}).get("payload") or {}
    t = p.get("item_type")

    def movements(pairs):
        for label, a, b in pairs:
            if not a or not b:
                continue
            ka, kb = _dt_key(a.get("time") or a), _dt_key(b.get("time") or b)
            if ka and kb and kb < ka:
                e(("RULE-01", f"{label}: arrival/end {kb} precedes departure/start {ka} "
                              f"(AirTrail aborts the ENTIRE batch on this)"))
            if ka and kb and ka == kb:
                e(("RULE-02", f"{label}: identical start/end {ka} — the exact TripIt "
                              f"midnight-stub failure mode"))

    if t == "flight":
        f = p.get("flight") or {}
        movements([("flight", f.get("departure"), f.get("arrival"))])
        for side in ("departure", "arrival"):
            m = f.get(side) or {}
            pl = m.get("place") or {}
            if not pl.get("iata") and not pl.get("icao"):
                e(("RULE-03", f"flight.{side}.place: no IATA/ICAO — AirTrail resolves "
                              f"airports by ICAO; this will land in unknownAirports"))
            tm = m.get("time") or {}
            if tm.get("precision") != "datetime":
                e(("RULE-04", f"flight.{side}.time: precision={tm.get('precision')} — "
                              f"must be needs_review, never defaulted"))

    if t == "ground_transport":
        g = p.get("ground_transport") or {}
        movements([("ground", g.get("departure"), g.get("arrival"))])

    if t == "lodging":
        l = p.get("lodging") or {}
        ci, co = l.get("check_in"), l.get("check_out")
        if ci and co:
            a, b = ci.get("local"), co.get("local")
            if a and b and b < a:
                e(("RULE-05", f"lodging: check_out {b} precedes check_in {a}"))
            if a and b and a == b:
                e(("RULE-06", "lodging: zero-night stay — verify"))

    if t == "event":
        ev = p.get("event") or {}
        movements([("event", {"time": ev.get("start")},
                    {"time": ev.get("end")} if ev.get("end") else None)])

    # timezone hygiene, all types
    def walk_tz(node, path=""):
        if isinstance(node, dict):
            tz = node.get("tz")
            if isinstance(tz, str) and TWO_SLASH_TZ.match(tz):
                e(("RULE-07", f"{path}.tz: {tz!r} has two slashes — AirTrail's regex "
                              f"rejects it; map to a backward-compatible alias"))
            for k, v in node.items():
                walk_tz(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk_tz(v, f"{path}[{i}]")
    walk_tz(p, "payload")

    # AdventureLog collection-exclusivity
    res = item.get("resolution") or {}
    targets = res.get("targets") or []
    exclusive = {"adventurelog.transportation", "adventurelog.lodging", "adventurelog.note"}
    # A minting item has no collection_id yet — the collection does not exist
    # until commit creates it — but it IS writable, because
    # adventurelog.collection is in its own target set and the commit graph
    # orders that first (D-14 depends_on). Only a resolved item with neither an
    # existing collection NOR one queued for creation is genuinely unwritable,
    # which is the state this rule exists to catch.
    will_create_collection = ("adventurelog.collection" in targets
                              and res.get("collection_action") == "created")
    # An ATTACH leg of a shared journey (resolver picks one minter, 2026-08-05):
    # its trip is created by the minter sibling and joined at commit via
    # mint_group, so it is writable without a collection_id of its own — the same
    # reasoning as the minter, one seam over.
    attaches_to_sibling = (bool(res.get("mint_group"))
                           and res.get("collection_action") == "created"
                           and res.get("is_minter") is False)
    if exclusive & set(targets) and not res.get("collection_id") \
            and not will_create_collection and not attaches_to_sibling:
        if res.get("outcome") == "resolved":
            e(("RULE-08", "AdventureLog transportation/lodging/note are collection-exclusive; "
                          "outcome=resolved with no collection_id is unwritable"))

    # only flights may mint a collection, and only when none already exists
    if res.get("collection_action") == "created":
        if t != "flight":
            e(("RULE-09", f"collection_action=created on item_type={t}"
                          f"{'/' + str((p.get('ground_transport') or {}).get('mode')) if t == 'ground_transport' else ''}"
                          f"; ONLY flights mint collections — everything else attaches or defers"))
        if res.get("candidate_collections"):
            e(("RULE-30", f"collection_action=created but "
                          f"{len(res['candidate_collections'])} candidate collection(s) matched; "
                          f"mint only if none exists"))
        if res.get("match_attempted") is not True:
            e(("RULE-30b", "collection_action=created without match_attempted=true — "
                           "cannot claim nothing matched without running the ladder"))
    if res.get("anchor_eligible") is True and t != "flight":
        e(("RULE-09b", f"anchor_eligible=true on item_type={t}; only flights anchor trips"))

    # ------------------------------------------------- capture-time metadata
    src = item.get("source") or {}
    cc = src.get("capture_context") or {}
    ext = (item.get("extraction") or {}).get("extractor")
    req = p.get("capture_required_fields") or []

    if ext == "maps_url":
        if not cc.get("travel_date"):
            e(("RULE-31", "maps_url capture with no capture_context.travel_date — a Google "
                          "Maps directions URL carries no date; it is unusable without one"))
        if not cc.get("collection_id") and res.get("outcome") == "resolved":
            e(("RULE-32", "maps_url resolved with no user-selected collection; the drive "
                          "plan attaches to a collection the human made in advance"))

    for f in req:
        if not cc.get(f):
            if not (item.get("review") or {}).get("required"):
                e(("RULE-33", f"capture_required_fields lists {f!r} but capture_context "
                              f"does not supply it and review.required is not set"))

    # dates that came from the human must say so
    def walk_dt(node, path=""):
        if isinstance(node, dict):
            if "local" in node and "precision" in node:
                o = node.get("origin")
                if ext == "maps_url" and o not in ("capture_context", "collection_default"):
                    e(("RULE-34", f"{path}: maps_url date origin={o!r}; must be "
                                  f"capture_context — nothing in the URL supplies it"))
                if o == "inferred":
                    e(("RULE-34b", f"{path}: date origin=inferred is never committable"))
                if o == "capture_context" and not (cc.get("travel_date") or cc.get("end_date")):
                    e(("RULE-34c", f"{path}: origin=capture_context but none was supplied"))
            for k, v in node.items():
                walk_dt(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk_dt(v, f"{path}[{i}]")
    walk_dt(p, "payload")

    # user_selected bypasses the ladder by design — but must actually be selected
    if res.get("collection_action") == "user_selected":
        if not cc.get("collection_id"):
            e(("RULE-35", "collection_action=user_selected but capture_context supplies "
                          "no collection_id"))
        elif res.get("collection_id") != cc.get("collection_id"):
            e(("RULE-35b", f"resolver assigned {res.get('collection_id')} but the human "
                           f"selected {cc.get('collection_id')} — never override an explicit choice"))
    if cc.get("collection_id") and res.get("collection_action") in ("created",):
        e(("RULE-35c", "human selected a collection but the resolver minted a new one"))

    # guest-only exclusion
    if res.get("self_on_booking") is False and "airtrail.flight" in targets:
        e(("RULE-10", "self_on_booking=false but airtrail.flight targeted; "
                      "guest-only flights are not tracked in AirTrail"))

    # inferred passenger must notify
    for i, tr in enumerate(p.get("travelers") or []):
        if tr.get("match_source") == "inferred_absent" and not (item.get("review") or {}).get("required"):
            e(("RULE-11", f"travelers[{i}] role inferred from an absent passenger list "
                          f"but review.required is not set (policy: assume self AND notify)"))

    # cancellation must reference something
    if p.get("booking_status") == "cancelled" and not (res.get("supersedes") or res.get("duplicate_of")):
        if res.get("outcome") == "resolved":
            e(("RULE-12", "cancellation resolved without supersedes/duplicate_of — "
                          "nothing was actually cancelled"))

    # ---------------------------------------------------- update decisions
    rev = item.get("revision") or {}
    act = rev.get("action")
    al = (item.get("commit") or {}).get("adventurelog") or []
    at = (item.get("commit") or {}).get("airtrail") or {}

    if act == "update":
        has_prior_record = bool(at.get("record_id")) or any(r.get("record_id") for r in al)
        if not rev.get("prior_item_id"):
            e(("RULE-13", "action=update with no prior_item_id — this is a create"))
        if item.get("commit") and not has_prior_record:
            e(("RULE-13b", "action=update but no prior target record_id exists to update; "
                           "downgrade to create or the write silently duplicates"))
        if not rev.get("changed_fields"):
            e(("RULE-18", "action=update with empty changed_fields — should be no_change"))

    if act == "no_change" and rev.get("content_hash") != rev.get("prior_content_hash"):
        e(("RULE-18b", "action=no_change but content_hash differs from prior — "
                       "a real change is about to be dropped"))

    if rev.get("stale") and act in ("update", "supersede"):
        e(("RULE-14", f"document_timestamp is older than the stored revision but "
                      f"action={act}; out-of-order delivery must yield conflict, not a write"))

    locks = set(rev.get("field_locks") or [])
    clash = locks & set(rev.get("changed_fields") or [])
    if clash and act != "conflict":
        e(("RULE-15", f"update would overwrite human-edited field(s) {sorted(clash)}; "
                      f"action must be conflict"))

    if act == "conflict" and not (item.get("review") or {}).get("required"):
        e(("RULE-15b", "action=conflict but review.required is not set"))

    # ------------------------------------------ AdventureLog per-record graph
    seen = collections.Counter(r.get("key") for r in al)
    for k, n in seen.items():
        if n > 1:
            e(("RULE-19", f"duplicate AdventureLog target key {k!r} ({n}x) — keys must be "
                          f"unique within an item"))
    by_key = {r.get("key"): r for r in al}
    for r in al:
        if r.get("target") in ("transportation", "lodging", "note") and \
           not any(x.get("target") == "collection" for x in al) and not res.get("collection_id"):
            e(("RULE-08b", f"{r.get('key')}: collection-exclusive target with no collection "
                           f"record or collection_id"))
        for dep in r.get("depends_on") or []:
            if dep not in by_key:
                e(("RULE-20", f"{r.get('key')} depends_on {dep!r} which is not in the target set"))
            elif by_key[dep].get("state") != "ok" and r.get("state") == "ok":
                e(("RULE-20b", f"{r.get('key')} is ok but its dependency {dep} is "
                               f"{by_key[dep].get('state')}"))

    # status rollup must not be rosier than the records
    RANK = {"failed": 0, "needs_review": 1, "deferred": 2, "pending": 3,
            "running": 3, "skipped": 4, "ok": 5}
    rollup = (item.get("status") or {}).get("adventurelog") or {}
    worst = {}
    for r in al:
        _t = r.get("target")
        st = r.get("state")
        if _t and st and RANK.get(st, 9) < RANK.get(worst.get(_t, "ok"), 9):
            worst[_t] = st
    for _t, st in worst.items():
        if _t in rollup and RANK.get(rollup[_t], 9) > RANK.get(st, 9):
            e(("RULE-21", f"status.adventurelog.{_t}={rollup[_t]} is rosier than its worst "
                          f"record ({st}) — partial failure would be invisible"))

    # -------------------------------------------------- home suppression
    def iter_places(node, path=""):
        if isinstance(node, dict):
            if "raw" in node and not {"local", "precision"} & set(node):
                yield path, node
            for k, v in node.items():
                yield from iter_places(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from iter_places(v, f"{path}[{i}]")

    def loc_key(pl):
        return f"location:{pl.get('iata') or pl.get('icao') or (pl.get('name') or pl.get('raw'))}"

    written_locs = {r.get("key") for r in al if r.get("target") == "location"
                    and r.get("state") in ("ok", "pending", "running")}
    for path, pl in iter_places(p):
        home = bool(pl.get("is_home"))
        if home:
            if not pl.get("home_ref"):
                e(("RULE-24", f"{path}: is_home=true with no home_ref"))
            if pl.get("suppressed") is not True:
                e(("RULE-23", f"{path}: home place {pl.get('raw')!r} is not suppressed — "
                              f"home is never written to AdventureLog as a Location"))
            k = loc_key(pl)
            if k in written_locs:
                e(("RULE-23b", f"{path}: AdventureLog location record {k!r} exists for a "
                               f"home place — suppress the write, not just the flag"))
        if pl.get("suppressed") and not pl.get("suppression_reason"):
            e(("RULE-24b", f"{path}: suppressed with no suppression_reason"))

    # -------------------------------------------------------- road trips
    if t == "ground_transport":
        g = p.get("ground_transport") or {}
        gmode = g.get("mode")
        wps = g.get("waypoints") or []
        keys = [_dt_key((w.get("time") or {})) for w in wps]
        seq = [k for k in keys if k]
        if seq != sorted(seq):
            e(("RULE-26", "waypoints are not in chronological order"))
        dep_t = _dt_key(((g.get("departure") or {}).get("time") or {}))
        arr_t = _dt_key(((g.get("arrival") or {}).get("time") or {}))
        for i, k in enumerate(seq):
            if dep_t and k < dep_t:
                e(("RULE-26b", f"waypoint[{i}] {k} precedes departure {dep_t}"))
            if arr_t and k > arr_t:
                e(("RULE-26c", f"waypoint[{i}] {k} follows arrival {arr_t}"))
        rt = g.get("route") or {}
        if gmode in ("car", "car_rental", "rideshare"):
            if rt.get("distance_km") is None and res.get("anchor_eligible"):
                e(("RULE-27", "road trip is anchor_eligible but has no route.distance_km — "
                              "home-radius test cannot be evaluated"))
            if rt.get("is_estimate") is False:
                e(("RULE-27b", "route.is_estimate=false; directions data is planning-time "
                               "only. Dawarich owns actual GPS traces"))

    # entirely-within-home movement is not a trip
    if res.get("within_home_radius") is True:
        if res.get("outcome") not in ("ignored", "needs_review"):
            e(("RULE-28", f"within_home_radius=true but outcome={res.get('outcome')}; "
                          f"movement that never left home is not a trip"))
        if res.get("targets"):
            e(("RULE-28b", "within_home_radius=true but write targets are set"))

    if res.get("anchor_eligible") is False and res.get("collection_action") == "created":
        e(("RULE-29", "collection minted by an item marked anchor_eligible=false"))
    if res.get("anchor_eligible") is True and res.get("within_home_radius") is True:
        e(("RULE-29b", "anchor_eligible and within_home_radius are mutually exclusive"))

    return out


def _batch_violations(batch: dict) -> list[tuple[str, str]]:
    """Batch-level integrity. A silently dropped leg errors nowhere else."""
    out: list[tuple[str, str]] = []
    e = out.append
    items = batch.get("items") or []
    declared = batch.get("declared_segment_count")
    if declared is not None and declared != len(items):
        e(("RULE-16", f"declared_segment_count={declared} but batch carries {len(items)} "
                      f"items — a segment was dropped without erroring"))
    sha = (batch.get("artifact") or {}).get("sha256")
    for i, it in enumerate(items):
        if (it.get("artifact") or {}).get("sha256") != sha:
            e(("RULE-16b", f"items[{i}] artifact sha256 differs from the batch artifact"))
        if it.get("batch_id") != batch.get("batch_id"):
            e(("RULE-16c", f"items[{i}].batch_id does not match the batch"))
        if it.get("segment_count") not in (None, len(items)):
            e(("RULE-16d", f"items[{i}].segment_count={it.get('segment_count')} != {len(items)}"))
    idx = [it.get("segment_index") for it in items if it.get("segment_index") is not None]
    if idx and sorted(idx) != list(range(len(items))):
        e(("RULE-17", f"segment_index values {sorted(idx)} are not a contiguous 0..n-1 range"))
    groups = {it.get("booking_group_id") for it in items if it.get("booking_group_id")}
    types = {it.get("item_type") for it in items}
    if len(groups) > 1 and types == {"flight"}:
        e(("RULE-17b", f"multiple booking_group_id values {groups} across flight segments "
                       f"from one document — legs of one PNR must share a group"))
    keys = [it.get("canonical_key") for it in items if it.get("canonical_key")]
    dupes = [k for k, n in collections.Counter(keys).items() if n > 1]
    if dupes:
        e(("RULE-17c", f"duplicate canonical_key within one batch: {dupes} — "
                       f"the same leg was extracted twice"))
    if batch.get("batch_status") == "complete":
        bad = [it.get("id") for it in items
               if any(v in ("failed", "needs_review", "deferred")
                      for k, v in (it.get("status") or {}).items() if isinstance(v, str))]
        if bad:
            e(("RULE-22", f"batch_status=complete but {len(bad)} item(s) are not done — "
                          f"must be partial"))
    return out


# ================================================================= docs

def generate_docs() -> str:
    lines = [
        "# Business Rules",
        "",
        "GENERATED from the registry in `rules.py` — **do not hand-edit**. Regenerate with",
        "`python3 rules.py --docs > RULES.md` after any rule change, or the two will drift.",
        "",
        "Every rule is toggleable per D-25: its mode (`enforce` / `audit` / `off`) lives in",
        "the `rule_settings` DB table (editable via SQLAdmin), seeded from this registry.",
        "New rules default to `enforce`. `audit` runs the rule and records violations",
        "without blocking — use it to trial a disable. Disabling a **critical** rule",
        "(one that encodes a past real-world failure) makes the app log a warning.",
        "",
        "JSON Schema cannot express any of these; schema validation itself is",
        "`validation.py` (the real `jsonschema` library) and runs first — rules never",
        "run on a schema-invalid object.",
    ]
    by_group: dict[str, list[Rule]] = {}
    for r in _R:
        by_group.setdefault(r.group, []).append(r)
    for g in GROUPS:
        lines += ["", f"## {g}", "", _GROUP_NOTES[g], "",
                  "| Rule | Critical | Check |", "|---|---|---|"]
        for r in by_group[g]:
            crit = "**yes**" if r.critical else ""
            scope = " *(batch)*" if r.scope == "batch" else ""
            lines.append(f"| `{r.id}` | {crit} | {r.summary}{scope} |")
    n_crit = sum(1 for r in _R if r.critical)
    lines += ["", "---", "",
              f"**{len(_R)} rules across {len(GROUPS)} groups** ({n_crit} critical). "
              f"Validated by the cases in `samples.json`; a case flipping from `fail` "
              f"to `pass` is a regression, not a fix."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if "--docs" in sys.argv:
        sys.stdout.write(generate_docs())
    else:
        sys.stdout.write(f"{len(RULES)} rules registered; use --docs to emit RULES.md\n")
