# SPEC (planning) — Rundere First-Party: optional integrations, per-user connections, storage posture

**Status: BUILDING (2026-10-12).** D-146–D-152 are **built** (4.8: YONDER-190,
YONDER-192), with the choices in D-186; D-149's stopgap and D-156 are superseded by
them. D-153–D-155 remain proposed; D-196 (owner, 2026-10-13) settles D-153's
storage, limits, retention and scanning for YONDER-179. Tracked in Plane: YONDER-190 (first-party core + airport data),
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

## 8. Storage posture (D-153, D-154, D-155, D-196)

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
- **D-196: the documents store's limits, retention and safety (YONDER-179;
  owner, 2026-10-13).** This makes D-153 concrete. Sizing from production on
  2026-10-13: 32 captures since 2026-08-04 total about 3.3 MB, and booking PDFs
  are usually 0.1–2 MB, so a heavy traveller stores roughly 150 MB a year.
  - **Where:** a local folder named by `TX_DOCUMENTS_DIR`, set by the hoster.
    Empty means file capture is off, and everything else works as before. The
    README leaves the location open: any local disk works. A network share
    works too, but a hard-mounted NFS share hangs uploads while the NAS is
    away, so a local folder backed up to the NAS is the recommended setup. The
    owner's deployment uses a folder on `/media/user/1TB_Vol21`. Bytes live
    at `<dir>/<owner_id>/<sha256>`, as in D-153.
  - **What:** PDF, .ics and .eml only. The type is decided from the file's
    content, never its name. Nothing else is stored; D-153's "no photos, audio
    or receipt images" stands.
  - **Limits:** at most **25 MB per file** and **1 GB per user**. The default is
    `TX_DOCUMENT_QUOTA_MB=1024`, and an admin can raise it for one user. Usage
    counts each of that user's distinct files once. At the limit, new files are
    refused with a clear message saying how much is used. Nothing is ever
    deleted to make room.
  - **Retention:** a file is kept for the life of its trip, and its stored copy
    is deleted **12 months after the trip ends** (its end date, else its start
    date). What was extracted from the file stays: the capture, the timeline
    items and the review record. A file filed to no trip is deleted 12 months
    after it was uploaded. Deleting a trip or an account deletes its files at
    once, as in D-153. A daily job does the purge and logs counts only. Each
    file shows the date it will be removed.
  - **Safety:** every file goes through YONDER-243's scanner before it is
    stored or parsed:
    - ClamAV (`TX_CLAMAV_URL`) must be configured, or file capture stays off.
      If the scanner is unreachable, the upload is refused rather than stored
      unscanned. Infected files are refused.
    - PDFs get the static checks. Launch, ImportData and GoToE actions, XFA,
      rich media, encryption, malformed files, and anything over 25 MB or 200
      pages are refused. JavaScript and embedded files are noted.
    - An .eml's attachments are checked the same way. Only its PDF and .ics
      attachments are ever parsed.
  - **Who sees a file:** only the person who uploaded it, as with the journal
    (D-134). Travellers on a shared trip see what was extracted from it, never
    the file itself.
  - **Serving a file back:** download only, with `Content-Disposition:
    attachment` and `X-Content-Type-Options: nosniff`. A file is never shown
    inside a Rundere page, so a PDF's scripts can never run in the app.
  - **Raw captures follow the same 12 months (amends D-15; owner,
    2026-10-13):** *"It needs to be archived and deleted after a while."*
    - D-15's "kept" now means kept for the life of the trip plus 12 months,
      not forever.
    - After that, a capture's raw payload is deleted: the whole email
      (`raw_message_base64`) with its attachments, and a web capture's HTML.
    - The row stays as a tombstone: its sha256 content address, when it
      arrived, its owner, and the subject and sender. Everything extracted
      from it stays: the capture, the timeline items and the review record.
    - Re-processing a purged capture is no longer possible, and the review
      screen says so.
    - A capture filed to no trip is purged 12 months after it arrived.
      Captures still waiting in review are never purged.
  - **The mailbox copies (owner, 2026-10-13).** The inbox poller files each
    handled email into `Rundere/Captured` or `Rundere/Dropped` in the hoster's
    mailbox. Rundere clears only those two folders, never anything else in the
    mailbox:
    - **Captured:** moved to Trash on the same day Rundere deletes its own copy,
      12 months after the trip ends (or after arrival, when the capture has no
      trip). The message is matched through `inbox_messages`.
    - **Dropped** (unknown address, too large or rate-limited, so never
      captured): moved to Trash after **30 days**. That's long enough to notice
      a misdirected booking and forward it again.
    - **Deletion moves mail to Trash** (`TX_INBOX_TRASH_FOLDER`, default
      `Trash`), so it can be recovered until the mail provider empties the
      Trash. Rundere never expunges mail permanently.
    - The daily retention job does this when the inbox is configured, and logs
      counts only.
  - **Settings for other hosters:** `TX_RETENTION_MONTHS` (default 12) and
    `TX_INBOX_DROPPED_DAYS` (default 30).

## Build order

1. **D-149 stopgap and its test** (YONDER-191): a possible leak today.
2. **D-147 airport table** (YONDER-190): the prerequisite for everything else,
   and the reason users without AirTrail lose time zones.
3. **D-148 Rundere-first write set** (YONDER-190).
4. **D-150–D-152 per-user connections, secrets and URL safety** (YONDER-192).
   This replaces the D-149 stopgap.
5. **D-153/D-154 documents store and photo references** (YONDER-179,
   YONDER-189), following the capture and journal roadmaps. YONDER-243
   (attachment scanning) ships first; YONDER-179 builds on it under D-196.

## Open questions

- ~~**Base URL scope**~~ — per user, with an admin allowlist for LAN hosts (D-186).
- ~~**Airport dataset**~~ — mwgg/Airports, refreshed each release (D-186).
- **Thumbnails for photo references:** about 20 KB each. Leaning no for v1
  (YONDER-189).
- **D-133 vs YONDER-188:** is pre-selecting a trip in the compose form consistent
  with "never auto-attach"? Probably yes, because the author confirms on save.

## Decisions (proposed, D-146–D-155)

- **D-146 — Rundere is first-party; AirTrail and AdventureLog are optional
  extras.** The core works with neither configured; integrations live under
  "Advanced". (built 4.8, 2026-10-12)
- **D-147 — Rundere ships its own airport reference data.** A bundled dataset,
  with time zones derived offline where missing. AirTrail can only fill gaps, and
  `airtrail_db_url` is retired. (built 4.8; see D-186 for how AirTrail works
  without it)
- **D-148 — `yonder.activity` is the primary write target.** External targets
  exist only for capture owners with an enabled connection. Trip matching runs
  against Rundere trips first. (built 4.8)
- **D-192 — In review, AdventureLog is optional and second (owner, 2026-10-12).**
  An item filed to a Run·dere trip is ready to approve even when no AdventureLog
  collection matches: the miss no longer puts it in "Needs you", the Run·dere
  activity is its target, and the AdventureLog records wait until a collection is
  picked. A flight's open passenger question still needs you. On the item page the
  **Trip** picker comes first; AdventureLog sits below it, folded and marked
  optional, and only appears when the owner has a connection (D-150). Notes and
  the "what logging writes" preview name the system they mean ("No AdventureLog
  collection matches these dates", never "No trip covers these dates" for an item
  already on a trip), and mention AdventureLog and AirTrail only to people who have
  them connected; each resolution records the `integrations` it saw for this.
  "Log approved" opens as a sheet (design board) and names only the systems its
  writes go to. (built 4.9)
- **D-193 — A taxi is its own kind of trip (owner, 2026-10-12).** Taxis and
  ride-hailing (Uber, Lyft, Bolt…) are ground mode `taxi` and a **Taxi** timeline
  activity (company, from, to, pickup and drop-off times, confirmation), not a
  "Road trip". The older mode `rideshare` means the same and files as a Taxi.
  The extractor is told to use `taxi` (prompt v3); a booked taxi keys on its
  reference like a rental, otherwise on its endpoints. In review, the ground
  *Kind* is a dropdown the reviewer can correct. AdventureLog gets `car`.
- **D-194 — The iOS app signs in with the server's own OAuth; password stays
  (owner, 2026-10-13).** A server supports password sign-in, OAuth/OIDC sign-in,
  or both, per host, and the app uses whatever the host it's pointed at offers
  (`/api/v1/auth/methods`). The app's OAuth runs against **Run·dere's own OAuth
  2.1 server** (YONDER-107), never the IdP directly: `/oauth/authorize` opens
  in `ASWebAuthenticationSession` and shows that host's normal sign-in, so a
  password-only LAN host and an SSO-forced public host both work through it.
  Every server has a **built-in public client `rundere-ios`** (PKCE, upserted at
  startup, redirect `https://rundere.app/auth/callback` — the universal link in
  rundere.app's AASA — set by `TX_OAUTH_APP_REDIRECT_URIS`), so self-hosters
  need no setup. It is **first-party**: no consent screen (signing in is the
  consent), and its tokens may change account settings (sharing, email) like
  the app's password token; registration (DCR or CLI) can never make a client
  first-party. Each signed-in phone appears under Account → Connected apps as a
  "signed-in device" you can disconnect. `/api/v1/auth/methods` names the client
  (`oauth_client_id`); null means "password only" to the app. Supersedes the
  server-brokered OIDC handoff in SPEC-4.2 §3.2 (YONDER-77/104, cancelled).
  (YONDER-235 server side; app side YONDER-175)
- **D-149 — External writes are owner-scoped.** Until per-user connections
  exist, only the credential owner's captures produce AirTrail or AdventureLog
  targets. (shipped as D-156; superseded by D-150 in 4.8)
- **D-150 — Integration connections are per user.** Each user supplies their own
  base URL and token, with no admin tokens. "Self" for RULE-10 is the capturing
  user's own traveller. `TX_*` integration env vars are deprecated and used only
  to bootstrap the owner's connections. (built 4.8)
- **D-151 — Credentials are encrypted at rest with a dedicated, rotatable key.**
  AES-256-GCM with row-bound associated data and a versioned keyring.
  Credentials are write-only and never logged. With no key there are no
  connections. (built 4.8)
- **D-152 — User-supplied URLs pass an SSRF policy.** `https` only; private,
  loopback and metadata ranges are blocked and re-checked at connect time; no
  redirects to blocked addresses; timeouts and size caps; an admin allowlist for
  LAN hosts. (built 4.8)
- **D-153 — Rundere stores documents, per user, and never photos, audio or
  receipt images.** A new owner-scoped `documents` store. (proposed)
- **D-154 — Journal photos are references resolved by time and place, verified
  at trip close-out.** This supersedes SPEC-6's `Artifact` media plan.
  (proposed)
- **D-155 — No transaction imports and no receipt OCR.** Expenses are entered by
  hand or pre-filled from bookings. (proposed)
- **D-156 — Server-wide AirTrail/AdventureLog credentials are admin-only until
  per-user connections ship (D-151).** Run·dere is the first-party write path for
  everyone: a non-admin's commit still files onto their Run·dere trip
  (`yonder.activity`); only the AirTrail/AdventureLog targets are skipped (marked
  `no external connection`, never attempted) and counted separately in the toast.
  The AdventureLog create-collection / sync page / push / pull routes, the
  in-review AdventureLog trip picker and the process/reprocess resolve stage's
  target lookups refuse or skip for non-admins. Auth-off and the operator CLI are
  unaffected. Interim for YONDER-191; lifted by YONDER-192. Skipped targets count
  as settled for the item's Logged state, so after 192 an old item needs a
  reprocess to push externally. Still open: the single-capture re-resolve
  (`_reresolve`, per-item edit path) and the Discover airport lookups read the
  owner's accounts without this gate. (superseded 2026-10-12 by D-150: every
  path — batch and single re-resolve, commit, the trip page's AdventureLog
  routes — now uses the capture owner's or acting person's own connection, and
  Discover's airports come from Run·dere's table, D-147.)
- **D-157 — Discovery does not run on a past trip.** A trip is past when its end
  date (its start date when it has no end) is before today; undated trips are
  still planning. `discover.runner.trip_has_ended` is the one check; the web full
  run and per-finding re-run enforce it server-side, and the UI shows "This trip
  has ended" instead of the buttons. Existing findings stay readable. Open
  (YONDER-196): agent-written `save_research` / `save_city_guide` /
  `save_phrases` / `save_safety` are NOT blocked (post-trip notes are
  legitimate), `/api/v1` and MCP have no resolver-run endpoints today, and there
  is no owner override. (proposed)
- **D-161 — The web UI's palette is the design system's tokens.** `design/tokens/tokens.css`
  is served at `/brand/tokens/tokens.css` and linked by every base template (app, trips,
  auth card, SQLAdmin) before the app CSS. `theme.css` and `trips.css` keep their variable
  names as aliases of the semantic `--rd-*` tokens, so dark, light and the `data-theme`
  override come from one place and a change to `design/tokens/` reaches the whole app.
  This replaces `theme.css` as the palette's source (its "D-52" header). Status colours in
  `trips.css` use `--rd-danger`, `--rd-success` and `--rd-highlight`. Type (brand fonts),
  components (44px controls, icons, one primary action) and layouts are separate steps under
  YONDER-202. (proposed)
- **D-165 — Dense screens may use the 32px control size.** Owner decision 2026-10-02: the
  design README's 44px touch target stays the default; every interactive control is at least
  the system's small size (`--rd-control-sm`, 32px). The theme toggle, selects, disclosure
  summaries and tab buttons use the floor rather than 44. Nothing goes below 32. (proposed)
- **D-172 — Next up is the soonest moment, not the soonest item.** Every timed timeline
  activity is one or two moments: its start (`when`) and, when it has one, its end
  (`endWhen`) — a rental is a pick-up and a drop-off, a hotel a check-in and a check-out.
  Home's Next-up card and `/api/v1/dashboard` (`next_activity`) show the soonest moment
  still ahead, so a multi-day booking never holds the card for its whole span; what has
  started and not ended is listed as in progress (`in_progress`), never as the card.
  Times are local wall clock: the app sends its device time as `now`, else the server's
  clock is used. A timed item with no end is a single moment; a date-only item with no
  end is under way for that day; an end date with no time runs to the end of that day;
  at the same minute an end comes before a start. Notes are never next up. Only the
  viewer's own activities count (traveller tags, `app/trips/who.py`).
  `app/trips/nextup.py` is the one implementation. Owner-reported bug, YONDER-177. (proposed)
- **D-186 — How per-user connections are built (YONDER-190/192; accepted by the
  owner 2026-10-12).**
  - *Airports:* the bundled table is mwgg/Airports (MIT, with time zones;
    `resolve/data/airports.json.gz`, rebuilt by `scripts/build_airports.py` each
    release, any missing zone derived with timezonefinder). It answers every
    lookup; an AirTrail connection only adds flown airports the table lacks.
  - *AirTrail is API-only; `TX_AIRTRAIL_DB_URL` is gone.* Run·dere never reads
    AirTrail's database, and there is no sidecar. Requires **AirTrail 3.13+**:
    flights are saved with airport **codes** (AirTrail resolves them against its
    own OurAirports-based table), the airline by its ICAO code — looked up
    through `GET /api/v1/airlines?query=<IATA>` when the person hasn't flown it —
    and the seat as `<USER_ID>`, which AirTrail fills in with the key's owner.
    The key needs `flight.read.own`, `flight.create.own`, `flight.update.own`,
    `flight.delete.own`, `reference_data.read` and `profile.read`; the
    connection test checks the airline lookup and says which scope or version
    is missing. A flight's booking reference goes to AirTrail only as the
    6-character airline record locator (a GDS/Sabre PNR, letters and digits);
    any other reference stays on Run·dere's record, because AirTrail rejects the
    whole flight over a custom field past its limit (seen live, 2026-10-12).
    If AirTrail ever drops this, the fallback is a sidecar in
    AirTrail's stack that Run·dere connects to — never a database URL in Run·dere.
  - *Base URL per user; local hosts only by server config.* Everything is public
    `https` unless an admin lists the host in `TX_INTEGRATION_ALLOWED_HOSTS`
    (env, not a table). The deprecated server-wide URLs' hosts stay trusted
    while their credential is still set, for the one-time migration.
  - *A token is never sent somewhere new:* changing a connection's URL requires
    re-entering its token, and redirects are refused (urllib would carry the
    credential header). Only Run·dere itself (the web session or the app's login
    token) can add, change or remove a connection — connected apps, AI agents
    and API tokens can't (as YONDER-101). The Connections tab has a "How to
    connect" page with the steps and scopes for each.
  - *Whose connection:* a capture uses its owner's. An open capture and auth-off
    mode use the deployment owner's (the user linked to the `self` traveller,
    else the first admin). Only a deployment with no users at all still uses the
    deprecated variables directly. A deactivated user's connections aren't used.
    The `TX_AIRTRAIL_*` / `TX_ADVENTURELOG_*` variables are no longer documented;
    the code reads them only to seed the owner's connections once on upgrade.
  - *RULE-10's "self"* is the capture owner's own traveller (`me` in
    `resolve_travelers`); an open capture falls back to the `self` kind.
  - *Not retroactive:* targets are decided when a capture resolves, so only
    captures processed after someone connects go to that system. Writes skipped
    under D-156 ("admins only") are retried by a later commit once the owner
    connects. (built 4.8)
- **D-187 — Each person's "now" is their own time zone (YONDER-219; owner,
  2026-10-12).** The Travel profile has a time zone (IANA, e.g.
  `Europe/London`) on the person's traveller, set once and independent of home
  profiles, which change over time. Everything date-relative that person sees is
  worked out against their own wall-clock now in that zone: Home (greeting,
  "in N days", Next up, trip cards), the trips list's upcoming/past, the
  Journal's default day, and `/api/v1/dashboard` when the app doesn't send its
  own `now` (the app's device clock still wins, D-172). Two people on one server
  each get their own today. With no zone set, the server clock is used; the
  page offers the browser's detected zone in one click. `app/clock.py` is the
  one implementation. Discovery runs, the PDF and capture stamps keep the
  server clock. (built 4.8)
