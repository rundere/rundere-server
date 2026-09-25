# SPEC-4.2 — iOS Companion: from read-only to a working travel app

Status: **plan, not started** (2026-09-25). Written after 4.1.1 shipped, for
the agent that picks up the iOS work. Read with [`ios/README.md`](ios/README.md),
[`SPEC-4-mobile.md`](SPEC-4-mobile.md) (the original 4.0 Companion spec,
decisions D-110–D-116) and [`SPEC-4-phase3-api.md`](SPEC-4-phase3-api.md).
Plane: label **v4.2**, module **4.0 Companion - iOS App** (and the JSON API /
Auth modules for server items). Plane ids are cited as `YONDER-n`.

## 1. Where things stand

**The app (`ios/`, SwiftUI, iOS 26, XcodeGen)** — 22 Swift files, read-only:
server profiles + 3 TLS trust modes, local username/password login with
Keychain refresh token and single-flight refresh, Face ID lock, SwiftData
one-way offline cache (D-113, no write queue), and screens for Home
(dashboard), Review (list), Trips (list + snapshot detail with Travellers /
Timeline / Discovery / Packing / Captures sections, activity detail, timeline
PDF). `APIClient` only calls: login, refresh, logout, me, dashboard, trips,
trip snapshot, review list, health, capture, timeline PDF. **No other writes.**

**The server** grew a lot in 4.1 (PRs #53–#63). What the app must now respect:

| 4.1 change | What it means for the app |
|---|---|
| Per-activity travellers (`ActivityOut.traveler_ids`, `[]` = everyone; POST/PATCH accept `traveler_ids`) | Show who each item is for; pick travellers when editing (YONDER-90). |
| **Trip context model**: travellers + timeline are SHARED; **Discover and Packing are per person** (snapshot `discovery`/`packing` are the caller's own) | Never show one person's cached snapshot to another (see §3.4). |
| Per-person home / discovery / packing profiles; admin "Profile of" picker | Web-only today; the app links out (§3.6). |
| Captures default to the capturing user's traveller; review has "Who flew" / "Who is this for"; review trip picker = trips you can *file into* (`scope.filing_trip_filter`) | Review write actions must use the same rules (YONDER-57/78). |
| **Admin sharing**: users share with admins by default, can opt out (`share_with_admins`); SSO/self-registered users are asked at first sign-in (`sharing_prompt`, browser only — the API is never redirected) | An SSO user signing up **through the app** would never be asked → the app must ask (§3.3). |
| Account self-service (email, password) and email password reset | Web `/account`; link out. |
| Review "to process" count | Show it on the Review tab (needs API, §2). |

## 2. Architecture (unchanged principles, sharpened)

- **The app is a thin client of `/api/v1`.** All rules (visibility, sharing,
  filing, who-is-this-for defaults) live server-side in `app/scope.py` and the
  routes; the app never re-implements them. If the app needs a rule, the API
  exposes the *result* (e.g. a list of trips you can file into), not the rule.
- **Online-only writes** (D-113). The only exception is the Share Extension
  outbox (Phase 6, YONDER-54): an append-only local queue of *captures*,
  replayed on reconnect. No general sync engine.
- **Additive API only** (`/api/v1` forever, no v2). Server adds fields; the app
  decodes leniently (unknown fields ignored, new optional fields default).
- **Per-person data is keyed per person** on the device too (§3.4).
- **The web stays the admin and deep-settings surface.** The app covers daily
  travel use: trips, timeline edits, packing check-off, review/triage,
  capture, journal, map. Profiles, users, admin tables, email setup stay on
  the web, reached from the app's Settings via an in-app browser
  (`SFSafariViewController`), signed in with the same account.
- **Auth**: one Yonder JWT for everything (D-111). Local login today; OIDC via
  a **server-brokered handoff** (§3.2) so the IdP client secret stays on the
  server and Authentik needs no separate iOS client.

## 3. Design decisions for this phase

### 3.1 API contract as a build input
Enable FastAPI's own OpenAPI for `/api/v1` (currently `docs_url=None`):
serve `/api/v1/openapi.json` **behind auth** (not public, the repo will be
public but servers aren't). The iOS agent diffs `Models/DTOs.swift` against it;
a small script can flag drift. (Rescopes YONDER-58 — its description wrongly
mentions drf-spectacular, a Django library; Yonder is FastAPI.)

### 3.2 OIDC in the app (YONDER-77, needs YONDER-92 live first)
Server-brokered PKCE handoff — the app never talks to Authentik directly:
1. App makes `code_verifier`/`code_challenge`, opens
   `ASWebAuthenticationSession` at
   `{server}/oidc/login?app_redirect=yonder://oidc&app_challenge=<S256>`.
2. Server runs its existing Authorization Code + PKCE flow with Authentik
   (unchanged), provisions/links the user (`authn.provision_oidc_user`).
3. On callback, instead of setting the web cookie, the server redirects to
   `yonder://oidc?code=<one-time>` — a single-use, ~60 s code bound to
   `app_challenge`. `app_redirect` must be on an allow-list
   (`TX_OIDC_APP_REDIRECTS`, default `yonder://oidc`).
4. App calls `POST /api/v1/auth/oidc/exchange {code, code_verifier}` →
   the normal `{access, refresh, user}` token response.
The user payload then carries `sharing_prompt` (§3.3).

### 3.3 Sharing prompt in the app
Extend the user payload (`/api/v1/auth/me`, login/exchange responses) with
`email`, `share_with_admins`, `sharing_prompt`, and `opt_out_blocker` (the
same text the web shows, `accounts_routes.opt_out_blocker`). Add
`PUT /api/v1/auth/me/sharing {share: bool}` (same rules as the web: local
accounts need SMTP + own email to opt out; audited). If `sharing_prompt` is
true, the app shows the same question as `/account/sharing` before the tabs,
with the same honest "this isn't encryption" note.

### 3.4 Per-person cache
Cache keys must include the **server profile and user id**
(`"{profile}:{userId}:trip:{id}"`). Sign-out already clears the cache; keep
that, and also clear when `/me` returns a different user than the cached one.
Discover and Packing in a snapshot are the signed-in person's — never reuse a
snapshot across users.

### 3.5 Review writes (YONDER-57 server, YONDER-78 app)
Server first. The web review pipeline (`app/review/routes.py`) becomes JSON:
- `GET /api/v1/review` adds `counts` and `to_process` (reuse `_counts`,
  `_to_process`).
- `GET /api/v1/review/{id}` adds the editable fields, the "Who flew" options
  (flights) or "Who is this for" options (others, only when filed to a 2+
  traveller trip), and `trip_options` = trips the caller can **file into**
  (`scope.filing_trip_filter`).
- `PATCH /api/v1/review/{id}` — field edits (same field locks), `passengers`,
  `traveler_ids`; re-resolves.
- `POST /api/v1/review/{id}/approve|unapprove|ignore`.
- `POST /api/v1/review/process` → job id; `GET /api/v1/jobs/{id}`.
- `POST /api/v1/review/commit {dry_run}` → job; preview then confirm, like web.
- **Fix first, both surfaces:** `commit_pending` commits *every* approved item
  on the server, whoever captured it. Scope commit to the caller's own
  captures (admins: + sharing users', per `scope.capture_filter`).
- **Fix:** `POST /api/v1/review/{id}/assign-trip` uses
  `require_trip(edit=True)`; it must use the filing rule like the web picker
  (an admin shouldn't file their capture into a sharing user's private trip).

### 3.6 Settings in the app
Native: server profiles, Face ID, sign out, **sharing choice** (§3.3).
Links (in-app browser, same account): Account, Travel / Home / Packing /
Discovery profiles, and for admins Users + admin tables. Native profile
editing is out of scope for 4.2.

## 4. Work breakdown (in order)

Server items can be done on the homelab host (Docker, test stack) or on the Mac
with Docker Desktop; each ships with tests like everything in 4.1.

| # | Work | Plane | Side | Depends on |
|---|---|---|---|---|
| 1 | OpenAPI for `/api/v1` behind auth + DTO drift check | YONDER-58 | server + app | — |
| 2 | User payload + sharing endpoint (§3.3) | YONDER-101 | server | — |
| 3 | DTO sync: `traveler_ids`, user fields, lenient decoding; per-person cache keys (§3.4) | YONDER-102 | app | 1, 2 |
| 4 | Finish the core tabs (define done: pull-to-refresh everywhere, empty/offline states, Settings links §3.6) | YONDER-52 | app | 3 |
| 5 | **Trip editing**: edit trip, add/edit/delete activity (APIs exist, D-125), packing check-off + custom line (APIs exist) | YONDER-103 | app | 3 |
| 6 | Who each activity is for: chips on rows/detail; picker in the activity editor only for 2+ traveller trips | YONDER-90 | app | 5 |
| 7 | Capture + journal compose (APIs exist) | YONDER-79 | app | 3 |
| 8 | Review over the API incl. commit scoping + assign-trip filing fixes (§3.5) | YONDER-57 | server | — |
| 9 | Review write actions in the app | YONDER-78 | app | 8 |
| 10 | Turn on OIDC in prod (Authentik) | YONDER-92 | ops | — |
| 11 | Native OIDC handoff (§3.2) | YONDER-104 | server | 10 |
| 12 | OIDC sign-in + first-sign-in sharing question in the app | YONDER-77 | app | 2, 11 |
| 13 | Share Extension + capture outbox | YONDER-54 | app | 7 |
| 14 | Map (snapshot `places` lat/lon) + in-app review notifications | YONDER-55 | app | 4 |
| 15 | App icon | YONDER-80 | app | — |
| 16 | Distribution: AltStore sideload vs TestFlight | YONDER-56 | owner decision | before sharing builds with others |

A sensible slicing: **4.2** = 1–7 (the app becomes useful day to day: edit
trips, check off packing, see who's on what, capture, journal); **4.3** = 8–12
(review triage + SSO); **4.4** = 13–16.

## 5. Not in this phase
Native profile editors, running Discover from the app (web button for now),
APNs push, a general offline write queue, iPad-specific layouts, the 5.0
inventory pillar (YONDER-62–65; the app keeps packing a self-contained module
so inventory can arrive beside it).

## 6. Open decisions for the owner
1. Distribution (YONDER-56): AltStore (free, 7-day re-sign) vs TestFlight ($99/yr).
2. Confirm "web for profiles/admin, native for daily use" (§3.6).
3. Confirm the OIDC handoff design (§3.2) over registering a separate native
   client in Authentik.
4. Version slicing in §4.
