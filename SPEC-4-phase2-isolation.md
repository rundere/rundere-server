# SPEC-4 Phase 2 — Per-user data isolation & trip sharing

Detailed design for Phase 2 of the Yonder 4.0 line (SPEC-4 Workstream B/Data).
Decisions **D-119–D-121**. Depends on Phase 1 (auth) being complete.

## Goal

Turn Yonder from single-tenant into a **shared household with per-user
isolation**: each person sees their own trips (plus trips shared with them) and
their own captures, while existing data stays open. Expansive but **steadily
migrated** (D-112): mixed null-owner (open) + owned rows must run correctly
forever, with **no backfill**.

**Storage-agnostic.** Entirely Alembic schema + app-layer query scoping — no
filesystem layout or OS permissions. Identical on a Docker named volume or a host
bind mount; the content-addressed Artifacts store stays shared (access gated on
the DB rows that reference a file, never per-file perms).

## Two ownership roots

Isolation has **two anchors**, each with its own `owner_id` and its own entry of
new data:

1. **Trip** — the planning side. A trip is owned by its creator and can be
   **shared** with other users. Trip *substance* (activities, discovery,
   packing, travellers) **inherits** the trip's visibility (no own column).
2. **TravelItem (capture)** — the intake side. A capture is owned by **whoever
   submitted it**, established by the **credential that ingested it** (the
   Apps Script API key or the `/capture/web` proxy identity). The review queue
   is per-submitter.

Everything else (global reference/config tables) is never isolated.

### Trip visibility — Trip is visible to `U` iff any of:
1. `trips.owner_id IS NULL` — legacy/open, shared with everyone
2. `trips.owner_id == U.id`
3. a `trip_members(trip_id, U.id)` row exists — shared with U (**member = full
   access**, D-112; `role` column reserved for future viewer/editor)
4. `U.is_admin`

### Capture visibility — TravelItem is visible to `U` iff any of:
1. `travel_items.owner_id IS NULL` — legacy/open (unscoped key or pre-4.0 data)
2. `travel_items.owner_id == U.id` — U submitted it (via their key / proxy login)
3. `U.is_admin`

(A capture assigned to a shared trip is additionally shown in that trip's
**linked-captures** view via *trip* visibility — but the **review queue** itself
is strictly per-submitter. The two views answer different questions.)

**Auth disabled** (`TX_AUTH_REQUIRED` off) → no `U`; all scoping is a **no-op**,
identical to pre-4.0. Phase 2 is inert on production until auth is enabled there.

## Writes set ownership

- Manual `create_trip` (`app/trips/routes.py:1392`): `owner_id = U.id`.
- Resolver-minted trip (`app/committing.py:1032`, `source="resolver"`):
  `owner_id = NULL` (open) — background pipeline has no user; open is the correct
  default (D-119).
- **Ingest** (`app/ingest.py`): the authenticating `ApiKey` carries a `user_id`;
  the `IngestBatch` records it, and every `TravelItem` created from that batch
  inherits `owner_id = key.user_id`. An **unscoped key** (`user_id NULL`, e.g. a
  legacy/system key) yields open captures.
- **`/capture/web`**: the proxy identity is resolved to a Yonder user (the
  existing `resolve_proxy_user`); that user owns the resulting capture.
- Trip children: no `owner_id`; creating one requires access to the parent trip.

## Schema (migration 0021)

- `trips.owner_id` — `String(36)`, nullable, indexed (→ users.id, soft). NULL = open.
- `travel_items.owner_id` — `String(36)`, nullable, indexed. NULL = open.
- `ingest_batches.user_id` — `String(36)`, nullable (propagates key → items).
- `api_keys.user_id` — `String(36)`, nullable (→ users.id). NULL = unscoped/legacy.
- New `trip_members`: `id` uuid PK, `trip_id` (idx), `user_id` (idx),
  `role` String(16) default `"member"`, `created_at`, unique(`trip_id`,`user_id`).
- **No backfill** — existing trips and captures keep `owner_id NULL` (open).

## Scoping primitives — new `app/scope.py`

One module, reused by the HTMX routes now and the `/api/v1` layer in Phase 3:

- `trip_filter(user)` — SQLAlchemy predicate for `query(Trip)`. `true()` when
  `user is None` (auth off) or `user.is_admin`; else NULL/own/member.
- `visible_trip_ids(session, user) -> set[str] | None` — for scoping child tables
  by `trip_id` (`None` = "all", skips the `IN`).
- `capture_filter(user)` — predicate for `query(TravelItem)`:
  `owner_id IS NULL OR owner_id == uid` (or `true()` for admin/auth-off).
- `can_access_trip(session, user, trip) -> bool`.
- `require_trip(session, user, trip_id) -> Trip` — load + check; **raise 404**
  (not 403) when not visible, so existence never leaks by id-guessing.

## Enforcement map

**Trip lists (apply `trip_filter`):** `app/dashboard.py:78`;
`app/trips/routes.py:125,142`; the review-detail trip picker
`app/review/routes.py:668`.

**Trip detail / action routes (~25 `session.get(Trip, trip_id)` in
`app/trips/routes.py` + discovery/packing sub-views):** replace with
`scope.require_trip(...)`, cleanest as a `Depends(accessible_trip)` reading the
path `trip_id` + `request.state.user`. Child single-row actions
(`session.get(TripActivity, id)` …) assert access to the parent trip.

**Review queue (apply `capture_filter`):** `app/review/routes.py` `_all_views`
(`query(TravelItem)`) and the dashboard's capture counts → per-submitter.

**Ingest stamping:** `app/ingest.py` sets `IngestBatch.user_id` from the key;
the item-creation path (extraction/processing) copies `owner_id` from the batch.

**Current user in routes:** middleware already sets `request.state.user` when
auth is on; add a light `authn.current_user_optional` dependency (returns `None`
when auth off) that routes pass to `scope.*`. No change when auth is off.

## User-scoped ingest keys (the mechanism the owner asked for)

- `ApiKey` gains `user_id`. `app/cli.py create-key <client> --user <username>`
  scopes a key to a user; without `--user` the key is unscoped (legacy/system,
  open captures). Also surfaced on the **Administer → Users** page and the
  ApiKey admin view.
- Each household member runs their **own** Gmail/GCal Apps Script with **their
  own** scoped key → their captures are owned by them and appear only in their
  review queue.
- `/capture/web` (bookmarklet / share) attributes to the logged-in / proxy user.

## Sharing UI

On the **trip detail page** (owner/admin only): "Shared with" — list members,
add a user from the roster, remove; `POST /trips/{id}/share` / `/unshare`.
Ownership reassignment is admin-only. Viewer/editor grades deferred (`role`
reserved).

## Backward compatibility & rollout

- Auth off → every `scope.*` is a no-op; identical to today. Safe to ship to prod
  ahead of enabling auth there.
- Null-owner trips/captures are universally visible (seeded sample trip + all
  existing prod data stay shared until claimed/assigned).
- Admin override everywhere. Migration additive and reversible.

## Test plan

- `trip_filter`/`visible_trip_ids`: owner sees own+null+shared; stranger sees
  only null(open); admin sees all; auth-off sees all.
- `capture_filter`: submitter sees own+null; stranger sees only null; admin all.
- `require_trip` → 404 for a non-visible trip.
- Child scoping: hidden trip's activities/findings/packing invisible; shared
  trip's visible.
- Writes: manual create stamps `owner_id`; resolver mint leaves NULL; ingest with
  a scoped key stamps batch + items; unscoped key leaves NULL.
- Sharing: add/remove member flips visibility; non-owner can't share.
- Regression: auth off leaves all existing trip/review tests unchanged.
- Live (test stack): `owner` scoped key vs `companion` scoped key → each sees
  only their own captures; `owner`'s private trip hidden from `companion` until
  shared.

## Decisions

- **D-124 — Admin sees platform + the traveller roster only; travel is personal.**
  An admin administers the platform and sets up user↔traveller links — nothing
  more. The admin UI (SQLAdmin) surfaces only platform-config tables (rules,
  source rules, packing catalog, bags, phrasebook, regional languages, API keys)
  plus the **de-duplicated traveller roster** (names, for linking — the only
  user-entered item surfaced to an admin). NO personal travel data — trips, trip
  activities, captures/TravelItems, ingest batches, commit attempts, item keys,
  artifacts, packing, discovery — is exposed in any admin UI; it lives in the
  database only (the operator's last-resort access). Generic placeholder guests
  are excluded from the traveller selection. The admin traveller view is trimmed
  to **name + kind** (linking only); a user's own personal traveller attributes
  (nationality, name-on-ticket aliases, AirTrail mapping — since moved to the
  AirTrail connection, D-150/YONDER-322 — notes) move to a
  **user-facing self-editor** at `/trips/settings/travel-profile` ("Travel
  profile") — each user edits their own, never the admin. (owner, 2026-08-28)

## Decisions (D-119–D-121)

- **D-119 — Resolver-minted trips are open (NULL owner).** The commit-stage mint
  has no user context; NULL keeps it visible to all, consistent with
  open-by-default (D-112). (owner, 2026-08-27)
- **D-120 — Captures are per-submitter, owned via the ingesting credential.**
  Rather than a shared review queue, each capture's owner is the user behind the
  API key (or `/capture/web` proxy identity) that submitted it; the review queue
  is per-submitter (+ open + admin). Household members each run their own
  user-scoped Apps Script key. (owner, 2026-08-27)
- **D-123 — Packing is PER-USER within a trip; Discovery stays joint.** A packing
  plan/items are generated for and tied to the *viewing user* (keyed by
  trip_id + user_id) with no cross-visibility — a companion never sees my socks &
  underwear. Only editors (travellers/owner/admin) generate packing, and each
  sees only their own. Existing plans (user_id NULL) stay legacy/shared until
  regenerated (steady migration). Discovery findings remain trip-level shared.
  The **PackingProfile is per-traveller** (its `traveler_id` — the D-82 hook):
  generation loads the viewing user's own profile via their traveller, and the
  Administer → Packing profile page edits *your own* profile (admin-managing
  another traveller's profile is a later enhancement). (owner, 2026-08-28)
- **D-122 — Trip access is two-tier: travellers (who are users) get implicit FULL
  access; explicit shares are VIEW-ONLY.** A trip's TripTraveler roster drives
  access — a traveller tied to a user sees & edits the trip automatically (add
  them as a traveller = share it in). A separate TripMember share grants a
  NON-traveller read-only visibility. Enforced by HTTP method inside
  `scope.trip_or_none` (reads need view, writes need edit), so no per-route
  churn. No public/anonymous visibility — access is always a Yonder user.
  (owner, 2026-08-28)
- **D-121 — Trip sharing v1 = "member = full access"; owner_id on trips +
  travel_items only.** A shared user gets full view+edit; the `role` column
  reserves viewer/editor for later. Ownership columns live on the two roots
  (trips, travel_items); trip substance inherits from its trip. (owner, 2026-08-27)
