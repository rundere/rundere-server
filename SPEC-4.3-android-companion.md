# SPEC-4.3 — Android Companion: the Run·dere app for Android

Status: **built, in beta** (written 2026-11-07). Android 0.2.0 is in Play
internal testing, checked against server 4.13.4 and 4.13.5. This is the Android
counterpart of [`SPEC-4.2-ios-companion.md`](SPEC-4.2-ios-companion.md): it
records what the app is, how it uses the server, and the decisions behind it,
so later server work respects the Android app the way it respects iOS. Unlike
SPEC-4.2 it describes what has been built, not a plan.

The app lives in its own repository, `rundere/rundere-android` (private). Plane:
project RundereAndroid (`RUNDEREAND-n`) for app work, YONDER (`YONDER-n`) for
server work.

## 1. Status and links

- Kotlin, Jetpack Compose and Material 3. minSdk 29, target 36.
- Application ID **`app.rundere.android`**, which can never change (it mirrors
  iOS's `app.rundere.ios`).
- The app keeps its **own version** (`versionName`), not the server's. Each
  release notes the server version it was checked against.

## 2. Where things stand

Feature parity with iOS 0.13.0:

- Today; Trips (timeline, map, Discover, packing, costs);
- Capture, and the Share target from any app;
- Inbox (the review queue);
- the Notebook (Journal and Places, D-222) and What's near me;
- Settings, and "Try the demo" against `demo.rundere.app` (D-198).

App-only details that need no decision: a generated licence list in Settings,
and launcher shortcuts for New journal entry and Capture.

Gaps that remain: Wear OS (its own spec later), the travel-day Live Update
notification, and the Log expense and Currency calculator shortcuts
(RUNDEREAND-52).

## 3. Architecture

The same principle as SPEC-4.2 §2: a **thin client of `/api/v1`**. Every rule
(scoping, sharing, validation) is enforced on the server.

- **Generated models.** API types are generated from the saved schema
  `api/openapi.json` by the app's `scripts/gen_models.py`.
  [`contracts/android/`](contracts/android/README.md) is the server's side of
  that contract (YONDER-363): CI fails when a server change would break the
  app's decoding or drop one of its writes.
- **Read cache.** Reads come from a Room cache keyed per person
  (`{profile}:{user}:{key}`), wiped on a user or server change.
- **Outbox.** Offline writes go through an outbox (a Room table sent by
  WorkManager when the phone is connected). Unlike iOS, it can send in the
  background.
- **Version gating.** The app reads `/health` and `/api/v1/auth/methods`. The
  minimum server is 4.13.
- **Maps.** Google Maps on the phone. Without Play services it shows a list with
  an Open in Maps (`geo:`) button instead. Phone-side place search uses
  `android.location.Geocoder`; what it finds stays on the phone. Server
  geocoding stays on Photon (D-146), so the server never calls Google.

## 4. Decisions

### 4.1 Existing decisions the app relies on

| Decision | What it means for the app |
|---|---|
| D-194 | The terms for a built-in, first-party app client |
| D-223 | The `rundere-android` OAuth client, its App Link redirect `https://rundere.app/auth/callback`, and `oauth_clients.android` in `/auth/methods` (SPEC-8) |
| D-224 | API instants carry a UTC offset; trip wall-clock times don't |
| D-211, D-220 | Admins have no standing access, and accounts are private by default |
| D-198 | The demo seed and the read-only demo server |

### 4.2 Build decisions (from the app's build plan; no new numbers)

| | Decision | Outcome |
|---|---|---|
| A | Application ID | `app.rundere.android` |
| B | Repository | Private, following rundere-ios's practices (own version, own licence, contract snapshot, Conventional Commits, the owner merges and tags) |
| C | Maps | Google Maps on the phone, with the no-Play-services fallback |
| D | Phone place search | `android.location.Geocoder` |
| E | OAuth | Required in the beta, by App Link only, never a custom scheme |
| F | Distribution | Play closed testing |
| G | Play developer account | Personal |
| H | Test data | Reused from the server's demo seed and the iOS tests, so no new invented flight numbers on real airlines |

### 4.3 Android-only decisions

- **D-225 — The app lock also locks after 5 minutes away (agreed
  2026-11-07).** iOS locks only at launch. Android keeps apps
  alive in the background for days, so a launch-only lock would rarely ask. It
  is the same setting as on iOS.
- **D-226 — Switching servers always means signing in again (agreed 2026-11-07).** The
  old server's session is revoked and its cache wiped. Changing the address or
  TLS trust of the server in use signs out too, so a token never goes somewhere,
  or over trust, it wasn't issued for.
- **D-227 — An add-server link checks the server over public-CA trust only
  (agreed 2026-11-07).** A `rundere://add-server?url=…&name=…` link can't carry a pinned
  certificate, so private-CA servers are added in Settings → Servers. Links with
  sign-in details in the address, or plain http to anything other than this
  device, are refused. Nothing is saved until the person confirms.

## 5. The server API the app uses

All paths are under `/api/v1` except `/health` and `/oauth/*`. The exact shapes
are in [`contracts/android/openapi.json`](contracts/android/openapi.json).

| Area | Endpoints |
|---|---|
| Auth | `GET /auth/methods`; `POST /auth/login`, `/auth/refresh`, `/auth/logout`; `GET /auth/me`; `PUT /auth/me/email`, `/auth/me/sharing`; `/oauth/authorize`, `/oauth/token`, `/oauth/revoke` and the authorization server metadata |
| Home | `GET /dashboard` |
| Trips | `GET /trips`; `GET`/`PATCH /trips/{id}`; `GET /trips/{id}/timeline.pdf`; `GET /activity-types`; `POST`/`PATCH`/`DELETE /trips/{id}/activities[/{aid}]`; `POST /trips/{id}/discovery-items/{item}/timeline` |
| Packing | `POST /trips/{id}/packing/generate`; `POST /trips/{id}/packing-items`; `PATCH /trips/{id}/packing-items/{iid}` |
| Costs | `GET`/`POST /trips/{id}/expenses`; `PATCH`/`DELETE /trips/{id}/expenses/{eid}`; expense comments; `POST`/`PATCH`/`DELETE /trips/{id}/budget[/{bid}]`; `POST /trips/{id}/settlements`; `PUT /trips/{id}/expenses/currency`; `POST /trips/{id}/rates/{cur}/refresh` |
| Discover | `POST /trips/{id}/discover`; `GET …/discover/status`; `GET …/discover/places`; `PUT …/discover/places/{key}` |
| Capture | `POST /capture`; `POST /capture/file` (multipart); `GET /capture/email-address`; `POST /capture/email-address/rotate`; `GET /documents`; `GET /documents/{id}/download`; `DELETE /documents/{id}` |
| Places | `GET`/`POST /places`; `PATCH`/`DELETE /places/{id}`; `POST /places/{id}/file/{trip_id}` |
| Inbox | `GET /review`; `GET`/`PATCH /review/{id}`; `POST /review/{id}/approve`, `/unapprove`, `/ignore`; `POST /review/{id}/assign-trip`; `POST /review/process`; `POST /review/commit`; `GET /jobs/{id}` |
| Journal | `GET`/`POST /journal`; `PATCH`/`DELETE /journal/{id}` |

The API is client-neutral: nothing above is Android-only.

## 6. Server and website asks

| Ask | Status |
|---|---|
| D-223, the Android OAuth client | Done: rundere-server#49, released in 4.13.4 |
| `/auth/methods` names the app client per platform | Done, with D-223 (4.13.4) |
| `assetlinks.json` on rundere.app for the OAuth App Link | Done |
| "Get the app" and the add-server button for Android | Done (YONDER-278). The About page shows Android as "Coming later" until `TX_ANDROID_APP_URL` is set |
| `contracts/android/` and the drift check in CI | YONDER-363 (rundere-server#58) |

## 7. Not in this phase

Wear OS, home-screen widgets, the travel-day Live Update notification, and
tablet-specific layouts beyond what Compose adapts on its own.

## 8. Open decisions for the owner

None. The owner agreed D-225 (with the 5-minute interval), D-226 and D-227 on
2026-11-07.
