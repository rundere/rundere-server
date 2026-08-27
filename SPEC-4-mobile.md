# SPEC-4 — Yonder 4.0 "Companion" (iOS Mobile App + API + Auth + MCP)

Status: **approved, not yet started** (2026-08-26). This is the **mature-release milestone**: a deliberately large, multi-version 4.x line built **quality over speed**, developed prod-safe and locally, with the repository **made public at launch**. Decisions recorded as **D-110+** (bottom of this file); pointer entry in `DECISIONS.md`.

## Context

Yonder today is a self-hosted, **single-tenant, server-rendered FastAPI + HTMX/Jinja monolith** (Python 3.12, SQLAlchemy 2.0, Postgres 16, Alembic). The private UI is protected only by **LAN-only Traefik routing** — no user login, no session/token system, no JSON API. Only `POST /ingest` is internet-facing (HMAC service-key auth) and `/capture/web` accepts shared web pages. Every first-party model (`Trip`, `TripActivity`, `Review`/`TravelItem`, `DiscoveryFinding`, `PackingPlan`, …) carries **no owner column** — all data belongs to the one deployment.

4.0 delivers the native iOS companion **and the server foundation it requires**: a JSON API, a dual-mode auth system, per-user data isolation with trip-level sharing, and a built-in MCP server over the same APIs. The iOS client is the **smallest** workstream; the server foundation is the bulk, and — importantly — **all server work (Phases 1–4) precedes any Mac/iOS build** (Phase 5+), so most of this release needs no Apple hardware.

Read-only offline is load-bearing: it removes any bidirectional sync engine, offline write queue, or conflict resolution. Offline is a **one-way pull cache**; every mutation is an authenticated online call.

## Locked decisions

| Area | Decision |
|---|---|
| Client technology | Native **Swift / SwiftUI**, iOS-first |
| Authentication | **Dual/pluggable** — in-house `User`+password **and** OIDC client (Authentik/Keycloak/…), both minting **one** Yonder-issued JWT |
| Multi-user | **Full per-user isolation + trip-level sharing**, migrated in steadily (null `owner_id` = open) |
| Offline | **Read-only** cached snapshot; all writes require connectivity |
| Connectivity | Configurable endpoints + **three TLS trust modes**: bare IP, step-ca root, public CA |
| MCP | **In-scope** (Workstream E) — folds in the old 4.0 MCP idea |
| Notifications | **In-app only, post-MVP** — no external/APNs push, ever |
| iOS distribution | **AltStore/sideload first** ($0, free Apple ID, re-sign ~weekly). TestFlight ($99/yr) deferred until the app is worth the recurring investment |
| Dev & deploy | **Parallel Portainer test stack, local image builds, prod untouched**; GitHub stays private; repo goes **public at launch** |

## Workstreams

### A — JSON API (`/api/v1`)
New FastAPI router (`app/api/`) wrapping the existing service modules (`app/committing.py`, `app/resolving.py`, `resolve/`, `discover/`, `planning/`, `adapters/`) and serializing existing models — no core rewrite. Pydantic `response_model` schemas; OpenAPI re-enabled at `/api/v1/docs` behind auth.
- **Read:** `GET /api/v1/dashboard`, `/trips`, `/trips/{id}` (Trip Snapshot bundle), `/review?bucket=…`, `/review/{id}`. Reuse `app/dashboard.py:dashboard()` + review bucket logic (`_bucket_of()`, `_counts()`, `_all_views()`) so API and web counts never diverge.
- **Write (online-only):** review actions (approve/edit/assign-trip/commit) and `POST /api/v1/capture` (shared URL/maps link → existing `capture→ingest→extract(maps_url)→resolve→review`).
- Rule: **API and HTMX UI call the same service layer.** Every endpoint is user-scoped.

### B — Auth (dual-mode, pluggable)
Two interchangeable backends behind one interface, both converging on a **Yonder-issued token** so app/MCP/web share one flow.
- **In-house:** `User` (username, email, argon2/bcrypt `password_hash`, `is_admin`, `auth_source`) → local login → Yonder JWT (access + refresh, rotation). Admin-provisioned; optional self-registration toggle.
- **OIDC:** Yonder is an OIDC client (Auth Code + PKCE). First login provisions a `User` shell mapped to `sub` (`auth_source=oidc`, null password); the callback **mints the same Yonder token**.
- **Token dependency** is the single authorization chokepoint for `/api/v1` and MCP; signing key + OIDC config in `TX_*` env.
- **Users ↔ Travelers:** `User.traveler_id → TravelerIdentity` (existing self/guest roster) — no parallel identity concept.
- **Consequence:** the HTMX **web UI gains login** (per-user isolation makes an anonymous UI undefined). LAN-only routing drops to defense-in-depth.

### C — iOS app (Swift/SwiftUI)
Three tabs mirroring the web's daily surfaces — **Home** (dashboard), **Review** (bucketed log), **Trips** (list → detail: timeline, findings, packing) — plus **Settings**. Local cache (SwiftData/GRDB) of last-pulled dashboard/snapshots/review log; offline renders from cache with writes disabled. Refresh token in **Keychain**; **FaceID/TouchID** unlock. One auth flow to Yonder's token endpoint (local = user/pass; OIDC = `ASWebAuthenticationSession` PKCE). **Share Extension** → `POST /api/v1/capture` via shared Keychain group; offline shares → append-only **outbox**, submit on reconnect.

### D — Settings / basic admin (point 7)
Multiple **server profiles**, each a URL + **trust mode** (bare IP / step-ca root / public CA), health-checked via `/health`. Security toggles: require-FaceID, sign out (clear Keychain + cache), token status, active backend. No SQLAdmin functions in-app for v1.

### E — Built-in MCP server (was old 4.0)
First-party MCP over the **same `/api/v1` service layer**, using **Yonder-token auth**; supersedes the broken `mcp-adventurelog-server` (`/home/user/docker/adventurelog/mcp/`: no Note/Checklist tools, broken Authentik JWKS). Tools v1: list/get trips (+ snapshot), read review queue, approve/commit, trigger/read discovery, read packing, capture a URL — honoring per-user scope. Natural home for the trip-prep skill's Phase-2 ambitions.

## Data: isolation, sharing, snapshot

**Ownership (expansive, steadily migrated):**
1. **Existing rows stay open** — null `owner_id` = shared to all authenticated users; no backfill.
2. **Net-new data is owned** — new rows set `owner_id` explicitly; the system runs mixed (null-open + owned) correctly, indefinitely.
3. **Sharing at Trip level** — `trip_members(trip_id, user_id, role)`; a Trip's substance (activities, findings, packing, linked captures) derives visibility from owner + membership. Captures inherit visibility from their linked trip; unassigned captures are visible to their submitter (+ admins).
4. **Users ↔ Travelers** — `User.traveler_id → TravelerIdentity` keeps model/UI consistent.

Schema: add `User` + `trip_members`; nullable `owner_id` on `Trip` first (child rows inherit from trip), widening incrementally; a `visible_to(user)` scoping helper applied **uniformly in both API and HTMX read paths**. Migrations continue from `0018`.

**Trip Snapshot bundle** (`GET /api/v1/trips/{id}`) — one self-contained, offline-cacheable object across the 8+ soft-linked tables: `Trip` core, `TripActivity[]` (+`values` JSON; `app/trips/types.py` is the field contract), `TripTraveler[]` + resolved names, `DiscoveryFinding[]` (topic/place_key/`body_md`/`data`/confidence/status — the bulk), `PackingPlan` + `PackingItem[]`, optional linked `TravelItem` captures, and **materialized `DerivedPlace` lat/lon + `place_key`**. Coordinates are derived on the fly via Photon today (`discover/places.py`, `resolve/geocode.py`) and **never stored**; Photon is unreachable offline, so the API must resolve and **embed** them at sync time — reuse `/trips/{id}/derived-locations` (`app/trips/routes.py:439-486`). This same payload feeds the later map feature.

## Dev & deploy strategy (prod-safe)

- **Parallel Portainer test stack**, fully isolated from production `yonder-yonder-1`: distinct stack + container names, its **own Postgres volume**, its own Traefik router host / port, built from **local images** (e.g. `yonder:4.0-dev`) — never a `:latest` re-pull of prod. Production stays on 3.9.x untouched throughout 4.x development.
- **Local builds** for the whole 4.x line; no GHCR publish and no public GitHub until launch.
- **Cross-machine / multi-session:** work lives on the long-lived `feat/4.0-companion` branch in the **private** GitHub repo, so any machine can fetch it. (Server work needs no Mac; only Phase 5+ requires the MacBook Air.)

## Path to public launch (the "mature" milestone)

Pre-public checklist (gate before flipping repo visibility): scrub the git history and configs for secrets (`TX_*` tokens, API keys), add/confirm a LICENSE, refresh README to 4.0, ensure no private hostnames/IPs are committed, and confirm the parallel stack proved the release end-to-end. At launch: make `jeeves5454/yonder` public, tag `v4.0.0`, GitHub release, then publish images.

## Non-goals / deferred (v1)
Offline writing / bidirectional sync; external/APNs push (in-app notifications are post-MVP); Android (feasible later via the JSON API); in-app server administration beyond settings; rich account lifecycle (password reset, in-house MFA) — minimal in v1.

## Later versions
- **Map & Discover:** `app/trips/types.py:map_url()` + snapshot coordinates + findings-by-`place_key`; iOS MapKit over the cached snapshot, CoreLocation "near me", pin → findings → map deeplink.
- **In-app notification center** (e.g. "3 items need review") — no APNs.

## Phasing (multi-version 4.x)
0. Sign-off; if OIDC in play, stand up the Authentik application; scaffold the parallel Portainer test stack.
1. **Auth core** — `User` + dual-mode → uniform Yonder token; auth dependency; web-UI login; `User↔TravelerIdentity`.
2. **Isolation** — `owner_id` (null=open) + `trip_members` + `visible_to(user)` across API and HTMX; mixed-data correctness.
3. **API** — `/api/v1` reads + Trip Snapshot (materialized coords), then writes, all user-scoped.
4. **MCP** — built-in server over the API; retire the old adventurelog MCP.
5. **iOS** — app: dual auth flow, three tabs, offline cache, FaceID, Settings (endpoints + 3 TLS modes). AltStore sideload.
6. **iOS** — Share Extension + offline outbox.
7. **Later** — Map & Discover; in-app notification center.

## Reuse map
| Need | Reuse |
|---|---|
| Dashboard payload | `app/dashboard.py:dashboard()` |
| Review buckets/counts | `app/review/routes.py` `_bucket_of()`/`_counts()`/`_all_views()`, `summary.py` |
| Review commit logic | `app/committing.py`, `app/resolving.py` |
| Activity field contract | `app/trips/types.py` (registry + `map_url()`) |
| Trip detail assembly | `app/trips/routes.py:_detail_context()` |
| Derived places + lat/lon | `app/trips/routes.py:439-486`, `discover/places.py`, `resolve/geocode.py` |
| Capture/share ingest | `app/capture.py` `/capture/web`, `app/ingest.py`, `extract/` `maps_url` |
| Machine-token pattern | `app/auth.py`, `ApiKey` model — MCP + share-extension tokens |
| Identity spine | `TravelerIdentity` (`app/models.py`) — `User.traveler_id` target |

## Verification
- **API contract:** `docker run --rm -v "$PWD":/srv -w /srv yonder:latest pytest -q`; assert API and HTMX review-bucket counts match.
- **Auth (both modes):** in-house login issues a valid token; OIDC PKCE callback mints the same token; expired/tampered rejected; `User↔Traveler` correct.
- **Isolation:** mixed-data — null-owner visible to all; owned visible to owner + trip members; sharing a trip grants its substance; unassigned captures scoped to submitter.
- **Offline:** load a trip online → airplane mode → full trip + review log render from cache, writes disabled.
- **Share path:** share a real Google Maps directions URL from Safari → lands in the review queue via the unchanged `maps_url` extractor.
- **Endpoints/TLS:** reach the API over bare-IP, step-ca-trusted, and public-CA hosts, each with correct trust.
- **MCP:** an MCP client lists trips / reads the queue / captures a URL, honoring per-user scope.

---

## Decisions (D-110+)

- **D-110 — Yonder 4.0 is the mobile "Companion" release; MCP folds in.** The mobile app requires a JSON API + real auth; a built-in MCP server over those APIs is a natural extension, so the previously-planned standalone 4.0 (MCP) becomes Workstream E here rather than its own major. (owner, 2026-08-26)
- **D-111 — Dual/pluggable auth, one Yonder token.** Support a simple in-house account system (for homelabs with no SSO) AND an OIDC client, both minting the same Yonder-issued JWT so app/MCP/web never branch on backend. Rejected: OIDC-only (excludes SSO-less homelabs); in-house-only (owner has Authentik). (owner, 2026-08-26)
- **D-112 — Full per-user isolation, steadily migrated; sharing at trip level.** Null `owner_id` = open-share for existing rows (no backfill); net-new rows owned; Trips shared via `trip_members`; child rows inherit trip visibility; `User.traveler_id → TravelerIdentity`. Chosen over shared-data-only because the owner wants real isolation, done carefully. (owner, 2026-08-26)
- **D-113 — Read-only offline.** The mobile cache is a one-way pull snapshot; every write requires connectivity. Deliberately excludes a sync engine / conflict resolution — the review→resolve→commit pipeline makes offline writes genuinely hard and not worth v1. (owner, 2026-08-26)
- **D-114 — Three TLS trust modes + multi-endpoint.** The app supports bare-IP, step-ca-root, and public-CA server profiles, selectable in settings — covers the homelab's private CA and a public endpoint without forcing one model. (owner, 2026-08-26)
- **D-115 — AltStore sideload before TestFlight.** Ship via free-Apple-ID sideload first (accepting ~weekly re-sign) to avoid the $99/yr cost until the app proves worth it. A Mac (owner's MacBook Air) is required to build/sign regardless; iOS work is Phase 5+, after all server workstreams. (owner, 2026-08-26)
- **D-116 — Prod-safe parallel dev; public at launch.** Develop 4.x on a parallel Portainer test stack from local image builds, production 3.9.x untouched; keep GitHub private and defer GHCR publish; make the repo public and tag `v4.0.0` only at the "mature" launch, after a secrets/README/license pre-public checklist. (owner, 2026-08-26)
- **D-117 — Onboarding: first-run bootstrap + admin-invite, accounts tie 1:1 to Travellers.** While the users table is empty, `/setup` creates the first admin (always reachable then; closed once any user exists). After that, accounts are admin-created from Administer → Users unless `TX_AUTH_REGISTRATION_OPEN` enables self-service `/register`. Every account links to a TravelerIdentity — **pick an existing traveller or create a new one inline** (the first admin mints a `self`). Password reset is a one-time link, **emailed when SMTP is configured, else admin-generated** from the Users page; we email a reset LINK, never a password. (owner, 2026-08-27)
