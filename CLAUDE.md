# CLAUDE.md

The shared rules for every contributor and coding agent are in AGENTS.md; this
file imports it so Claude Code loads them automatically:

@AGENTS.md

## Claude Code specifics

- **Web sessions:** the container may default to Python 3.11. Run everything
  through `uv run …`, as AGENTS.md shows.
- **When a PR is reviewed** (by a human, Claude Code Review or another agent):
  address every thread, re-run the full suite, then push.
- **Out-of-scope findings:** note them in the PR description rather than
  widening the PR.
