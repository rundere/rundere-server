# Contributing to Run·dere

Thanks for helping. This page covers getting set up and how changes are made. The house
rules that every change has to follow (scoping, secrets, the pipeline invariants and so
on) are in [AGENTS.md](AGENTS.md), which applies to people and coding agents alike.

By taking part you agree to the [code of conduct](CODE_OF_CONDUCT.md). Report security
issues privately, as [SECURITY.md](SECURITY.md) describes, never in a public issue.

## Set up

You need Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/jeeves5454/rundere-server.git
cd rundere-server
uv run --extra dev pytest -q          # the full suite, about a minute
uv run uvicorn app.main:app --reload  # http://localhost:8000, SQLite by default
```

The first visit asks you to create the admin account at `/setup`.

To click around invented trips, run a local demo server. It's read-only and needs its own
database ([docs/demo-server.md](docs/demo-server.md)):

```bash
export TX_DATABASE_URL=sqlite:///./demo.sqlite3 TX_DEMO_MODE=true \
       TX_DEMO_PASSWORD=pick-one TX_AUTH_COOKIE_SECURE=false
uv run alembic upgrade head
uv run python -m app.cli demo-seed
uv run uvicorn app.main:app --reload
```

For a Postgres-backed stack like production, run
`docker compose -f docker-compose.dev.yml up -d --build` (port 8100).

## Before you write code

- **Start with an issue** for anything bigger than a small fix, so the approach can be
  agreed first. Changes to sign-in, scoping, secrets, or anything that writes to an
  external service always need that agreement.
- **Read the decision.** Behaviour is decided in the `SPEC-*.md` files and
  [DECISIONS.md](DECISIONS.md) as numbered decisions (**D-###**). If your change needs to
  differ from one, say so in the issue and propose the new wording. A new decision takes
  the next free number:

  ```bash
  grep -rhoE "D-[0-9]{3}" *.md | sort -u | tail -1
  ```

## Making the change

- Branch from `main` as `feat/…`, `fix/…`, `docs/…` or `chore/…`, and keep a pull request
  to one topic.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/),
  with an optional scope: `fix(trips): …`, `feat(mcp): …`, `docs: …`.
- Match the code around you: explain *why* in comments and cite the D-number when a
  decision drives the code.
- Changing models needs a new Alembic migration (`alembic/versions/00NN_name.py`). Never
  edit one that's already merged.
- Changing `rules.py` means regenerating `RULES.md`: `python3 rules.py --docs > RULES.md`.
- Changing dependencies in `pyproject.toml` means running `uv lock` and committing
  `uv.lock`. Say why a new dependency is needed.
- **Test data is invented.** Use made-up people, hotels, booking codes and flight
  numbers, with `example.com` or `.test` hosts. Never paste a real confirmation email or
  itinerary.

## Before you open a pull request

- [ ] `uv run --extra dev pytest -q` passes.
- [ ] `uv run python tests/validate.py` shows no regression in the extraction corpus.
- [ ] `uv run python scripts/prepublish.py` reports nothing blocking.
- [ ] New behaviour has tests. For anything a user owns, that includes a test that
      another user can't see or change it.
- [ ] Docs and the relevant spec are updated.

CI runs the suite and the corpus check on every pull request. The maintainer merges and
cuts releases, so don't bump the version or add tags.

Commits and comments sometimes mention work items such as **YONDER-123**. They're in the
maintainer's private tracker; you don't need to add them.

## Licence

Run·dere is licensed under the [GNU AGPL-3.0](LICENSE). By contributing you agree that
your contribution is licensed the same way.
