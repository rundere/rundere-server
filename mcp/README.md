# Yonder MCP server

A first-party [MCP](https://modelcontextprotocol.io) server that lets an agent
(Claude Desktop / Claude Code) drive Yonder — read your trips, check what's next,
add an event, check off a packing item, or share a link into your review queue.

It talks to Yonder's `/api/v1` over HTTP as **one Yonder user**, so it can only
see and change what that user is allowed to (the API is per-user scoped). It adds
nothing to the Yonder app itself — it's a standalone client.

## Configure

**Recommended — a long-lived, revocable API token** (no password in the config).
Mint one for your account (once), then set it as `YONDER_TOKEN`:

```bash
docker exec yonder-yonder-1 python -m app.cli mint-api-token me --days 365
```

Environment variables:

| var | meaning |
|---|---|
| `YONDER_URL` | base URL of the Yonder app (e.g. `https://yonder.home.example`) |
| `YONDER_TOKEN` | **preferred** — the long-lived API token minted above |
| `YONDER_USER` / `YONDER_PASSWORD` | alternative — the MCP logs in and auto-refreshes |

Either way the MCP acts **as that one user** and sees only what they may see (the
API is per-user scoped). Revoke the token by resetting that user's password
(bumps their token version, invalidating all their tokens).

Install the runtime once (the client itself is stdlib-only):

```bash
pip install -r mcp/requirements.txt
```

## Run / wire up

Claude Code:

```bash
claude mcp add yonder -- env YONDER_URL=https://yonder.home.example \
  YONDER_USER=me YONDER_PASSWORD=… python /path/to/yonder/mcp/server.py
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "yonder": {
      "command": "python",
      "args": ["/path/to/yonder/mcp/server.py"],
      "env": {
        "YONDER_URL": "https://yonder.home.example",
        "YONDER_USER": "me",
        "YONDER_PASSWORD": "…"
      }
    }
  }
}
```

## Tools

- `list_trips`, `get_trip(trip_id)` — trips + full snapshot (activities, places
  with coordinates, discovery findings, your packing).
- `dashboard`, `review(bucket)` — the home overview and your capture queue.
- `capture_url(url, travel_date?, title?, notes?)` — share a booking page or a
  Google Maps directions link into the review queue.
- `add_activity(trip_id, type, values, starts_at?)` — add a location/event/… to
  a trip you can edit.
- `generate_packing(trip_id)`, `set_packing_status(trip_id, item_id, status)` —
  your own per-user packing.

The capture **review/commit** pipeline is intentionally not exposed over MCP yet
(a later version).
