# CLAUDE.md

The shared rules for every coding agent are in AGENTS.md; this file imports it
so Claude Code loads them automatically:

@AGENTS.md

## Claude Code specifics

- **Plane:** the Plane MCP connector may be available (project `YONDER`). File
  follow-ups there rather than widening a PR, and link PRs back to their items.
- **Web sessions:** the container may default to Python 3.11. Run everything
  through `uv run …`, as AGENTS.md shows.
- **When a PR is reviewed** (by a human, Claude Code Review or another agent):
  address every thread, re-run the full suite, then push.
