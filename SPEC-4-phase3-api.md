# SPEC-4 Phase 3 — JSON `/api/v1` (mobile-facing API)

The first machine-readable surface. Feeds the iOS app's read-only offline cache
and its online writes (share-to-app, review actions). Reuses the existing service
layer and `app/scope.py`, so the API and the HTMX UI never disagree.

## Auth & scoping
- Every `/api/v1/*` route (except `/api/v1/auth/*`, already built) requires a
  bearer access token via `Depends(authn.current_user)` → 401 otherwise. This is
  independent of `TX_AUTH_REQUIRED`: the API is always authenticated (mobile
  always logs in).
- All reads/writes are scoped with `app/scope.py` (`trip_filter`,
  `capture_filter`, `require_trip`, `require_capture`). The API always has a real
  user, so isolation always applies (admins see all).
- Per-user packing (D-123) and the two-tier trip access (D-122) carry over.

## Endpoints (v1 delivery)
**Read (the offline cache):**
- `GET /api/v1/dashboard` — next trip + review counts + capture activity (reuses
  `app/dashboard.py` helpers, user-scoped).
- `GET /api/v1/trips` — visible trip summaries.
- `GET /api/v1/trips/{id}` — the **Trip Snapshot bundle**: trip core, activities
  (+values), travellers, **materialized places (lat/lon via derive_locations →
  airport ref/Photon)**, discovery findings, the user's own packing plan/items,
  linked captures. 404 for a trip the user can't see.
- `GET /api/v1/review?bucket=…` — the per-submitter review queue (`_all_views` +
  `_counts`).
- `GET /api/v1/review/{id}` — one review item.

**Write (online):**
- `POST /api/v1/capture` — share-to-app: a URL (webpage or Google Maps
  directions) or shared page HTML → the existing capture pipeline, owned by the
  caller (D-120).
- `POST /api/v1/review/{id}/assign-trip` — link a capture to one of the user's
  trips (sets `trip_pinned`).

**Deferred to 3b:** full review approve/commit over the API (the commit pipeline
is intricate — kept on the HTMX surface for now); OpenAPI/Swagger UI.

## Reuse map
| Need | Reuse |
|---|---|
| Dashboard | `app/dashboard.py` `_next_trip`/`_pipeline_counts`/`_capture_activity` |
| Review items | `app/review/routes.py` `_view`/`_all_views`/`_counts` |
| Activities | `app/trips/routes.py:trip_activities` |
| Materialized coords | `discover/places.py:derive_locations` + `_airport_resolver`/`_photon` |
| Discovery findings | `DiscoveryFinding` by trip_id |
| Packing (per-user) | `PackingPlan`/`PackingItem` by (trip_id, user_id) |
| Capture | `app/capture.py:_queue_web`/`_queue_maps` (owner_user_id) |
| Scoping | `app/scope.py` (+ new `require_capture`) |

## Verification
pytest (auth required, scoping); live: obtain a token via `/api/v1/auth/login`,
then GET dashboard/trips/snapshot/review and POST a maps-URL capture as `owner`
vs `companion` — each sees only their own, snapshot carries coordinates.
