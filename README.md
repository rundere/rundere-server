# travel-extractor

Turns travel documents (booking emails, calendar invites, web captures, Google
Maps directions) into records in **AirTrail** (flight history) and
**AdventureLog** (trips). Self-hosted FastAPI service; Phase 0 scaffold.

Start with `HANDOFF.md` (status + tasks), `DECISIONS.md` (locked decisions —
read before changing anything), `RULES.md` (generated — see below).

## Layout

```
app/            FastAPI service: /health, /ingest, /admin (SQLAdmin), CLI,
                processing.py (extract stage: batches -> TravelItems)
extract/        the extractor chain, in try-order (D-10/D-27):
                jsonld -> ical -> pdf -> maps_url -> llm (Ollama, D-11)
resolve/        keys.py (A5 canonical keys), collections.py (A6 ladder)
rules.py        rule registry + engine (57 rules, tri-state toggles — D-25)
validation.py   schema gate (real jsonschema)
schemas/        travel-item (canonical) + travel-extraction (derived, D-09)
alembic/        migrations (baseline 0001)
capture/        Gmail add-on (Apps Script, D-03/D-26)
tests/          pytest suite + samples.json regression corpus + legacy harness
docs/           A5/A6 specs
scripts/        extract_eval.py — run the chain over dumped real payloads
```

Process captured batches (extract stage):

```bash
docker exec travel_extractor-travel-extractor-1 python -m app.cli process
```

## Home profiles (first-run setup)

Home is effective-dated config (D-07): it decides which places are never
written to AdventureLog as "visited," and which local drives don't become
trips. **Manage it in the admin UI — `/admin → Home Profiles`** (full CRUD,
with help text on each field). Nothing about anyone's home is hardcoded; every
deployment sets its own.

Add one row per home airport and one per residence, per era. When you move,
set `effective_to` on the old rows and add new ones — historical trips stay
judged against the home you had at the time.

To bootstrap a new stack without hand-entering rows, copy
`docs/home-profiles.example.json`, fill it in, and:

```bash
docker exec travel_extractor-travel-extractor-1 python -m app.cli import-home /data/home-profiles.json
```

Real profile files are gitignored — keep them out of the repo.

## Dev

```bash
pip install -e .[dev]
pytest                                  # all suites; 35-case corpus is the contract
python -m app.cli create-key my-client  # prints the API key once
uvicorn app.main:app --reload           # sqlite by default; TX_DATABASE_URL to override
```

Full dev stack (app + Postgres, port 8100):

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

## Production — Portainer only

Production is launched and managed **in Portainer by the operator** — never
`docker compose up` on the host. Build the image, then paste
`portainer-stack.yml` into a Portainer stack and set the two
`CHANGE_ME_DB_PASSWORD` occurrences (env vars are inline by design; Portainer
cannot interpolate `.env` files).

```bash
docker build -t travel-extractor:latest .
```

Routing (labels in the stack file): `travel-extractor.home.example` serves the
full app on the LAN; `travel-extractor.example.net` exposes **only**
`POST /ingest` publicly for the Apps Script capture add-on.

## /ingest auth

Per-client revocable API keys (created via CLI, sha256-stored) + signed
timestamp:

```
X-Api-Key:   tx_<hex>
X-Timestamp: <unix seconds>          # rejected outside a 5-minute window
X-Signature: HMAC_SHA256(key, "<timestamp>." + body) as hex
```

Returns `202 {"batch_id": ...}` immediately; the batch is stored (and
content-addressed, D-15) for async processing — never processed inline.

## Rules (D-25)

Rule logic lives in `rules.py`; whether each rule is in force lives in the
`rule_settings` table (`enforce` / `audit` / `off`), seeded at startup and
editable in SQLAdmin. `RULES.md` is **generated**:

```bash
python3 rules.py --docs > RULES.md
```
