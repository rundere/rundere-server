# A6 — Collection Resolution Ruleset

Implementation: `resolve/collections.py` (pure decision logic, no I/O — the
resolver service does the DB reads/writes around it). Tests:
`tests/test_collections.py`. Vocabulary matches the schema's `Resolution`
object exactly.

## The MVP ladder (owner decisions 2026-08-03, see D-24)

The original plan had a 7-rung ladder. The owner cut it to four:

| # | Rung | Result |
|---|---|---|
| 1 | `capture_context.collection_id` present | `user_selected` / `resolved` — skip everything else. The human is never overridden (D-05, RULE-35b/35c) |
| 2 | Exactly one existing collection whose date range **contains** the item's span | `matched_exact` / `resolved` |
| 2b | More than one containing collection | `none` / `needs_review`, `candidate_collections` populated. Never guess (RULE-30) |
| 3 | No match + **flight** + a CLOSED home-to-home journey | `created` / `resolved`, `match_attempted=true` (D-04, RULE-30b), spanning the whole journey, named per D-23 |
| 3b | No match + **flight** whose journey is still open (no return home yet) | `none` / `deferred` — the trip has no known end date, so minting would invent one (**D-34**) |
| 4 | No match + anything else | `none` / `deferred`, `deferred_until = today + 2 days` |

**Removed rungs, and why (owner, verbatim intent):**

- **No hint rung** (was: `collection_hints` table match). Owner: hint rules are
  too fuzzy to maintain reliably — "not disciplined enough to try this and it
  may be disruptive." Flagged as future work (OQ-15); the `collection_hints`
  table drops out of the Phase-0 schema until then.
- **No fuzzy rung** (was: ±N days + shared geography). Owner chose "never
  auto-file near-misses": an item whose dates fall *near* but not *inside* a
  trip goes to deferred/review instead. A wrong auto-filing costs more than a
  review tap. This also deletes the OQ-14 window-size debate entirely.

## Deferred handling (OQ-05)

TTL is **2 days** (`DEFERRED_TTL_DAYS`). Owner: "if the collection doesn't
exist yet, then I messed up badly in my planning" — i.e. in the real workflow
the trip is created (or the flight booked) before hotels and trains are, so an
orphan is almost always a mistake worth surfacing fast. The deferred-item
sweep (the one scheduled job in the architecture, D-02) calls
`escalate_deferred()`; past the fuse the item becomes `needs_review`.

Deferred items are re-run through the ladder on every sweep before the
escalation check — a collection minted yesterday adopts yesterday's orphans.

## Naming on mint (D-23)

`collection_name(country, start, end)` → `"{Country} — {Month Year}"`.

- Month = the month containing the **midpoint** of the span, which is the
  owner's "where the majority of the trip happens" rule in tie-proof form.
  Aug 29 → Sep 15 names as **September**, not August (tested).
- `primary_destination(legs)` picks the country: the turnaround point — the
  arrival with the longest gap before the next departure. The final leg's
  arrival (home) never wins unless it's the only leg (tested).
- `recompute_name()` handles range extension (return flight arrives later) and
  returns nothing when the human has renamed — human edits are locked, always
  (tested).

## What stays out of MVP

- `collection_hints` (OQ-15) — future work, owner-flagged.
- `matched_fuzzy` / `matched_hint` enum values — remain in the schema
  (harmless, no schema change) but are never emitted.
- `DateTimeLocal.origin=collection_default` (OQ-16) — not wired. RULE-34
  already forbids anything but `capture_context`/`document` dates on maps_url
  items, which was its only plausible consumer. Revisit only if a concrete
  need appears.

## Family-tie cities (OQ-01, owner)

A city you have family ties to, or once lived in, is a **normal destination**. Flights logged, trips created, nothing suppressed.
`home_profiles` seeding includes only places you actually live.
