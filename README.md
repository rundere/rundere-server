# Yonder

**Version 4.3.2** · [AGPL-3.0](LICENSE) · [changelog](https://github.com/jeeves5454/yonder/releases) · [releasing](RELEASING.md)

Yonder turns travel documents — booking emails, calendar invites, PDF
confirmations, and Google Maps directions — into clean records in your
self-hosted travel apps: **flights → [AirTrail](https://github.com/johanohly/AirTrail)**,
**trips & places → [AdventureLog](https://github.com/seanmorley15/AdventureLog)** —
and into **Yonder's own built-in trips**, a first-party trip system with its own
timeline, pre-trip research (**Discovery**) and packing plans (**Planning**), that
Yonder writes to alongside the others.

It is a self-hosted FastAPI service. You *capture* travel items from where you
already read them (Gmail, Google Calendar, Maps), Yonder extracts and resolves
the structured facts, you review them in a web queue, and it commits them to
your trackers. Nothing is auto-posted without passing through review. The root
URL (`/`) is a dashboard: what's next, what the review queue looks like right
now, and quick links into every surface.

---

## What it does (and what's covered)

Yonder is a pipeline of five stages:

```
capture  →  /ingest  →  process (extract)  →  resolve  →  review  →  commit
 (Gmail,     (stores      (chain of           (canonical  (web      (writes to
  GCal,       raw,         extractors →        keys, trip  queue,     AirTrail /
  Maps)       signed)      TravelItems)        matching)   HTMX)      AdventureLog)
```

**Capture surfaces:**

| Surface | Channel | Client | How |
|---|---|---|---|
| Gmail | `gmail` | Apps Script add-on | Click "Capture" on an open booking email |
| Google Calendar | `gcal` | Apps Script add-on | Click "Capture" on an open event |
| Google Maps directions | `maps_url` | Built-in `/capture` | Paste a directions link + travel date |
| Web page | `web` | Built-in `/capture` + bookmarklet | Click a bookmarklet on any booking page, or paste its source/URL |

**Extractor chain** (`extract/`, tried in order — D-10/D-27):
`jsonld → ical → pdf → maps_url → llm`. Structured sources (schema.org JSON-LD
in booking emails, `.ics` invites, Google's Maps URL formats) are parsed
deterministically; free-form text falls through to a **local LLM** (Ollama,
D-11) — nothing is sent to a cloud model.

**Item types recognised:** `flight`, `hotel`, `lodging`, `event`,
`ground_transport`, `car_rental`, `train`, `ferry`.

**Resolution & safety:**
- **Canonical keys** (`resolve/keys.py`, spec [`docs/A5-canonical-keys.md`](docs/A5-canonical-keys.md)) dedupe re-captures of the same booking.
- **Collection ladder** (`resolve/collections.py`, spec [`docs/A6-collection-resolution.md`](docs/A6-collection-resolution.md)) attaches items to the right AdventureLog trip.
- **Home profiles** (effective-dated, D-07) decide which places never count as "visited" and which local drives aren't trips.
- **57 rules** (`rules.py`) with per-rule `enforce` / `audit` / `off` toggles gate what commits and what goes to review.
- **Content-addressed originals** (D-15): the raw captured artifact is stored immutably so reprocessing never loses information.

---

## Yonder's own Trips

Since v2.0, Yonder isn't just a pipe to AirTrail/AdventureLog — trips are also a
first-party feature at **`/trips`**, with their own day-by-day timeline
(flights, lodging, drives, notes — added by hand or linked from a capture),
a traveller roster (self + guests, per-trip), and two capabilities layered on
top:

- **Discovery** (v3.0–3.2) — a self-contained, local pre-trip research pass over
  nine topics (weather, currency + a per-trip calculator, plugs & voltage,
  visa & vaccination, safety & transport, events & activities, food & drink,
  language, packing). A **deterministic tier** (Frankfurter for currency,
  Open-Meteo for weather, a plugs/language reference) needs no extra service;
  an optional **LLM tier** (visa, safety, events, food, and regional/local
  language) runs entirely locally through **SearXNG** web search + your
  **Ollama** model — nothing leaves your network. Findings are reviewed,
  editable, and re-runnable per topic. A **Discovery Profile**
  (`/trips/settings/discovery-profile`) captures dietary needs, drink/activity
  interests, budget and mobility constraints so the LLM tier searches for
  things you'd actually want.
- **Planning** (v3.1) — turns the trip + its Discovery findings into a
  **Packing Plan**: a bag-assigned, categorized checklist derived from trip
  length, climate, and detected trip types (beach, business, hiking, …), tuned
  by a **Packing Profile** (`/trips/settings/packing-profile` — bag size,
  laundry comfort, style, must-have items). Pack/unpack/omit items inline; a
  removed item can always be brought back.
- **AdventureLog managed sync** (v3.8) — once a Yonder trip is pushed to
  AdventureLog as a collection, `/trips/{id}/adventurelog` is a two-way,
  dedup-aware sync surface: **push** selected timeline activities and approved
  Discovery findings into that collection (ownership markers keep re-pushing
  idempotent — updates in place, never duplicates), or **pull** AdventureLog's
  own transportation/lodging records into the Yonder timeline.

Trips work with or without AdventureLog/AirTrail configured — captured items
can link straight to a Yonder trip (independent of any AdventureLog
collection), so the in-house trip system is never a second-class citizen.

---

## Prerequisites

### Hard prerequisites (Yonder will not run without these)

| Requirement | Why | Notes |
|---|---|---|
| **Docker Engine + Compose** | Runs the app and its Postgres | Any orchestrator works — a reference stack is provided |
| **Docker network reachability to your tools** | Yonder talks to the reverse proxy and to each target/geocoder it calls over Docker networking | Put the container on whatever existing Docker network(s) those services use — one or several. The stack *joins* external networks, it doesn't create them (`docker network create` any that are missing). The reference stack's `traefik-net` + `travel-net` split is just this deployment's shape, not a requirement |
| **PostgreSQL 16** | App database | Bundled in the stack as `yonder-db` — nothing to install separately |
| **A reverse proxy with public HTTPS** | The Gmail/GCal add-ons POST from Google's servers to `/ingest` | Reference uses **Traefik**. The public route exposes **only** `POST /ingest` |
| **A Google account** | For the Gmail & Calendar capture add-ons | Personal, unpublished Apps Script — no Google review needed |

### Soft prerequisites (optional — degrade gracefully)

| Requirement | Unlocks | Without it |
|---|---|---|
| **AdventureLog and/or AirTrail** (external targets) | Also pushing out — flights → AirTrail, trips/places → AdventureLog | Yonder still records everything to its **own built-in trips** (the default, first-party target); the external pushes simply don't happen |
| **[Ollama](https://ollama.com/)** + a model (`qwen3:14b`) | LLM extractor for free-form text | Structured sources (JSON-LD/iCal/PDF/Maps) still work; unstructured items go to review unparsed |
| **[Photon](https://github.com/komoot/photon)** (self-hosted geocoder) | Address → coordinates without Google quota (D-22) | Geocoding unavailable; places needing lookup stall in review |
| **AirTrail's own Postgres** (read-only) | Committing flights to airports you've **never flown before** (D-50) — supplies internal airport id/coords/tz | A flight to a never-flown airport is refused at commit |
| **[SearXNG](https://github.com/searxng/searxng)** (self-hosted metasearch) + Ollama | Discovery's **LLM tier** — visa, safety, events, food & drink, and regional/local language findings | Discovery still runs its deterministic tier (weather/currency/plugs/language basics); the four search-backed topics are simply absent |
| **CrowdSec bouncer** | Hardens the public `/ingest` route | Route works; less abuse protection |

Discovery's deterministic tier also calls two free, keyless public APIs —
[Frankfurter](https://frankfurter.dev/) (currency) and
[Open-Meteo](https://open-meteo.com/) (weather) — over the open internet by
default; both are overridable (`TX_FRANKFURTER_URL`, `TX_OPEN_METEO_URL`,
`TX_OPEN_METEO_ARCHIVE_URL`) if you'd rather point at a self-hosted mirror to
keep Yonder fully local.

### How to find each prerequisite / credential

- **Docker network reachability** — Yonder must share a Docker network with your reverse proxy and with each target/tool it calls (AdventureLog, AirTrail, Photon, Ollama). Put it on whatever external network(s) those already use — check with `docker network ls`; the stack joins them, so `docker network create <name>` any that are missing. Whether that's one network or several (the reference stack uses `traefik-net` + `travel-net`) is up to your setup.
- **AdventureLog API key** (`TX_ADVENTURELOG_API_KEY`) — in the AdventureLog web UI → **Settings → Integrations / API tokens** → generate a token.
- **AirTrail token** (`TX_AIRTRAIL_TOKEN`) — in the AirTrail UI → your **account / API settings** → create an API token.
- **AirTrail DB URL** (`TX_AIRTRAIL_DB_URL`) — read the Postgres user/password/db from AirTrail's own compose file; it looks like
  `postgresql://airtrail:<password>@airtrail_db:5432/airtrail`. Yonder's container must share a Docker network with that DB.
- **Ollama** — `docker`-run Ollama on `travel-net`/`traefik-net`, then `ollama pull qwen3:14b`. Point `TX_OLLAMA_URL` at it.
- **Photon** — self-host the Photon container with an OpenStreetMap index; point `TX_PHOTON_URL` at it.
- **SearXNG** — self-host with its JSON API enabled (`search.formats: [html, json]` in `settings.yml`); point `TX_SEARXNG_URL` at it. Needed only for Discovery's LLM-tier topics (visa/safety/events/food/regional language) — it also requires Ollama.
- **Yonder `/ingest` API keys** — you create these yourself with the CLI (see [First-run setup](#3-first-run-setup)); one per capture client.

---

## Install

### Development (local)

```bash
pip install -e .[dev]
pytest                                  # all suites; the 35-case corpus in tests/samples.json is the contract
python -m app.cli create-key my-client  # prints an /ingest API key once
uvicorn app.main:app --reload           # sqlite by default; set TX_DATABASE_URL to override
```

Full dev stack (app + Postgres on port 8100):

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

### Production (Docker)

**1. Get the image.** CI builds and publishes it to GHCR on every `vX.Y.Z` tag
([`.github/workflows/release.yml`](.github/workflows/release.yml)), so you pull a
versioned artifact rather than building locally:

```bash
docker pull ghcr.io/jeeves5454/yonder:latest   # or a pinned :3.8.1
```

The package is private; give Portainer a registry credential (Registries → Add →
Custom: `ghcr.io`, your GitHub username, a PAT with `read:packages`). To build
locally instead, `docker build -t yonder:latest .` still works.

**2. Deploy the stack.** [`portainer-stack.yml`](portainer-stack.yml) is a
reference Compose/stack definition — use it with `docker compose`, Portainer,
or any orchestrator you prefer. Set the two `CHANGE_ME_DB_PASSWORD` occurrences,
and fill in the target env vars (`TX_ADVENTURELOG_*`, `TX_AIRTRAIL_*`, etc.) with
the credentials from the section above. The reference stack declares env vars
inline (some orchestrators can't interpolate `.env` files); adapt to your setup.

**Two routes, split by exposure** (the reference stack wires these with Traefik
labels — adapt to your proxy):
- A **LAN/private** route to the **full app** (`/admin`, the review queue, the CLI-backed UI).
- A **public HTTPS** route exposing **only `POST /ingest`**, for the Apps Script add-ons — they POST from Google's servers, which can't do an interactive proxy login, so this route authenticates purely with Yonder's own API keys + HMAC. Keep the rest of the app off the public route.

> **Container names in commands below.** The examples use `yonder` as
> the app container name. Yours will match your deployment — e.g. a Compose
> project in a `yonder/` folder yields `yonder-yonder-1`.
> Substitute your actual container name (`docker ps`), or use
> `docker compose exec yonder …`.

### 3. First-run setup

**Home profiles** decide which places are never logged as "visited" and which
local drives aren't trips. Manage them in the admin UI (**`/admin → Home
Profiles`**, full CRUD with per-field help), or bootstrap from a file:

```bash
# copy docs/home-profiles.example.json, fill it in, then:
docker exec yonder python -m app.cli import-home /data/home-profiles.json
```

**Create an API key for each Google add-on** (Gmail, Calendar):

```bash
docker exec -it yonder python -m app.cli create-key gmail-addon
docker exec -it yonder python -m app.cli create-key gcal-addon
```

Each prints its key **once** — paste it into the matching add-on below. (Web
pages and Maps directions use the built-in `/capture` page and need no key.)

---

## Setting up the capture surfaces

Each surface has a detailed README in `capture/`. Condensed steps follow.

### Gmail add-on (Apps Script)

Full guide: [`capture/gmail-addon/README.md`](capture/gmail-addon/README.md).

1. **script.google.com → New project.**
2. Project Settings (⚙️) → check **"Show appsscript.json"** → paste [`capture/gmail-addon/appsscript.json`](capture/gmail-addon/appsscript.json).
3. Replace `Code.gs` with [`capture/gmail-addon/Code.gs`](capture/gmail-addon/Code.gs).
4. **Deploy → Test deployments → Install add-on** (installs for your account only; approve the "unverified app" screen — expected for your own script). Scopes: `gmail.addons.current.message.readonly` + `gmail.modify` (label moves).
5. In Gmail, open the add-on panel → paste the **`gmail-addon`** API key on the homepage card → **Save**.

**Label prerequisite (D-26):** three nested Gmail labels must already exist —
`trip-planning/untracked` (intake queue), `trip-planning/tracked` (success),
`trip-planning/toreview` (failure). Gmail's internal name uses a **forward
slash** for nesting. The add-on moves threads between them but never creates
them.

### Google Calendar add-on (Apps Script)

Full guide: [`capture/gcal-addon/README.md`](capture/gcal-addon/README.md).

Identical flow to Gmail, with Calendar scopes:

1. **script.google.com → New project** (or merge into the Gmail project — see that README).
2. Show `appsscript.json` → paste [`capture/gcal-addon/appsscript.json`](capture/gcal-addon/appsscript.json). It enables the **Advanced Calendar Service** (needed to read timezone / iCalUID / attendees / recurrence).
3. Replace `Code.gs` with [`capture/gcal-addon/Code.gs`](capture/gcal-addon/Code.gs).
4. **Deploy → Test deployments → Install add-on.** Scopes: `calendar.addons.current.event.read`, `calendar.readonly`, `script.external_request`.
5. In Google Calendar, open the add-on panel → paste the **`gcal-addon`** API key → **Save** (a distinct key from Gmail keeps ingest logs readable; reusing one also works).

### Web pages & Google Maps directions (built-in `/capture`, no external client)

Yonder serves its own capture page at **`/capture`** on the private app — no add-on, no n8n, no API key or HMAC. Because this page lives on the private router, it is authenticated by whatever you put in front of that router (a reverse-proxy auth layer, SSO, mTLS, or a trusted LAN); if your proxy forwards an identity header (`X-authentik-username`, `X-Forwarded-User`, …) Yonder records it as the submitter.

Three ways to use it:
- **Bookmarklet** (primary, for web pages) — the `/capture` page offers a bookmarklet to drag to your bookmarks bar. Click it on any booking page — including one behind a login — and it POSTs the page's rendered HTML to Yonder. This is the reliable path: your browser has already loaded and authenticated the page. If a strict-CSP page blocks the direct send, it copies the HTML to your clipboard to paste instead.
- **Paste / URL** — paste a page's HTML source, or a **public** URL Yonder fetches itself (SSRF-guarded: only public hosts; login-walled pages can't be fetched, so use the bookmarklet for those).
- **Google Maps directions** — paste a directions link (`maps.app.goo.gl`, `/maps/dir/…`, or `?api=1&origin=…&destination=…`) and set a **Travel date** (a route has no date of its own — RULE-31). Yonder routes it to the road-trip extractor automatically.

Extraction reuses the JSON-LD reader for web pages (most booking pages embed schema.org), falling back to the LLM for pages without markup. Keep the `/capture` route on the **private** router only — never expose it publicly.

> **The n8n Maps form is retired** (v1.2). It POSTed to `/ingest` with an HMAC key; the native `/capture` page does the same job internally with no key. The old workflow still lives in [`capture/maps-n8n/`](capture/maps-n8n/) for reference.

---

## Daily workflow

0. **Open the dashboard** — the app's root URL (`/`, e.g. `https://yonder.home.example/`)
   is a home page: the trip coming up next, a live snapshot of the review
   pipeline (needs you / waiting / ready / approved / logged), today's capture
   activity, and quick links into Capture, Review, Trips, and every settings
   page. It's the front door — start here.
1. **Capture** — click the add-on button on an email/event, or use `/capture` for a web page or Maps drive. The item is stored immediately and never processed inline.
2. **Process** — extract stored batches into `TravelItem`s:
   ```bash
   docker exec yonder python -m app.cli process
   ```
   (or **Process new captures** in the review queue UI).
3. **Resolve** — assign canonical keys and trip collections:
   ```bash
   docker exec yonder python -m app.cli resolve
   ```
4. **Review** — open the review queue (your LAN/private app URL), check the extracted items, correct anything, approve.
5. **Commit** — write approved items to AirTrail / AdventureLog:
   ```bash
   docker exec yonder python -m app.cli commit
   ```
6. **Trips (optional)** — open `/trips` to see the timeline take shape, run
   **Discovery** for a pre-trip briefing, generate a **Packing Plan**, and (if
   the trip is linked to AdventureLog) sync activities and findings both ways
   from `/trips/{id}/adventurelog`.

---

## `/ingest` authentication

Per-client revocable API keys (created via CLI, stored sha256-hashed) plus a
signed timestamp:

```
X-Api-Key:   tx_<hex>
X-Timestamp: <unix seconds>          # rejected outside a 5-minute window
X-Signature: HMAC_SHA256(key, "<timestamp>." + body) as hex
```

Returns `202 {"batch_id": ...}`; the batch is stored and content-addressed
(D-15) for async processing. Rejections are explicit: `401` (bad key/signature/
skew), `413` (body over `TX_INGEST_MAX_BODY_BYTES`), `429` (rate-limited).

---

## Configuration reference

All config is env vars, prefix **`TX_`** (`app/config.py`). Set them in your
deployment stack.

| Variable | Default | Purpose |
|---|---|---|
| `TX_DATABASE_URL` | `sqlite:///./yonder_dev.sqlite3` | App DB (Postgres in prod) |
| `TX_ARTIFACTS_DIR` | `/data/artifacts` | Immutable captured originals (D-15) |
| `TX_INGEST_MAX_SKEW_SECONDS` | `300` | Signed-timestamp replay window |
| `TX_INGEST_RATE_LIMIT` | `30` | Requests per key per window |
| `TX_INGEST_RATE_WINDOW_SECONDS` | `300` | Rate-limit window |
| `TX_INGEST_MAX_BODY_BYTES` | `5000000` | Max `/ingest` body (5 MB) |
| `TX_OLLAMA_URL` | `http://ollama:11434` | Local LLM extractor endpoint |
| `TX_OLLAMA_MODEL` | `qwen3:14b` | LLM model (OQ-08) |
| `TX_LLM_TIMEOUT_SECONDS` | `600` | LLM call timeout |
| `TX_ADVENTURELOG_URL` | `http://adventurelog-server:80` | AdventureLog base URL (internal, container-to-container) |
| `TX_ADVENTURELOG_API_KEY` | `""` | AdventureLog API token |
| `TX_ADVENTURELOG_PUBLIC_URL` | `""` | AdventureLog's browser-facing base URL, used to link directly to a trip's collection from the Yonder UI. Empty = no link rendered |
| `TX_AIRTRAIL_URL` | `http://airtrail:3000` | AirTrail base URL |
| `TX_AIRTRAIL_TOKEN` | `""` | AirTrail API token |
| `TX_AIRTRAIL_DB_URL` | `""` | AirTrail Postgres, read-only (D-50) |
| `TX_PHOTON_URL` | `http://photon:2322` | Self-hosted geocoder (D-22) |
| `TX_DISCOVERY_BASE_CURRENCIES` | `USD` | Always-on base currencies Discovery's resolver/calculator convert FROM (D-65); unioned with each traveller's own travel-profile pick, so these stay visible (e.g. USD for a closed-currency destination) alongside a per-traveller currency. Defaults to USD when unset; set to your home currencies, e.g. `CAD,USD` |
| `TX_FRANKFURTER_URL` | `https://api.frankfurter.dev/v2` | Currency-rate source for Discovery (public, keyless; overridable for a self-hosted mirror) |
| `TX_OPEN_METEO_URL` | `https://api.open-meteo.com/v1` | Forecast weather source for Discovery |
| `TX_OPEN_METEO_ARCHIVE_URL` | `https://archive-api.open-meteo.com/v1` | Historical weather source for Discovery (trips beyond the forecast window) |
| `TX_SEARXNG_URL` | `http://searxng:8080` | Metasearch backend for Discovery's LLM tier (visa/safety/events/food/regional language). Empty = those topics degrade to absent findings |
| `TX_FORWARDED_ALLOW_IPS` | `*` | Trust Traefik's `X-Forwarded-Proto` |

| `TX_PUBLIC_BASE_URL` | `""` | Public https base (e.g. `https://yonder.example.net`) for email links and the OAuth/MCP metadata. Set it when using the Claude connector |
| `TX_OAUTH_ACCESS_TTL_SECONDS` | `3600` | OAuth access-token lifetime (Claude connector) |
| `TX_OAUTH_REFRESH_IDLE_TTL_SECONDS` | `7776000` | A connected app's grant expires after this long unused (90 days) |

### Single sign-on (OIDC, e.g. Authentik)

| Var | Default | Purpose |
|---|---|---|
| `TX_OIDC_ENABLED` | `false` | Turn SSO on (also needs issuer + client id) |
| `TX_OIDC_ISSUER` | `""` | Issuer URL; `/.well-known/openid-configuration` lives under it. Authentik: `https://auth.example.net/application/o/<slug>/` |
| `TX_OIDC_CLIENT_ID` / `TX_OIDC_CLIENT_SECRET` | `""` | From the IdP's OAuth2/OpenID provider |
| `TX_OIDC_DISPLAY_NAME` | `SSO` | Button label: "Sign in with …" |
| `TX_OIDC_ADMIN_GROUP` | `""` | A `groups` claim value that makes the user an admin (upgrade only) |
| `TX_OIDC_AUTO_CREATE` | `true` | Create an account on first SSO sign-in. An existing account with the same username is adopted |
| `TX_OIDC_HOSTS` | `""` | Hosts that offer "Sign in with SSO" (comma-separated). Empty = every host |
| `TX_OIDC_FORCE_HOSTS` | `""` | SSO-only hosts: `/login` goes straight to the IdP; password sign-in, sign-up and "forgot password" are refused there |
| `TX_OIDC_FORCE_API_LOGIN` | `false` | Also refuse the app's password sign-in on forced hosts. Leave off while the iOS app signs in with a password |
| `TX_OIDC_REDIRECT_URL` | `""` | Fixed callback URL. Leave empty when using the host lists — each listed host then uses `https://<host>/oidc/callback` |

**Force SSO on the public name, keep the LAN as the fallback.** One Authentik redirect URI, and a way in when Authentik is down:

```
TX_OIDC_HOSTS=yonder.example.net
TX_OIDC_FORCE_HOSTS=yonder.example.net
```

`yonder.example.net` always signs in through Authentik (redirect URI `https://yonder.example.net/oidc/callback`); `yonder.home.example` shows only the username/password form. To offer SSO on the LAN too, leave `TX_OIDC_HOSTS` empty and add the LAN callback as a second redirect URI in Authentik. Emailed password-reset links (built from `TX_PUBLIC_BASE_URL`) still open on a forced host; the new password is used on the LAN.

---

## Use Yonder from Claude (remote MCP)

Yonder serves an MCP endpoint at `/mcp` and is its own OAuth server. Add
`https://<yonder-host>/mcp` as a custom connector in claude.ai to read and edit
your trips, timeline, packing and journal, capture links, and save Claude's
research to your Discover tab. Everything runs as you and is scoped like the web
app. It's opt-in per user: nothing reaches Claude unless you connect it (the
capture pipeline itself still uses only the local LLM). See
[`docs/claude-connector.md`](docs/claude-connector.md). Connected apps and
personal API tokens are managed on the Account page.

---

## Rules

Rule logic lives in `rules.py`; whether each rule is in force lives in the
`rule_settings` table (`enforce` / `audit` / `off`), seeded at startup and
editable in SQLAdmin. `RULES.md` is **generated**:

```bash
python3 rules.py --docs > RULES.md
```

---

## Project layout

```
app/            FastAPI service
  dashboard.py    the root ("/") dashboard — next trip, pipeline snapshot,
                  capture activity, quick links (v3.8.1)
  review/         the review queue (HTMX + Jinja), Home Profiles editor
  trips/          Yonder's own trips: timeline, travellers, Discovery,
                  Planning, AdventureLog managed sync
  main.py         router wiring: /health, /ingest, /capture, /admin (SQLAdmin)
  cli.py          operational CLI: create-key, process, resolve, commit, ...
  processing.py, resolving.py, committing.py   the pipeline stages
extract/        extractor chain, in try-order (D-10/D-27):
                jsonld -> ical -> pdf -> maps_url -> web -> llm (Ollama, D-11)
resolve/        keys.py (A5 canonical keys), collections.py (A6 ladder)
discover/       Discovery (v3.0-3.2): deterministic + LLM resolvers per topic,
                location derivation, the durable run/finding pipeline
planning/       Planning (v3.1): the Packing Plan engine, catalog, profiles
adapters/       target writers: adventurelog.py, airtrail.py, airtrail_db.py
rules.py        rule registry + engine (57 rules, tri-state toggles — D-25)
validation.py   schema gate (real jsonschema)
schemas/        travel-item (canonical) + travel-extraction (derived, D-09)
alembic/        migrations (baseline 0001)
capture/        gmail-addon/, gcal-addon/ (Apps Script); maps-n8n/ (deprecated,
                superseded by the built-in /capture page in v1.2)
tests/          pytest suite + samples.json regression corpus
docs/           A5/A6 specs, shared-capabilities guideline (D-96),
                home-profiles.example.json, the setup/usage guide
scripts/        extract_eval.py — run the chain over dumped real payloads
```

The design specs (`docs/A5-canonical-keys.md`, `docs/A6-collection-resolution.md`,
`SPEC-3-discovery.md`, `SPEC-3.1-planning.md`, `SPEC-3.2-discovery-llm.md`,
`SPEC-4-directions-capture.md`) and the generated `RULES.md` explain the
resolution, Discovery/Planning, and rule behaviour in depth.

**Engineering guideline:** `docs/shared-capabilities.md` — build capabilities as
common project-level functions and treat a channel/provider's needs as a
subsection of that shared capability, not a rewrite. Review it before adding a new
capability or channel (D-89/D-96).

Inline `D-NN` / `RULE-NN` / `OQ-NN` codes throughout the code and capture docs
are provenance markers for decisions, rules, and open questions tracked in the
author's local working notes (not part of this repo).

---

## License

[GNU Affero General Public License v3.0](LICENSE). Yonder talks to AdventureLog
and AirTrail over their APIs — it does not bundle their code — but AGPL keeps
Yonder and any network-hosted derivative open.
