# AGENTS.md — working on rundere-server

How to work on this repository, for people and for coding agents (Claude Code,
Codex, others). `CLAUDE.md` imports this file, so the shared rules live here.

## What this is

Run·dere is a self-hosted, multi-user travel tracker. It was called **Yonder**
until 2026, so the package name, the `TX_*` settings prefix and many docstrings
still say Yonder.

- **Capture** (email, calendar, .ics, web, iOS, MCP) → **extract** → **resolve**
  → **review** → **commit**, onto Run·dere trips and a timeline.
- **First-party features:** trips, timeline, Discover research, packing, travel
  journal, sharing.
- **Optional integrations:** AirTrail and AdventureLog. Run·dere is the primary
  system (SPEC-8 D-146), so treat both as extras.

The stack is Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Jinja2 + HTMX, SQLite
(dev) / Postgres (prod), and an MCP server at `/mcp`.

## Commands

```bash
uv run --extra dev pytest -q          # full suite (~70s); must pass before any PR
uv run python tests/validate.py       # extraction corpus: "N/N cases behaved as expected"
uv run python scripts/prepublish.py   # secrets, personal data and dependency checks
uv run uvicorn app.main:app --reload  # local server, SQLite by default (TX_DATABASE_URL overrides)
uv run alembic upgrade head           # apply migrations
python3 rules.py --docs > RULES.md    # regenerate after ANY change to rules.py
```

- Use `uv run` (or Python 3.12). Bare `pip install .` refuses Python 3.11.
- **`uv.lock` is checked in CI** (`uv lock --check`). If you change dependencies
  in `pyproject.toml`, run `uv lock` and commit the lockfile in the same PR.
- CI (`.github/workflows/ci.yml`) runs the suite and the `validate.py` accuracy
  line on every push and PR.

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
| `deploy/`, `docs/` | Example stacks; self-hosting, demo-server and publishing guides |
| `SPEC-*.md`, `DECISIONS.md` | Specs and numbered **decisions (D-###)**: the source of truth for intent |
| `RULES.md` | Generated from `rules.py`. **Don't hand-edit it** |

## House rules (breaking these blocks a review)

1. **Specs and decisions come first.**
   - Behaviour is decided in `SPEC-*.md` and `DECISIONS.md` as numbered
     decisions (`D-###`). Cite the D-number in a code comment where a decision
     drives the code, as the existing code does.
   - Don't contradict a decision silently. If the code needs to differ, say so
     in the PR and propose the spec change.
   - A new decision takes the next free number:
     `grep -rhoE "D-[0-9]{3}" *.md | sort -u | tail -1`.
2. **Per-user scoping on every read and write.**
   - Visibility goes through `app/scope.py` (`trip_filter`, `capture_filter`,
     `can_view_trip`, `can_edit_trip`, the journal own-only filter, …). Never
     query user data unscoped in a route, API or MCP tool.
   - Every authenticated write sets `owner_id`. Ownerless rows are visible to
     admins only.
   - The journal is own-only (D-134): sharing a trip never exposes someone's
     journal.
3. **Soft links, no database foreign keys, for trip links.** `trip_id` columns
   are plain indexed columns (see `TravelItem.trip_id` and
   `JournalEntry.trip_id`). Delete routes clear dangling links.
4. **Keep the pipeline invariants.**
   - Raw captures are content-addressed and kept for the life of their trip
     plus 12 months; the hash stays as a tombstone (D-15, amended by D-196).
   - Schema validation runs before rules.
   - Rules are toggleable (`enforce` / `audit` / `off`, D-25). Don't weaken a
     **critical** rule (RULE-01..07 encode real past failures).
   - Resolution is deterministic and safe to re-run.
5. **External services are optional (D-146).**
   - Code must degrade gracefully when AirTrail, AdventureLog, Photon, an LLM or
     a map/routing provider is unconfigured or down. Follow the existing
     `_airtrail()` / `_adventurelog()` "return None when unconfigured" pattern.
   - Cache lookups as Run·dere-owned data instead of re-fetching on every view.
6. **Secrets.**
   - Never commit credentials, tokens or `.env` files.
   - Never log a credential, and never put one in an exception message or in
     `commit_attempts`.
   - Per-user integration credentials are encrypted at rest and write-only
     (D-151).
   - Any user-supplied URL the server fetches must pass the SSRF policy (D-152).
7. **Storage posture (D-153–D-155).** Store no photos, audio or receipt images:
   journal photos are references. There are no transaction imports and no
   receipt OCR.
8. **Test data is fictional.** Use invented people, hotels, addresses, booking
   codes and flight numbers (`example.com` / `.test` hosts, `traveller@example.test`).
   Never paste a real confirmation email or itinerary into a test or the corpus.
9. **Match the surrounding code.** Keep the comment density (explain *why*,
   cite D-numbers) and the naming. Don't add a dependency without giving the
   reason in the PR.

## Contributing

- **Start with an issue** for anything bigger than a small fix, so the approach
  can be agreed before you write it. Security-sensitive changes (auth, scoping,
  secrets, anything that writes to an external service) always need that
  agreement first.
- **Branches:** `feat/…`, `fix/…`, `docs/…`, `chore/…` off `main`. Keep PRs small
  and on one topic; note anything else you find in the PR or a new issue rather
  than widening it.
- **Commits:** Conventional Commits with an optional scope, e.g.
  `fix(trips): …`, `feat(mcp): …`, `docs: …`.
- **Before opening a PR:**
  - the full `pytest` suite passes;
  - `validate.py` shows no regression;
  - `scripts/prepublish.py` reports nothing blocking;
  - `RULES.md` is regenerated if `rules.py` changed;
  - there's a new migration if the models changed;
  - there are tests for new behaviour, including a scoping test (another user
    can't see or edit it) for anything user-owned.
- **Reviews:** answer every review thread, and fix it or explain why not.
- **Merging and releases** are done by the maintainer. PRs merge with a merge
  commit; don't bump versions or tag releases.

### References to the maintainer's tracker

Commits, PRs and comments cite work items such as **YONDER-123**. They live in
the maintainer's private issue tracker and aren't publicly accessible; the PRs
and commits that mention them carry the details. Contributors don't need to add
them.

## Working in parallel

- Expect files to change under you. Merge `main` into your branch before
  finishing, and resolve conflicts by keeping both intents.
- **Hot files** (`app/models.py`, `app/scope.py`, `app/resolving.py`,
  `app/committing.py`, `alembic/versions/`): keep edits there minimal and say so
  in the PR.
- **Migration numbers:** two branches can pick the same next number. Re-check
  it after merging `main`.
- Stage files by name, not `git add -A`: the repo tolerates local untracked
  files (dev databases, a private `.prepublish.local.toml`) that must never be
  committed.
