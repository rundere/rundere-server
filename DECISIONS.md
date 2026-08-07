# Locked Decisions

Each has a rationale. Reopen only with a reason that addresses it.

---

## D-01 — Standalone FastAPI app, not n8n workflows

Extraction accuracy is the entire value of this project, and accuracy cannot be iterated on
without a test harness. n8n gives you 300-line JavaScript in a browser textarea with no
linting, no unit tests, no git diff, and no way to run an extractor against a fixture corpus.
The n8n cost curve inverts: fastest to a demo, slowest to a system.

Accepted losses: OAuth token lifecycle handling, n8n's per-node execution inspector, free
retry/backoff, and fast ad-hoc changes.

## D-02 — n8n removed from the architecture entirely

Originally planned as a hybrid (n8n for Gmail/GCal polling). Explicit user-initiated capture
via an Apps Script add-on eliminated the polling requirement, which eliminated the OAuth
requirement, which eliminated n8n's remaining role. The only scheduled job left is the
deferred-item sweep, which belongs in-app.

## D-03 — Capture runs inside Google, not against it

A Gmail/Calendar add-on written in Apps Script runs in the user's own session. `GmailApp`
already has authorized access to the open message. A script you author and authorize for
your own account needs no Google verification, no CASA assessment, and no Cloud Console
OAuth client.

This matters for distribution: `gmail.readonly` and `gmail.modify` are **restricted scopes**.
A publicly distributed app using them needs OAuth verification plus an annual CASA
third-party security assessment — multi-thousand-dollar recurring, months-long. The standard
self-hosted workaround (each user creates their own Cloud project and OAuth client) is ~15
console steps and is the largest onboarding friction in any Gmail-integrated self-hosted
tool. Running inside Apps Script avoids all of it: collaborators paste the script into their
own editor and point it at their own endpoint.

Note also that OAuth clients left in "Testing" publishing status issue refresh tokens that
expire in 7 days. Verify against current Google policy before writing docs.

## D-04 — Only flights mint collections, and only when none matches

Road trips are too brittle to anchor a trip: a directions URL carries no booking, no
confirmed dates, and no guarantee the drive happened. Everything else — road trip, train,
lodging, event — attaches to an existing collection or defers.

The "if and only if" has two clauses, enforced separately:
- **RULE-30** — `created` with a non-empty `candidate_collections` is a violation.
- **RULE-30b** — `created` requires `match_attempted=true`. Without it, a resolver that
  errors mid-ladder, catches the exception, and mints because it "found nothing" produces
  duplicate collections silently. This is the most likely real-world failure.

## D-05 — Human-selected collection short-circuits the ladder

Real workflow: create the collection in advance (to book time off), attach the drive plan
later. `capture_context.collection_id` → `collection_action: user_selected`, ladder skipped.
The resolver may never override an explicit human choice (RULE-35b) and may never mint over
one (RULE-35c).

## D-06 — Time is never synthesized

`DateTimeLocal` is an object carrying `local` + `precision` + `tz` + `origin`, never a bare
ISO string. `precision: date` means the time was genuinely absent.

This exists because of a real failure: a TripIt import where `arrival > departure` violations
from midnight-stub timestamps aborted the entire 325-flight batch on one bad record. There is
now no way to express "unknown time" as `00:00`.

Corollaries:
- Commit flights **one at a time**, never in bulk. AirTrail aborts the whole batch.
- Two-slash IANA zones (`America/Argentina/Buenos_Aires`) are rejected by schema pattern.
  Map to backward-compatible aliases before emit.
- `origin: inferred` is never committable (RULE-34b).

## D-07 — Home is effective-dated config, never a tracked Location

`HomeProfile` with `kind: residence | home_airport | home_region` and
`effective_from`/`effective_to`. The dating matters for historical backfill: a 2013 flight
must be judged against the home in force *then*.

Three separate flags on `Place`, deliberately not collapsed:
- `is_home` — matched a profile
- `suppressed` + `suppression_reason` — the **write decision**
- `distance_from_home_km` — the radius measurement

**Suppression is not deletion.** YYZ stays fully populated — the Transportation record needs
a departure point. What is suppressed is the *Location write*.

`can_anchor_collection` is separate from `suppress_location`: home is never a tracked place,
but departing from and returning to it is what makes a round trip legible.

`within_home_radius: true` → `outcome: ignored`, zero write targets. Without this guard,
every 12km drive becomes a trip. `radius_km` default 50 is a guess; tune it.

**Home config is UI-managed, never hardcoded (owner requirement, 2026-08-03).**
This repo is shared (D-21) and every collaborator has a different home, which changes
over time. So: no home values live in code. `/admin → Home Profiles` is the management
surface (full CRUD, with per-field help text explaining effective dating). A
`python -m app.cli import-home <file.json>` bootstrap exists for standing up a new
stack from a local file — `docs/home-profiles.example.json` shows the shape with
placeholder values, and `.gitignore` excludes any real `home-profiles*.json`.

Airport matching understands **IATA metro codes** (`resolve/home.py` METRO_GROUPS):
booking systems sometimes emit the city code rather than the specific airport, so
YTO matches a YYZ/YTZ home, WAS matches DCA/IAD/BWI, and so on in both directions.

## D-08 — Extractors never decide what home is

All home-detection, geocoding, and collection fields are stripped from
`travel-extraction.schema.json`. An extractor reports places; the resolver classifies them.
Field ownership is declared inline via `x-owner` and is lintable.

## D-09 — The extraction schema is derived, never hand-written

```python
import json
root = json.load(open("schemas/travel-item.schema.json")); defs = root["$defs"]
KEEP = ["PayloadBatch","Payload","ItemType","DateTimeLocal","Place","Provider","Traveler",
        "Money","FlightDetail","Movement","GroundDetail","RouteDetail","LodgingDetail",
        "EventDetail","CaptureContext"]
sub = {k: json.loads(json.dumps(defs[k])) for k in KEEP}
STRIP = {"DateTimeLocal": ["utc"],
         "Place": ["lat","lon","tz","geocode_source","adventurelog_location_id",
                   "is_home","home_ref","suppressed","suppression_reason",
                   "distance_from_home_km"],
         "Traveler": ["role","match_source","match_confidence"]}
for d, fs in STRIP.items():
    for f in fs:
        sub[d]["properties"].pop(f, None)
json.dump({"$schema": "https://json-schema.org/draft/2020-12/schema",
           "$id": "https://github.com/OWNER/travel-extractor/schemas/travel-extraction.schema.json",
           "title": "TravelExtractionBatch",
           "x-derived-from": root["$id"],
           "x-schema-version": root["x-schema-version"],
           "$ref": "#/$defs/PayloadBatch", "$defs": sub},
          open("schemas/travel-extraction.schema.json", "w"), indent=2)
```

Make this a CI check, not a manual step.

## D-10 — Deterministic extraction first, LLM last

Order: JSON-LD (schema.org `FlightReservation`, `LodgingReservation`) → iCal → Maps URL
path parsing → LLM fallback with strict schema validation.

`maps_url` is the cheapest extractor in the system: a directions URL carries origin,
waypoints, and destination in its path segments. Zero tokens.

**Measure the deterministic parse rate before building the LLM path.** If 70% of booking
emails carry usable JSON-LD, the LLM is a minor component. If it is 20%, extraction is the
project. Nothing else will tell you which.

Also consider: Gmail auto-creates Calendar entries for flights and hotels with structured
fields. Reading Google's parse is often more reliable than parsing the source email.

## D-11 — Local inference only; cloud is a manual, audited escalation

Local models are unreliable at emitting strict tool-call JSON — hence the mandatory schema
validation gate and one retry before `needs_review`.

Cloud escalation is a **button in the review queue**, never a pipeline fallback. Requires
redaction (`redaction_version`, `redaction_hash` recorded) and an audit row. Travel emails
contain full names, addresses, passenger manifests, and partial payment data.

## D-12 — One TravelItem per segment; ingest accepts batches

A multi-leg PNR fans out to N items sharing `booking_group_id`. Matches AirTrail's per-leg
records; a schedule change to leg 2 invalidates only leg 2.

`IngestBatch` is the unit `/ingest` accepts and the unit extractors emit — always an array,
even for one segment. `declared_segment_count` exists because **a dropped leg errors
nowhere else**: you get 1 flight instead of 2 and nothing looks wrong.

## D-13 — Updates are an explicit decision, not an upsert

`UpdateAction`: `create | update | no_change | supersede | conflict | ignore`, driven by
`content_hash` vs `prior_content_hash`.

- **`no_change` is the common case.** Check-in reminders and boarding passes restate a flight
  you already have. Identical hash → nothing written, nothing reviewed.
- **`field_locks` protects human edits.** A JSON pointer corrected in the review UI is
  locked; an incoming update touching it becomes `conflict`, never a silent overwrite.
- **`stale` blocks writes.** A document older than the stored revision cannot produce a
  write. Out-of-order delivery is common with airline schedule changes and is where data
  quietly regresses.
- A cancellation is not a new item. It is `booking_status: cancelled` on an existing
  `canonical_key` with `supersedes` set.

## D-14 — AdventureLog status is per-record, not per-type

One flight writes **two Locations** (departure and arrival airports). A `location: ok`
rollup hides one of them failing.

`commit.adventurelog` is an array of `AdventureLogResult` keyed `location:YYZ`,
`location:LHR`, `transportation`, `collection`. Each has its own state, operation, record_id,
attempts, status code. `depends_on` encodes that transportation/lodging/note require
`collection` — AdventureLog has no orphan path for them.

RULE-21 blocks a per-type rollup being rosier than its worst record.

## D-15 — Keep the original artifact forever (Paperless-ngx pattern)

Store the raw `.eml`, SingleFile HTML, or `.ics` as immutable content-addressed files —
not just parsed jsonb.

- **Reprocessing.** Improve the extractor in month four, re-run against 300 stored originals,
  measure the delta. Without originals you are stuck with what the old parser produced.
- **The fixture corpus becomes free.** Every ingested item is a candidate test case.
- **Debugging is possible at all.** "Why did this parse wrong" requires the input.

Per-item and bulk reprocess actions are required, with an extraction diff view.

## D-16 — SingleFile for web capture; no server-side scraping

SingleFile serializes the fully-rendered page — post-JS, post-login, assets inlined — and
POSTs it. Most travel pages worth capturing are **behind authentication**: manage-my-booking,
hotel reservation detail, ticketing order pages. A server-side fetcher gets a login wall.
The browser already has the session.

Crawl4AI was removed from the plan for this reason.

## D-17 — Ingestion rules and collection hints are data, not code

Borrowed from Paperless-ngx. `source_rules` (label/folder/filter → action) and
`collection_hints` (pattern + date window → collection) as DB tables. Collaborators will use
different Gmail label names, and hints are the pressure valve when the date heuristic fails.

## D-18 — Three review surfaces, not one

- **SQLAdmin** over the models — raw table explorer, ~30 lines of config, internal-only
  behind Authentik ForwardAuth.
- **Purpose-built review queue** — the only hand-designed screen. Split view: rendered
  original left, editable extracted JSON right. Actions: approve, edit-and-approve, reassign
  collection, ignore, escalate-to-cloud, reprocess. Target: triage 10 items in under 2 minutes.
- **Adminer/pgweb** for raw SQL.

Note: Django would give a full DB explorer free via django-admin, which is what Paperless
and AdventureLog both get. FastAPI was chosen anyway (matches existing house pattern, this is
a pipeline not a CRUD app, better async for concurrent outbound calls) — but SQLAdmin is not
as good as django-admin. Known trade.

## D-19 — `commit_attempts` stores verbatim HTTP responses

Status, headers, full body, request payload. Not optional: AirTrail's batch-abort and
AdventureLog's `order` constraint both fail with responses whose meaning is only visible in
the raw body.

## D-20 — Notifications are a digest, not per-record

One Pushover message per run — `3 committed, 1 deferred, 2 need review` — plus immediate
alerts only on hard failures. Per-record alerts get Pushover muted within a week.

## D-21 — Single-tenant now, private repo with a few collaborators

Not public open source. That removes the public issue tracker, docs polish, i18n, and a
security posture for hostile strangers.

Still required for the shared repo (~3-4 sessions): externalized config, working
`docker-compose.yml`, real README, Alembic used properly, stable target-adapter interfaces.

Two risks specific to the multi-contributor model:

**Fixture PII.** The test corpus is the most valuable artifact in the repo and consists of
real booking confirmations. **These cannot be committed.** Build a redaction script producing
sanitized fixtures with deterministic substitution and structure preserved; raw originals
stay gitignored. Synthetic fixtures are worse — real emails contain the edge cases.

**Divergence without CI.** Multiple people running independent Claude Code sessions will
produce silent extraction regressions fast. Non-negotiable: branch protection on `main`, CI
running pytest against the fixture corpus, and **an accuracy metric reported in CI**
("extraction correct on 187/200 fixtures") so a change that fixes one airline while breaking
another is visible in the diff. Without this, three contributors make it worse, not better.

## D-22 — Architectural boundaries with existing systems

- **AdventureLog owns trips and visits permanently.**
- **Dawarich owns the raw GPS timeline.** `RouteDetail.is_estimate` must be `true`
  (RULE-27b) — directions data is planning-time intent, never actuals.
- **AirTrail:** self + guests when self is aboard. Guest-only flights excluded entirely,
  enforced at resolution (RULE-10), not at commit.
- **Photon** for geocoding — already running on `travel-net`, avoids Google Maps quota.
- Missing passenger list → assume self, set `match_source: inferred_absent`, and **notify**.
  RULE-11 enforces that `review.required` is set; the policy is checked, not trusted.

## D-26 — Gmail capture moves between the owner's existing triage labels (owner decision, 2026-08-03, REVISED)

**Superseded same-day revision.** The first version of this decision (add a
single `trip-planning-tracked` label on success, auto-creating it if absent)
was built on an incomplete picture and is wrong — the owner already runs a
three-label manual triage workflow that predates this project:

- **`trip-planning-untracked`** — the owner's intake queue; they label
  candidate emails into this themselves before ever touching the add-on.
- **`trip-planning-tracked`** — destination on a successful capture.
- **`trip-planning-toreview`** — destination on a failed capture.

**All three are pre-existing labels the owner manages. The add-on must never
create any of them** — a missing destination label is a setup problem to
surface loudly (`captureMessage` fails fast, before the network call, if
`trip-planning-tracked` or `trip-planning-toreview` doesn't already exist),
not something to paper over by minting a new one.

Gmail has no real folders, so "moving" a thread means two operations: remove
the old label, add the new one (`moveLabel_` in `Code.gs`). This now happens
on **both** outcomes — untracked → tracked on success, untracked → to-review on
failure — not only on success as the first version had it. Removing
`trip-planning/untracked` is lenient (a no-op if the thread doesn't actually
have it); the destination label is the one that must exist.

**Nesting caveat (same-day correction):** these are nested labels under a
`trip-planning` parent. Gmail's internal name for a nested label is
`Parent/Child` (forward slash) — the sidebar's visual indentation hides this
entirely, so a flat hyphenated guess (`trip-planning-untracked`) matches
nothing even though the label is clearly visible in the UI. The constants use
the slash form (`trip-planning/untracked`, `trip-planning/tracked`,
`trip-planning/to-review`); the "missing label" error additionally lists every
actual label name so a future naming mismatch is self-diagnosing.

**Scope correction (second same-day revision):** the original claim here —
that `gmail.labels` was sufficient and `gmail.modify` could be avoided — was
wrong, discovered via a real "permission failure" on the actual label-move
call. `gmail.labels` only covers managing label *objects* (list/create/read a
label); applying or removing a label from a message/thread
(`addToThread`/`removeFromThread`) requires the broader `gmail.modify` scope —
there is no narrower Gmail Add-on scope that covers label mutation. This is
still a *sensitive*, not *restricted*, scope, so D-03's no-verification
rationale still holds for personal/test-install use — only the CASA/public-app
verification burden was ever the concern, and `gmail.modify` doesn't trigger
that. A labeling failure is still reported independently and never represents
a capture failure: the ingest outcome (success or not) is already durably
recorded server-side by the time the label move runs.

## D-23 — Collection naming on mint (owner decision, 2026-08-03)

`{Primary destination country} — {Month Year}`, e.g. "United Kingdom — September 2026".

- **Month = where the majority of the trip happens**, not the departure month. A trip
  leaving Aug 29 and returning Sep 15 is a *September* trip. Implemented as the month
  containing the midpoint of the trip's date span — equivalent to majority-of-days and
  structurally tie-proof.
- **Primary destination country** at mint time: the arrival country of the minting
  flight; for a multi-leg booking, the turnaround point (the stop with the longest gap
  between legs). Only flights mint (D-04), so a destination airport always exists.
- **Rename on range extension.** A trip minted from an outbound-only booking may be
  named with an incomplete span; when a later item (e.g. the return flight) extends
  the collection's range, the name is recomputed — unless the human has renamed the
  collection, in which case the human edit is locked and wins (same principle as
  D-13 field_locks / RULE-15).

## D-24 — MVP ladder: no hints, no fuzzy matching (owner decision, 2026-08-03)

The resolution ladder is cut to: user choice → exact date containment → mint
(flights only) → defer. Two planned rungs are removed:

- **Fuzzy matching** (±N days + geography): owner chose "never auto-file
  near-misses" — a wrong auto-filing costs more than a review tap. OQ-14's
  window-size question dies with it.
- **Hint rules** (`collection_hints`, Paperless-style): owner assessment —
  too fuzzy, requires maintenance discipline that won't happen, potentially
  disruptive. **Future work**, not MVP. The `collection_hints` table drops out
  of the Phase-0 scaffold; D-17 stands for `source_rules` only.

Deferred TTL is **2 days** (OQ-05): in the real workflow the trip exists before
hotels/trains are booked, so an orphan is a planning mistake worth surfacing
fast, not something to sit on for weeks.

## D-25 — Rules are code; rule *enablement* is data (owner decision, 2026-08-03)

Rule logic lives in `rules.py` (the only place it can), but whether each rule is
in force is **not hardcoded**: every rule has a row in the `rule_settings` DB
table and the engine takes an enablement map at evaluation time.

- **Registry** in `rules.py` is the single source of truth: id, group, summary,
  critical flag, scope. `RULES.md` is *generated* from it (`python3 rules.py
  --docs > RULES.md`) — the docs requirement, with drift made impossible.
- **Tri-state, not boolean:** `enforce` (default) / `audit` (rule runs, records,
  doesn't block — for trialing a disable safely) / `off`. Unknown rule ids or
  modes in the map raise — a typo must not silently change enforcement.
- **`rule_settings` table** (Phase 0): `rule_id PK, mode, note, updated_at`,
  seeded from the registry at startup; new rules appear as `enforce`. SQLAdmin
  over this table is the management UI — a checkbox per rule, for free.
  The admin view **joins the registry at render time** (group, summary,
  critical flag) via `column_formatters`: the table deliberately stores no
  descriptive text (that would duplicate the registry and drift), but a screen
  showing bare rule IDs is unusable — the operator would have to
  cross-reference RULES.md to toggle anything. A row whose `rule_id` has no
  registry entry renders an explicit warning instead of a blank.
- **Critical rules** (the 8 encoding past real-world failures — the TripIt
  temporal group and the dropped-leg check) can still be toggled, but the app
  logs a loud warning when one is not `enforce`.
- **Tests always run all-enforce** against `samples.json`, regardless of any DB
  state: the regression contract is the full ruleset.

## D-10 addendum — Measured deterministic parse rate: ~0% (2026-08-03)

First real measurement against a 16-email corpus captured live via the Gmail
add-on (Air Canada, United, Best Western, Marriott/Cactus Ridge/Limmat House, Hyatt,
Hilton ×2, Amtrak, Club Med, Costco Travel, TicketWeb, Lakeshore Playhouse, AutoShow, plus
2 personal calendar invites): **zero of fourteen real booking confirmations
carry JSON-LD or schema.org markup.** Every one is human-formatted HTML only.

This answers D-10's own question decisively: **extraction is the project, not
a minor component.** JSON-LD stays first in the extractor order (near-zero
cost to attempt, and some vendor might carry it), but `extract/llm.py` is now
confirmed as the PRIMARY mechanism, not a fallback — elevating OQ-08 (which
Ollama model, structured-output reliability) from "worth revisiting" to
next-priority-after-this.

The only structured signal found at all was iCal (`text/calendar`), and only
in 2 of 16 emails — both personal calendar invites, not airline/hotel
bookings (see D-28).

## D-27 — PDF attachments get their own extraction stage (owner decision, 2026-08-03)

Unplanned discovery from the same corpus: 4 of 16 emails (a concert ticket, a
Club Med invoice + terms PDF, an Amtrak eTicket) carry the *actual* structured
booking details in a **PDF attachment**, not the wrapper email body — the
HTML the email client renders is often just a generic "your purchase is
attached" notice.

Adds a stage to D-10's extraction order: **JSON-LD → iCal → PDF text
(new) → maps_url → LLM.** `extract/pdf.py` pulls text from any attachment via
`pypdf`, then that text runs through the SAME deterministic-first pipeline
(try structured parsing on the extracted text, fall through to LLM) rather
than being a wholly separate path. Attachment discovery already works for
free — the Gmail capture stores the full raw MIME (D-15), so the PDF bytes
are already sitting in every existing capture; nothing needs to be
re-captured.

## D-28 — Personal calendar invites are legitimate event items (owner decision, 2026-08-03)

Two real captures ("Daytime Dance Party," "Dinner w/Erin and Danielle") are
personal calendar invites (iCal attachments), not airline/hotel bookings.
Owner decision: these are **not out of scope** — they become ordinary
`item_type: event` TravelItems, resolved into whatever collection matches
their date, same as a ticketed concert or theatre show. Matches how
AdventureLog already models day-to-day trip activities, not just anchor
bookings. No special-case "is this travel-relevant" classification step is
needed — the existing collection-resolution ladder (A6) already handles an
event with no match by deferring, same as anything else.


## D-30 — Review queue: HTMX + Jinja, served by the app itself (owner, 2026-08-03)

Resolves OQ-07. The review queue is D-18's "only hand-designed screen" and is
server-rendered from the existing FastAPI app: no build step, no Node in the
image, no second deploy artifact. SvelteKit would match AdventureLog but adds a
build pipeline for ~3 screens used a few minutes a week. HTMX is vendored
(`app/review/static/`), not loaded from a CDN.

Reached only via the INTERNAL Traefik router; the public router still exposes
`/ingest` and nothing else.

The **admin** shares this identity (owner request): SQLAdmin's packaged
templates are registered under a `_sqladmin` prefix loader so a thin override
can `extend` the original and inject `admin-theme.css` into its `head_tail`
block — no forked templates to drift when SQLAdmin updates. The stylesheet
re-points Tabler's own custom properties at this palette rather than fighting
its selectors, and gives data columns the monospace face for the same reason
the queue does: identifiers and timestamps ARE data.

Design decisions that came out of running it against real data:
- **Rows name the booking, not the record.** The SQLAdmin list showed
  `id`/`canonical_key`/`batch_id` — two UUIDs and a technical key — which is
  correct for a table explorer and useless for triage.
- **Reason codes become sentences.** `review.reasons` is a CLOSED enum in the
  schema (`additionalProperties: false`, no free-text field), so rule
  violations are stored as codes via `rules.REVIEW_REASON_FOR_RULE` and
  rendered as actionable sentences by `app/review/reasons.py`. The exact rule
  text is re-derived on demand in the detail view — it is a pure function of
  the item, so storing it would only create something to drift.
- **"What logging this will write" sits above the actions**, listing SUPPRESSED
  writes explicitly (struck through, with the reason). The system deciding not
  to write your home airport is otherwise invisible, and invisible decisions
  are the ones that erode trust in an automated write.
- **Deferred is its own bucket ("Waiting"), not "Needs you"** — it is waiting by
  design until its TTL (D-24); showing it as blocked would demand action the
  design says not to take yet.

## D-61 — Google Maps directions capture: n8n form → signed /ingest; geocode place names; accept all URL shapes (2026-08-07)

Third capture source. The server side was ~70% built (D-10's `maps_url` extractor,
the pipeline channel, `CaptureContext` with mandatory `travel_date`, the geohash-5
`road_key`), with three real gaps closed here:

1. **Place-name segments now geocode.** `"Toronto, ON"` returned None from
   `geocode_place` (its area tier needs a *structured* city, not a raw string), so
   a road trip had no coords → no `road_key`, no route line, no home suppression.
   `maps_url.py` now splits `"City, Region[, Country]"` stops into city/region
   (skipping street addresses, which the D-59 tier handles) and turns coordinate
   stops into lat/lon. Verified live: Toronto/Port Calloway/Ottawa all resolve.
2. **All three URL shapes accepted:** the `/maps/dir/A/B/C` path, the
   `?api=1&origin=…&destination=…&waypoints=A|B` share format, and
   `maps.app.goo.gl`/`goo.gl/maps` short links (expanded by following the redirect
   — Google hosts only, an SSRF allowlist).
3. **Capture client = an n8n hosted form** (`capture/maps-n8n/`: workflow.json +
   code-node.js + README). Maps has no Apps Script surface, so n8n's Form Trigger
   collects the URL + travel date + optional context, and a Code node HMAC-signs
   (byte-for-byte the `app/auth.py` contract — verified: Node HMAC == server
   `sign()`, and a signed maps POST to live `/ingest` returned 202) and POSTs the
   raw body. n8n here is a *thin capture surface*, not the parsing role D-01/D-02
   removed.

206 tests pass (+5); image rebuilt. Owner still needs to import the workflow,
create a `maps-n8n` key, and run a real drive through review→log.

**First live drive surfaced three more bugs (fixed):**
1. **Coords belong at resolve, not extraction.** Google's formatted addresses are
   often un-geocodable ("100 … Pkwy, … ON Z9Z 9Z9" → nothing from OSM), but it
   embeds each stop's exact coords in the expanded URL (`!1d<lon>!2d<lat>`, route
   order). Setting them on the Place at extraction broke schema validation (the
   extraction Place has no coord fields). Fix: `resolving._apply_maps_coords`
   applies them at resolve from `route.source_url` (and from a bare-coordinate
   stop's own raw).
2. **Those coords must be applied BEFORE keying.** The road key is built from the
   endpoint coordinates, so `_apply_maps_coords` runs right after normalize, not
   at the geocode step (which is after keying) — otherwise every drive is
   unkeyable → needs_review.
3. **The road key read the wrong fields.** `keys.py` took origin/dest from
   `waypoints[0]`/`waypoints[-1]` (requiring ≥2 waypoints), but the endpoints are
   `departure`/`arrival` — waypoints are the *intermediate* stops, so a simple
   A→B drive could never be keyed. Fixed to use departure/arrival (matching the
   booked/scheduled paths, the extractor, and the committer). The old road test
   had enshrined the bug.

Verified live on the real Toronto→Port Calloway drive: key `road:2026-08-14:dpz9t:drcee`,
from/to coords present, origin correctly flagged home. 209 tests pass; image rebuilt.

## D-60 — Commit dedups against EXTERNAL state, not just the DB; Reprocess protects externally-present items (2026-08-06)

A Reprocess deleted the DB rows for 8 flights + 3 lodging that were already in
AirTrail + AdventureLog, then re-extracted them as fresh "un-logged" items. Root
cause: the DB had no valid commit record for them (`_is_logged` → False), so
Reprocess's protection (a batch is spared only if one item `_is_logged`) didn't
apply. Nothing was re-written — caught before "Log approved" — but logging them
would have created **duplicate AdventureLog transportation + lodging**, because
AdventureLog has no server-side dedup (A4 Q6) and the ONLY idempotency guard for
those types was the DB's own commit record.

Two fixes so a lost/rebuilt commit record can never duplicate:
1. **Commit-stage dedup for transportation/lodging/notes.** Before creating one,
   `get_collection` its target trip and match an existing record — transportation
   by route codes + mode (else name+date), lodging by reservation number (else
   name), note by name+date — and skip if found, exactly as flights
   (AirTrail `find_duplicate`) and locations (proximity) already did. Runs in the
   dry preview too, so an already-present record shows as "already in
   AdventureLog" before the operator confirms.
2. **Reprocess protects externally-present items.** Before deleting a "doomed"
   item it checks AirTrail/AdventureLog (best-effort; unreachable target → falls
   back to DB-only protection) and spares that batch — never re-extract a record
   we might re-log into a duplicate.

The 11 orphaned items were reconciled by reconstructing their commit records from
the live external ids (AirTrail 517–524; matched AdventureLog transportation/
lodging ids), so they read as Logged again. 201 tests pass (+2); image rebuilt.

## D-59 — Geocode a place whose whole address is one string (Google Maps / iCal LOCATION) (2026-08-06)

The first real Google Calendar capture (D-58) surfaced a **pre-existing**
geocoder gap: `geocode_place` built its Photon queries only from STRUCTURED
fields (`address`, `city`, `region`, `country`) and required an `address` or a
stated `city` to build any query at all. An event/venue whose entire location is a
single formatted-address string in `raw`/`name` (exactly what Google Maps picks
and iCal `LOCATION` give — "Ed Lakeshore Playhouse Theatre, 244 Victoria St, Toronto, ON M5B
1V8, Canada") produced ZERO queries and returned None, so city/region/country/
address were never filled. The two earlier Gmail-captured invites only had those
fields because the owner **hand-typed them** in review (their `field_locks`
prove it).

Fix (in the shared geocoder, so every channel benefits):
- A new **address tier** queries the whole `raw`/`name` string, but only when it
  `_looks_like_address` (a comma AND a digit — a street number or postal code). A
  bare venue/chain name ("SureStay Madrid", "Starbucks") has neither and is still
  never queried, preserving the wrong-branch guard (D-22's whole reason to exist).
- A street-level hit from that tier is **self-verifying** — a full address the
  user pasted resolving to a specific BUILDING is not the wrong-branch case. A
  non-street hit (park centroid, fuzzy match) stays unverified and is dropped.
- `GeocodeResult`/`apply_geocode` now also carry and fill **region** (state) and
  **address** (housenumber + street), not just city/country.

199 tests pass (+3); image rebuilt. Existing captured items must be **reprocessed**
to pick up the enrichment (geocoding is a resolve-stage step). Not a schema change.

## D-58 — Google Calendar capture: send the Event resource as JSON, map it server-side (2026-08-06)

Second capture source after Gmail (owner's priority order). A Calendar add-on
(`capture/gcal-addon/`) opens a card on any event with a "Capture" button and
POSTs the Google Calendar API **Event resource** as JSON to the same signed
`/ingest` (`capture_channel: 'gcal'`).

**Why JSON, not client-side .ics:** the add-on can't be unit-tested (it runs in
the user's Google account), so the mapping lives in Python — `extract/gcal.py` —
where it is. It emits the SAME event Payload shape `extract/ical.py` produces, so
a Gmail-captured .ics invite and a Calendar-captured event resolve and commit
through identical downstream code. The pipeline gains a small `gcal` channel
branch (deterministic, no LLM). Google Calendar always uses IANA zone names, so
the Windows-name mapping (D-56) is not needed — but tz still passes through the
shared validator so junk can't survive. All-day events stay date-precision (no
fabricated 00:00, D-06); `iCalUID` is the identity anchor (stable across a series
and across Gmail-vs-Calendar capture of the same invite).

The add-on mirrors the Gmail one exactly (User-Properties config, byte-identical
HMAC signing) and is a SEPARATE Apps Script project so it can't disturb the
working Gmail add-on; the README documents merging both into one project later.
196 tests pass (+4); image rebuilt. Not yet installed/exercised in Google
Calendar by the owner.

## D-57 — Commit acts on the revision decision: update PATCHes, cancellation deletes the flight + marks AdventureLog (2026-08-06)

The committer only ever CREATEd (D-44); the D-13 revision ladder already produced
`update`/`supersede`, but the commit stage ignored `revision.action`, so a
changed re-capture (a **delay**, a gate/seat change) would POST a *second* record
and a cancellation did nothing. Closed the loop:

- **`update` → PATCH in place.** Rebuild the body from the current payload and
  update the record the prior item wrote. AdventureLog: `PATCH /api/{type}/{id}/`
  (partial, verified live). AirTrail: `/api/flight/save` is an UPSERT — the same
  body plus the flight `id` (verified in the server build:
  `updateFlightPrimitiveWithConnection`).
- **`supersede` (cancellation) → delete the flight, mark AdventureLog** (owner
  choice, keep the paper trail). AirTrail: `POST /api/flight/delete {id}` (a
  cancelled booking was never flown). AdventureLog: PATCH the name/description
  with a `[CANCELLED]` prefix, never delete.
- **Delays are updates, not cancellations.** The ladder routes
  `booking_status != cancelled` with changed fields to `update`; the existing
  stale (RULE-14) and field-lock (RULE-15) guards already stop an out-of-order
  delay or a hand-corrected time from being clobbered.

**Caution guardrail (owner):** only ever revise/delete a record identified
EXACTLY — AdventureLog by the record id stored at create, AirTrail by the full
flight natural key (carrier+number+date+both endpoints; the API returns no record
id, so this is the only handle, and it is the same exact key used for dedup, not a
fuzzy match). No exact target → an update CREATES A NEW record and a cancellation
SKIPS + flags. We never mutate a vaguely-matched record.

Adapters gained `update_/delete_` for AdventureLog transportation/lodging/note and
AirTrail `update_flight`/`delete_flight`. Locations and collections are never
revised (places are stable, the trip is not re-minted).

**Review UI is revision-aware:** the detail-pane write plan (`write_preview`) and
both commit panels (`_commit_preview` / `_commit_result`) now read "Update …"
(revised in place) and "Cancel flight …" / "Mark cancelled …" instead of the
create-shaped copy, with colour-coded operation badges. Default (create) is
unchanged. 192 tests pass (+6 across commit + review); image rebuilt. Not yet
exercised live end-to-end (dry-run is the default; the first real update/cancel
should be watched).

## D-56 — Calendar TZID: map Windows/Outlook zone names, never drop them (2026-08-06)

Two Outlook/Exchange calendar invites (`TZID=Eastern Standard Time`, the classic
`040000008200E000…` UID) extracted with `tz: null`. `extract/ical.py` had a
deliberate line that DROPPED any TZID not already in IANA `Area/City` form —
which is exactly what every Outlook/Exchange invite sends. Consequence was
silent, not a hard failure: the item reported `extract=ok`, but at commit
`_to_utc_z` cannot build a UTC time without a zone, so `_al_datetime` degrades
the event to a **date-only midnight stub** (A4 Q3 / D-06) — the real 4 pm start
lost, with only the `synthesized_time` flag as evidence.

Fix: `extract/windows_tz.py` maps CLDR Windows names → primary IANA zone
(`Eastern Standard Time → America/New_York`) and `to_iana()` validates ANY
candidate against the tz database via `ZoneInfo`, so:
- **Outlook/Exchange** Windows names now map instead of drop;
- **Google/Apple** IANA names pass through — including multi-segment ones
  (`America/Argentina/Buenos_Aires`), which the old `[A-Za-z_]+/[A-Za-z_+-]+`
  regex also wrongly dropped;
- genuinely unknown TZIDs still resolve to `None` (never a guessed zone), and
  the local wall-clock time is still read.

The parser already ignored the `VTIMEZONE` block (it only keys off `VEVENT`), so
the STANDARD/DAYLIGHT `DTSTART:16010101` stubs and their RRULE never touched the
event date or `gcal_recurring` — regression-tested to keep it that way.

Because the zone is baked into `extraction.payload`, existing items must be
**re-extracted** (Reprocess), not just `resolve --redo` (same as D-42). 186 tests
pass (+4); image rebuilt. Redeploy is the OWNER's in Portainer.

## D-55 — AirTrail /api/flight/save wants code strings + mapped enums, not objects (2026-08-05)

The first REAL AirTrail writes (D-44's object shape had only ever been dry-run
previewed) failed Zod validation on every flight. Read the running AirTrail's
compiled schema (`_server.ts-U1xe-kb0.js`) to get it exactly right:

- **`from`/`to` are code STRINGS, not objects** — the handler resolves them
  `getAirportByIcao(x) ?? getAirportByIata(x)`, so we send `frm.icao or frm.iata`.
- **`airline` is the ICAO code only** (`getAirlineByIcao`), e.g. `ACA`/`AIC` — NOT
  the IATA `AC`/`AI`. Sending the object gave "Set an ICAO code".
- **`seatClass` maps to AirTrail's `SeatClasses` enum**, where premium economy is
  spelled **`economy+`** — our `premium_economy` was the (truncated) 4th error on
  the PE legs. Unmapped/blank → null (the field is nullable), never a bad enum.
- The dup-check read `body["from"].get("iata")`; now that `from` is a string it
  reads the IATA off the payload place instead.
- **Date and time are SEPARATE fields, applied in the airport's own tz.** After
  the string fix, every flight then failed "Cannot have arrival date without
  time": AirTrail wants `departure`/`arrival` as the local DATE plus
  `departureTime`/`arrivalTime` as local HH:MM, and merges them in the resolved
  airport's tz itself (`mergeTimeWithDate`). We were sending a combined UTC 'Z'
  datetime and no time field. Now send local date + local HH:MM
  (`_airtrail_date`/`_airtrail_time`); an arrival date is never sent without its
  time (date-only → both null). AdventureLog transportation is UNCHANGED — it
  still wants explicit UTC 'Z' (D-54). Read straight from the running AirTrail's
  compiled server logic, not guessed.

**Lodging type:** AdventureLog defaulted missing type to "Other". `build_lodging`
now sets `type` — from our `lodging_kind` if extracted (mapping our "camping" →
AL "campground"), else inferred from the property name ("…Resort" → resort), else
**hotel**. (Existing already-logged lodgings keep "Other" — they must be fixed in
place, not re-created.)

180 tests pass. Same deploy: rebuild + Recreate in Portainer.

## D-54 — Commit time & location dedup, recalibrated against the real AdventureLog (2026-08-05)

Two faults found while reviewing a real India/Turks commit, both from the
extracted data lacking things the document never stated, and both verified with
LIVE probes against the actual AdventureLog (7246 Swarm-imported locations).

**Timezone — a timed flight was logging as date-only, and AirTrail rejects an
empty departure.** The extractor emits `local` + `precision=datetime` but almost
never a `tz` (the document rarely states one); for a never-flown airport the tz
is only known at commit from the resolved airport (D-50). `_to_utc_z` /
`_al_datetime` read the zone only off the time object, so both `build_airtrail_
flight` and `build_transportation` collapsed to date-only ("time unknown"). Fix:
both accept a **tz fallback** and pass the resolved airport's tz (`frm.tz`/`to.tz`,
`_airport_tz(airports, iata)`). A timed flight now keeps its real UTC departure.

**Location dedup was silently a no-op and would have created duplicates in the
user's curated 7246-location set.** Three live findings:

- **`/api/locations/?search=<term>` is ignored on this AL version** — it returns
  the full unfiltered list for ANY term. The working endpoint is the dedicated
  **`/api/search/?query=`** (returns a `locations` array). `find_locations_near`
  now uses it, proximity-filters (search relevance is loose — bare `DEL` also
  matches Spanish "del …" 14000 km away), and returns nearest-first.
- **Airports must be queried by IATA, not name.** Swarm airports are named
  "Jomo Kenyatta International Airport (BLR)"; our extracted names ("…Bengaluru",
  "Bole Intl.") don't match, but the parenthesised IATA does.
- **Airports need a ~2 km radius, not 0.5 km.** A Swarm check-in sits at a
  terminal, our geocode at the airport reference point: measured DEL 1.00 km,
  YYZ 1.15 km apart — the old 0.5 km MISSED them. `_existing_location` uses 2 km
  for anything with an IATA, 0.5 km otherwise (so nearby hotels never merge).
  Measured result for the queue: MUC/BLR/DEL/YYZ reuse an existing location,
  PLS (genuinely absent) is created.

**Locations are M2M with collections, so a reused airport is now LINKED to this
trip too** (`set_location_collections`, union of existing + new — never drops a
link; OPTIONS confirmed `collections` is writable). This answers "if I fly through
Munich again in 2028, does the MUC location attach to the new trip?" — yes.

177 tests pass. Same deploy: rebuild `travel-extractor:latest`, Recreate in Portainer.

## D-53 — The pipeline is a named linear stage model; edited ≠ approved (2026-08-05)

Triggered by a real commit that logged **0 writes across 2 items, 4 failed**
while the queue showed **11 Approved**. Three separate faults, one design gap.

**The design gap — "Approved" was lying.** `_bucket_of` filed items with
`review.action_taken ∈ {approved, edited, reassigned}` into the **Approved**
(`staged`) bucket, but `committable()` only ever writes `action_taken ==
"approved"` (D-31). So the 9 flights whose passengers were assigned via the
RULE-11 selector sat under "Approved" with an Approved badge yet were silently
skipped at commit. **Fix: only an explicit approval stages an item.** Editing
or reassigning is a *correction*, not a ruling — it changes the payload that
would be written, so the item returns to **Ready to Approve** for a conscious
yes on the new data. Bucket and committable now agree: everything in Approved
will actually write.

**The stage model, made explicit and named** (`_STAGES`, and a per-item chevron
in the detail header):

    Needs you → Waiting for a trip → Ready to Approve → Approved → Logged

- Not every item visits every stage: a clean flight with a trip enters at
  **Ready to Approve**; a hotel with no trip waits at **Waiting** (D-24). Only
  data/rule problems sit at **Needs you**. **Logged** is always the end;
  **Ignored** is the terminal off-ramp (shown only under Everything).
- **"Ready to log" was renamed "Ready to Approve"** — the old name lied: nothing
  there is ready to *log*, it is ready for the operator's *yes*. The two really
  are consecutive stages, distinguished by *who acted last*: machine-resolved-
  and-awaiting-you (Ready to Approve) vs you-approved-and-awaiting-the-write
  (Approved). Approve and log stay separate clicks (D-31).
- **"Save & approve"**: the primary approve control is now a form SUBMIT, not a
  bare POST — it carries any edit just made (e.g. the passenger selection) and
  approves in one step. The old button silently dropped unsaved edits.

**Two write-path bugs fixed alongside:**

- **Coordinates rejected by AdventureLog (400 on every location/lodging/
  transportation write).** Its DecimalField caps latitude at 6 decimal places
  and longitude at 9 total digits; a raw Photon geocode (`35.5849387` /
  `-106.1024807`) blew the limit. `_coord()` rounds every lat/long to 6 dp
  (~0.11 m, far finer than any address) before it is sent. This is why the
  preview was all-green then execute 400'd — precision is not checked on the
  read-only preview pass.
- **`<built-in method items of dict object at 0x…>` in the commit preview.**
  Jinja resolves `summary.items` attribute-first, finding the dict's `.items`
  method instead of the `"items"` key. Fixed to `summary['items']`. (The
  execute path was unaffected — its count comes from an f-string, not the
  template.)
- **Failures were invisible in the UI.** The job-done panel showed only a count;
  the reasons lived in `commit_attempts`. The panel now lists each failed write
  with its exact reason (jobs carry an optional `details` list).

174 tests pass. Deploy: rebuild `travel-extractor:latest`, Recreate the stack in
Portainer (the secrets live there, not in the repo).

## D-52 — One-product UI: global header, operations strip, shared theme (2026-08-05)

The app was three loosely-linked pages (Review, SQLAdmin, Home profiles) each
hand-rolling its own topbar and (for the admin) its own copy of the palette —
the D-48 UX review's findings. Redesigned into one product with two sections:

- **Global app header** (`_appheader.html`, styled in `theme.css`), rendered
  above every page: `Travel Extractor` wordmark, then **Action** (Review) and
  **Administer** (Home profiles · Travellers · Rules · Tables), plus a light/dark
  toggle. Active item is derived from the request path. It replaces the ad-hoc
  crosslinks in the queue/home templates and SQLAdmin's own top bar (overridden
  via its `topbar` block; the admin still keeps its left sidebar for table nav).
- **Review page split** into an **operations strip** (page title + the pipeline
  funnel + the Process/Reprocess/Log buttons + the job/toast panels — the
  "run something over many items" controls) and the **work area** (bucket rail +
  queue + per-item detail). The two are visually distinct planes, so global
  actions no longer sit amongst the per-item work.
- **Single palette** (`app/review/static/theme.css`) — the colour vars lived
  inline in the review base AND again in `admin-theme.css`. Extracted to one
  served file, linked by both; `admin-theme.css` now re-points Tabler's
  `--tblr-*` at the shared vars (no duplicated hex). The vars flip under BOTH
  `@media(prefers-color-scheme)` and `[data-theme]`, so the header's toggle
  themes the whole app — admin included — with the choice persisted in
  localStorage and applied before first paint (no flash).

Verified in the dev stack, light and dark, across Review / Home / Admin. No
build step, HTMX/Jinja only, HTMX + palette vendored. 171 tests pass.

## D-51 — A journey's trip is named after its destination, not a layover (2026-08-05)

Real-data bug: a 4-leg trip YYZ→MUC→BLR … →YYZ (Toronto to Bangalore via a
Munich layover) grouped correctly into ONE journey (one mint_group, one minter),
but the trip name disagreed leg-to-leg — "Germany — December 2026" on the first
two legs, "India — December 2026" on the last two — so the preview read as two
trips (a Germany trip with a nested India trip).

Root cause: the destination country was derived PER LEG from that leg's own
arrival country. The minter was the earliest leg (YYZ→MUC), whose arrival is
Munich, so it named the whole trip "Germany"; later legs named it "India". A
layover was being mistaken for the destination.

Fix: `_journey_minter` now computes the destination ONCE for the whole journey
and returns it, so every leg derives the same name. The destination is the
non-home airport where the traveller **dwells longest** — the biggest gap
between a leg's arrival and the next leg's departure (`_journey_destination`).
Munich is a ~12h connection; Bangalore is a ~3-week stay, so the trip is named
India. `_leg_facts` now carries the arrival time + country for this. Verified
live: the four India legs all resolve to "India — December 2026", one minter.
(This is naming/planning only — commit already made a single collection via the
one minter + mint_group; the bug was that it was the wrong name and the preview
looked like two trips.) 171 tests pass.

## D-50 — Never-flown airports resolve from AirTrail's own Postgres (2026-08-05)

`/api/flight/save` needs resolved airport OBJECTS (`from.id`/`lat`/`lon`), and
the flown-flight reference (`airtrail.airport_reference()`) only knows airports
already in the history — so a first trip to MUC/BLR/DEL was refused at commit
("AirTrail has no airport on record"). AirTrail ships a COMPLETE `airport` (and
`airline`) table in its Postgres, carrying the id/coords/tz for every airport;
one read resolves any of them.

New optional `adapters/targets/airtrail_db.py` (`AirTrailDB`) does READ-ONLY
lookups against that DB, behind `TX_AIRTRAIL_DB_URL`. `committing._Resolver`
prefers the flown reference (exact, already loaded), then the DB for anything
never flown, caching hits AND misses. This deliberately couples to AirTrail's
schema — the same thing the owner's n8n workflow did (`SELECT … FROM airport
WHERE iata = …`) — but it is read-only and OPTIONAL: with the URL unset the
committer falls back to the flown reference and refuses a never-flown airport
rather than mis-logging it. Verified live: MUC/BLR/DEL/YYZ resolve to their ids,
and the previously-refused MUC→BLR leg now builds a complete flight body.
**New env var `TX_AIRTRAIL_DB_URL`** (in the Portainer stack; must share a
network with the AirTrail db). 169 tests pass.

## D-49 — Long operations run as background jobs with a live progress panel (2026-08-05)

Process / Reprocess / Log were fully synchronous: the route blocked through the
whole LLM pass (minutes for Reprocess) and the only feedback was the button
saying "Working…". They now run as **background jobs** with a self-polling
progress panel (the D-48 UX review's top recommendation).

- **In-memory registry** (`app/review/jobs.py`): a module-level dict guarded by a
  lock, plus a single-worker `ThreadPoolExecutor`. The app runs ONE uvicorn
  worker (no `--workers`), single-tenant, so this needs no DB table or broker.
  **Constraint:** adding uvicorn workers would break it (a poll could hit a
  different process) — keep it single-worker, or promote the registry to a table.
- **Flow:** the route starts a job (the real work runs on a background thread
  with its OWN `SessionLocal`), returns the running job card immediately; the
  card polls `/review/jobs/{id}` every ~1.3s (`hx-trigger="load, every 1300ms"`,
  `hx-swap="outerHTML"`). When the job finishes, the poll returns `_job_done.html`
  — no `every` trigger, so polling stops on its own — plus the SAME out-of-band
  refresh of `#queue-body`/`#funnel`/`#rail` the old synchronous path did.
- **What it shows:** phase (Reprocess's destructive reset is now a visible
  `resetting` phase, not dead air) → `done / total` with a determinate bar → the
  current email/item being worked → a rough ETA (`~40s left · 8 to go`,
  "estimating…" until 2 items complete). Totals are known up front, so a
  `progress(phase, done, total, current)` callback threaded through
  `process_pending` / `resolve_pending` / `commit_pending` is all it took.
- **One job at a time:** the routes guard on `jobs.current_running()` (and the
  executor has one worker), so a second Reprocess can't race the first's
  destructive reset — it just shows the running job.
- The commit PREVIEW stays synchronous (a quick read); only the irreversible
  EXECUTE became a job. 168 tests pass.

## D-48 — n8n parity for the target writes; passengers are chosen, not typed (2026-08-05)

Reverse-engineered the owner's partial n8n workflow (`Travel Extractor.json`) to
confirm the AirTrail + AdventureLog push shapes and check we extract what they
need. Findings and fixes:

- **AirTrail `from`/`to` must be RESOLVED airport OBJECTS** (`{id, lat, lon}`) —
  the `/api/flight/save` handler reads `from.id`/`from.lon`. The n8n workflow
  sent bare IATA strings, which is exactly why its AirTrail push was only
  "partial working". Our commit already sends objects (D-44), so it's correct.
  **Known limitation, now explicit:** the ids come from AirTrail's OWN flown-
  airport reference, so a never-before-flown airport (e.g. MUC/BLR/DEL on a
  first India trip) can't be resolved and the flight is refused at commit with
  a readable reason. A proper AirTrail airport lookup is future work.
- **AirTrail `customFields`** (`{booking_reference, ticket_number}`) — a real
  feature of `/flight/save` (it destructures `customFields`), and where a ticket
  number belongs (no first-class column). We now send it.
- **AdventureLog transportation `description`** ("Booking Ref: … Passengers: …")
  and **`start_timezone`/`end_timezone`** — n8n set both, we set neither; now we
  do (AdventureLog has no fields for ref/passengers otherwise).
- **`ticket_numbers` was being dropped** — the D-29 trap again: optional in the
  grammar, so the model never emitted it. Now required-but-nullable in the flight
  variant, so e-ticket numbers are actually captured. (booking_reference,
  passengers, timezones were already captured.)

**Passengers are chosen from configured Travellers, not typed (owner).** The
flight detail pane now shows a "Who flew" control: a checkbox per traveller
identity, pre-checked when it matches a name the document listed; a document name
matching NO traveller is surfaced with a "Create a traveller" link to
`/admin/traveler-identity/create` (self or guest). Saving writes the chosen
identities as the payload's `[{raw_name}]` and re-resolves, so self/guest — and
therefore whether AirTrail is written (RULE-10/11) — is always a known identity,
never a free-text guess. This also fixes the deadlock where RULE-11 asked "who
flew" with no field to answer it. **Also fixed:** `_reresolve` (the review-save
re-resolution) now passes `identities`, so a guest-only correction actually
excludes AirTrail instead of silently assuming self. 167 tests pass.

## D-47 — Reprocess button, and the write preview names the trip (2026-08-05)

Two gaps surfaced using the fixed pipeline on the real queue:

- **No way to re-run the pipeline over already-processed emails.** "Process new
  captures" only advances NEW (`received`) batches, so a code fix like the
  airport-code grammar change (D-46 §1) never reached the six multi-leg flights
  already extracted under the old grammar. New **Reprocess** button: resets every
  batch that has no LOGGED item back to `received`, drops those non-logged items,
  and re-runs extract + resolve — applying the current extractor/resolver to the
  stored originals (D-15). Destructive (edits/approvals on non-logged items are
  redone from the email), so it is behind a confirm; a batch with ANY logged item
  is left entirely alone so re-extraction can never duplicate a written record.
  Shares one `_run_extract_resolve` with the Process button.

- **The write preview didn't name the trip an item files into.** A matched item
  stored only `collection_id`, not the name, so the preview said "Files into
  existing trip" with no name — the operator couldn't tell WHICH trip, which is
  the entire point of the review. Fix: the resolver now carries the matched
  collection's NAME (it's right there in the CollectionRef), and `write_preview`
  names the trip for any item filing into one it isn't itself minting — a match
  OR an attach leg (D-46 §3). A minting flight still reads "New trip …". Existing
  items show the name once reprocessed/re-resolved. 163 tests pass.

## D-46 — Pipeline-to-commit correctness pass on real data (2026-08-05)

Processing the real queue surfaced four faults, each now fixed and verified
against the live data:

1. **Airport codes were dropped from multi-leg emails.** The first two Air
   Canada mails carry schema.org JSON-LD (D-42); the newer multi-leg ones
   (Toronto–Bangalore, Toronto–Providenciales) do NOT, so they hit the LLM — and
   the codes, printed in the itinerary table ("Toronto YYZ Munich MUC"), were
   dropped. Root cause was NOT the prompt: `place.iata` was an OPTIONAL grammar
   key, and a grammar-constrained model never emits optional keys (the D-29
   lesson, third recurrence). Fix: `iata` joins address/city/country as
   required-but-nullable in `build_format_schema`, plus a prompt example for the
   table format. Re-extraction now yields all four Bangalore legs coded.

2. **Deferred/unresolved items advertised write targets.** `resolve_item` only
   cleared targets for `no_change`; a deferred flight kept transportation/
   location/flight and looked ready to log (it can't — a transportation with no
   collection breaks RULE-08). Now targets are cleared whenever `outcome !=
   "resolved"` (and in the rules-blocking branch).

3. **Legs of one journey each minted their own trip / an outbound deferred
   forever.** Owner chose **resolver picks one minter**: `_journey_minter`
   designates the earliest leg of a closed home-to-home journey as the sole
   minter (collection_action=created + `adventurelog.collection` target); every
   sibling ATTACHES — same `mint_group`, same trip name, NO collection target —
   and the commit stage threads the minter's created collection id to the
   attach legs via `mint_group` (`_resolve_collection_id`), refusing a
   collection-bound write whose trip isn't created yet. RULE-08 exempts attach
   legs the same way it exempts the minter. Verified: AC9471 mints "United
   States — November 2026"; AC9205 attaches to it instead of deferring.

4. **Hotels "approved but wrote nothing".** Not a matching bug — the Phoenix
   collection exists and matches on re-resolve. The items had EMPTY `{}`
   resolution because they were **approved before resolve ran**, and approve
   forced `resolve=ok` without computing targets. Fix: the approve action
   re-resolves an item with no resolution outcome and refuses to stage one that
   still isn't resolved — no more silent "approved, writes nothing".

Also this pass: **`_find_prior` now coerces naive/aware datetimes** before
comparing (Postgres is tz-aware, SQLite is not — a latent crash), and — owner
request — an **Un-approve** action returns a staged (not yet logged) item to
review and re-resolves it, since once approved there was no way back. A LOGGED
item cannot be un-approved. 162 tests pass.

## D-45 — "Log approved" is a preview-then-confirm button; committed items move to a Logged bucket (2026-08-05)

The commit stage (D-44) gets a UI: a **"Log approved…"** button in the review
topbar, beside "Process new captures". It is two clicks by design, because the
write is irreversible and reaches two live systems:

1. First click → **dry-run preview** (POST `/review/commit`): builds the exact
   bodies, resolves airport/airline ids, runs the read-only duplicate checks,
   and lists per item what WILL be written — with any "will fail" (no airport on
   record, no `self` traveller) or "already present" surfaced up front. Nothing
   is written.
2. Confirm → **execute** (same route, `confirm=1`): performs the writes, then
   refreshes the list, counts and rail out-of-band.

A dry-run row is a PLAN, never an outcome, so it renders neutral — an early bug
marked every planned write red because `ok` was derived from an absent HTTP
status. The preview also runs AirTrail's duplicate check (a read) so an
already-logged flight shows as skip before the operator confirms, not after.

**New "Logged" bucket.** A fully-written item (`item.commit` real, every target
ok) leaves "Approved" for "Logged" (`_is_logged`), so "Approved" keeps meaning
"approved, awaiting the write" and "Logged" is the record of what was sent.
`committable()` also excludes fully-logged items, so the button never re-offers
them. Commit stays CLI-capable too (`app.cli commit`); the button and the CLI
share the one `commit_pending`, so they cannot diverge. 160 tests pass.

## D-44 — The commit stage: dry-run by default, verbatim evidence, idempotent (2026-08-04)

`app/committing.py` is the ONLY code that writes to AirTrail and AdventureLog.
It makes no policy decisions — resolve already decided the write set
(`resolution.targets`), the matched-or-minted collection, and self-vs-guest —
it executes them (D-14/D-22). CLI: `python -m app.cli commit [--limit N]
[--execute]`.

- **Dry-run is the default.** `commit` with no flag builds and prints the exact
  request bodies for every target and sends nothing; `--execute` performs the
  writes. The first live write should be one item, watched (`--limit 1
  --execute`). An irreversible two-system write must always be previewable.
- **Only APPROVED items commit (D-31).** `committable()` requires
  `review.action_taken == "approved"` and `status.resolve == "ok"`; resolve
  alone never reaches an external system.
- **Verbatim evidence (D-19).** Each real HTTP write becomes a `CommitAttempt`
  row with the request payload and the raw response body — an AdventureLog/
  AirTrail error's meaning often lives only in its body.
- **Idempotent, two ways.** A target already written (recorded in `item.commit`)
  is skipped on re-run; additionally AirTrail is probed with `find_duplicate`
  and AdventureLog locations with proximity search, because neither dedups
  server-side (A4 Q6) and a double-commit cannot be undone.
- **Dependency order within an item:** collection (mint) → locations → the leaf
  record (transportation/lodging/note) → AirTrail flight. The minted collection
  id threads into its children. Items commit sequentially — one flight at a
  time (D-06), so AirTrail never gets a batch it would abort wholesale.
- **D-06 midnight stub is marked, never hidden (A4 Q3).** A date-only time is
  written as a bare date (AdventureLog coerces to midnight); the commit result
  carries `synthesized_time: true`. AirTrail uses `datePrecision: "day"` and
  simply omits the time — never a synthesized 00:00. Full datetimes are
  converted to explicit UTC "Z" from `local` + the airport tz; a datetime with
  no zone is downgraded to date-only rather than sent as naive local (A4).

**Write shapes were confirmed against the LIVE services, not assumed** — the
running AdventureLog's field set had drifted from the A4-probe version (its
location model, e.g., differs), and AirTrail's flight-create endpoint
(`POST /api/flight/save`, taking resolved `from`/`to`/`airline` objects with
internal ids and a per-seat `userId`) was never probed because A4 only ever read
AirTrail. The airport/airline ids come free from AirTrail's own existing-flight
data (same principle as the airport reference); an airport/airline never flown,
or no configured `self` traveller, refuses the flight with a readable reason
rather than sending it half-formed.

**Still to do:** a UI "Log approved batch" button (commit is CLI-only for now);
the `supersede` path (D-13) currently writes a new record rather than PATCHing
the prior target — updates are not yet wired, only creates.

## D-42 — schema.org JSON-LD is the primary path for airline email, and marketing ≠ operating carrier (2026-08-04)

D-10 measured a ~0% JSON-LD hit rate and concluded the LLM was the primary
extractor. That conclusion was **corpus-specific**: the first corpus happened to
lack structured markup. A real Air Canada e-ticket carries a complete
`<script type="application/ld+json">` `FlightReservation` — airports with
`iataCode`, `departureTime`/`arrivalTime` with offsets, the PNR — i.e. the most
trustworthy source in the system, present and free. It was being thrown away.

Root cause: `jsonld._flight` passed the reservation's `provider` IATA to
`normalize_flight_number`. But schema.org's `provider` is frequently the
**operating** carrier — a regional (Jazz, "QK") flying an "AC9471" that Air
Canada markets — while the flight number carries the **marketing** code. The
mismatch raised `ValueError`, `_flight` returned `None`, and the WHOLE
deterministic extraction was discarded so the item fell to the LLM, which then
under-read the very times and codes sitting in the markup (returning
`precision=date`, no IATA — exactly the D-41 `missing_airport_code` symptom).

Fixes:
- The marketing carrier is derived from the **flight number's own prefix**; the
  reservation `provider` is kept as `operating_carrier` only when it differs.
  The canonical key is built on the marketing carrier, so conflating them would
  also have broken cross-channel collapse (A5).
- `normalize_flight_number` strips a leading carrier code from the number itself,
  so "AC9471" normalises to "8780" even when a different operating carrier is
  supplied.
- The PNR is read from `reservationId` OR `reservationNumber` (Air Canada uses
  the former).

Consequence: airline emails that embed `FlightReservation` now extract
deterministically and exactly — no LLM tokens, no review — and only genuinely
unstructured documents reach the model. The LLM's under-reading of plain text is
real but secondary; tightening its prompt is defense-in-depth, tracked separately,
not the fix for this class of email.

## D-43 — The review datetime field accepts human time formats (2026-08-04)

The correction field required `HH:MM` with a literal colon, so a reviewer typing
`0720` was rejected by the browser and, on the paths where it slipped through,
`_apply_edit` stored `2026-11-20T0720` verbatim — an invalid datetime that every
downstream check then refused, leaving the item stuck. `_normalize_local` now
accepts `07:20`, `0720`, and space-separated forms, normalises to
`YYYY-MM-DDTHH:MM`, keeps a bare date as date-only (D-06 precision), and returns
None for the unparseable rather than poisoning the payload.

## D-40 — On-demand "Process new captures" button; the worker stays deferred (owner, 2026-08-04)

A captured email lands as a `received` ingest batch and, until now, sat there
forever: `process` (batch→item) and `resolve` were CLI-only, so an operator who
sent mail saw a 202 success and then an empty review queue with nothing to
show for it (the exact confusion that surfaced this: two Air Canada e-tickets
ingested fine, `resolve` reported nothing, because `process` had never run).

The owner chose **button now, worker later**: a "Process new captures" action
in the review topbar runs the SAME `process_pending` → `resolve_pending` the
CLI does — never a second, divergent pipeline — synchronously in FastAPI's
threadpool (the operator clicked and is watching the count move). It calls
`resolve_pending(redo=False)`, which only touches `pending` items, so items
already in review and any human decisions are never disturbed. The scheduled
in-app worker (anticipated by D-02 for the deferred sweep) is still NOT built;
this closes the "captures never appear" gap without committing to automatic
processing yet.

## D-41 — A flight with no printed airport code is `missing_airport_code`, not `unknown_type` (2026-08-04)

The extractor deliberately does not guess IATA codes from city names
(`extract/llm.py` — "Never guess codes from city names"), and some documents
(the Air Canada e-tickets above) print only airport NAMES. `flight_key`
legitimately needs a code, so such an item cannot be keyed — but reporting it
as `unknown_type` told the operator a perfectly obvious flight was
unclassifiable. `keys_for` stays boolean-ish (`KeyResult | None`); a new pure
`keys.unkeyable_reason()` picks the ACTIONABLE reason, and the resolver uses it.
The review pane already exposes the airport-code fields as editable and now
flags an empty one as the field to fix; typing the code re-keys the item on
save (D-32) and it leaves the queue. Blaming the code only when the flight is
otherwise keyable (carrier, number, date present) means "enter the code" is
always advice that actually clears the block.

Also fixed here (regression, same session): `TravelerIdentityAdmin` was listed
in the SQLAdmin registration loop but never defined, so `mount_admin` raised
`NameError` at startup; `main.py` swallowed it and `/admin` — the "Tables &
settings" page — rendered as an empty shell with no tables. The view is now
defined (D-35's Travellers screen); `tests/test_review.py` asserts the mount
succeeds and the Travellers list resolves, so the loop and the class definitions
can never drift apart silently again.

## D-37 — Traveller review is flight-only; soft reasons stay approvable (2026-08-04)

Two related fixes from real use:

- **Traveller identity is resolved for FLIGHTS ONLY.** It exists solely to keep
  guest-only flights out of AirTrail (RULE-10); AdventureLog does not track who
  was on a booking. The resolver was running it for every type, so an event
  ("Dinner w/Erin") got a self traveller injected and RULE-11 demanded "confirm
  who travelled" for a dinner invite. Now non-flights get no traveller
  processing at all.
- **A SOFT review reason does not disable Approve.** The assume-self policy is
  "assume AND notify" — the operator glancing and approving IS the
  confirmation. But a flight flagged `missing_passenger_list` had its Approve
  button disabled with "Confirm who travelled" and no field to confirm with:
  a deadlock. `_SOFT_REASONS` marks such codes approvable-as-is; hard reasons
  (missing time, conflict, …) still block until the data is fixed.
- **Clean re-resolution clears stale review state.** When a resolver fix makes
  an old reason moot, the item now resolves to ok AND its stale
  `required`/`reasons` are cleared, so it stops showing "needs you". A human's
  own `action_taken` and field locks are preserved — only machine-generated
  review state is cleared.

## D-39 — Host bind mounts, not named volumes (owner convention, 2026-08-04)

Reverses the Task-5 deployment constraint "Postgres on a named Docker volume,
never a bind mount." The owner's homelab convention is host mounts — easier to
back up and clear. The original constraint's real hazard was Postgres on NFS
(corruption); the target path (`/home/user/docker/travel_extractor/`) is
local ext4, verified at deploy time, so that hazard does not apply. NFS remains
forbidden for the database.

- Artifacts: `…/travel_extractor/Artifacts` → `/data/artifacts`
- Database:  `…/travel_extractor/Database`  → `/var/lib/postgresql/data`, with
  `PGDATA=/var/lib/postgresql/data/pgdata` (a subdirectory) so the mount root
  can hold sibling files — a `lost+found`, or the operator's own backups —
  without initdb refusing a "not empty" data directory.

**Home Profiles editor (2026-08-04).** SQLAdmin's generic forms can't do the
interactive lookups the owner wanted, so home profiles moved to a purpose-built
HTMX page (`/review/home`), and HomeProfile was dropped from SQLAdmin — one
clear place to edit. Kind is a dropdown, not free text. Coordinates are looked
up, not typed:
- **Home airport**: enter the IATA code → verified against AirTrail's own
  airport data (from `/api/flight/list`, authoritative), which fills
  coords/country. A code AirTrail has never seen flown falls back to Photon.
  AirTrail's tRPC airport API was deliberately NOT used — it is superjson-wrapped
  and would couple us to AirTrail internals (D-22 clean seam).
- **Residence/region**: type an address → Photon fills coords/country, editable
  after. Conditional required fields (airport needs a code; residence needs
  coords) enforced server-side.
Added `home_profiles.notes` (migration 0005).

The named-volume `volumes:` block is gone. Switching an EXISTING deployment
means migrating the data out of its old named volume first (a fresh bind mount
would start an empty database) — see the migration note in HANDOFF.

## D-38 — Actions preserve the operator's bucket and refresh counts live (2026-08-04)

Approve/ignore/save used to always return the "attention" list, bouncing the
operator off whatever queue they were viewing, and the pipeline/queue counts
only updated on a manual page refresh. Now every action threads the current
`bucket` and returns it, with the funnel and rail counts re-rendered via HTMX
`hx-swap-oob` so the numbers update in place. Small server-rendered fragments
(`_funnel.html`, `_rail.html`, `_after_action.html`, `_after_save.html`) — no
client state, consistent with the HTMX choice (D-30).

## D-36 — Trust the reverse proxy's forwarded headers (2026-08-03)

Uvicorn honours `X-Forwarded-Proto` only from peers in
`--forwarded-allow-ips`, which defaults to `127.0.0.1`. Behind Traefik the
proxy is a different container, so the app believed every request was plain
HTTP and built `http://` URLs into pages served over HTTPS. Two symptoms, one
cause, and neither points at the real problem:

- the admin rendered COMPLETELY UNSTYLED, because the browser blocked its
  stylesheets as mixed content — easy to misread as "the theming didn't work";
- Firefox warned that saving a form would "send information over an insecure
  connection", because the form action was `http://`.

The container now runs with `--proxy-headers --forwarded-allow-ips` (default
`*`, narrowed via `TX_FORWARDED_ALLOW_IPS`). The service is only reachable
through the proxy, so trusting it is correct.

**Migrations must survive a FRESH database.** Found while testing this: the
0001 baseline builds tables from the CURRENT models via `create_all`, so a new
deployment already has every later column and table, and 0002/0004 failed with
"column already exists" — a collaborator standing up a new stack could not boot
at all, while existing deployments were fine because they had migrated
incrementally. Later migrations now inspect before altering. Test a fresh
database, not only an upgraded one.

**Form controls follow the data, not the storage.** `kind` is a dropdown
("Me — the AirTrail account holder" / "Someone else who travels with me"), not
a free-text box that invites typing an unrelated username into it. Aliases are
one per line rather than hand-written JSON. The two screens also link to each
other; they are halves of one job.

## D-34 — A trip is bounded by home; an open journey never mints (owner, 2026-08-03)

Amends D-04. "Only flights mint collections" was necessary but not sufficient:
**flying away from home is half a journey.** Until a flight brings the traveller
back to a home airport, the system does not know when the trip ends, and a
collection minted from an open-ended departure invents an end date. Every later
leg falling outside that invented range then mints ANOTHER trip for the same
journey — the duplicate-trip failure, arrived at from a different direction.

So minting now requires a CLOSED home-to-home journey. `_trip_envelope` walks
the flight legs chronologically and takes the last home-departure at or before
this leg and the first home-arrival at or after it. Both present => the real
boundaries are known and the collection spans them. Otherwise the flight
DEFERS with "no return flight home yet, so the trip has no known end date",
and waits for the return leg — which routinely arrives in a separate email.

Consequences:
- The mint span is the whole journey, so a middle leg that never touches home
  (IAD→PHX) still resolves into the enclosing trip.
- A genuine one-way (relocation) defers, then escalates to review on the D-24
  TTL. Correct: a one-way move is not a round trip and a human should say what
  it is.
- Supersedes the earlier booking-reference span patch, which widened the range
  but still minted from an unclosed journey.

## D-35 — Traveller identity: who is "self" (owner, 2026-08-03)

AirTrail holds only the account holder's own flight history — a flight booked
for family alone is not the owner's. D-22 and RULE-10 always said so, but
`self_on_booking` was hardcoded `True` in the resolver, so **RULE-10 could
never fire**. Fixed.

`traveler_identities` (migration 0004, managed in `/admin → Travellers`, never
hardcoded — same principle as HomeProfile): exactly one row is `kind="self"`,
carrying the AirTrail user id; everyone else is a guest. AirTrail's own model
matches this — a seat holds either a registered user or a `guestName` string.

Matching (`resolve/identity.py`) compares normalised token SETS, because
airlines print names in shapes no two agree on ("OKAFOR/DANIEL MR", "R OKAFOR",
"Okafor, Daniel"). Word order, punctuation, honorifics and middle names must not
decide identity — but a shared surname must NOT make a relative's ticket look
like yours, so a match requires every token of the shorter name to be present
and at least two real tokens (single initials are ignored entirely).

With no identities configured the resolver falls back to assuming self rather
than excluding everything — a fresh deployment should not silently drop every
flight — and an absent passenger list still means assume-self-AND-NOTIFY
(D-22), with RULE-11 checking that the notify half actually happened.

## D-33 — A collection is a WRITE only when minted (2026-08-03)

`compute_targets` used to add `adventurelog.collection` whenever an item had a
collection at all, so every hotel and event that MATCHED an existing trip
listed "New trip …" in its write preview. The resolver was correct —
`collection_action` was `matched_exact` on all 24 — but the queue was telling
the operator it was about to break D-04. A preview that misreports an
irreversible write is as bad as the write being wrong.

Now `adventurelog.collection` is a target only when
`collection_action == "created"`; a matched item shows "Files into existing
trip X" instead.

**Minting spans the whole booking, not one leg** (`_booking_span`). A trip
minted from an outbound leg alone would cover only that day, so the return
flight — routinely a separate email — would fail date containment and mint a
SECOND trip for the same journey. The span now covers every leg sharing the
booking reference, including legs already stored from earlier captures.
Legs booked under DIFFERENT references still will not group: that needs the
fuzzy matching the owner deliberately cut (D-24), so they go to review instead
of being guessed together.

**`--redo` scope corrected.** It skips only `ignored` items. Edited and
approved items ARE re-resolved: their edits live in the payload and are
protected by field locks, while targets, collection match and rule outcomes are
DERIVED — leaving those stale is exactly the bug this decision fixes.

## D-32 — The review queue EDITS; corrections become field locks (2026-08-03)

The queue's whole purpose is to fix what extraction missed, so every field is
an input, not a label. Submitted values carry the JSON pointer they edit, so
they are written straight back into `extraction.payload`.

- **Times set `precision` from the SHAPE of the entry** (D-06): typing a date
  alone leaves it date-only rather than padding to midnight — the failure the
  DateTimeLocal design exists to prevent. Typing date+time makes it `datetime`.
- **`origin` stays `document`.** The reviewer is transcribing what the original
  says — the split view exists for exactly that. That a HUMAN supplied it is
  recorded separately as a field lock, which is the audit trail that matters.
- **Every edited pointer becomes a `revision.field_locks` entry** (D-13), and
  `resolve_item` now MERGES existing locks rather than overwriting them: a lock
  is the record of a human decision and must survive every later resolver pass,
  or RULE-15 silently stops protecting anything.
- **Saving re-resolves the item immediately**, reusing the same `resolve_item`
  the batch path uses, so the two can never diverge and a fixed item leaves the
  queue on the spot.

**Bug worth remembering (regression-tested in `tests/test_review.py`):** the
first implementation shallow-copied the payload before editing, so the edit
mutated the loaded object in place; SQLAlchemy then compared the reassigned
attribute against an already-identical value, concluded nothing had changed,
and skipped the UPDATE. The response was 200, the rules re-ran and passed, the
item left the queue — and the value never reached the database. JSON columns
here are not mutation-tracked, so edits deep-copy first and `flag_modified`
explicitly. Tests assert against the STORED ROW, never the response.

## D-31 — Approving stages; logging is a separate, explicit act (owner, 2026-08-03)

Approve marks an item ready and writes nothing. A separate action performs the
target writes for the whole approved batch. Rationale: the write is
irreversible and reaches two external systems, so the decision to review and
the decision to commit should not be the same click. `review.action_taken`
(`approved` / `ignored` / …) already models this in the canonical schema.

Consequence for re-resolution: `resolve --redo` never re-decides an item whose
`review.action_taken` is set. A human ruling is not the resolver's to overwrite.

## D-29 — Extraction LLM: qwen3:14b, with a variant-shaped grammar (2026-08-03)

Measured head-to-head on the 16-email real corpus (`scripts/extract_eval.py`):

- **`qwen3:14b` is the extraction model.** 16/16 schema-valid, zero retries,
  correct types/segments/PNRs/statuses/carriers, honors D-06 precision rules.
- **`qwen3:4b-instruct` is disqualified**, not for invalid JSON but for
  *semantically wrong valid JSON*: under grammar constraint it labeled nearly
  everything lodging, including airline emails. The validation gate cannot
  catch plausible-but-wrong; only a capable model prevents it.

Two grammar lessons, both regression-tested (`build_format_schema()`):

1. **Grammar-constrained models omit optional keys.** First seen as 11/14
   items missing their item_type's detail object (llama.cpp grammars cannot
   express the schema's if/then), then AGAIN as every PNR silently dropped.
   Fix: the Ollama `format` schema is an anyOf of per-item_type variants,
   each const-pinning item_type and hard-requiring its detail object, plus
   `booking_reference` required-but-nullable so the model must consciously
   choose. The real schema, unchanged, still enforces everything at the gate.
2. **Prompt v2** additionally instructs: mine the subject/sender header
   (PNRs often live only there), modification notices are `changed`,
   precision must match the local string's shape, carrier from the sending
   airline. Each line traces to a measured miss.

D-11 stands: gate + one retry + manual-only cloud escalation.
