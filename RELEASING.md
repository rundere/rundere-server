# Releasing Run·dere

Issues and the roadmap are tracked in Plane (self-hosted, internal). GitHub
stays the source of truth for code, tags, and published releases; Plane
stays the source of truth for what's planned/in-progress/done.

## Cutting a release

Every change to `main` goes through a PR, releases included (D-207). Rules on
`main` require a PR and passing checks, and no workflow pushes to `main`.

1. **Actions → "cut release" → Run workflow** (`.github/workflows/cut-release.yml`).
   Pick **patch / minor / major** (or type an explicit `X.Y.Z`) and leave
   **publish** unticked for a dry run. The run summary shows the next version and
   every PR merged since the last tag, after the pre-publish gate passes. Nothing
   is changed.
2. Happy? Run it again with **publish** ticked. The `prepare` job commits
   `chore: release X.Y.Z` to a new branch `release/vX.Y.Z`. That commit bumps
   `pyproject.toml`, `app/config.py`, the project's version in `uv.lock`, and the
   version line in `README.md` and `docs/howto.md`. It pushes that branch, never
   `main`.
3. **Open the PR** from the link in the run summary. CI runs on it like any other
   PR. Merge it with a merge commit.
4. The merge changes the version on `main`, so `tag-release.yml` tags the merge
   commit `vX.Y.Z` and calls `release.yml` for that tag:
   - runs the test suite;
   - builds and pushes `ghcr.io/rundere/rundere-server:{X.Y.Z, X.Y, latest}`.
     Before the public launch the image was the private
     `ghcr.io/jeeves5454/rundere-server`, and up to 4.3.x `ghcr.io/jeeves5454/yonder`
     (D-207);
   - creates the GitHub Release with auto-generated notes
     (`gh release create --generate-notes`). It skips this if the release already
     exists, so a rebuild never overwrites its notes.

A version bump in any merged PR releases the same way, so the release PR from
step 2 is just the convenient way to make one. The `release` environment only
deploys from `main`. Once the repository is public, add yourself as its required
reviewer (Settings → Environments → `release`) and GitHub pauses each release for
an **Approve and deploy** click.

Then run the Plane sync below.

`workflow_dispatch` on `release.yml` itself (manual run from the Actions tab) still only builds and
publishes the image for a given version — it's for rebuilding/backfilling a
tag that predates the workflow, and deliberately does **not** touch the
GitHub Release or README/docs.

## Refreshing bundled data

Before a minor release, refresh Run·dere's own airport table (SPEC-8 D-147,
D-186) in the release PR — new airports and renamed codes land there:

```bash
uv run python scripts/build_airports.py    # rewrites resolve/data/airports.json.gz
```

Commit it only if the diff looks sane (the script prints the airport count).

## Syncing the tracker

Work is tracked in the maintainer's own Plane, which isn't public and isn't
reachable from GitHub's runners, so this step isn't part of CI. After a release
ships, the maintainer closes the shipped items there and files a release item
with its notes (the Plane MCP connector does this from a Claude session).

This reads the GitHub release notes for that tag and:

- Publishes them as a work item in Plane's **Releases** module (creates it
  the first time, updates it in place on a re-run — safe to run twice for
  the same tag).
- Closes out any tracked Plane issue the release notes reference — either a
  `PR #123` mention matching an issue's own description (how the historical
  backfill issues are written), or an explicit `YONDER-123` issue-number
  reference — and leaves a comment noting which release shipped it.

To get a Plane issue auto-closed by that second mechanism going forward,
reference its `YONDER-NNN` id somewhere in the PR description (it flows
through into the generated release notes).

Everything else in Plane (moving backlog items to Todo/In Progress, adding
new backlog issues, re-slicing modules) stays a normal manual edit in Plane
— the script only ever pushes release notes in and flips shipped issues to
Done.
