# Yonder and MCP

## Primary path: the claude.ai connector (built in)

Yonder has a remote MCP server at **`https://<yonder-host>/mcp`**, built into the
app, with OAuth sign-in. Add it to claude.ai as a custom connector and it works
in claude.ai, Claude Desktop and the Claude mobile app. Claude Code can use it
too, through a browser sign-in or a personal API token.

→ **[docs/claude-connector.md](../docs/claude-connector.md)**: setup, tools, and
how to disconnect.

## This folder: the stdio script (LAN / Claude Code / Claude Desktop)

`mcp/server.py` is a standalone **stdio** MCP server with the **same tools** as
`/mcp`. It's a thin client of Yonder's `/api/v1` (`client.py`, stdlib only).
Use it where the host can reach `/api/v1` but not `/mcp`, or when you'd rather
run a local process. It acts as **one Yonder user**, so it sees and changes only
what that user may (the API is per-user scoped).

### Token

Create a **personal API token**: Yonder → Account → **Personal API tokens**. It's
shown once, is individually revocable, and has an optional expiry. Use it as
`YONDER_TOKEN`.

(`python -m app.cli mint-api-token <user>` still works, but those tokens can
only be revoked all at once, by resetting the user's password.)

| var | meaning |
|---|---|
| `YONDER_URL` | base URL of the Yonder app (e.g. `https://rundere.example.com`) |
| `YONDER_TOKEN` | **preferred**: a personal API token (`ypat_…`) |
| `YONDER_USER` / `YONDER_PASSWORD` | alternative: the script logs in and re-logs in on expiry |

Install the runtime once (MCP SDK 2.x):

```bash
pip install -r mcp/requirements.txt
```

### Wire it up

Claude Code:

```bash
claude mcp add yonder -- env YONDER_URL=https://rundere.example.com \
  YONDER_TOKEN=ypat_… python /path/to/yonder/mcp/server.py
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "yonder": {
      "command": "python",
      "args": ["/path/to/yonder/mcp/server.py"],
      "env": {
        "YONDER_URL": "https://rundere.example.com",
        "YONDER_TOKEN": "ypat_…"
      }
    }
  }
}
```

### Tools

The same set as the remote endpoint (see the table in
[docs/claude-connector.md](../docs/claude-connector.md#tools)): trips, timeline
(incl. `traveler_ids`), packing, journal, capture, review + filing, and
`save_research` into your Discover tab. One difference: over stdio,
`update_activity`'s `values` **replaces** all of an activity's fields (the API's
semantics), while the remote tool merges them.

Tests: `pytest mcp/test_client.py` (no network, no MCP runtime).
