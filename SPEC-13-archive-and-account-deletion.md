# SPEC-13 — The Run·dere archive, and deleting an account

Status: **5.0, proposed** (written 2026-11-13; YONDER-375 and YONDER-377, under
YONDER-274 and YONDER-273). Nothing here is built. Both halves are
security-sensitive, so the exporter (wave 8b) and the purge (wave 9b) wait for
the owner's agreement to this spec.

Before 5.0 the only way to take data out of Run·dere is the trip PDF, and the
only way to remove a person is for an admin to deactivate the account, which
keeps every row. D-151 and D-196 already promise that deleting an account
removes its credentials and files, but nothing deletes an account.

This spec has three parts, in the order the work depends on them:

1. **The table-ownership map** (§1): what each of the 57 tables holds, how a
   row is tied to a person, and what export and deletion do with it.
2. **D-241, the Run·dere archive** (§2): a versioned export of a person's own
   data.
3. **D-242, deleting an account** (§3), which amends D-211, and its threat
   model (§4).

5.0 ships the export and the format. Import, and an admin export of the whole
server, are left for 5.x (owner, 2026-11-13).

## 1. The table-ownership map

### 1.1 Rules the map follows

- **A person's data is found by five kinds of link**, and the map names the
  one each table uses:
  - `owner_id` or `user_id` on the row;
  - `author_id`, `actor_id`, `created_by`, `updated_by` or `fetched_by`: someone
    who touched a row that belongs to a trip or to the server;
  - `traveler_id`: the person's traveller (`users.traveler_id`), an integer
    that is server-wide and shared with trips they don't own (D-204);
  - a parent row (`trip_id`, `item_id`, `template_id`, `expense_id`, …);
  - a folder on disk named after the user id.
- **Ownerless rows belong to nobody.** A NULL `owner_id` or `user_id` is a row
  from before sign-in existed, or from a server with sign-in off. It is in no
  one's archive, and no account deletion touches it. Admins can still see and
  clean up such rows (D-211).
- **A server with sign-in off has no accounts** (`TX_AUTH_REQUIRED=false`,
  D-203), so it offers neither the export nor deletion. Its operator has the
  database.
- **The map becomes a registry in code** (wave 8b, `app/ownership.py`). A test
  fails when a table in `Base.metadata` isn't in the registry, and when a
  column named like a person link (the list above) isn't declared. A migration
  that adds a table can't merge until someone has decided what export and
  deletion do with it.

The "Export" and "On deletion" columns are summaries. §2.4 and §3.5 are the
rules.

### 1.2 Account and sign-in (11 tables)

| Table | Holds | Tied to a person by | Export | On deletion |
|---|---|---|---|---|
| `users` | The account: username, email, password hash, SSO subject, flags | `id` | Username, email, sign-in kind, created date, settings. Never the hash, the SSO subject or `token_version` | Deleted, last of all |
| `refresh_tokens` | Hashes of the apps' refresh tokens | `user_id` (database FK) | Never | Deleted |
| `password_reset_tokens` | Hashes of reset links | `user_id` (database FK) | Never | Deleted |
| `oauth_codes` | Hashes of one-time sign-in codes | `user_id` | Never | Deleted |
| `oauth_grants` | A person's consent to a connected app | `user_id` | Never | Deleted |
| `personal_api_tokens` | Hashes of `ypat_…` tokens | `user_id` | Never | Deleted |
| `api_keys` | Hashes of ingest keys | `user_id` (nullable) | Never | Deleted. A key that a batch of another owner still points at is revoked and unlinked instead |
| `user_integrations` | AirTrail and AdventureLog connections, with the encrypted credential | `user_id` | Never, nothing from this table (D-151) | Deleted, ciphertext included (D-151) |
| `inbox_addresses` | The person's email-forward token | `user_id` (primary key) | Never: the token is a capability | Deleted |
| `account_events` | Audit log of actions on an account | `user_id` (who it's about), `actor_id` (who did it) | The person's own log: action, time, and whether they or an admin did it | Kept without the person: `user_id` and `detail` cleared. Where they were the actor, `actor_id` cleared |
| `ai_calls` | One row per AI call: feature, model, duration, outcome | `user_id` (nullable) | No. It holds no content, and it's gone after 90 days | Kept: `user_id` cleared. The row still ages out at 90 days |

### 1.3 The traveller and their profiles (4 tables)

| Table | Holds | Tied to a person by | Export | On deletion |
|---|---|---|---|---|
| `traveler_identities` | A person on trips: name, aliases, nationality, base currency, time zone | `users.traveler_id` points at it. Server-wide | The person's own row. Companions appear in trip files as names only | Kept as a guest name when a surviving trip still lists it, with every other field cleared. Otherwise deleted (§3.5) |
| `home_profiles` | Homes and home airports, by date | `traveler_id` | Their own | Deleted |
| `packing_profiles` | Packing preferences | `traveler_id` | Their own | Deleted |
| `discovery_profiles` | Discovery preferences | `traveler_id` | Their own | Deleted |

Rows with a NULL `traveler_id` are the server's defaults. They are never
exported and never deleted with an account.

### 1.4 Own-only content (10 tables)

Only the person sees these rows today (D-134 and the decisions that follow
it), so each one is exported in full and deleted in full.

| Table | Holds | Tied to a person by | Export | On deletion |
|---|---|---|---|---|
| `journal_entries` | Journal entries | `owner_id` | All, wherever they're attached | Deleted, including entries attached to other people's trips |
| `saved_places` | The places library, with "dismissed" tombstones | `owner_id` | All, dismissed ones marked | Deleted |
| `saved_place_filings` | Which trips a saved place was put on | `owner_id` | With the place | Deleted. The timeline item it made on someone else's trip stays |
| `place_lists` | Lists of saved places | `owner_id` | All | Deleted |
| `place_list_members` | A place in a list | `owner_id` | With the list | Deleted |
| `inventory_items` | What the person owns and packs | `user_id` | All | Deleted |
| `packing_templates` | Their packing templates | `user_id` | All | Deleted |
| `packing_template_items` | A template's lines | Its template | With the template | Deleted |
| `bags` | Their bags | `user_id` | All | Deleted |
| `documents` | Uploaded files (PDF, .ics, .eml). Bytes at `<TX_DOCUMENTS_DIR>/<owner_id>/<sha256>` | `owner_id`, and the folder | The files still stored, plus the list of ones already removed by retention | Rows deleted and the person's whole folder removed (D-153, D-196) |

### 1.5 Trips and what hangs off them (11 tables)

A trip belongs to its owner and is shared with its travellers and viewers
(D-122, D-199). §2.4 says what an archive holds for an owned trip and for a
shared one. §3.4 says which trips an account deletion removes and which it
hands on. "With the trip" below means the row follows its trip either way.

| Table | Holds | Tied to a person by | Export | On deletion |
|---|---|---|---|---|
| `trips` | The trip | `owner_id` | Owned: in full. Shared: a header only | Owned: deleted or handed over (§3.4). Others': untouched |
| `trip_members` | View-only shares | `user_id` (who it's shared with) | No | The person's own shares deleted. Shares of a deleted trip go with the trip |
| `trip_travelers` | The roster | `traveler_id` | As names | With the trip. On a surviving trip the row stays, now a guest |
| `trip_activities` | Timeline items | Its trip. No author column | Owned: all. Shared: only items logged from the person's own captures | With the trip |
| `trip_activity_travelers` | Who an item is for | `traveler_id` | As names | With the trip |
| `trip_expenses` | Expenses and settle-ups | Its trip; `owner_id` is who entered it; `payer_id` is a traveller | Owned: all. Shared: the ones they entered | With the trip. On a surviving trip the row stays and `owner_id` is cleared |
| `trip_expense_shares` | Each traveller's part of an expense | `traveler_id` | With the expense | With the expense |
| `trip_expense_comments` | Comments on an expense | `author_id` | Their own comments | Their comments deleted everywhere |
| `trip_budget_items` | Planned costs | Its trip; `owner_id` is who entered it | Owned: the ones they can see (D-183). Shared: the ones they entered | With the trip. On a surviving trip: deleted when it was planned only for the leaver, otherwise kept with `owner_id` cleared |
| `trip_budget_travelers` | Who a planned cost is for | `traveler_id` | As names | With the budget item |
| `trip_rates` | The trip's exchange-rate table | Its trip; `fetched_by` | Owned: the rates. Never who fetched them | With the trip. On a surviving trip `fetched_by` is cleared |

### 1.6 Per-person data on a trip (6 tables)

Discover and packing are per person (D-123, migration 0026): each row has a
`trip_id` and a `user_id`, and nobody else on the trip sees it.

| Table | Holds | Tied to a person by | Export | On deletion |
|---|---|---|---|---|
| `discovery_runs` | A research run | `user_id` | No: run bookkeeping | Deleted on every trip |
| `discovery_findings` | A researched topic for a place | `user_id` | Their own, inside the trip file | Deleted on every trip |
| `discovery_items` | Places and events inside a finding | `user_id` | With the finding | Deleted on every trip |
| `discovery_exclusions` | Places they chose not to research | `user_id` | With the trip file | Deleted on every trip |
| `packing_plans` | A packing plan | `user_id` | Their own, inside the trip file | Deleted on every trip |
| `packing_items` | The plan's lines | `user_id` | With the plan | Deleted on every trip |

### 1.7 The capture pipeline (6 tables)

| Table | Holds | Tied to a person by | Export | On deletion |
|---|---|---|---|---|
| `ingest_batches` | A raw capture: the email, page or file reference, or its tombstone (D-196) | `user_id` | No (owner, 2026-11-13: raw captures are short-lived by design) | Deleted outright, tombstones included (§3.5) |
| `travel_items` | An extracted capture and its review state | `owner_id` | Not in format 1 (§2.6) | Deleted, including ones filed to other people's trips. The timeline items logged from them stay |
| `item_keys` | Identity keys of a capture | Its item (database FK) | No | Deleted with the item, by an explicit delete |
| `commit_attempts` | The verbatim exchange with AirTrail or AdventureLog for a capture | Its item (database FK) | No | Deleted with the item, by an explicit delete |
| `artifacts` | A content hash, type and size. No owner; no bytes are written | Referenced by batches and items | No | Deleted when no remaining batch or item points at it |
| `inbox_messages` | A hash per handled email, and the outcome | `user_id` (nullable) | No | Deleted |

### 1.8 Server-wide tables (9 tables)

None of these hold a person's data. Four of them record which admin last
touched a row.

| Table | Holds | Tied to a person by | Export | On deletion |
|---|---|---|---|---|
| `source_rules` | Capture routing rules | Nothing | No | Untouched |
| `rule_settings` | Rule modes (D-25) | Nothing | No | Untouched |
| `packing_catalog_items` | The packing catalogue | Nothing | No | Untouched |
| `phrasebook_phrases` | The phrasebook | Nothing | No | Untouched |
| `regional_languages` | The region to language map | Nothing | No | Untouched |
| `server_settings` | Admin settings | `updated_by` | No | `updated_by` cleared |
| `notification_targets` | Where admin alerts go | `created_by` | No | `created_by` cleared |
| `alert_states` | The health of each watched thing | The key `integration:<user id>:<kind>` | No | The person's `integration:` rows deleted |
| `oauth_clients` | Registered client apps | `created_by` | No | `created_by` cleared. Unused self-registered clients are YONDER-309's job |

### 1.9 Outside the database

| Where | Holds | On deletion |
|---|---|---|
| `<TX_DOCUMENTS_DIR>/<user id>/` | The person's files | Folder removed |
| The export folder (§2.5), `<user id>/` | Their archive, for up to 7 days | Folder removed |
| The hoster's mailbox, `Rundere/Captured` | Emails the person forwarded | Moved to Trash, found by Message-ID as the retention job does (D-196). Never expunged |
| The hoster's mailbox, `Rundere/Dropped` | Mail that matched nobody | Left to its 30-day rule |
| The map tile cache | Public map tiles | Untouched: nothing personal |
| The person's own AirTrail or AdventureLog | What Run·dere wrote with their connection | **Not deleted.** It is their account on their own service. The deletion page says so |
| The operator's backups and server logs | Whatever they held on the day | Outside Run·dere's reach. The page and the self-hosting guide say so (§4.3) |

## 2. The Run·dere archive (D-241)

### 2.1 Decision

**D-241 — A person can download their own data as a Run·dere archive: a zip of
JSON files in a versioned, documented format. It holds what they can already
read in the app and nothing else, and never a credential.** (YONDER-274,
YONDER-375; owner, 2026-11-13.)

- **Whose data.** A person exports their own account. Nobody exports someone
  else's: an admin has no export of another person's data (D-211), and there
  is no whole-server export in 5.0 (`pg_dump` is the server backup).
- **Never wider than the app.** The exporter reads through `app/scope.py`. If
  the app wouldn't show a row to this person, the archive doesn't hold it.
  Shared trips are held to a narrower rule still (§2.4).
- **Never a credential** (D-151). No password hash, token, token hash, API
  key, integration credential or its hint, forwarding address, SSO subject or
  OAuth data. Nothing from the eight tables §1.2 marks "Never".
- **Web only, behind a fresh sign-in.** The pages live under Account in the
  browser. There is no `/api/v1` endpoint, no MCP tool, and a personal API
  token or OAuth token can't start or download an export. The apps link to
  the web page (owner, 2026-11-13).
- **Built in the background, kept 7 days**, then deleted. The zip has no
  password; the download asks the person to confirm who they are (§2.5).
- **Export and format only.** Format 1 is written so an importer can be built
  on it, and import comes in 5.x.

### 2.2 The format

An archive is a `.zip` named `rundere-archive-<username>-<YYYYMMDD>.zip`:

```
manifest.json
README.txt
profile/account.json
profile/traveller.json
profile/discovery.json
profile/packing.json
trips/<trip id>.json            one per trip
places.json
journal.json
documents/index.json
documents/<sha256>.<pdf|ics|eml>
```

- **`manifest.json`** names the format and lists everything else:

  | Field | |
  |---|---|
  | `format` | Always `rundere-archive` |
  | `format_version` | `1` |
  | `server_version` | The Run·dere version that wrote it |
  | `exported_at` | UTC instant |
  | `username` | Whose archive it is |
  | `files` | One entry per file: `path`, `section`, `sha256`, `bytes`, and `count` for JSON sections |
  | `not_included` | Fixed, plain-word list of what an archive never holds (§2.6), so a reader isn't left guessing |

- **`README.txt`** says the same in plain words for someone who opens the zip.
- **JSON conventions.** UTF-8. Instants are UTC and end in `Z` (D-224). Dates
  are `YYYY-MM-DD`. Money is a decimal string with its currency, as stored
  (SPEC-10). A missing value is `null`, never an empty string standing in for
  it.
- **Ids stay as they are.** A trip, place, list, entry or document keeps its
  server id, so links inside the archive (a journal entry's `trip_id`, a
  filing's `place_id`) resolve within it. An importer issues new ids and
  rewrites those links.
- **Other people are names.** Each trip file has its own `travellers` list of
  `{ref, name, is_me}`; expenses, shares and "who it's for" point at a `ref`.
  An archive never holds another person's user id, username, email, traveller
  id, nationality or profile.
- **File names are generated.** A document is stored under its sha256 and its
  detected type. The name the person uploaded it with is a field in
  `documents/index.json` and is never used as a path, so a crafted filename
  can't write outside the folder the zip is opened into.

### 2.3 Versioning

- **Adding** a field, a file or a section keeps `format_version`. A reader
  must ignore what it doesn't know.
- **Removing** a field, or changing what one means, raises `format_version`.
- A future importer reads the current version and the one before it.
- **The schemas are the contract.** Each JSON file has a JSON Schema in
  `schemas/archive/v1/` (`manifest`, `account`, `traveller`, `discovery`,
  `packing`, `trip`, `places`, `journal`, `documents`). They are strict
  (`additionalProperties: false`), so adding a field means editing a schema in
  the same PR, where a reviewer sees it.
- **CI holds the line.** A test exports the demo seed's account and validates
  every file against the schemas, and checks every checksum in the manifest.
  The format can't drift without a failing test.
- **Writers are allow-lists.** Each section writer names the fields it emits.
  A new column is not exported until it is added to the writer and the
  schema. No writer serialises a model row wholesale.

The schema files land with the exporter (wave 8b), written from the field
lists in §2.4 and checked against real output there. Writing them now, with no
exporter to validate, would be guessing.

### 2.4 What each section holds

**`profile/account.json`**: username, email, sign-in kind (local, SSO or
proxy), when the account was made, whether it is an admin, the support-access
setting (D-211), the theme and scenery choice once wave 6a adds them, and the
person's own account log (action, time, and "you", "an admin" or "the
server").

**`profile/traveller.json`**: the person's traveller (name, aliases,
nationality, base currency, time zone, notes) and their homes and home
airports with their dates.

**`profile/discovery.json`**: the Discovery profile's values.

**`profile/packing.json`**: the packing profile's values, the inventory, the
packing templates with their lines, and the bags.

**`trips/<id>.json`**, for a trip the person **owns** (`trips.owner_id`):

- the trip: name, dates, notes, expense currency, where it came from;
- the roster, as names;
- the whole timeline: each item's type, start, `values`, located points, who
  it's for, and whether it came from a capture;
- costs: expenses with their shares, settle-ups, the planned costs this
  person can see (D-183), the rates used, and their own comments;
- their own Discover findings and items, and excluded places;
- their own packing plan and its lines.

It does not hold the other travellers' Discover, packing, journals, files or
planned costs, because the owner can't see those in the app either.

For a trip **shared with** the person (a traveller or a viewer on it), the
file is marked `"shared_with_me": true` and holds only what they added
(owner, 2026-11-13):

- a header: the trip's name and dates, and their role (D-199);
- timeline items logged from their own captures;
- expenses and planned costs they entered, and their own comments;
- their own Discover and packing for the trip.

Hand-made timeline items on a shared trip are left out, whoever typed them:
`trip_activities` has no author column, so the exporter can't tell. The trip
PDF remains the way to take a copy of a shared timeline. A trip the person can
see only through support access (D-211) is not in their archive at all.

**`places.json`**: saved places (dismissed ones included and marked, so a
later import doesn't bring them back, D-169), lists, which places are in
which list, and which trips each place was filed to.

**`journal.json`**: every entry of theirs: the date it's about, when and in
which zone it was written, the text, values, place, and the trip it's attached
to. Other people's entries are never included (D-134).

**`documents/`**: every file of theirs that is still stored, and an index of
all of them: id, original name, type, size, sha256, trip, upload date, the
date retention will remove it, and for files already removed, when (D-196).

### 2.5 Building and downloading

- **Start.** Account › Export my data shows what the archive will hold and a
  "Build my archive" button. One build per person at a time, one build on the
  server at a time, and at most three a day per person.
- **The job is a durable row**, like a Discovery run (D-63), not an entry in
  the in-memory job list: a new `archive_exports` table (id, user, status,
  requested and finished times, expiry, size, sha256, format version, and an
  error's class name only). A build orphaned by a restart is marked failed the
  next time someone looks. The table joins the map as own-only: it is not
  exported, and it is deleted with the account.
- **Where the zip lives.** `TX_EXPORT_DIR`, default `<TX_ARTIFACTS_DIR>/exports`,
  at `<dir>/<user id>/<export id>.zip`, readable by the server's user only.
  It is never under a web root and is served only by the download route. A
  new build replaces the person's previous archive.
- **Room.** A build is refused when the folder lacks free space for the
  person's stored files plus a margin. Nothing is deleted to make room.
- **When it's ready** the Account page says so, and the person is emailed if
  they have an address and SMTP is set up. The email links to the Account
  page, never to the file.
- **Download** needs a signed-in web session **and** a fresh confirmation of
  who they are, at most 10 minutes old (§3.2 defines it, and deletion uses the
  same helper). The response is `Content-Disposition: attachment`,
  `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`. Someone
  else's export id answers 404.
- **After 7 days** the daily retention job deletes the file and marks the row
  expired. The page shows the date.
- **The demo server** has no export page (404, D-198).
- **The account log** records each build and each download.

### 2.6 Left out of format 1 on purpose

- **Raw captures** (owner, 2026-11-13) and everything in the capture
  pipeline: extracted captures, their review state and the verbatim exchanges
  with AirTrail or AdventureLog. What a logged capture produced is in the trip
  files as timeline items. Captures still waiting in review are the gap; a
  `captures.json` section can be added later without a version change (§2.3).
- **Credentials and sign-in data** (§2.1).
- **The AI call log** and Discovery run bookkeeping: no content, short-lived.
- **Server-wide data**: the catalogue, phrasebook, rules and settings.
- **Sharing lists**: who a trip is shared with means nothing on another
  server and names other accounts.
- **Anything in AirTrail or AdventureLog.** Those services have their own
  exports.

## 3. Deleting an account (D-242)

### 3.1 Decision

**D-242 — A person can delete their own account. The account is locked at
once and its data is deleted 7 days later; signing in during those days offers
to cancel. Admins delete other accounts through the same service.** It amends
D-211, which made deleting accounts admin-only. (YONDER-273, YONDER-377;
owner, 2026-11-13.)

- **Self-service**, under Account › Delete my account, in the browser. As
  with the export there is no `/api/v1` endpoint and no MCP tool. The apps'
  "Delete account" entry opens the server's page (owner, 2026-11-13; §3.7).
- **Locked at once, deleted after 7 days.** The 7 days are fixed; there is no
  setting.
- **A trip the person owns that other people are on** must be handed to one of
  them or marked for deletion before the request is accepted (§3.4).
- **Their name stays on other people's trips**, as a guest with no account
  behind it. Those timelines still say who flew.
- **Audit and AI-call rows stay, without the person** (§1.2).
- **Deleting on Run·dere never touches the SSO provider.** Someone who signs
  in through SSO afterwards gets a new, empty account, as any first-time SSO
  user does. Behind a trusted proxy header the same happens on their next
  visit.
- **Emails** go out when deletion is requested, cancelled and finished, when
  the account has an address and SMTP is set up.
- **Guards.** The last active admin can't delete their own account, and no
  deletion may leave the server without an active admin. The demo server
  offers no deletion (404, D-198).

D-211's list of admin-only actions changes in one word: admins still create
and deactivate accounts and can delete **other** accounts; deleting your own
is yours.

### 3.2 Confirming who you are

Export download and the deletion request both need a **fresh confirmation**,
one helper used by both:

- **Local account:** the password, typed on the page.
- **SSO account:** a round trip to the provider that asks it to sign the
  person in again (`prompt=login`, `max_age=0`). Where the provider returns
  `auth_time`, it must be recent; a provider that ignores the request still
  has to complete the round trip.
- **Trusted proxy header:** Run·dere can't re-authenticate; the proxy is the
  authenticator. The person types their username, and the page says the proxy
  is what protects this step.

A confirmation lasts 10 minutes, is bound to the user and their
`token_version`, and is held in a signed, http-only cookie. It grants nothing
by itself.

### 3.3 The request and the grace period

**The page** lists, in plain words, what will be deleted and what stays (§3.5
and §1.9, including that AirTrail, AdventureLog and the operator's backups
are out of reach). It offers "Export my data first" (§2). It lists the trips
that need a choice (§3.4). To submit, the person confirms who they are (§3.2)
and types their username.

**On submit:**

- Every session, refresh token, personal API token and OAuth grant ends, as
  "sign out everywhere" does (`authn.end_sessions`). Cancelling doesn't bring
  them back: the person signs in to the apps again and makes new API tokens.
  The page says so before they confirm.
- Support access is switched off.
- The request, the due date and the per-trip choices are stored. The account
  log records it.
- The person is emailed. People on a trip that is marked for deletion or
  hand-over are emailed what will happen and when (§3.4).

**During the 7 days the account is locked:**

- Signing in on the web reaches one page only: the due date, "Cancel
  deletion", and the export (build and download). Every other page redirects
  there.
- The API issues no tokens for the account. A sign-in attempt answers 403
  with `{"detail": …, "deletion_pending": true}`, so the apps can say why and
  link to the page.
- Ingest keys are refused. Mail to the person's forward address is dropped
  with its own outcome and sits in `Rundere/Dropped` for the usual 30 days,
  so it can be forwarded again after a cancel.
- Nothing about the account's data changes. Other people still see the trips
  they shared with it, and its traveller on their rosters.

**Cancel** is one click for the signed-in person. It clears the request,
emails them, and tells the people who were warned about a trip.

**No "delete now" on the web.** Deleting at once would undo what the 7 days
are for: someone who gets hold of a password could delete everything before
the owner reads the email. The operator can skip the wait from the command
line (§3.6).

### 3.4 Trips

Which trips are "shared" for this purpose: a trip the person owns that has
another **account** on it, either a traveller with a login or a view-only
share. A guest with no login doesn't count, because there's no one to hand the
trip to.

| The trip | What happens |
|---|---|
| Owned, nobody else with an account on it | Deleted with the account (`purge_trip`) |
| Owned, other accounts on it | The person chooses, per trip: **hand it to** one of those people, or **delete it**. The request isn't accepted until every such trip has a choice |
| Someone else's, the person is a traveller on it | The trip is untouched. Their roster row stays and their traveller becomes a guest. Their own Discover, packing, journal entries, files and captures for it are deleted |
| Someone else's, shared with them view-only | Their share is deleted |

- **Choices are applied when the account is deleted**, not when they're made,
  so cancelling undoes everything.
- **Notice.** When the request is made, the people on a trip marked "delete"
  are emailed that it will be deleted on the due date, and the person a trip
  is handed to is emailed that it will become theirs. They can't veto either.
  The 7 days are their time to take a PDF or ask the owner to hand it over
  instead.
- **Hand-over** sets `trips.owner_id` to the new owner. The trip's shared
  content stays: timeline, roster, expenses, planned costs, rates. The
  leaver's per-person data on it is deleted like on any other trip.
- **What changed during the 7 days.** Other people can still edit a locked
  account's trips. So the purge decides again when it runs:
  - A trip marked "hand to X", where X no longer has an account or is no
    longer on the trip, goes to another account on it: travellers with a
    login first, then view-only shares, the longest-standing account first.
  - A trip with **no recorded choice** that now has another account on it is
    handed over by the same rule. It is never deleted by default.
  - A trip marked "delete" is deleted.
  - A trip is never left ownerless. An ownerless trip is visible to admins
    (D-211), which would hand people's data to the admin by accident.

### 3.5 What is deleted

One service, `app/account_purge.py`, does the deletion, for self-service and
for admins. It is driven by the registry of §1.1, and these rules hold:

- **Every statement is keyed to the leaver.** A delete or update filters by
  the leaver's user id, their traveller id, or ids read from their own rows
  (their trips, items, templates, expenses). The account purge never runs a
  general orphan sweep (`purge_orphans`), which would touch other people's
  rows.
- **Order.** Trips first (§3.4), each through the existing `purge_trip`. Then
  per-person trip data, own-only content, the capture pipeline, the profiles
  and traveller, the sign-in rows, the unlinking of §1.2 and §1.8, and the
  `users` row last.
- **No reliance on cascades.** A few tables have database foreign keys
  (`refresh_tokens`, `password_reset_tokens`, `item_keys`, `commit_attempts`,
  `ingest_batches.api_key_id`, `travel_items.batch_id`). SQLite in the tests
  doesn't enforce them, so children are deleted explicitly, in an order that
  also satisfies Postgres.
- **One transaction for the rows.** Files can't be rolled back, so the
  folders (§1.9) are removed after the commit. The daily job also removes any
  documents or export folder whose user no longer exists, which finishes a
  removal that failed.
- **Safe to run again.** Because the `users` row goes last, a purge that
  stops halfway leaves a pending account, and the next daily run finishes it.
- **Captures leave no tombstone.** D-196 keeps a purged capture's hash,
  subject and sender so its owner can see what it was. With no owner there is
  nobody to show it to, and a subject and sender are personal data. The batch
  rows are deleted. This is the one place an account deletion goes further
  than retention does.
- **The mailbox.** The leaver's emails in `Rundere/Captured` are moved to
  Trash by Message-ID. If the mailbox can't be reached, the Message-IDs alone
  (no user id) are kept on the deletion record and the daily job retries;
  after 30 days it stops and logs a count.
- **The traveller.**
  - If a surviving trip lists the leaver's traveller (someone else's trip, or
    one handed over), the row stays as a guest: the display name is kept, and
    aliases, nationality, base currency, time zone, notes and the AirTrail id
    are cleared. Their homes and profiles are deleted.
  - Otherwise the row is deleted with its homes and profiles.
  - Guests who were only on the leaver's deleted trips, are on no other trip
    and are linked to no account are deleted too. `traveler_identities` has
    no "created by" column, so this is the narrowest rule that doesn't strand
    them. A guest nobody put on a trip is left alone.
  - A kept guest name stays unique on the server (D-204). If the same person
    comes back, they pick a new traveller name, or an admin links the new
    account to the old name, which is audited as it is today.
- **Nothing is left pointing at the account.** After a purge the leaver's
  user id appears in no column of any table, JSON columns included. A test
  scans the whole database for it (§4.4).
- **The account log** gets one row, tied to no user: an account was deleted,
  when, and whether by the person or by which admin.
- **The "finished" email** is sent to the address read before the purge
  started. If it can't be sent it isn't retried, because the address is gone.

### 3.6 Admins and the operator

- **Deleting another account** is on the Users page: the admin types the
  account's username to confirm. It follows the same path: locked at once,
  deleted after 7 days, the person emailed.
  - The person can't cancel an admin's deletion; an admin can.
  - Unless the account was also deactivated, the person can still sign in to
    the locked page and take their export.
  - The admin sees counts only ("3 trips pass to other travellers, 5 are
    deleted"), never trip names, and makes no per-trip choice: an admin can't
    see the trips (D-211). Every trip with another account on it is handed
    over by the rule of §3.4; the rest are deleted.
  - The admin gets no export of the person's data.
- **The command line**, for the operator: `python -m app.cli delete-account
  <username>`, with `--dry-run` (counts per table, changes nothing) and
  `--now` (skip the 7 days). Whoever has a shell on the server has the
  database anyway, so `--now` gives away nothing new. It is also the way out
  for an account whose owner can't sign in.
- **A deactivated account** can be deleted by an admin like any other.

### 3.7 The apps and the stores

The apps add one entry each, "Delete account" (and "Export my data"), under
their account settings. It opens `<server>/account/delete` (or
`/account/export`) in the system browser, where the person signs in. That
sign-in is useful in itself: an app token can't delete an account.

Checked against the stores' own pages on 2026-11-13 (Q-A2):

- **Apple** (guideline 5.1.1(v), "Offering account deletion in your app"):
  an app that supports account creation must let people *start* deletion in
  the app, and may send them to a website to finish, provided it links
  "directly to the page" where that happens. Confirmation steps are allowed.
  A process that takes time is acceptable when the person is told how long
  and gets a confirmation when it's done; the due date on the page and the
  two emails are that. The entry must be easy to find in account settings.
- **Google Play** ("Understanding Google Play's app account deletion
  requirements"): an app that lets people create accounts must offer an
  in-app path, which may be a link to a web page, **and** a web link in the
  Play Console that works without the app installed. Deactivating isn't
  deleting, and the retention practices must be stated.
- **So a direct link is enough for both**, with one extra piece for Google:
  the Play Console field needs a single URL, and Run·dere servers are
  self-hosted, so there isn't one. A page on rundere.app, "Delete your
  Run·dere account", will explain that accounts live on the server the person
  signed in to, give the path (`/account/delete`), say to contact that
  server's operator if they can't sign in, and state what is kept (§1.9). It
  belongs to wave 10 with the privacy page. Whether a Play reviewer accepts a
  page that points to someone else's server is the residual risk (§5).

App contract changes (wave 9c): the two fixed paths, and the sign-in
response's `deletion_pending`.

## 4. Threat model

Two questions, as YONDER-377 puts them: what could leak, and what could be
deleted by mistake.

### 4.1 What could leak through the archive

The archive is the most sensitive file Run·dere makes: a person's whole
record in one download.

| Threat | Answer |
|---|---|
| A stolen web session downloads the archive | The download needs a fresh confirmation (§3.2). The session alone isn't enough |
| A stolen API token, OAuth token or MCP client exports the data | There is no API or MCP path. Export is web-session only |
| A download link is forwarded or leaks from an email or a log | The link carries no token. It works only inside the person's own confirmed session. Emails link to the Account page |
| Someone guesses another person's export id | Random ids, and a 404 for anything that isn't yours |
| The zip is read on the server's disk or from a backup | The file is readable by the server's user only and is deleted after 7 days. The self-hosting guide will say to keep the export folder out of backups. Like D-151, this is in-app policy: whoever runs the server can read the database anyway |
| The archive holds a credential | Allow-list writers (§2.3), and nothing from the "Never" tables. A test builds an archive for an account that has every kind of credential and asserts that no stored hash, ciphertext, hint or token appears anywhere in the bytes |
| The archive holds someone else's data | The exporter reads through `app/scope.py`, shared trips are narrowed further, and other people are names. A two-user test asserts that the second person's journal, files, Discover, packing, hidden planned costs, email and ids are absent |
| A crafted filename escapes the folder the zip is opened in | Paths inside the zip are generated (§2.2) |
| An admin exports someone | No admin export exists (D-211) |
| Builds are used to fill the disk or stall the server | One build per person and per server at a time, three a day, a free-space check, one archive kept per person |
| The browser or a proxy caches the file | `Cache-Control: no-store`, attachment, nosniff |

### 4.2 What could be deleted by mistake, or by someone else

| Threat | Answer |
|---|---|
| A forged request from another site | Writes are same-origin only (the existing `same_origin_writes` guard and the SameSite cookie), and the request needs a fresh confirmation and the typed username |
| A stolen session | The fresh confirmation (§3.2) |
| A stolen password | The 7 days and the email: the owner sees it and cancels. This is why there is no "delete now" on the web (§3.3). On a server without SMTP there is no email, so the grace period is the only protection. The page says so |
| A stolen API token or a prompt-injected agent | No API or MCP path |
| An admin deletes the wrong account, or a compromised admin deletes people | The typed username, the 7 days, the email to the person, an audit row naming the admin, and a web UI that can't shorten the wait |
| The last admin deletes themselves and locks the server | Refused (§3.1) |
| **The purge deletes or changes another person's rows** | The largest risk. Every statement is keyed to the leaver (§3.5); no orphan sweep; the registry is complete by test; and the survivor test of §4.4 |
| A trip other people rely on disappears | It is deleted only when its owner chose that, and the people on it had 7 days' notice. With no recorded choice it is handed over, never deleted (§3.4) |
| A trip ends up ownerless and so visible to admins | The purge never leaves one (§3.4) |
| The roster changed during the 7 days | The purge decides again when it runs (§3.4) |
| A purge stops halfway | It can be run again, and the `users` row goes last (§3.5) |
| A new table is added later and the purge doesn't know it | The registry test fails the build (§1.1) |

### 4.3 What a deletion leaves behind

Stated on the page, in the docs and on the rundere.app privacy page:

- The person's **name on other people's trips**, and what was logged from
  their bookings onto shared timelines (a flight item may carry passenger
  names in its values). By design (owner, 2026-11-13).
- **Expenses they entered on surviving trips**, with no link to them. The
  group's balances depend on those rows.
- **Counts**: account-log and AI-call rows with no user.
- **Their own AirTrail and AdventureLog**, which Run·dere doesn't reach into.
- **The operator's backups**, until they rotate, and **server logs**, which
  never hold content but can hold a username or an id in a request path.
- **Mail already sent**, and mail in the hoster's Trash until the provider
  empties it.
- **The username is free again.** Someone else can register it. Shares are by
  account id, so the newcomer inherits no access, but people might take them
  for the old account. Registration is closed by default and accounts are
  normally made by an admin; holding deleted usernames back would mean
  keeping a record of them, so this spec doesn't.
- **A capture chain.** If a later capture of someone else's could ever point
  at the leaver's item (`revision.prior_item_id`), that link must be cleared.
  Captures are per owner, so it shouldn't happen; wave 9b verifies it.

### 4.4 Tests the build must have

Wave 9b can't merge without these, and the adversarial review goes looking for
what they miss:

1. **The survivor test.** Two accounts with a full set of data each, sharing
   trips in every arrangement of §3.4. Delete one. Every row of the other,
   in every table, is byte-for-byte unchanged, except the changes this spec
   lists: the `trip_members` rows, the cleared `owner_id`, `author_id` and
   `fetched_by` values, the handed-over `trips.owner_id`, and the traveller
   turned guest.
2. **The id scan.** After the purge, the leaver's user id is in no column of
   any table, JSON included, and in no folder name on disk.
3. **Registry completeness** (§1.1), for tables and for person-link columns.
4. **Credentials absent** from an archive, and **another person's data
   absent** (§4.1).
5. **Schema conformance** of the demo account's archive (§2.3).
6. **Scoping**: another user gets a 404 for an export id, can't cancel
   someone's deletion, and a non-admin can't delete anyone else.
7. **Guards**: last admin, demo server, sign-in off, a locked account's
   tokens and ingest keys refused.
8. **Postgres.** CI runs the suite on SQLite. The purge's delete order
   matters only where foreign keys are enforced, so the survivor test also
   runs against Postgres before the wave merges, in CI if a service container
   is added, else by hand on the NEXT server with the result noted in the PR.

## 5. Open points for the owner

Decisions this spec made that the 2026-11-13 answers didn't cover. Each has
the choice made here; say which to change.

1. **Expense comments** by a deleted account are deleted, on surviving trips
   too. The alternative is to keep the words with no author.
2. **Captures are not in format 1** (§2.6). Captures waiting in review are
   lost in an export. Adding them later needs no version change.
3. **Hand-made items on a shared trip are not exported** (§2.4), because
   there's no author column, and this spec doesn't add one.
4. **A shared trip its owner marks for deletion is deleted**; the others get
   7 days' notice and no veto (§3.4).
5. **No "delete now" on the web**, only `--now` on the command line (§3.3).
6. **An admin's deletion can't be cancelled by the person**, who can still
   take their export unless deactivated (§3.6).
7. **The archive folder** defaults to `<TX_ARTIFACTS_DIR>/exports` (§2.5).
8. **The Play Console link** is a page on rundere.app (§3.7). If a reviewer
   rejects it, the fallback is a contact address for deletion requests, which
   on a self-hosted product can only forward people to their operator.

## 6. Build order

1. **8b, the exporter:** the registry and its tests, the section writers and
   schemas, the `archive_exports` table, the background build, the fresh
   confirmation helper, the Account page, the retention step.
2. **9b, the purge:** the deletion record and the lock, `account_purge.py`,
   trip hand-over, the guards, the emails, the command line, the tests of
   §4.4. Fable 5.2 reviews it adversarially before it merges.
3. **9c, the screens and the apps:** the web pages' wording, the app entries
   and contract changes.
4. **10, release:** the rundere.app pages, the self-hosting notes on backups
   and the export folder.

Migrations: 8b adds `archive_exports`; 9b adds the deletion record and the
pending marker on `users`, and makes `account_events.user_id` nullable. The
numbers are taken when each wave branches.

## Decisions

- **D-241 — The Run·dere archive.** A person downloads their own data as a
  zip of JSON in a versioned format (`rundere-archive`, version 1) with JSON
  Schemas checked in CI. It holds what they can read in the app, never a
  credential, never another person's data beyond names. Owned trips in full;
  shared trips only what they added. Files included, raw captures not. Built
  in the background, kept 7 days, downloaded from the web after a fresh
  confirmation of identity. No API, no MCP, no admin export. Import is 5.x.
  (proposed)
- **D-242 — Deleting an account.** Self-service on the web; amends D-211.
  Locked at once, deleted after 7 days, cancellable by signing in. Owned
  trips with other accounts on them are handed over or deleted by choice, and
  never left ownerless. The person's name stays as a guest on other people's
  trips; audit and AI-call rows stay without them. One purge service, driven
  by the table-ownership registry, serves self-service, admins and the
  command line. The last admin and the demo server are guarded. The apps link
  to the web page. (proposed)
