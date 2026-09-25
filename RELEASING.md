# Releasing Yonder

Issues and the roadmap are tracked in Plane (self-hosted, internal). GitHub
stays the source of truth for code, tags, and published releases; Plane
stays the source of truth for what's planned/in-progress/done.

## Cutting a release

1. Feature branch off `main`; as part of that PR, bump **both**
   `app/config.py` `app_version` and `pyproject.toml` `version` to the same
   `X.Y.Z`, and update the `/health` version test.
2. PR to `main`, merged with a **merge commit** (keeps the individual commits
   so version-boundary tags can anchor).
3. Annotated tag `vX.Y.Z` on the release commit; push it.

Pushing the tag triggers `.github/workflows/release.yml`, which now handles
the GitHub side end-to-end:

- Runs the test suite.
- Builds + pushes `ghcr.io/jeeves5454/yonder:{X.Y.Z, X.Y, latest}`.
- Creates the GitHub Release for the tag with auto-generated notes
  (`gh release create --generate-notes`) — skipped if a release for that tag
  already exists, so re-running via `workflow_dispatch` to rebuild a
  historical version never clobbers its release notes.
- Syncs the version line in `README.md` and `docs/howto.md` to the new tag
  and pushes that commit straight to `main` (`[skip ci]`), so those never
  drift from `pyproject.toml` again the way `README.md` had (it sat at 3.8.1
  while the app was at 3.9.2).

`workflow_dispatch` (manual run from the Actions tab) still only builds and
publishes the image for a given version — it's for rebuilding/backfilling a
tag that predates the workflow, and deliberately does **not** touch the
GitHub Release or README/docs.

## Syncing to Plane

Plane (`plane.home.example`) is only reachable from inside the homelab
network — GitHub-hosted runners can't reach it, so this step isn't part of
CI. After a release ships, run:

```bash
export PLANE_API_KEY=...   # Plane workspace API token
python3 scripts/plane_release_sync.py vX.Y.Z
```

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
