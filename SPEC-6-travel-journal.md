# SPEC (planning) — Travel Journal

**Status: PARTIALLY IMPLEMENTED (2026-09-07).** A mobile-first pillar for the
4.x line. Decisions D-131+ are **proposed**; the first increment is built and
merged to `feat/4.0-companion`.

> **Built so far:** `JournalEntry` model + migration `0024`; own-only scope
> (D-134); the `app/journal.py` service; the `/api/v1` journal CRUD + duplicate;
> and the **web** compose/list surface (`/journal`). Online-only, tested.
>
> **Deferred (later cycles):** the mobile app + its offline outbox (D-135, needs
> the iOS app), MCP `list_journal`/`add_journal_entry` tools, and the optional
> Trip Snapshot `journal[]` array. These are spec-driven backlog, not scope cuts.

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
- captured_at (datetime, REQUIRED)  # date+time context — the minimum stamp (D-142)
- trip_id (nullable, soft link)     # attach-to-trip, backfillable (D-133)
- body (Text)                       # the note itself, markdown-lite
- values (JSON)                     # mood, tags[], place label, weather —
                                    #   a blob so adding a field is not a migration
- place_lat / place_lon (nullable)  # geo fix when the device offers one (D-142)
- place_label (nullable)            # reverse-geocoded name, best-effort/deferred
- source (String)                   # mobile | web | share
- duplicated_from (nullable)        # the entry this was forked from (D-133)
- created_at, updated_at
```

~~Media (photos) ride as `Artifact` rows (the existing content-addressed store),
referenced from `values.media[]` — no bytes in the entry row.~~ **Superseded by
SPEC-8 D-154 (2026-09-28):** Rundere stores no photo bytes. An entry carries
photo *references* (time, place and checksum, resolved against the user's own
photo library), which are verified at trip close-out. See YONDER-189. Media
remains a v1.1 concern; text-first for v1.

## 2. `entry_date` vs `captured_at` — the day it is ABOUT (D-132)

The organising axis is **the day the entry describes**, not the wall-clock of
when it was typed. You write up "yesterday's hike" over breakfast; that entry
belongs to yesterday. `entry_date` defaults to the device's local date at
capture and is freely editable; `captured_at` keeps the true device timestamp
for provenance and ordering within a day. Grouping, the timeline, and the
post-mortem all key off `entry_date`.

## 2.5 Capture metadata — time always, geo when it can (D-142)

Every entry carries, at minimum, **date + time context**: `entry_date` (the day
it is about) and `captured_at` (the device timestamp at capture) are **always**
recorded — the floor for any entry, on any surface.

**Geo is the differentiator, best-effort.** When the device offers a location
fix and the user has permitted it, the entry stores `place_lat`/`place_lon` (and
later a reverse-geocoded `place_label`), plus the fix's accuracy in metres
(`place_accuracy_m`) — the same four fields an actual expense records
(YONDER-212), so the app captures both with one routine and one permission. Geo is what makes the journal an
**in-the-moment, stream-of-consciousness** surface — "where I was when I thought
this" — and ties naturally into the map feature and the post-mortem's day
reconstruction. By surface:

- **Mobile:** geo is captured **automatically and silently** on create (one
  permission grant), no extra step — the easiest and richest path, and the
  reason the field journal is mobile-first.
- **Browser:** geo is **opportunistic** — the Geolocation API prompt if the user
  allows it, otherwise skipped. Never required; a web entry with no coordinates
  is complete.

Geo is always **optional at the data layer** — an entry never fails to save for
lack of a fix (essential offline, where a fix may be stale or absent).

## 3. Standalone first, attach later — assumed, never automatic (D-133)

Capture is **additive and reviewed later**: push ten quick fragments or one long
entry in the moment; they land unattached and are **associated to the right trip
later**, on review. `trip_id` is nullable and set by an **"attach to trip"**
action — the same soft-link, app-enforced pattern as `travel_items.trip_id`
(no DB FK; the trip delete route clears dangling links).

- **Assumed match, never auto-applied.** On review Yonder always **pre-selects a
  best-guess trip** (the author's trip whose date span best contains
  `entry_date`), but the author must **confirm** — nothing is silently attached.
  Even when several trips are candidates (including **overlapping** trips), one
  is offered as the assumption and the author can change it. A capture with no
  plausible trip stays unattached, still valid.
- **One entry ↔ one trip.** An entry belongs to **exactly one** trip at a time,
  regardless of overlapping trip spans. There is no many-trips-per-entry link.
- **Duplicate to span trips.** To use the same note against more than one trip,
  the author **duplicates** the entry and attaches each copy to a different trip.
  The copy is an independent entry (`duplicated_from` records its origin for
  provenance); editing or deleting one never touches the other.

Full CRUD: the author can **create, edit, delete, and duplicate** entries
freely, and re-attach (or detach) at any time — entries are mutable, unlike the
immutable commit records elsewhere in Yonder.

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
2. **Mobile (native, Phase 5+).** The **primary** surface. A prominent "New
   entry" affordance; `captured_at` stamped from the device clock, `entry_date`
   defaulted to today, a **geo fix captured automatically** when location is
   permitted (§2.5), optional mood/tag chips, optional photo. **Writes offline**
   (§5.1) — the in-the-moment case.
3. **Share (Phase 6).** Share a photo/text from another app → a draft entry.

### 5.1 Offline write is mobile-only (D-135)

SPEC-4 locked **read-only offline** (D-113) to avoid a sync engine. The journal
is the one surface where offline **write** is genuinely wanted — in the field,
off-grid, is exactly when you jot a note — and a journal entry is **append-only,
single-author, and needs no server-side resolution**, the degenerate case an
outbox handles with no conflict model. So the decision (resolving the earlier
open D-135):

- **Mobile: offline-capable.** The native app writes to a local **append-only
  outbox** and submits on reconnect. New rows only; the server assigns ids;
  nothing to merge. This is a **narrow, contained exception** to D-113 — the
  read-only-cache posture for everything else (trips, review, findings) is
  unchanged; only journal *creation* queues offline. Sequenced as its own phase
  so the v1 API/web journal is not blocked on it.
- **Browser: online-only.** Reliable offline capture (and background geo) is
  hard to do well in a browser and not worth an offline story there; the web
  composer requires connectivity. The desk path is the online, considered one;
  the field path is mobile.

This is the **one exception to a locked 4.0 decision** (D-113) — deliberately
scoped to mobile journal *creation* only, and the safest possible one (no
conflict model). Confirm the outbox phase in the mobile plan; it does not block
the v1 API/web journal.

## 6. API & MCP (additive)

- `POST /api/v1/journal` (create), `GET /api/v1/journal?trip_id=&from=&to=`
  (list, own-scoped), `GET/PATCH/DELETE /api/v1/journal/{id}`, and
  `POST /api/v1/journal/{id}/duplicate` (fork for another trip, D-133). The
  endpoint itself is stateless about connectivity; **offline queuing lives in
  the mobile client** (§5.1), not the API — the server just accepts the create
  when the outbox drains. `entry_date` + `captured_at` are required on create;
  geo is optional (D-142).
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

**"Act now" items:** the offline posture is now decided (D-135, mobile-only
outbox) — plan the outbox as its own mobile phase; reserve the compose affordance
+ a Journal area in the iOS IA; and request the **location permission** in the
iOS app so automatic geo (D-142) is available from first launch.

## Open questions (for the design phase)

- **Media in v1 or v1.1?** Text-first is cheaper and the mobile outbox stays
  trivial. Lean v1.1. Photos are references, not files (SPEC-8 D-154), so there's
  no Artifact plumbing and offline payloads stay small.
- **One entry per day, or many?** Many (a stream), grouped by `entry_date` — the
  "push ten quick things" case. A "day summary" is a post-mortem synthesis
  concern, not a storage constraint.
- **Markdown depth.** Reuse the finding `_render_md` subset (bold, bullets,
  headings) or plain text? Lean the existing subset for consistency.
- **Reverse geocoding.** Store raw `place_lat`/`place_lon` at capture and derive
  `place_label` lazily server-side (reusing `resolve/geocode.py`/Photon), so an
  offline entry needs no network for a label. Confirm the derive-later split.
- **Version placement.** A 4.x minor once the API/auth foundation is in; the
  mobile offline-write phase (D-135) trails as its own increment.

## Decisions (proposed, D-131–D-135, D-142)

- **D-131 — Journal entries are a first-class per-user record, not a
  `TripActivity`.** A new `JournalEntry` table; authored, unstructured, and
  never routed through capture→extract→resolve→commit or the rules engine.
  (proposed, 2026-09-06)
- **D-132 — The organising axis is `entry_date` (the day described), not
  capture time.** `captured_at` is kept for provenance/intra-day ordering; all
  grouping keys off `entry_date`, which defaults to the device date and is
  editable. (proposed)
- **D-133 — Standalone-capable; assumed-not-automatic trip attach; one entry ↔
  one trip; duplicate to span trips.** `trip_id` is nullable and backfilled by an
  explicit action. On review Yonder always pre-selects a best-guess trip but
  never auto-attaches — even with overlapping candidate trips the author
  confirms. An entry belongs to exactly one trip at a time; to apply the same
  note to another trip the author **duplicates** it (`duplicated_from` for
  provenance) and attaches the copy. Full CRUD (create/edit/delete/duplicate/
  re-attach). (proposed)
- **D-134 — A journal is personal, never trip-shared.** One own-only
  `scope.py` filter; sharing a trip does not expose co-travellers' entries. The
  post-mortem reads only the interviewee's own entries. (proposed)
- **D-135 — Offline write is mobile-only, via an append-only outbox.** The native
  app queues journal *creations* locally and submits on reconnect (new rows only,
  server-assigned ids, no conflict model) — a narrow, contained exception to
  read-only offline (D-113) that leaves the rest of the cache read-only. The web
  composer is online-only. Sequenced as its own mobile phase; does not block the
  v1 API/web journal. (proposed — resolves the earlier open posture)
- **D-142 — Time context is mandatory, geo is best-effort.** Every entry records
  `entry_date` + `captured_at` at minimum, on any surface. A geo fix
  (`place_lat`/`place_lon`, later `place_label`) is captured automatically on
  mobile when permitted and opportunistically in the browser, never required, and
  never blocks a save — the "in-the-moment, where-I-was" signal that also feeds
  the map and the post-mortem. (proposed)
