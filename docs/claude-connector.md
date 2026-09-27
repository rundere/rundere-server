# Connect Run·dere (formerly Yonder) to Claude

Run·dere has a built-in remote MCP server at **`https://<rundere-host>/mcp`**
(YONDER-108) and is its own OAuth 2.1 authorization server (YONDER-107). Add it
to claude.ai once and it works in claude.ai on the web, Claude Desktop and the
Claude mobile app. Claude then acts **as you**: it sees and changes exactly what
you can in Run·dere, and nothing more. Per-user scoping and the admin-sharing
opt-out apply to every tool.

**Not only Claude.** `/mcp` is a standard MCP server with standard OAuth
(dynamic registration, PKCE), so any MCP-capable agent can connect the same way,
e.g. Perplexity or Perplexica. Use whichever agent suits the job. Each connected
app shows under Account → Connected apps by the name it registered with, and
that name labels whatever it writes to Discover.

## Server prerequisites (operator)

| Setting | Why |
|---|---|
| `TX_PUBLIC_BASE_URL=https://yonder.example.net` | The OAuth metadata, the `resource` and the token audience are built from it. Without it they fall back to the request URL, which is wrong behind a proxy that rewrites the host. |
| `TX_JWT_SECRET` | Must be set (a random ephemeral key otherwise, so every token dies on restart). |
| `TX_AUTH_REQUIRED=true` | Connectors are meant for authenticated deployments. |
| Reachability | claude.ai connects **from Anthropic's cloud** (`160.79.104.0/21`), so these must be public and must bypass browser forward-auth (Authentik): `/mcp`, `/oauth/token`, `/oauth/register`, `/oauth/revoke`, `/.well-known/oauth-*` (plus `/api/v1` for the app). That's YONDER-109. Keep `/oauth/authorize` and `/login` behind your normal browser route. |

Run·dere never trusts a proxy identity header (`TX_AUTH_PROXY_HEADER_TRUST`) on
those machine endpoints, even when the setting is on. Traefik should strip the
header there anyway.

Optional OAuth knobs (defaults in brackets): `TX_OAUTH_ACCESS_TTL_SECONDS`
[3600], `TX_OAUTH_REFRESH_IDLE_TTL_SECONDS` [90 days], `TX_OAUTH_CODE_TTL_SECONDS`
[60], `TX_OAUTH_REGISTER_RATE_LIMIT` / `…_WINDOW_SECONDS` [10 per hour per IP],
`TX_OAUTH_TOKEN_RATE_LIMIT` / `…_WINDOW_SECONDS` [60 per minute per IP].

## Add the connector (each user)

1. claude.ai → **Settings → Connectors → Add custom connector**.
2. Name: `Run·dere`. URL: `https://<rundere-host>/mcp`. Leave the advanced settings
   empty: Claude registers itself automatically (dynamic client registration).
3. **Connect**. You land on Run·dere's sign-in page. Sign in the way your
   deployment offers: username + password, "Sign in with <SSO>" when OIDC is on,
   or automatically when a trusted proxy header is on.
4. Run·dere asks **"Allow Claude to access your Run·dere data?"**. Choose **Allow**.
5. Back in Claude, enable the Run·dere tools in a chat and ask, e.g., *"What's on
   my Banff trip on day 2?"* or *"Research day hikes near Banff and save them to
   my Discover tab."*

**Static client instead of registration** (optional). An admin runs
`python -m app.cli oauth-client create "Claude" --redirect https://claude.ai/api/mcp/auth_callback [--confidential]`
and pastes the printed client id (and secret, if confidential) into the
connector's advanced settings.

## Claude Code

Claude Code can use the same endpoint. Either:

- `claude mcp add --transport http yonder https://<rundere-host>/mcp`, then
  `/mcp` in Claude Code to sign in through the browser (loopback redirect), or
- use a **personal API token** (Account → Personal API tokens):
  `claude mcp add --transport http yonder https://<rundere-host>/mcp --header "Authorization: Bearer ypat_…"`

On a LAN that can't reach `/mcp`, the stdio script in [`mcp/`](../mcp/README.md)
does the same over `/api/v1`.

## Disconnecting

Account → **Connected apps** → **Disconnect** ends Claude's access immediately:
the refresh token dies, and so do access tokens on their next call. These also
end every token: changing or resetting your password, "sign out everywhere"
(token version bump), and an admin deactivating the account.

## Tools

| Area | Tools |
|---|---|
| Trips | `list_trips`, `get_trip` (timeline with `traveler_ids`, travellers, places, your Discover findings + packing, captures), `update_trip`, `dashboard` |
| Timeline | `list_activity_types`, `add_activity`, `update_activity` (values merged), `delete_activity`. `traveler_ids: []` = everyone. |
| Packing (yours) | `generate_packing`, `set_packing_status`, `add_packing_item` |
| Journal (yours) | `create_journal_entry`, `list_journal_entries`, `get_journal_entry`, `update_journal_entry`, `attach_journal_entry` |
| Capture / review | `capture_url`, `list_review`, `get_review_item`, `assign_review_item_to_trip` (own / open / trips you travel on) |
| Discover | `save_research`: saves to **your** Discover tab as a draft. By default it goes into a "Research (AI agents)" section; with `topic` it can instead update the Food, Events, Safety, Visa, Plugs or Packing finding for a place. A later save supersedes the earlier one, which stays under "Earlier versions". A **locked** finding is refused. Also available as `POST /api/v1/trips/{id}/findings`. |
| Discover (4.5) | `save_city_guide`: a city's **Food & Drink** or **Events & Activities** — a summary + recommendation, and the places/events as rows (food rows carry *what to get*; events have dates, activities don't). Rows land in `get_trip`'s `discovery_items`; re-saving updates them by name and keeps anything you hid, edited or scheduled (`replace_items=false` only adds). `save_phrases`: a city's **Local phrases & register** (greetings, expressions, etiquette) under Language. `add_item_to_timeline`: puts a Discover item on the timeline as a Restaurant, Event or Tour, filled in from the item. REST: `POST /api/v1/trips/{id}/findings` with `items` / `phrases`, and `POST /api/v1/trips/{id}/discovery-items/{item_id}/timeline`. |
| Profile (4.5) | `get_discovery_profile`: your own discovery profile — dietary restrictions, cuisines to avoid, allergies, budget ceiling, drink and activity interests, mobility, nationality and base currency — with labels and how to apply them (the same rules Discover follows). `get_trip` includes it as `discovery_profile`. Read-only; never another traveller's. REST: `GET /api/v1/me/discovery-profile`. |

## Discover: who can overwrite what

Two kinds of writer produce Discover findings. **Local** is the server's own
Discover run: deterministic resolvers plus the local LLM. **External** is any AI
agent you connect over MCP (Claude, Perplexity/Perplexica, …), through the tool
above. The rules live in `discover/policy.py`:

| Topic | Can be locked? | Local whole-trip run | AI agent |
|---|---|---|---|
| Weather, Currency | never | always refreshes | can't write |
| Food, Events, Safety, Packing | never | refreshes, even if approved or edited | can update |
| Plugs, Visa | yes: **Lock**, or edit it | skips it when locked | refused when locked; otherwise updates |
| Languages | yes: pick languages, or **Lock** | skips it when locked | can't write |
| Research (AI agents) | never | never touches it | updates |

- **Where it was run matters.** The local run never overwrites anything an
  agent wrote, and never overwrites a finding you rejected. An agent's finding
  shows a "via <name>" badge, where the name is the app it connected as: its
  OAuth app name (e.g. "Claude") or your personal API token's name. The agent
  can't choose the label itself.
- **A lock is absolute.** Nothing overwrites a locked finding until you
  **Unlock** it, or press **Re-run** on that one finding, which is always an
  explicit override.
- **Nothing is lost.** Every superseded version is kept, the last 10, under
  "Earlier versions" on the card.

## How it works (for the curious)

- `GET /.well-known/oauth-protected-resource` → `resource = <base>/mcp`,
  `authorization_servers = [<base>]`. `/mcp` answers unauthenticated calls with
  `401` + `WWW-Authenticate: Bearer resource_metadata="…"`.
- `GET /.well-known/oauth-authorization-server` → the endpoints. PKCE S256 is
  required; public clients (`none`) and confidential clients
  (`client_secret_post` / `client_secret_basic`) are both supported.
- Access token: a Run·dere JWT (`typ=oauth`, `aud=<base>/mcp`, ~1 h) naming its
  grant. Refresh token: rotated on every use; replaying an old one revokes the
  grant. Codes are single-use and expire after 60 s; a replayed code revokes the
  grant it produced.
- Loopback redirect URIs (`http://localhost/…`, `http://127.0.0.1/…`) match on
  any port (RFC 8252). Everything else is an exact match.
- Client ID Metadata Documents (CIMD) aren't advertised; Claude falls back to
  dynamic registration.
