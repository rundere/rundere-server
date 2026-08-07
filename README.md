# Yonder

**Version 1.0** · [AGPL-3.0](LICENSE)

Yonder turns travel documents — booking emails, calendar invites, PDF
confirmations, and Google Maps directions — into clean records in your
self-hosted travel apps: **flights → [AirTrail](https://github.com/johanohly/AirTrail)**,
**trips & places → [AdventureLog](https://github.com/seanmorley15/AdventureLog)**.

It is a self-hosted FastAPI service. You *capture* travel items from where you
already read them (Gmail, Google Calendar, Maps), Yonder extracts and resolves
the structured facts, you review them in a web queue, and it commits them to
your trackers. Nothing is auto-posted without passing through review.

> **A note on the name.** The project is **Yonder**, but the running artifacts
> — Docker image, service/container names, package, and LAN hostnames — are
> still `travel-extractor` / `travel_extractor`. That rename is a separate stack
> migration; every command below uses the **real, currently-deployed** names so
> they work as-is. See [Renaming to Yonder](#appendix-renaming-the-stack-to-yonder).

---

## What it does (and what's covered)

Yonder is a pipeline of five stages:

```
capture  →  /ingest  →  process (extract)  →  resolve  →  review  →  commit
 (Gmail,     (stores      (chain of           (canonical  (web      (writes to
  GCal,       raw,         extractors →        keys, trip  queue,     AirTrail /
  Maps)       signed)      TravelItems)        matching)   HTMX)      AdventureLog)
```

**Capture surfaces (v1.0):**

| Surface | Channel | Client | How |
|---|---|---|---|
| Gmail | `gmail` | Apps Script add-on | Click "Capture" on an open booking email |
| Google Calendar | `gcal` | Apps Script add-on | Click "Capture" on an open event |
| Google Maps directions | `maps_url` | n8n hosted form | Paste a directions link + travel date |

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
- **148 rules** (`rules.py`) with per-rule `enforce` / `audit` / `off` toggles gate what commits and what goes to review.
- **Content-addressed originals** (D-15): the raw captured artifact is stored immutably so reprocessing never loses information.

---

## Prerequisites

### Hard prerequisites (Yonder will not run without these)

| Requirement | Why | Notes |
|---|---|---|
| **Docker Engine + Compose** | Runs the app and its Postgres | Any orchestrator works — a reference stack is provided |
| **Two external Docker networks** | The stack joins them, doesn't create them | `traefik-net` (proxy) and `travel-net` (targets/geocode). Create with `docker network create` if absent |
| **PostgreSQL 16** | App database | Bundled in the stack as `travel-extractor-db` — nothing to install separately |
| **A reverse proxy with public HTTPS** | The Gmail/GCal add-ons POST from Google's servers to `/ingest` | Reference uses **Traefik**. The public route exposes **only** `POST /ingest` |
| **At least one target** | Somewhere to commit to | **AdventureLog** (trips/places) and/or **AirTrail** (flights) |
| **Python 3.12** | *Dev only* — running tests / uvicorn locally | Not needed for the Docker deployment |
| **A Google account** | For the Gmail & Calendar capture add-ons | Personal, unpublished Apps Script — no Google review needed |

### Soft prerequisites (optional — degrade gracefully)

| Requirement | Unlocks | Without it |
|---|---|---|
| **[Ollama](https://ollama.com/)** + a model (`qwen3:14b`) | LLM extractor for free-form text | Structured sources (JSON-LD/iCal/PDF/Maps) still work; unstructured items go to review unparsed |
| **[Photon](https://github.com/komoot/photon)** (self-hosted geocoder) | Address → coordinates without Google quota (D-22) | Geocoding unavailable; places needing lookup stall in review |
| **AirTrail's own Postgres** (read-only) | Committing flights to airports you've **never flown before** (D-50) — supplies internal airport id/coords/tz | A flight to a never-flown airport is refused at commit |
| **[n8n](https://n8n.io/)** | The Google Maps directions capture form | Gmail + Calendar capture still work |
| **CrowdSec bouncer** | Hardens the public `/ingest` route | Route works; less abuse protection |

### How to find each prerequisite / credential

- **`traefik-net` / `travel-net` networks** — likely already exist if you run other homelab services behind Traefik. Check with `docker network ls`; create any missing one with `docker network create <name>`.
- **AdventureLog API key** (`TX_ADVENTURELOG_API_KEY`) — in the AdventureLog web UI → **Settings → Integrations / API tokens** → generate a token.
- **AirTrail token** (`TX_AIRTRAIL_TOKEN`) — in the AirTrail UI → your **account / API settings** → create an API token.
- **AirTrail DB URL** (`TX_AIRTRAIL_DB_URL`) — read the Postgres user/password/db from AirTrail's own compose file; it looks like
  `postgresql://airtrail:<password>@airtrail_db:5432/airtrail`. Yonder's container must share a Docker network with that DB.
- **Ollama** — `docker`-run Ollama on `travel-net`/`traefik-net`, then `ollama pull qwen3:14b`. Point `TX_OLLAMA_URL` at it.
- **Photon** — self-host the Photon container with an OpenStreetMap index; point `TX_PHOTON_URL` at it.
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

**1. Build the image:**

```bash
docker build -t travel-extractor:latest .
```

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

> **Container names in commands below.** The examples use `travel-extractor` as
> the app container name. Yours will match your deployment — e.g. a Compose
> project in a `travel_extractor/` folder yields `travel_extractor-travel-extractor-1`.
> Substitute your actual container name (`docker ps`), or use
> `docker compose exec travel-extractor …`.

### 3. First-run setup

**Home profiles** decide which places are never logged as "visited" and which
local drives aren't trips. Manage them in the admin UI (**`/admin → Home
Profiles`**, full CRUD with per-field help), or bootstrap from a file:

```bash
# copy docs/home-profiles.example.json, fill it in, then:
docker exec travel-extractor python -m app.cli import-home /data/home-profiles.json
```

**Create an API key per capture client** (Gmail, Calendar, Maps):

```bash
docker exec -it travel-extractor python -m app.cli create-key gmail-addon
docker exec -it travel-extractor python -m app.cli create-key gcal-addon
docker exec -it travel-extractor python -m app.cli create-key maps-n8n
```

Each prints its key **once** — paste it into the matching capture client below.

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

### Google Maps directions (n8n workflow)

Full guide: [`capture/maps-n8n/README.md`](capture/maps-n8n/README.md).

Maps has no Apps Script surface, so capture is a small n8n hosted form.

1. Create the key: `create-key maps-n8n` (above).
2. In n8n: **Workflows → Import from File →** [`capture/maps-n8n/workflow.json`](capture/maps-n8n/workflow.json). It builds four nodes: `Trip form` → `Config` → `Sign & build` → `POST to /ingest`.
3. Edit the **`Config`** node only:
   - `apiKey` → the `maps-n8n` key.
   - `ingestUrl` → `http://travel-extractor:8000/ingest` if n8n shares the app's Docker network; otherwise your public `https://<your-ingest-host>/ingest`.
4. **Activate** the workflow → open the Form Trigger's **Production URL** on desktop or phone.

The `Sign & build` node mirrors [`capture/maps-n8n/code-node.js`](capture/maps-n8n/code-node.js); it signs the request identically to `app/auth.py` and sends the **raw** signed string — don't change the HTTP node's body type away from *Raw*, or the signature breaks. **Travel date is required** (a directions link has no date — RULE-31).

---

## Daily workflow

1. **Capture** — click the add-on button on an email/event, or submit the Maps form. The item lands on `/ingest` (stored, returns `202` immediately; never processed inline).
2. **Process** — extract stored batches into `TravelItem`s:
   ```bash
   docker exec travel-extractor python -m app.cli process
   ```
   (or **Process new captures** in the review queue UI).
3. **Resolve** — assign canonical keys and trip collections:
   ```bash
   docker exec travel-extractor python -m app.cli resolve
   ```
4. **Review** — open the review queue (your LAN/private app URL), check the extracted items, correct anything, approve.
5. **Commit** — write approved items to AirTrail / AdventureLog:
   ```bash
   docker exec travel-extractor python -m app.cli commit
   ```

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
| `TX_DATABASE_URL` | `sqlite:///./travel_extractor_dev.sqlite3` | App DB (Postgres in prod) |
| `TX_ARTIFACTS_DIR` | `/data/artifacts` | Immutable captured originals (D-15) |
| `TX_INGEST_MAX_SKEW_SECONDS` | `300` | Signed-timestamp replay window |
| `TX_INGEST_RATE_LIMIT` | `30` | Requests per key per window |
| `TX_INGEST_RATE_WINDOW_SECONDS` | `300` | Rate-limit window |
| `TX_INGEST_MAX_BODY_BYTES` | `5000000` | Max `/ingest` body (5 MB) |
| `TX_OLLAMA_URL` | `http://ollama:11434` | Local LLM extractor endpoint |
| `TX_OLLAMA_MODEL` | `qwen3:14b` | LLM model (OQ-08) |
| `TX_LLM_TIMEOUT_SECONDS` | `600` | LLM call timeout |
| `TX_ADVENTURELOG_URL` | `http://adventurelog-server:80` | AdventureLog base URL |
| `TX_ADVENTURELOG_API_KEY` | `""` | AdventureLog API token |
| `TX_AIRTRAIL_URL` | `http://airtrail:3000` | AirTrail base URL |
| `TX_AIRTRAIL_TOKEN` | `""` | AirTrail API token |
| `TX_AIRTRAIL_DB_URL` | `""` | AirTrail Postgres, read-only (D-50) |
| `TX_PHOTON_URL` | `http://photon:2322` | Self-hosted geocoder (D-22) |
| `TX_FORWARDED_ALLOW_IPS` | `*` | Trust Traefik's `X-Forwarded-Proto` |

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
app/            FastAPI service: /health, /ingest, /admin (SQLAdmin), review queue,
                cli.py, processing.py (extract stage: batches -> TravelItems)
extract/        extractor chain, in try-order (D-10/D-27):
                jsonld -> ical -> pdf -> maps_url -> llm (Ollama, D-11)
resolve/        keys.py (A5 canonical keys), collections.py (A6 ladder)
adapters/       target writers: adventurelog.py, airtrail.py, airtrail_db.py
rules.py        rule registry + engine (148 rules, tri-state toggles — D-25)
validation.py   schema gate (real jsonschema)
schemas/        travel-item (canonical) + travel-extraction (derived, D-09)
alembic/        migrations (baseline 0001)
capture/        gmail-addon/, gcal-addon/ (Apps Script), maps-n8n/ (n8n form)
tests/          pytest suite + samples.json regression corpus
docs/           A5/A6 specs, home-profiles.example.json
scripts/        extract_eval.py — run the chain over dumped real payloads
```

For deeper context: `HANDOFF.md` (status + tasks), `DECISIONS.md` (locked
decisions — read before changing anything), `RULES.md` (generated).

---

## Appendix: renaming the stack to Yonder

The prose says "Yonder"; the artifacts still say `travel-extractor`. A full
rename (do it as one deliberate migration, not piecemeal) touches:

- **Image**: `travel-extractor:latest` → `yonder:latest` (rebuild, update stack).
- **Service/container names** in the deployment stack (`travel-extractor`, `travel-extractor-db`) and every `docker exec travel-extractor …` command.
- **Package name** in `pyproject.toml` and `[tool.setuptools]`.
- **Hostnames / DNS** for your LAN and public routes (proxy labels + DNS).
- **Bind-mount paths** and the Postgres data dir if you rename the folder.
- The `create-key` client names are cosmetic and can stay.

Until then, every command in this README is correct as written.

---

## License

[GNU Affero General Public License v3.0](LICENSE). Yonder talks to AdventureLog
and AirTrail over their APIs — it does not bundle their code — but AGPL keeps
Yonder and any network-hosted derivative open.
