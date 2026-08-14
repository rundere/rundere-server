# Yonder 3.0 — Discovery (in-house trip research pipeline)

Status: **PLANNED** (spec). Supersedes the MCP work as the 3.0 headline; MCP moves
to 4.0 (direct DB access makes MCP a thin win — see the roadmap). Discovery will,
after significant testing, become the **primary** replacement for the external
`adventurelog-trip-prep` Claude skill; that skill stays as a **backup** until
Yonder proves out.

## Goal

Give a Yonder Trip a self-contained, **local** research pipeline that produces a
pre-travel briefing (the same nine topics the trip-prep skill covers) plus a
currency calculator — with **no Claude in the loop**. Research runs
asynchronously, quality-paced (generous timeouts, not time-boxed), holds its
output **on Yonder** for a human to review / edit / approve, and only then pushes
to AdventureLog Notes via the existing push path.

The whole design rests on one principle carried over from the pipeline: **the
local LLM is not a supervised fact source here, so its role is shrunk until it is
safe to run unattended.** Most high-stakes topics are made deterministic; the LLM
only survives for genuinely fresh, low-stakes, or well-sourced content, always
grounded and always flagged.

## Why this is cheap (already in the codebase)

- **Grammar-constrained LLM output already exists.** `extract/llm.py`
  `OllamaExtractor` calls `/api/chat` with a JSON-schema `format` grammar and
  `temperature: 0`, `num_ctx: 16384`, `timeout: 600`. Every Discovery LLM topic
  reuses this exact pattern with a per-topic schema — reliable structured output
  for free.
- **Verified geocoding already exists.** `resolve/geocode.py` `PhotonGeocoder`
  turns a place into `{lat, lon, country, city, region}` and **rejects hits that
  contradict the document** (the wrong-branch guard). This is how Discovery
  derives a trip's locations without inventing a "Location" concept.
- **An async job runner already exists.** `app/review/jobs.py` gives
  `progress(phase, done, total, current)`, a single-worker executor, and a
  self-polling HTMX panel (`_job_panel.html`). Discovery reuses the *shape* and
  changes only the backing store (durable, below).
- **The AdventureLog push path already exists.** `app/trips/routes.py`
  `push_adventurelog` / `_al_body_for_activity` / `_resolve_coords` already write
  collection objects and resolve coordinates locally (AirTrail airport ref →
  Photon, never Google — [[geocoding-stays-local]]). Notes-writing follows the
  same client.
- **Local models already configured.** `TX_OLLAMA_URL`, `TX_PHOTON_URL` in
  config; `qwen3:14b` in the stack. Discovery adds only `TX_SEARXNG_URL`.

## 1. First-party location derivation (the crux)

Yonder has no "Location" table and does **not** read locations from
AdventureLog — that would violate first-party ownership. Instead the location set
is a pure function of a trip's own `TripActivity.values`, which already name
places (see `app/trips/types.py`):

| Activity type | Place-bearing fields |
|---|---|
| flight | `from`, `to` (IATA) |
| train | `from`, `to` |
| lodging | `address`, `city` |
| dining | `address` |
| tour | `location` |
| event | `venue` |
| carRental | `pickupLocation`, `dropoffLocation` |
| cruise | `departurePort`, `arrivalPort` |
| drive | `route` (best-effort) |

**Algorithm (`discover/places.py`):**

1. Walk the trip's activities; collect place strings per the table above.
2. Geocode each via the existing path — IATA through AirTrail's airport
   reference, everything else through `PhotonGeocoder.geocode_place()` (verified
   only; unverified places are dropped, not guessed).
3. Dedup into a **destination set** of `{country (ISO-2), city, lat, lon}`.
4. Tier the set:
   - **country-level** (dedup by ISO country) → visa, currency, plugs, language,
     safety
   - **city-level** (dedup by city) → weather, events, food

Empty set (e.g. a trip with only a `note` activity) → the Discovery tab says "add
an activity with a location first" rather than researching nothing. This is
entirely first-party; AdventureLog is never consulted.

**Derived-locations viewer (D-68).** The operator must be able to *see* what
Yonder derived, never guess at the pipeline's starting point. The trip detail
gets a **"What Yonder found" control** — a button opening a modal (or an
expandable section), **not shown by default**. It renders the destination set as
a **tag list**: one chip per derived place, showing `city, country` with the
resolved coordinates and, on the chip, **which activity(ies) and field it came
from** (e.g. "from lodging · city") and its scope (country/city). Places that were
*dropped* as unverified are listed separately ("couldn't place: <raw string>") so
a missing destination is visible, not silent. This viewer reads the same
`discover/places.py` output the run will use, so it doubles as a pre-flight check
before launching Discovery.

## 2. Data model (durable findings)

Findings must be reviewed, edited, and **held on Yonder** before push, so they are
DB rows, not in-memory — following the JSON-envelope philosophy of `TravelItem`.
One Alembic migration (**0010**; head is 0009).

```
DiscoveryRun
  id            str  pk
  trip_id       str  fk trips.id (index)
  status        str  running | done | failed | cancelled
  phase         str  free text ("weather · Geneva")
  done, total   int  progress counters
  params        json topic set + place-set snapshot at launch
  error         str|null
  started_at, finished_at

DiscoveryFinding
  id            str  pk
  run_id        str  fk discovery_runs.id
  trip_id       str  fk trips.id (index)  -- denormalized for the trip view
  topic         str  weather|currency|visa|plugs|events|safety|food|language|packing
  scope         str  country | city
  place_key     str  "US" | "US:Glendale"        -- idempotency handle w/ topic
  status        str  draft | approved | edited | rejected
  body_md       text rendered Note text (what pushes to AdventureLog)
  data          json structured payload (rates, weather series) for rich UI
  sources       json [{url, title}]
  confidence    str  high | medium | flagged
  resolver      str  which resolver produced it
  ttl_seconds   int  cache lifetime hint
  generated_at
  pushed_at, adventurelog_note_id   -- set on push
```

`(trip_id, topic, place_key)` is the upsert key — a re-run updates a finding in
place instead of duplicating (mirrors `TripActivity.source_item_key`, D-* re-commit
dedup). `data` lets a finding carry structured content (the currency calculator's
rates, a weather series) alongside its rendered `body_md`.

## 3. Resolver registry

New package `discover/` beside `resolve/`. One uniform interface:

```python
def resolve(place: Place, dates: Dates, profile: HomeProfile) -> Finding
    # returns body_md, data, sources, confidence, ttl_seconds
```

The static/dynamic line is drawn **per field, not per topic** (see language).

| Topic | Tier | Backing | LLM? |
|---|---|---|---|
| Plugs / voltage | static dataset | `discover/data/plugs.json` | no |
| Weather | deterministic API | Open-Meteo (no key) | no |
| Currency | deterministic API | Frankfurter/ECB + calculator | no |
| Packing | rule engine | weather + duration + activity types | no |
| Language phrases | **hybrid** | CLDR/Babel + vetted static + grounded overlay | partial |
| Visa / vaccination | searxng + llm, **flagged** | SearXNG → Ollama | yes |
| Safety / transport | searxng + llm, corroborated | SearXNG → Ollama | yes |
| Events | searxng + llm | SearXNG → Ollama | yes |
| Food & drink | searxng + llm | SearXNG → Ollama | yes |

Five of nine never touch the LLM. That is the safety argument for unattended runs.

## 4. Currency resolver + trip calculator

Mirrors the itinerary-planner's currency capability, but the rates live in Yonder's
DB instead of baked into an HTML file.

**Resolver (`discover/resolvers/currency.py`), deterministic:**

- Country → currency code via **CLDR/Babel** offline
  (`babel.numbers.get_territory_currencies('CH')` → `['CHF']`) — same offline
  reference family as the language L1 layer.
- Rates from **Frankfurter (ECB-backed)**: fetch `base=CAD` and `base=USD` → each
  trip currency. No LLM. Store the snapshot in `DiscoveryFinding.data`:
  ```json
  { "date": "2026-08-14",
    "rates": { "CHF": { "per_CAD": 0.63, "per_USD": 0.86 },
               "EUR": { "per_CAD": 0.67, "per_USD": 0.92 } },
    "usd_useful": false }
  ```
- `body_md` renders the itinerary-planner format: `1 CAD = X CHF`, `1 USD = X CHF`,
  with the date checked.
- **`usd_useful`** mirrors the skill's Step 4 rule — flag USD only when it is the
  official/widely-accepted or standard-exchange currency at the destination.
  Decided as a **single flagged LLM call** (D-70) — this is a judgement question,
  not a search-extraction one, sanity-checked by a quick lookup only when needed —
  kept out of the otherwise fully-deterministic rate math. When uncertain it omits
  USD and adds the skill's caveat ("confirm whether USD is useful before travel").
  CAD always shown (except domestic Canadian trips).

**Calculator (per-trip, opened from the currency finding — "Open calculator"):**

- HTMX modal / panel on the trip detail, styled like the existing activity modal.
- Controls exactly per the itinerary planner: **amount input · From dropdown · To
  dropdown · live result · swap (⇄)**. Dropdowns populated with **every currency
  in the trip** + **CAD** + **USD** (USD included only when `usd_useful`).
- Compute **client-side from the stored rates** — no live API call on open (the
  finding's snapshot is the source, matching the skill's "static file" model). For
  local↔local on multi-country trips, **bridge through CAD**.
- Footer note: *"Rates as of {date} — for reference only, rates fluctuate."*
- A **"Refresh rates"** action re-runs the currency resolver (re-hits Frankfurter,
  updates the finding snapshot + the calculator). Rates otherwise use the TTL
  cache like every other finding.

This keeps the calculator deterministic and offline-at-open, and gives the trip a
persistent converter without ever depending on AdventureLog.

## 5. Language phrases (layered — country ≠ language ≠ register)

Language is the one topic where "static per country" is wrong on two axes:
country ≠ language (CH = de/fr/it/rm), and language ≠ register (CH-German greets
"Grüezi", not "Guten Tag"). So the resolver is three layers, each sourced
differently:

- **L1 — which language(s):** country → official languages from **CLDR via Babel**
  (`get_official_languages('CH')`), offline. Narrow by the **region Photon already
  returns** (Zurich→de, Geneva→fr) rather than dumping all four — **region-from-
  Photon is the MVP granularity (D-69)**; no bespoke canton/state→language map in
  v1. If a region can't be mapped, present the country's official languages.
- **L2 — stable skeleton (vetted static):** the ~15–20 structurally-stable,
  high-stakes phrases (please, thank you, numbers, "how much", "where is…", "do
  you speak English"). **No clean per-country phrasebook API exists at travel
  quality** — seed from **Wikivoyage phrasebooks (CC)** per *language*, then
  human-vet. Stored keyed by language, not country. This layer must be *right*, so
  it is never LLM-generated.
- **L3 — regional register (searxng + llm, flagged):** the greetings/politeness
  band that genuinely varies (Grüezi / Servus / Grüß Gott). Narrow LLM job:
  *"Does {region} diverge from standard {language}? Give the local form and cite
  a source."* Wikipedia/Wikivoyage source this well. Rendered as a clearly-marked
  "Local flavor" section on top of the trustworthy skeleton, reviewer-approved.

**Hard rule:** pronunciation / transliteration (romaji, Arabic/Thai romanization,
phonetic respelling) is where local LLMs hallucinate worst — it lives only in the
vetted L2; any L3 pronunciation is marked unverified.

## 6. Durable async job (the one deliberate deviation)

`app/review/jobs.py` is the right *shape* but is in-memory and explicitly
disposable (its docstring: a restart orphaning a job is fine because the work is
seconds long). Discovery runs minutes-to-hours and its output must survive a
restart. So: **keep the pattern, change the backing store.**

- The `DiscoveryRun` row **is** the job state. The background thread writes
  `phase/done/total` to the row; the poll panel reads the row.
- A restart mid-run → next poll marks the row `failed` (clean resume story:
  operator re-runs), instead of the job silently vanishing.
- Quality-paced by construction: per-resolver LLM timeout (already 600s) × topics,
  **no overall wall clock**.
- One run per trip at a time (guard on an existing `running` row for the trip),
  reusing the single-worker executor.

## 7. LLM grounding (replaces "Claude reviews it")

- **Schema-constrained generation** — reuse `OllamaExtractor`'s `format` grammar;
  each searxng topic gets a small schema `{ body_md, items:[{claim, source_url}] }`.
- **Grounding check** — drop any `item` whose `source_url` was **not** in that
  topic's SearXNG result set. Mechanical; kills invented facts, no judgment.
- **Corroboration** — visa/safety require N≥2 sources or render under a visible
  `⚠️ Verify at official source` banner baked into `body_md`.
- `temperature: 0` throughout.

## 8. Containers / config

- **SearXNG** — new service on the existing external `travel-net` network,
  reachable as `http://searxng:8080` whether it lives in the Yonder stack or its
  own. Enable JSON output. Add `TX_SEARXNG_URL` (matches `TX_OLLAMA_URL` /
  `TX_PHOTON_URL` convention).
- **Ollama, Photon** — reused as-is.
- **Perplexica** — **out of v1** (duplicates SearXNG+Ollama, hides the prompt/
  schema needed for grounding). Reconsider later only as a flagged "explore
  deeper" resolver producing drafts a human must approve.
- **Weather / currency** — external HTTP APIs, no container. Self-host Frankfurter
  later for zero external calls.
- Nothing is forked.

## 9. Review UI + push

- **Discovery tab on the trip detail page.** "Run discovery" → durable job panel
  (reuse `_job_panel.html`) → findings grouped by topic, each with **approve /
  edit / reject** (HTMX, like the review queue). The currency finding also offers
  **Open calculator**. The trip detail also carries the **"What Yonder found"**
  derived-locations viewer (§1).
- **Steerable single-topic re-run (D-71).** Each finding has a **"Re-run this
  topic"** action — reviewers iterate one topic without re-running the whole trip.
  It optionally takes a free-text **correction/steer note** ("Geneva is
  French-speaking, not German"; "ignore the 2019 result") that is passed into that
  resolver's prompt for the re-run. This is a direct concession to local models
  going off the rails: the human can *correct and re-aim* a single topic cheaply
  rather than accept or discard a bad finding wholesale. The re-run upserts the
  same `(trip_id, topic, place_key)` finding in place; the reviewer's steer note is
  recorded on the finding for provenance.
- **Push.** Approved findings → AdventureLog Notes via the existing push path,
  keyed idempotently by topic exactly as the trip-prep skill does, so re-push
  updates in place (consistent with the "no reconciliation engine" stance —
  [[migration-is-local]]).

## 10. Decisions (new)

- **D-62** — A trip's research locations are **derived first-party** from
  `TripActivity.values` via the existing verified-geocode path; Yonder gains no
  "Location" concept and never reads locations from AdventureLog.
- **D-63** — Discovery findings are **durable DB rows** (`DiscoveryRun` /
  `DiscoveryFinding`), reviewed/edited/approved on Yonder before any push. The
  in-memory `jobs.py` registry is *not* reused for run state (its restart-orphan
  contract is wrong for long runs).
- **D-64** — The static/dynamic split is **per field, not per topic**. Language
  splits into L1 CLDR/Babel + L2 vetted static (Wikivoyage-seeded) + L3
  grounded/flagged overlay; pronunciation stays L2-only.
- **D-65** — Currency rates are **deterministic** (Frankfurter/ECB, CAD+USD
  bases), snapshotted into the finding; the trip **calculator computes
  client-side from the stored snapshot** (bridge local↔local through CAD),
  mirroring the itinerary-planner converter. USD shown only when `usd_useful`.
- **D-66** — Unattended LLM output is admissible only when **schema-constrained +
  source-grounded**; high-stakes topics (visa/safety) additionally require
  corroboration or a visible verify banner. Perplexica excluded from v1.
- **D-67** — Discovery is 3.0 and becomes the primary engine after testing; the
  `adventurelog-trip-prep` skill is the backup until then. MCP → 4.0.
- **D-68** — A **derived-locations viewer** ("What Yonder found", on-demand modal/
  expansion, tag-list) exposes the destination set + provenance + dropped places,
  so the operator sees the pipeline's starting point rather than guessing.
- **D-69** — Language L1 city refinement uses the **region Photon already
  returns**; no bespoke canton/state→language map in the MVP.
- **D-70** — `usd_useful` is a **single flagged LLM (judgement) call**, isolated
  from the deterministic rate math.
- **D-71** — Reviewers can trigger **steerable single-topic re-runs** with an
  optional correction note fed into the resolver prompt; the note is recorded on
  the finding.

## 11. Phasing

1. **Deterministic spine (first slice).** Location derivation (`discover/places.py`)
   + `DiscoveryRun`/`DiscoveryFinding` migration + static datasets (plugs) +
   deterministic APIs (weather, currency) + the **currency calculator**. Fully
   testable with **zero LLM variance**; proves the first-party location model and
   ships a visibly useful feature (the calculator) before any search enters.
2. **Rule + hybrid.** Packing rule engine; language L1/L2 (offline); durable job
   panel + Discovery tab + review/approve UI; push-to-AdventureLog for approved
   findings.
3. **Grounded LLM topics.** SearXNG service + client; visa, safety, events, food;
   language L3 overlay; grounding + corroboration guards.
4. **Harden.** TTL caching per topic; re-run/refresh; confidence surfacing;
   idempotent re-push.

## 12. Open questions

*All three original OQs resolved 2026-08-14 → D-69 (region-from-Photon is enough
for MVP), D-70 (`usd_useful` = single flagged LLM call), D-71 (steerable
single-topic re-runs, in v1). None currently open.*
