# Releasing Run·dere

Issues and the roadmap are tracked in Plane (self-hosted, internal). GitHub
stays the source of truth for code, tags, and published releases; Plane
stays the source of truth for what's planned/in-progress/done.

## Cutting a release (the one-click way)

**Actions → "cut release" → Run workflow** (`.github/workflows/cut-release.yml`):

1. Pick **patch / minor / major** (or type an explicit `X.Y.Z`) and leave
   **publish** unticked for a dry run: the run summary shows the next version
   and every PR merged since the last tag. Nothing is changed.
2. Happy? Run it again with **publish** ticked. The `release` job runs in the
   `release` environment. If you've added yourself as a required reviewer
   (Settings → Environments → `release` → Required reviewers — needs GitHub
   Pro/Team on a private repo), GitHub pauses for your **Approve and deploy**;
   otherwise the ticked box is the approval (only people with write access can
   run the workflow).
3. It bumps `app/config.py` + `pyproject.toml`, commits `chore: release X.Y.Z`
   to `main`, pushes the annotated tag `vX.Y.Z`, and then runs `release.yml`
   for that tag (tests → GHCR image → GitHub Release with generated notes →
   README/docs version sync), exactly as a hand-pushed tag would.

Then run the Plane sync below.

## Cutting a release (by hand)

1. Feature branch off `main`; as part of that PR, bump **both**
   `app/config.py` `app_version` and `pyproject.toml` `version` to the same
   `X.Y.Z`, and update the `/health` version test.
2. PR to `main`, merged with a **merge commit** (keeps the individual commits
   so version-boundary tags can anchor).
3. Annotated tag `vX.Y.Z` on the release commit; push it.

Pushing the tag triggers `.github/workflows/release.yml`, which now handles
the GitHub side end-to-end:

- Runs the test suite.
- Builds + pushes `ghcr.io/rundere/rundere-server:{X.Y.Z, X.Y, latest}` (before the public launch it was the private `ghcr.io/jeeves5454/rundere-server`, and up to 4.3.x `ghcr.io/jeeves5454/yonder`, D-207).
- Creates the GitHub Release for the tag with auto-generated notes
  (`gh release create --generate-notes`) — skipped if a release for that tag
  already exists, so re-running via `workflow_dispatch` to rebuild a
  historical version never clobbers its release notes.
- Syncs the version line in `README.md` and `docs/howto.md` to the new tag
  and pushes that commit straight to `main` (`[skip ci]`), so those never
  drift from `pyproject.toml` again the way `README.md` had (it sat at 3.8.1
  while the app was at 3.9.2).

`release.yml` is also *called* by "cut release" with the new tag (a tag
pushed with the workflow's own token doesn't trigger other workflows); that
path does everything a tag push does.

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
