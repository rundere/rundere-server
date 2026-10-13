# AGENTS.md — working on rundere-server

Instructions for any coding agent (Claude Code, Codex, others) and for humans new
to the repo. `CLAUDE.md` imports this file, so keep the shared rules here.

## What this is

Run·dere (formerly **Yonder**; the package, image history, `TX_*` env prefix and
many docstrings still say Yonder) is a self-hosted, multi-user travel tracker:

- **Capture** (email, calendar, .ics, web, iOS, MCP) → **extract** → **resolve**
  → **review** → **commit**, onto Rundere trips and a timeline.
- **First-party features:** trips, timeline, Discover research, packing, travel
  journal, sharing.
- **Optional integrations:** AirTrail and AdventureLog. SPEC-8 (D-146) makes
  Rundere the primary system, so treat both as extras.

The stack is Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Jinja2 + HTMX, SQLite
(dev) / Postgres (prod), and an MCP server at `/mcp`.

## Commands

```bash
uv run --extra dev pytest -q          # full suite (~50s); must pass before any push
uv run python tests/validate.py       # extraction corpus: "N/N cases behaved as expected"
uv run uvicorn app.main:app --reload  # local server, SQLite by default (TX_DATABASE_URL overrides)
uv run alembic upgrade head           # apply migrations
python3 rules.py --docs > RULES.md    # regenerate after ANY change to rules.py
```

- The system Python may be 3.11. Use `uv run` (or Python 3.12) rather than bare
  `pip install .`, which refuses to install on 3.11.
- **`uv.lock` is checked in CI** (`uv lock --check`). If you change
  dependencies in `pyproject.toml`, run `uv lock` and commit the lockfile in the
  same PR. The release workflow bumps the project's own version in it.
- CI (`.github/workflows/ci.yml`) runs `pytest -q` and the `validate.py`
  accuracy line on every push and PR.

## Where things are

| Path | What |
|---|---|
| `app/` | FastAPI app: routes, services, `models.py`, `scope.py`, `mcp_server.py`, `api_v1.py` |
| `app/review/`, `app/trips/` | Review queue; trips, timeline, Discover, maps, PDF |
| `extract/`, `resolve/`, `rules.py`, `validation.py` | Pipeline stages; the business-rule registry; JSON Schema validation |
| `adapters/targets/` | AirTrail and AdventureLog clients (optional integrations) |
| `discover/`, `planning/` | Research resolvers and data; packing and planning data |
| `alembic/versions/` | Migrations, numbered `00NN_name.py`. Always add a new one; never edit a merged one |
| `schemas/`, `contracts/` | JSON Schemas; iOS DTO contracts |
| `tests/` | pytest suite. `tests/samples.json` is the extraction regression contract |
| `SPEC-*.md` | Planning specs and **decisions (D-###)**. The source of truth for intent |
| `RULES.md` | Generated from `rules.py`. **Don't hand-edit it** |

## House rules (breaking these is a review blocker)

1. **Specs and decisions come first.**
   - Behaviour is decided in `SPEC-*.md` as numbered decisions (`D-###`, the
     latest being D-155 in SPEC-8). Cite the D-number in code comments where a
     decision drives the code, as the existing code does.
   - Don't contradict a decision silently. If the code needs to differ, say so
     in the PR and propose a spec change.
   - New decisions take the next free D-number. Check `grep -rhoE "D-[0-9]{3}"
     *.md | sort -u | tail`.
2. **Per-user scoping on every read and write.**
   - Visibility goes through `app/scope.py` (`trip_filter`, `capture_filter`,
     `can_view_trip`, `can_edit_trip`, the journal own-only filter, …). Never
     query user data unscoped in a route, API or MCP tool.
   - Every authenticated write sets `owner_id`. Ownerless rows are closed
     (admins only), per YONDER-186.
   - The journal is own-only (D-134): sharing a trip never exposes someone's
     journal.
3. **Soft links, no database foreign keys, for trip links.** `trip_id` columns
   are plain indexed columns (house convention, see `TravelItem.trip_id` and
   `JournalEntry.trip_id`). Delete routes clear dangling links.
4. **Keep the pipeline invariants.**
   - Raw captures are content-addressed and kept for the life of their trip
     plus 12 months; the hash stays as a tombstone (D-15, amended by D-196).
   - Schema validation runs before rules.
   - Rules are toggleable (`enforce` / `audit` / `off`, D-25). Don't weaken a
     **critical** rule (RULE-01..07 encode real past failures).
   - Resolution is deterministic and safe to re-run.
5. **External services are optional (SPEC-8 D-146).**
   - Code must degrade gracefully when AirTrail, AdventureLog, Photon, an LLM or
     a map/routing provider is unconfigured or down. Follow the existing
     `_airtrail()` / `_adventurelog()` "return None when unconfigured" pattern.
   - Cache lookups as Rundere-owned data instead of re-fetching on every view.
6. **Secrets.**
   - Never commit credentials, tokens or `.env` files.
   - Never log a credential, and never put one in an exception message or in
     `commit_attempts`.
   - Per-user integration credentials are encrypted at rest and write-only (SPEC-8
     D-151, in progress).
   - Any user-supplied URL the server fetches must pass the SSRF policy (D-152).
7. **Storage posture (D-153–D-155).** Store no photos, audio or receipt images:
   journal photos are references. There are no transaction imports and no
   receipt OCR.
8. **Match the surrounding code.** Keep the comment density (explain *why*, cite
   D-numbers and YONDER-IDs) and the naming. Don't add dependencies without a
   reason stated in the PR.

## Workflow

- **Tracking:** work items live in **Plane** (project `YONDER`, e.g. YONDER-191).
  GitHub holds the code, tags and releases. Name the YONDER-ID in the branch or
  PR title and in the PR body.
- **Branches:** `feat/…`, `fix/…`, `docs/…`, `chore/…` off `main`. Never push to
  `main`. Keep one Plane item per PR, and keep PRs small.
- **Commits:** Conventional Commits with an optional scope, e.g.
  `fix(trips): …`, `feat(mcp): …`, `docs: …`.
- **Merging:** PRs merge with a **merge commit** (RELEASING.md). Don't rebase or
  force-push branches someone else is working on.
- **Releases:** handled by the owner through the "cut release" workflow. Agents
  don't bump versions or tag releases unless asked.
- **Before opening a PR:**
  - the full `pytest` suite passes;
  - `validate.py` shows no regression;
  - `RULES.md` is regenerated if `rules.py` changed;
  - there's a new migration if the models changed;
  - there are tests for new behaviour, including a scoping test (another user
    can't see or edit it) for anything user-owned.
- **Reviews:** another agent may review your PR. Answer every review thread, and
  fix it or explain why not.

## Working in parallel with other agents

- Take **one Plane item** and stay within its scope. If you find something else,
  note it in the PR or file it in Plane; don't widen the PR.
- Expect files to change under you. Merge `main` into your branch before
  finishing, and fix conflicts by keeping both intents.
- **Hot files** (`app/models.py`, `app/scope.py`, `app/resolving.py`,
  `app/committing.py`, `alembic/versions/`): keep edits there minimal and say so
  in the PR.
- **Migration numbers:** two agents can both pick the same next number. Re-check
  it after merging `main`.
- **Security-sensitive work needs a human's go-ahead before starting:** auth,
  scoping, secrets and anything that writes to external services
  (YONDER-191, YONDER-192).
