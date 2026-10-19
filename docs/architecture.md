# Architecture

How Run·dere is put together: the pipeline, the rules, the code layout and where decisions are recorded.

## The pipeline

Every capture goes through the same stages:

```
capture  →  store   →  extract        →  resolve        →  review   →  commit
 (any        (raw,      (structured       (canonical keys,   (Inbox,     (Run·dere trip
  channel)    content-   readers, then     trip matching,     a person    timeline; AirTrail /
              addressed) local LLM)        rules)             approves)   AdventureLog if connected)
```

The channels are described in [capture.md](capture.md).

**Extractor chain** (`extract/`, tried in order — D-10/D-27):
`jsonld → ical → pdf → maps_url → web → llm`. Structured sources (schema.org JSON-LD
in booking emails, `.ics` invites, Google's Maps URL formats) are parsed
deterministically; free-form text falls through to a **local LLM** (Ollama,
D-11) — nothing is sent to a cloud model.

**Item types recognised:** `flight`, `hotel`, `lodging`, `event`,
`ground_transport`, `car_rental`, `train`, `ferry`.

**Resolution & safety:**
- **Canonical keys** (`resolve/keys.py`, spec [`docs/A5-canonical-keys.md`](A5-canonical-keys.md)) dedupe re-captures of the same booking.
- **Collection ladder** (`resolve/collections.py`, spec [`docs/A6-collection-resolution.md`](A6-collection-resolution.md)) attaches items to the right trip.
- **Home profiles** (effective-dated, D-07) decide which places never count as "visited" and which local drives aren't trips.
- **57 rules** (`rules.py`) with per-rule `enforce` / `audit` / `off` toggles gate what commits and what goes to review.
- **Content-addressed originals** (D-15): the raw capture is kept unchanged so reprocessing never loses information, for the life of its trip plus `TX_RETENTION_MONTHS` (D-196); its hash stays afterwards as a tombstone.

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
  trips/          Run·dere's own trips: timeline, travellers, Discovery,
                  Planning, AdventureLog managed sync
  main.py         router wiring, the sign-in gate and security headers
  scope.py        per-user visibility: every read and write goes through it
  mcp_server.py   the built-in MCP server at /mcp; api_v1.py is the JSON API
  cli.py          operational CLI: create-key, process, resolve, commit, ...
  processing.py, resolving.py, committing.py   the pipeline stages
extract/        extractor chain, in try-order (D-10/D-27):
                jsonld -> ical -> pdf -> maps_url -> web -> llm (Ollama, D-11)
resolve/        keys.py (A5 canonical keys), collections.py (A6 ladder)
discover/       Discovery (v3.0-3.2): deterministic + LLM resolvers per topic,
                location derivation, the durable run/finding pipeline
planning/       Planning (v3.1): the Packing Plan engine, catalog, profiles
adapters/       target writers: adventurelog.py, airtrail.py (HTTP APIs only)
rules.py        rule registry + engine (57 rules, tri-state toggles — D-25)
validation.py   schema gate (real jsonschema)
schemas/        travel-item (canonical) + travel-extraction (derived, D-09)
alembic/        migrations, numbered 00NN_name.py
capture/        gmail-addon/, gcal-addon/ (Apps Script)
design/         brand + design system: logos, icons, tokens (read design/README.md
                before UI work); logos/icons are served at /brand/*
tests/          pytest suite + samples.json regression corpus
docs/           A5/A6 specs, shared-capabilities guideline (D-96),
                home-profiles.example.json, the setup/usage guide
scripts/        prepublish.py (secrets and personal-data check), extract_eval.py,
                dto_drift.py, build_airports.py
```

The design specs (`docs/A5-canonical-keys.md`, `docs/A6-collection-resolution.md`,
`SPEC-3-discovery.md`, `SPEC-3.1-planning.md`, `SPEC-3.2-discovery-llm.md`,
`SPEC-4-directions-capture.md`) and the generated `RULES.md` explain the
resolution, Discovery/Planning, and rule behaviour in depth.

**Engineering guideline:** `docs/shared-capabilities.md` — build capabilities as
common project-level functions and treat a channel/provider's needs as a
subsection of that shared capability, not a rewrite. Review it before adding a new
capability or channel (D-89/D-96).

## Decisions and references

Design decisions are numbered **D-###**. The early ones (D-01 – D-109) are in
[DECISIONS.md](../DECISIONS.md), later ones in the `SPEC-*.md` file they belong to, and
open questions (**OQ-##**) in [OPEN_QUESTIONS.md](../OPEN_QUESTIONS.md). Code comments
cite them to say *why*.

References like **YONDER-123** point to work items in the maintainer's private issue
tracker (Plane), used to keep the project's work defined. They aren't publicly
accessible; the pull requests and commits that mention them carry the details.
