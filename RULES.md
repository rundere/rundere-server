# Business Rules

GENERATED from the registry in `rules.py` — **do not hand-edit**. Regenerate with
`python3 rules.py --docs > RULES.md` after any rule change, or the two will drift.

Every rule is toggleable per D-25: its mode (`enforce` / `audit` / `off`) lives in
the `rule_settings` DB table (editable via SQLAdmin), seeded from this registry.
New rules default to `enforce`. `audit` runs the rule and records violations
without blocking — use it to trial a disable. Disabling a **critical** rule
(one that encodes a past real-world failure) makes the app log a warning.

JSON Schema cannot express any of these; schema validation itself is
`validation.py` (the real `jsonschema` library) and runs first — rules never
run on a schema-invalid object.

## Temporal integrity

Encode the TripIt→AirTrail import failure. Do not weaken.

| Rule | Critical | Check |
|---|---|---|
| `RULE-01` | **yes** | Arrival/end precedes departure/start — AirTrail aborts the entire batch on this |
| `RULE-02` | **yes** | Identical start and end timestamp — the exact TripIt midnight-stub failure |
| `RULE-03` | **yes** | Flight place has no IATA/ICAO — AirTrail resolves airports by code; lands in unknownAirports |
| `RULE-04` | **yes** | Flight time precision is not full datetime — must go to review, never be defaulted |
| `RULE-05` | **yes** | Lodging check_out precedes check_in |
| `RULE-06` | **yes** | Zero-night stay (check_in == check_out) — verify before writing |
| `RULE-07` | **yes** | IANA timezone with two slashes — AirTrail's regex rejects it; map to backward-compatible alias |

## Collection model

AdventureLog structural constraints and the minting rule (D-04).

| Rule | Critical | Check |
|---|---|---|
| `RULE-08` |  | Collection-exclusive target (transportation/lodging/note) resolved with no collection_id |
| `RULE-08b` |  | Collection-exclusive commit record with no collection record or collection_id |
| `RULE-09` |  | collection_action=created on a non-flight — only flights mint collections (D-04) |
| `RULE-09b` |  | anchor_eligible=true on a non-flight — only flights anchor trips |
| `RULE-30` |  | Minted a collection while candidate collections matched — mint only if none exists |
| `RULE-30b` |  | Minted without match_attempted=true — an erroring ladder must not silently mint duplicates |

## Identity & scope

Who travelled, and what AirTrail is allowed to see (D-22).

| Rule | Critical | Check |
|---|---|---|
| `RULE-10` |  | Guest-only flight (self_on_booking=false) targeted at AirTrail |
| `RULE-11` |  | Traveler inferred from an absent passenger list without review.required (assume self AND notify) |
| `RULE-12` |  | Cancellation resolved without supersedes/duplicate_of — nothing was actually cancelled |

## Update decisions

The create/update/no_change/conflict ladder (D-13).

| Rule | Critical | Check |
|---|---|---|
| `RULE-13` |  | action=update with no prior_item_id — this is a create |
| `RULE-13b` |  | action=update but no prior target record_id exists — downgrade to create or the write duplicates |
| `RULE-14` |  | Stale document (older than stored revision) about to write — must yield conflict |
| `RULE-15` |  | Update would overwrite human-edited (locked) fields — action must be conflict |
| `RULE-15b` |  | action=conflict without review.required |

## Batch integrity

A dropped leg errors nowhere else (D-12).

| Rule | Critical | Check |
|---|---|---|
| `RULE-16` | **yes** | declared_segment_count differs from items carried — a leg was dropped without erroring *(batch)* |
| `RULE-16b` |  | Item artifact sha256 differs from the batch artifact *(batch)* |
| `RULE-16c` |  | Item batch_id does not match the batch *(batch)* |
| `RULE-16d` |  | Item segment_count differs from the batch size *(batch)* |
| `RULE-17` |  | segment_index values are not a contiguous 0..n-1 range *(batch)* |
| `RULE-17b` |  | Multiple booking_group_id values across flight segments from one document *(batch)* |
| `RULE-17c` |  | Duplicate canonical_key within one batch — the same leg extracted twice *(batch)* |
| `RULE-22` |  | batch_status=complete while items are failed/needs_review/deferred — must be partial *(batch)* |

## Commit graph

Per-record AdventureLog status and dependencies (D-14).

| Rule | Critical | Check |
|---|---|---|
| `RULE-18` |  | action=update with empty changed_fields — should be no_change |
| `RULE-18b` |  | action=no_change but content_hash differs from prior — a real change is being dropped |
| `RULE-19` |  | Duplicate AdventureLog target key within an item |
| `RULE-20` |  | depends_on references a key not in the target set |
| `RULE-20b` |  | Record is ok while its dependency is not |
| `RULE-21` |  | Per-type status rollup rosier than its worst record — partial failure would be invisible |

## Home suppression

Home is never a tracked Location (D-07).

| Rule | Critical | Check |
|---|---|---|
| `RULE-23` |  | Home place not suppressed — home is never written to AdventureLog as a Location |
| `RULE-23b` |  | AdventureLog location record exists for a home place — suppress the write, not just the flag |
| `RULE-24` |  | is_home=true with no home_ref |
| `RULE-24b` |  | suppressed with no suppression_reason |

## Road trips

Route data is intent, not actuals (D-22).

| Rule | Critical | Check |
|---|---|---|
| `RULE-26` |  | Waypoints not in chronological order |
| `RULE-26b` |  | Waypoint precedes departure |
| `RULE-26c` |  | Waypoint follows arrival |
| `RULE-27` |  | Road trip anchor_eligible without route.distance_km — home-radius test cannot run |
| `RULE-27b` |  | route.is_estimate=false — directions data is planning-time only; Dawarich owns GPS actuals |
| `RULE-28` |  | within_home_radius=true but outcome is not ignored/needs_review — a local drive is not a trip |
| `RULE-28b` |  | within_home_radius=true but write targets are set |
| `RULE-29` |  | Collection minted by an item marked anchor_eligible=false |
| `RULE-29b` |  | anchor_eligible and within_home_radius are mutually exclusive |

## Capture context

Human-supplied metadata for dateless sources (D-05).

| Rule | Critical | Check |
|---|---|---|
| `RULE-31` |  | directions-URL capture without capture_context.travel_date — a directions URL carries no date |
| `RULE-32` |  | directions-URL capture resolved without a user-selected collection (D-05) |
| `RULE-33` |  | capture_required_fields entry unsupplied and review.required not set |
| `RULE-34` |  | directions-URL date with origin other than capture_context — nothing in the URL supplies it |
| `RULE-34b` |  | Date origin=inferred is never committable (D-06) |
| `RULE-34c` |  | origin=capture_context but capture_context supplied no date |
| `RULE-35` |  | collection_action=user_selected but capture_context supplies no collection_id |
| `RULE-35b` |  | Resolver assigned a different collection than the human selected — never override |
| `RULE-35c` |  | Human selected a collection but the resolver minted a new one |

---

**57 rules across 9 groups** (8 critical). Validated by the cases in `samples.json`; a case flipping from `fail` to `pass` is a regression, not a fix.
