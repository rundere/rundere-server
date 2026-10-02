# SPEC (planning) — Rundere First-Party: optional integrations, per-user connections, storage posture

**Status: PLANNING (2026-09-28).** Decisions D-146–D-155 are **proposed**. Nothing
here is built yet. Tracked in Plane: YONDER-190 (first-party core + airport data),
YONDER-191 (urgent: cross-user integration writes), YONDER-192 (per-user
connections + secrets), YONDER-189 (journal photo references), YONDER-172
(expenses), YONDER-179 (document capture). **Gate for making the repo public**
(YONDER-59): D-149, D-151 and D-152 must ship first.

Rundere started as an extract→resolve→commit pipeline feeding AirTrail and
AdventureLog. It now has its own trips, timeline, journal, packing and users, and
people without AirTrail or AdventureLog accounts capture into it. This spec makes
Rundere the **primary system**. AirTrail and AdventureLog become **optional
extras** that each user connects for themselves, and it states what Rundere will
and will not store.

## Principle: the core works on Rundere's own data (D-146)

- **Core:** trips, captures (the raw capture lives in `ingest_batches.payload`),
  the timeline, the journal text, the traveller roster, packing, hand-entered
  expenses, and photo *references*. All of it works with no external service
  configured.
- **Extras:** AirTrail and AdventureLog sync, Immich/Apple photo resolution,
  currency rates (Frankfurter), geocoding (Photon, with a commercial fallback
  possible), LLM discovery, map tiles and routing. Each one is optional. When one
  is missing or down, pages still render and the feature is simply absent.
- **Every external lookup leaves Rundere-owned data behind.** Cache the result
  (as currency snapshots already are in `DiscoveryFinding.data`) and don't
  re-fetch it on every page view.

## 1. What breaks today without AirTrail / AdventureLog (code read, 2026-09-28)

**AirTrail is Rundere's only airport data.**
- `AirTrailClient.airport_reference()` (`adapters/targets/airtrail.py:95`)
  harvests only airports the user has already flown.
- `airtrail_db_url` reads AirTrail's Postgres (read-only, D-50) for the full
  table.
- Both feed `_airport_resolver()` (`app/trips/routes.py:434`) and
  `resolve_pending()` (`app/resolving.py:880`).
- Without them:
  - airport places get no time zone (`resolve/targets.py:65`) unless the source
    document carries one, so flight times lose their zones;
  - the home-radius check (RULE-28b) lacks country and coordinates, so more items
    go to review;
  - map and Discover locations fall back to a fuzzy Photon search for
    "`<code>` airport".

**AdventureLog shapes trip matching and the write set.**
- `resolve_pending` matches captures against AdventureLog collections. Without
  AdventureLog the list is empty.
- `compute_targets` (`resolve/targets.py:13`) builds the write set in AdventureLog
  and AirTrail terms. `yonder.activity` is only added when a Rundere trip is
  already set (`app/resolving.py:667`). A lodging or event with no Rundere trip
  ends up with no targets and waits in review.
- `country_names()` comes from AdventureLog (it falls back to ISO codes).

**Credentials are server-wide.**
- There is one `TX_ADVENTURELOG_API_KEY` and one `TX_AIRTRAIL_TOKEN`
  (`app/config.py:38-51`), both the owner's.
- They're used for every user's captures (`app/review/routes.py:698`, `:861`;
  `app/review/home.py:47`; `app/cli.py:324-365`).
- "Who is me" is also server-wide: exactly one `TravelerIdentity.kind="self"`, and
  `self_user_id()` (`app/committing.py:158`) returns it.

## 2. Rundere's own airport data (D-147)

- Ship an airport reference table (IATA, ICAO, name, municipality, country,
  lat/lon, IANA time zone), built from a public dataset (OurAirports, or
  mwgg/Airports, which includes time zones). Seed it with a migration or data
  file, and refresh it each release.
- Where a row has no time zone, derive it offline from lat/lon with
  `timezonefinder`, which is already a dependency.
- Airport lookups use this table first. The AirTrail harvest can fill gaps only
  when the user has an AirTrail connection. `airtrail_db_url` is **retired**.

## 3. Rundere-first write set (D-148)

- `yonder.activity` is the **primary** target whenever a Rundere trip exists or
  will be created.
- `airtrail.*` and `adventurelog.*` targets are added **only** when the
  *capture owner* has an enabled connection of that kind (§5).
- Trip matching runs against **Rundere trips**. AdventureLog collection matching
  runs only for users with an AdventureLog connection, and only against that
  user's own collections.
- Country display names come from CLDR/Babel, not AdventureLog.
- **Test:** with nothing configured, a flight capture resolves its airports with
  time zones, attaches to a Rundere trip, is approved and appears on the timeline.

## 4. External writes are owner-scoped (D-149) — urgent stopgap

Until §5 ships, `airtrail.*` and `adventurelog.*` targets are only added for
captures owned by the user the server-wide credentials belong to (the user
linked to the `self` identity). Everyone else gets `yonder.activity` only.

- **Test:** with both integrations configured, a non-owner's capture makes no
  AirTrail or AdventureLog calls.
- **Audit:** list existing external records written from non-owner captures
  (YONDER-191).

## 5. Per-user connections (D-150)

- **No admin tokens.** Once §2 ships there's no reason to have one. Each
  connection belongs to **one user**: their own base URL and their own token,
  limited to what that remote user can do. Two users can connect to the same
  instance with different tokens, or to different instances anywhere on the
  internet.
- **Table `user_integrations`:**
  - `id`, `user_id`, `kind` (`airtrail` or `adventurelog`);
  - `base_url`, `public_url`;
  - `credential_ct` (encrypted, §6), `credential_hint` (last 4 characters);
  - `remote_user_id`, `enabled`;
  - `last_check_at`, `last_check_ok`, `last_error`;
  - timestamps.
- **UI:** Profile → Advanced → Connections, with add, test, disable and delete.
  The health check runs with the user's own token. An admin can see which users
  have which connections and their status, **never** the credentials.
- **"Self" is per user.** RULE-10 (a guest-only flight never goes to AirTrail)
  uses the capturing user's own `User.traveler_id` identity, not the single
  `kind="self"` row. `airtrail_user_id` is read from that user's AirTrail
  connection.
- **Migration:** seed the owner's connections from the existing `TX_*` env vars
  on first boot, and log a deprecation warning plus a notice on the admin page.
  Then remove the variables from `portainer-stack*.yml` and the docs.

## 6. Secrets at rest (D-151)

- **Key:** `TX_SECRETS_KEY` (32 random bytes) or `TX_SECRETS_KEY_FILE` (Docker
  secret), separate from `jwt_secret`. With no key, connections can't be enabled.
  There's **no** temporary per-process key, because stored credentials would be
  unreadable after a restart.
- **Cipher:** AES-256-GCM (`cryptography`, a new dependency), with a fresh
  random nonce for each value. The associated data is
  `integration_id|user_id|kind`, which ties each ciphertext to its row. Each value
  starts with a key-version prefix.
- **Rotation:** `TX_SECRETS_KEYS=v2:…,v1:…`. New values are encrypted with the
  first key, and any listed key can decrypt. `rundere secrets rotate`
  re-encrypts every row with the newest key; then retire the old key.
- **Write-only:** a saved credential is never returned by the UI, the API or
  MCP. The UI shows "saved · ends in …1234 · last OK <time>", with replace or
  delete only.
- **Handling:** decrypt only at the moment of the outgoing call. Never write a
  credential to logs, exception text or `commit_attempts`, which store the
  request payload and the response, never request headers. A test pins that.
- **Lifecycle:** deleting a connection, deleting a user, or the user purge
  removes the ciphertext. Advise users to create a dedicated, revocable
  "Rundere" API key in AirTrail or AdventureLog.
- **Backups:** back up the key separately from the database. If the key is lost,
  users re-enter their tokens.
- **Limit:** this protects database dumps and backups. It doesn't stop someone
  who holds both the running server and the key. That is the same stance as
  `share_with_admins`: in-app policy, not zero-knowledge.

## 7. Outbound URL safety (D-152)

User-supplied base URLs are fetched by the server, which is a server-side
request forgery (SSRF) risk.

- Require `https`.
- Resolve the host and reject loopback, private (RFC 1918 / ULA), link-local,
  CGNAT and metadata addresses. Check again **at connect time**, not only when the
  connection is saved (DNS rebinding).
- Don't follow redirects to blocked addresses.
- Set connect and read timeouts and a cap on response size.
- **Admin allowlist:** an admin-managed list permits specific LAN hosts, e.g. the
  home AirTrail at `http://airtrail:3000`.
- User-facing errors show a status code and a short reason, never the raw
  response body.

## 8. Storage posture (D-153, D-154, D-155)

- **D-153: Rundere stores documents, per user.** It stores files tied to the
  capture surface (PDF, .ics, .eml; YONDER-179). It never stores photos, audio or
  receipt images.
  - A new `documents` store: `owner_id` (not null), sha256, content type, size,
    source, and optionally a trip.
  - Bytes live at `documents/<owner_id>/<sha256>`, deduplicated only within one
    user.
  - Deleting a user or trip removes their files directly, with no counting of
    references across users. Per-user storage quotas.
  - The current `artifacts` table is one global index with no owner (and no
    bytes are actually written to disk). Leave it as capture provenance; don't
    extend it.
- **D-154: journal photos are references, not files** (this supersedes the
  SPEC-6 §1 plan for `Artifact` media).
  - An entry records photo references: the service, the reference, when and where
    the photo was taken, a checksum, and whether it's pinned.
  - On iOS, the capture date and location read from the shared file are kept, and
    the file itself is discarded.
  - "This note's photos" is a lookup by capture time (and place) in the user's
    own library: PhotoKit on iOS, the Immich API, and manual links for Google
    Photos.
  - Photos are verified once, at **trip close-out**, in a photo section of the
    post-trip review shell (SPEC-5 D-130). Kept photos are then frozen as real
    IDs, and an album can optionally be created in the user's own photo service.
  - Receipts are ordinary photo references. Details: YONDER-189.
- **D-155: no transaction imports, no receipt OCR.** No bank or card feeds, CSV
  or statement imports, aggregators, or vision-based extraction. Expenses
  (YONDER-172) are entered by hand or pre-filled from prices in booking captures.
  Rundere isn't a corporate expense system.

## Build order

1. **D-149 stopgap and its test** (YONDER-191): a possible leak today.
2. **D-147 airport table** (YONDER-190): the prerequisite for everything else,
   and the reason users without AirTrail lose time zones.
3. **D-148 Rundere-first write set** (YONDER-190).
4. **D-150–D-152 per-user connections, secrets and URL safety** (YONDER-192).
   This replaces the D-149 stopgap.
5. **D-153/D-154 documents store and photo references** (YONDER-179,
   YONDER-189), following the capture and journal roadmaps.

## Open questions

- **Base URL scope:** per user (as proposed), or also a server-wide default for
  "the home instance" to make the common case easier?
- **Airport dataset:** OurAirports (public domain, no time zones, so derive them)
  or mwgg/Airports (MIT, includes time zones)? And how often to refresh?
- **Thumbnails for photo references:** about 20 KB each. Leaning no for v1
  (YONDER-189).
- **D-133 vs YONDER-188:** is pre-selecting a trip in the compose form consistent
  with "never auto-attach"? Probably yes, because the author confirms on save.

## Decisions (proposed, D-146–D-155)

- **D-146 — Rundere is first-party; AirTrail and AdventureLog are optional
  extras.** The core works with neither configured; integrations live under
  "Advanced". (proposed, 2026-09-28)
- **D-147 — Rundere ships its own airport reference data.** A bundled dataset,
  with time zones derived offline where missing. AirTrail can only fill gaps, and
  `airtrail_db_url` is retired. (proposed)
- **D-148 — `yonder.activity` is the primary write target.** External targets
  exist only for capture owners with an enabled connection. Trip matching runs
  against Rundere trips first. (proposed)
- **D-149 — External writes are owner-scoped.** Until per-user connections
  exist, only the credential owner's captures produce AirTrail or AdventureLog
  targets. (proposed, urgent)
- **D-150 — Integration connections are per user.** Each user supplies their own
  base URL and token, with no admin tokens. "Self" for RULE-10 is the capturing
  user's own traveller. `TX_*` integration env vars are deprecated and used only
  to bootstrap the owner's connections. (proposed)
- **D-151 — Credentials are encrypted at rest with a dedicated, rotatable key.**
  AES-256-GCM with row-bound associated data and a versioned keyring.
  Credentials are write-only and never logged. With no key there are no
  connections. (proposed)
- **D-152 — User-supplied URLs pass an SSRF policy.** `https` only; private,
  loopback and metadata ranges are blocked and re-checked at connect time; no
  redirects to blocked addresses; timeouts and size caps; an admin allowlist for
  LAN hosts. (proposed)
- **D-153 — Rundere stores documents, per user, and never photos, audio or
  receipt images.** A new owner-scoped `documents` store. (proposed)
- **D-154 — Journal photos are references resolved by time and place, verified
  at trip close-out.** This supersedes SPEC-6's `Artifact` media plan.
  (proposed)
- **D-155 — No transaction imports and no receipt OCR.** Expenses are entered by
  hand or pre-filled from bookings. (proposed)
- **D-156 — Server-wide AirTrail/AdventureLog credentials are admin-only until
  per-user connections ship (D-151).** Commit, create-collection and the
  AdventureLog sync page/push/pull, the in-review trip picker and the
  process/reprocess resolve stage's target lookups refuse or skip for non-admins;
  auth-off and the operator CLI are unaffected. Interim for YONDER-191; lifted by
  YONDER-192. Still open: the single-capture re-resolve (`app/review/routes.py`
  `_reresolve`, per-item edit path) and the Discover airport lookups read the owner's
  accounts without this gate. (proposed)
