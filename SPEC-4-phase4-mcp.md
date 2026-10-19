# SPEC-4 Phase 4 — Built-in MCP server (Workstream E)

> **Superseded (D-205, 2026-10-19).** The standalone stdio server in `mcp/` and the
> CLI-minted `typ="api"` token (D-126) described below were removed. Use the
> built-in `/mcp` (OAuth, SPEC-4.2) or a personal API token.

A first-party MCP server that exposes Yonder to agents (Claude Desktop/Code) over
the **same `/api/v1`** surface — so it inherits all auth + per-user scoping for
free, and can never diverge from the app. Supersedes the broken
`mcp-adventurelog-server` (no Note/Checklist tools, broken Authentik JWKS).

## Shape
Standalone component under `mcp/` (its own deps, like the old adventurelog mcp),
NOT wired into the main app:
- `mcp/client.py` — `YonderClient`: pure-stdlib (urllib) HTTP client for the
  Yonder API. Logs in via `/api/v1/auth/login` (username+password from env) or a
  pre-issued `YONDER_TOKEN`; caches the access token; **re-logs-in once on a
  401** (token expiry). No MCP dependency → unit-testable and live-verifiable on
  its own.
- `mcp/server.py` — a `FastMCP` server (the `mcp` SDK) that registers each tool
  as a thin wrapper over a `YonderClient` method and runs on **stdio**.
- `mcp/requirements.txt` (`mcp>=1.2`), `mcp/README.md`, `mcp/test_client.py`.

## Auth (per-user; D-111, D-126)
The MCP acts **as one Yonder user** — the account in its env. Every `/api/v1`
call is user-scoped, so it sees exactly and only what that user may see.

**D-126 — long-lived machine token.** *Superseded by SPEC-8 D-205.* For a machine client, storing a password
or a 30-min access token is poor, so the CLI mints a **long-lived, revocable API
token**: `python -m app.cli mint-api-token <user> [--days N]` issues a JWT with
`typ="api"` (default 1 year, `TX_JWT_API_TTL_SECONDS`) that the API's bearer auth
accepts alongside access tokens. It carries the user's `token_version`, so it's
revoked en masse by a password reset (bumps the version). The MCP uses it as
`YONDER_TOKEN`; username+password (auto-refresh) remains a fallback. MCP transport
is stdio (local subprocess), so there is no separate network key for the channel.

## Tools (v1)
Read: `list_trips`, `get_trip` (full snapshot — activities, places lat/lon,
discovery findings, the user's packing), `dashboard`, `review`.
Write (manual edits, D-125): `capture_url` (share a link into the review queue),
`add_activity` (add a location/event to a trip), `generate_packing`,
`set_packing_status` (check an item off).

Deferred (future version, owner decision): the capture **review/commit** pipeline
over MCP — significant complexity, not in 4.0.

## Verification
- `mcp/test_client.py`: login caching, 401 → re-login-and-retry, GET/POST/PATCH
  shaping (mocked urllib; runs in the yonder image).
- Live: point a `YonderClient` at the running test stack (owner creds) and call
  the tools end-to-end — proves the whole MCP data layer against the real API.
- Wiring: documented for `claude mcp add` / Claude Desktop config in the README;
  the FastMCP runtime itself needs `pip install mcp` on the host running it.
