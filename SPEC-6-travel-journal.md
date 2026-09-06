# SPEC (planning) — Travel Journal

**Status: PLANNING** — a mobile-first pillar for the 4.x line, proposed after the
4.0 companion foundation (API + auth + isolation) lands. Decisions D-131+ are
**proposed** (captured 2026-09-06). Not scheduled/built.

A **quick daily note-capture** surface: the traveller jots what happened, from
the phone in the field or the home page at the desk. Deliberately unstructured
and first-person — the counter-weight to everything else in Yonder, which is
*captured* structured travel data run through extract→resolve→commit. The
journal is **authored, not extracted**, and never touches that pipeline.

It is also the **raw material** the Post-Mortem interview (SPEC-7) reads to be
itinerary-aware and specific. The two features are separable — either ships
alone — but the journal is what makes the post-mortem good.

## What it is (and is not)

- **Is:** a stream of short, dated, personal entries — "ferry was late, ate the
  best fish tacos near the harbour, got sunburnt." Optional mood, tags, a photo.
- **Is not:** a `TripActivity`. Activities are the structured, trip-bound
  timeline fed by capture (a flight, a hotel night) and are subject to the
  rules engine and the AdventureLog commit path. A journal entry has none of
  that machinery and must not inherit it (D-131).
- **Is not:** a review-queue item. Nothing to approve, resolve, or push
  downstream. It is owned by its author and stays in Yonder.

## 1. The entry is a first-class per-user record (D-131)

A new `JournalEntry` table, **not** an overloaded `TripActivity` type. Reasons:
- Activities require a `trip_id` and a registered type contract
  (`app/trips/types.py`); a field note often exists **before** any trip is
  formalised, and carries no type.
- Activities flow through the pipeline / rules / commit graph; entries never do.
- Entries are strictly **personal** (`owner_id`, own-only scope), where trip
  activities are trip-shared.

```
JournalEntry (per-user)
- id, owner_id                      # personal; own-only scope filter (§4)
- entry_date (ISO date)             # the day the entry is ABOUT (D-132)
- trip_id (nullable, soft link)     # attach-to-trip, backfillable (D-133)
- body (Text)                       # the note itself, markdown-lite
- values (JSON)                     # mood, tags[], place label, weather —
                                    #   a blob so adding a field is not a migration
- place_lat / place_lon (nullable)  # captured coords when the phone offers them
- source (String)                   # mobile | web | share
- created_at, updated_at
- captured_at (nullable)            # device clock at capture, ≠ entry_date
```

Media (photos) ride as `Artifact` rows (the existing content-addressed store),
referenced from `values.media[]` — no bytes in the entry row. Media is a v1.1
concern; text-first for v1.

## 2. `entry_date` vs `captured_at` — the day it is ABOUT (D-132)

The organising axis is **the day the entry describes**, not the wall-clock of
when it was typed. You write up "yesterday's hike" over breakfast; that entry
belongs to yesterday. `entry_date` defaults to the device's local date at
capture and is freely editable; `captured_at` keeps the true device timestamp
for provenance and ordering within a day. Grouping, the timeline, and the
post-mortem all key off `entry_date`.

## 3. Standalone first, attach later (D-133)

A field note captured with no active trip is valid and owned by its author.
`trip_id` is nullable and set by an **"attach to trip"** action — the same
soft-link, app-enforced pattern as `travel_items.trip_id` (no DB FK; the trip
delete route clears dangling links). Auto-attach is an assist, never a
requirement: if an entry's `entry_date` falls inside exactly one of the
author's trips' date spans, offer that trip pre-selected. Overlapping trips →
no guess, the author picks.

## 4. Isolation & scope

The journal is a **personal** resource, the cleanest case for the SPEC-4
isolation model:
- Every entry has a non-null `owner_id` (net-new data is owned, D-112). There is
  no null-open legacy tier — the table is born after auth.
- One new **own-only** filter in `app/scope.py`: an entry is visible to its
  author and admins, **never** to trip members. Sharing a trip does **not**
  share its co-travellers' journals — each person's diary is theirs (D-134).
- The post-mortem interview (SPEC-7) reads only the interviewee's own entries,
  preserving this boundary.

## 5. Capture surfaces (three paths, one write)

Mirrors the existing three-path capture posture (web / share / API), but every
path writes the **same** `POST /api/v1/journal` endpoint:
1. **Home page (web).** A single always-present "What happened today?" box on
   the dashboard — date pre-filled to today, one field, submit. The lowest-
   friction desk path.
2. **Mobile (native, Phase 5+).** A prominent "New entry" affordance; date
   defaults to today, optional mood/tag chips, optional photo, optional
   current-location capture. The primary intended surface.
3. **Share (Phase 6).** Share a photo/text from another app → a draft entry.

### Offline is the open decision (D-135, proposed)

SPEC-4 locked **read-only offline** (D-113) specifically to avoid a sync engine.
The journal is the one surface where offline **write** is genuinely wanted — you
are in the field, off-grid, and that is exactly when you want to jot a note.

The saving grace: a journal entry is **append-only, single-author, and needs no
server-side resolution** — the degenerate case an outbox handles safely, with no
conflict model. Two candidate postures:
- **(a) Online-only v1 (honour D-113).** Entry composer requires connectivity;
  simplest, ships with the app's read-only cache unchanged.
- **(b) Journal is the first offline-write surface.** An **append-only outbox**
  (the same mechanism Phase 6 already plans for the share extension) queues
  entries locally and submits on reconnect. No bidirectional sync, no conflict
  resolution — new rows only, server assigns ids. This is the recommended
  target, sequenced as its own phase so the v1 API/web journal is not blocked
  on it.

This is the **one decision that touches a locked 4.0 decision** and should be
made explicitly before the mobile build.

## 6. API & MCP (additive)

- `POST /api/v1/journal` (create), `GET /api/v1/journal?trip_id=&from=&to=`
  (list, own-scoped), `GET/PATCH/DELETE /api/v1/journal/{id}`. Online-only
  writes per D-113 unless D-135(b) is adopted.
- The **Trip Snapshot** bundle (`GET /api/v1/trips/{id}`) gains an optional
  `journal[]` array — **the viewer's own entries for that trip only** — so the
  offline cache carries them and the post-mortem has them at hand. Honours §4:
  never another member's entries.
- MCP: `list_journal` / `add_journal_entry` tools over the same service layer,
  own-scoped — lets the trip-prep assistant read "how the trip is going" and
  lets a voice/agent flow append an entry.
- `/api/v1` stays **additive**; no v2 (iOS forward-compat guard, SPEC-5).

## 7. Impact on the 4.0 build

**Additive; no rework of completed 4.0 work.** By element:
- **DB:** one new `journal_entries` table; migrations continue from the 4.0 head.
  No change to any existing table.
- **API:** new endpoints; the Trip Snapshot gains one optional array (lenient
  decoding tolerates it — the forward-compat guard already assumed new fields).
- **`scope.py`:** one new own-only filter (§4) — the first purely-personal
  resource, simpler than trip-shared visibility.
- **iOS:** a later screen (Journal tab or a Home compose affordance), not a
  change to the core three tabs. Reserve IA space now (the SPEC-5 guard).
- **Pipeline / rules / commit:** untouched. The journal is orthogonal to them by
  design (D-131).

**Only "act now" item:** decide D-135 (offline write) before the mobile build,
and reserve the compose affordance + a Journal area in the iOS IA so no Phase-5
work is wasted.

## Open questions (for the design phase)

- **Media in v1 or v1.1?** Text-first is cheaper and the outbox stays trivial;
  photos add Artifact plumbing and larger offline payloads. Lean v1.1.
- **One entry per day, or many?** Many (a stream), grouped by `entry_date`. A
  "day summary" is a post-mortem synthesis concern, not a storage constraint.
- **Markdown depth.** Reuse the finding `_render_md` subset (bold, bullets,
  headings) or plain text? Lean the existing subset for consistency.
- **Edit window.** Entries are freely editable by their author indefinitely
  (unlike commits, which are immutable) — confirm no audit requirement.
- **Version placement.** A 4.x minor once the API/auth foundation is in; the
  offline-write phase (D-135b) can trail as its own increment.

## Decisions (proposed, D-131–D-135)

- **D-131 — Journal entries are a first-class per-user record, not a
  `TripActivity`.** A new `JournalEntry` table; authored, unstructured, and
  never routed through capture→extract→resolve→commit or the rules engine.
  (proposed, 2026-09-06)
- **D-132 — The organising axis is `entry_date` (the day described), not
  capture time.** `captured_at` is kept for provenance/intra-day ordering; all
  grouping keys off `entry_date`, which defaults to the device date and is
  editable. (proposed)
- **D-133 — Standalone-capable, attach-to-trip later.** `trip_id` is nullable
  and backfilled by an explicit action; auto-attach is offered only on an
  unambiguous single-trip date match, never forced. (proposed)
- **D-134 — A journal is personal, never trip-shared.** One own-only
  `scope.py` filter; sharing a trip does not expose co-travellers' entries. The
  post-mortem reads only the interviewee's own entries. (proposed)
- **D-135 — Offline capture posture (OPEN).** Either honour read-only offline
  (D-113) and make the composer online-only, or make the journal the first
  offline-write surface via an append-only outbox (single-author, no conflict
  model). Recommended: (b) as a trailing phase; decide before the mobile build.
  (proposed)
