# Configuration

All configuration is environment variables with the prefix **`TX_`** (`app/config.py`; the prefix is the project's old name, D-206). Set them in your deployment stack. [self-hosting.md](self-hosting.md) covers a first deployment; this page is the full reference.

## Services Run·dere can use

### Required

| Requirement | Why | Notes |
|---|---|---|
| **Docker Engine + Compose** | Runs the app and its Postgres | Any orchestrator works — a reference stack is provided |
| **Docker network reachability to your tools** | Run·dere talks to the reverse proxy and to each target/geocoder it calls over Docker networking | Put the container on whatever existing Docker network(s) those services use — one or several. The stack *joins* external networks, it doesn't create them (`docker network create` any that are missing). The reference stack uses one external network, `proxy`, plus a private `rundere-internal` one for the database; that is just its shape, not a requirement |
| **PostgreSQL 16** | App database | Bundled in the stack as `rundere-db` — nothing to install separately |

### Optional (Run·dere works without each of these)

| Requirement | Unlocks | Without it |
|---|---|---|
| **A reverse proxy with public HTTPS** | The Gmail and Calendar add-ons (they POST from Google's servers to `/ingest`), the iOS app away from home, and the Claude connector | Run·dere works on your LAN only. The reference stack uses **Traefik** |
| **A Google account** | The Gmail and Calendar capture add-ons | Capture by email forward, file upload, the bookmarklet or the app instead. The add-ons are your own unpublished Apps Script, so no Google review is needed |
| **AdventureLog and/or AirTrail** (external targets) | Also pushing out — flights → AirTrail, trips/places → AdventureLog | Run·dere still records everything to its **own built-in trips** (the default, first-party target); the external pushes simply don't happen |
| **[Ollama](https://ollama.com/)** + a model (`qwen3:14b`) | LLM extractor for free-form text | Structured sources (JSON-LD/iCal/PDF/Maps) still work; unstructured items go to review unparsed |
| **[Photon](https://github.com/komoot/photon)** (self-hosted geocoder) | Address → coordinates without Google quota (D-22) | Geocoding unavailable; places needing lookup stall in review |
| **[SearXNG](https://github.com/searxng/searxng)** (self-hosted metasearch) + Ollama | Discovery's **LLM tier** — visa, safety, events, food & drink, and regional/local language findings | Discovery still runs its deterministic tier (weather/currency/plugs/language basics); the four search-backed topics are simply absent |
| **CrowdSec bouncer** | Hardens the public `/ingest` route | Route works; less abuse protection |

Discovery's deterministic tier also calls two free, keyless public APIs —
[Frankfurter](https://frankfurter.dev/) (currency) and
[Open-Meteo](https://open-meteo.com/) (weather) — over the open internet by
default; both are overridable (`TX_FRANKFURTER_URL`, `TX_OPEN_METEO_URL`,
`TX_OPEN_METEO_ARCHIVE_URL`) if you'd rather point at a self-hosted mirror to
keep Run·dere fully local.

### Setting each one up

- **Docker network reachability** — Run·dere must share a Docker network with your reverse proxy and with each target/tool it calls (AdventureLog, AirTrail, Photon, Ollama). Put it on whatever external network(s) those already use — check with `docker network ls`; the stack joins them, so `docker network create <name>` any that are missing. Whether that's one network or several (the reference stack uses `proxy` for the reverse proxy and a private `rundere-internal` for the database) is up to your setup.
- **Secrets key** (`TX_SECRETS_KEY`) — `openssl rand -base64 32`. Encrypts the per-user AirTrail / AdventureLog tokens at rest (SPEC-8 D-151). Back it up apart from the database. Without it, connections can't be saved; Run·dere itself works fine.
- **AirTrail / AdventureLog (optional, per person)** — nothing to configure on the server. Each person connects their own under **Settings → Connections**, which walks through creating a token in that app. Run·dere works fully without either (D-146). AirTrail needs 3.13 or newer; Run·dere talks to it only through its API. If they run on your LAN, list their hosts in `TX_INTEGRATION_ALLOWED_HOSTS` (e.g. `airtrail:3000,adventurelog-server`); anything else has to be public https (D-152).
- **Ollama** — `docker`-run Ollama on a network Run·dere shares (`proxy` in the reference stack), then `ollama pull qwen3:14b`. Point `TX_OLLAMA_URL` at it.
- **Photon** — self-host the Photon container with an OpenStreetMap index; point `TX_PHOTON_URL` at it.
- **SearXNG** — self-host with its JSON API enabled (`search.formats: [html, json]` in `settings.yml`); point `TX_SEARXNG_URL` at it. Needed only for Discovery's LLM-tier topics (visa/safety/events/food/regional language) — it also requires Ollama.
- **Run·dere `/ingest` API keys** — you create these yourself with the CLI (see [capture.md](capture.md#the-google-add-ons)); one per capture client.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `TX_DATABASE_URL` | `sqlite:///./yonder_dev.sqlite3` | App DB (Postgres in prod). The dev file keeps the old Yonder name |
| `TX_DB_POOL_SIZE` / `TX_DB_MAX_OVERFLOW` | `20` / `20` | Postgres connection pool (40 total matches the worker threads) |
| `TX_DB_POOL_TIMEOUT_SECONDS` | `10` | How long a request waits for a pooled connection before failing |
| `TX_DB_POOL_RECYCLE_SECONDS` | `1800` | Replace pooled connections older than this |
| `TX_MAX_INFLIGHT_REQUESTS` | `64` | Past this many requests in flight, answer `503` + `Retry-After` instead of queueing (`/health` and static files exempt). `0` = off |
| `TX_ARTIFACTS_DIR` | `/data/artifacts` | Immutable captured originals (D-15) |
| `TX_INGEST_MAX_SKEW_SECONDS` | `300` | Signed-timestamp replay window |
| `TX_INGEST_RATE_LIMIT` | `30` | Requests per key per window |
| `TX_INGEST_RATE_WINDOW_SECONDS` | `300` | Rate-limit window |
| `TX_INGEST_MAX_BODY_BYTES` | `5000000` | Max `/ingest` body (5 MB) |
| `TX_OLLAMA_URL` | `http://ollama:11434` | Local LLM extractor endpoint |
| `TX_OLLAMA_MODEL` | `qwen3:14b` | LLM model (OQ-08) |
| `TX_LLM_TIMEOUT_SECONDS` | `600` | LLM call timeout |
| `TX_SECRETS_KEY` | `""` | 32 random bytes (base64 or hex) that encrypt per-user connection tokens (D-151). Empty = connections can't be saved. `TX_SECRETS_KEY_FILE` reads it from a Docker secret |
| `TX_SECRETS_KEYS` | `""` | Keyring for rotation, `v2:<key>,v1:<key>`: the first encrypts, any decrypts. Then `python -m app.cli secrets-rotate` |
| `TX_INTEGRATION_ALLOWED_HOSTS` | `""` | Hosts (or host:port) a connection may reach on the LAN, over http (D-152). Everything else must be public https |
| `TX_PHOTON_URL` | `http://photon:2322` | Self-hosted geocoder (D-22) |
| `TX_DISCOVERY_BASE_CURRENCIES` | `USD` | Always-on base currencies Discovery's resolver/calculator convert FROM (D-65); unioned with each traveller's own travel-profile pick, so these stay visible (e.g. USD for a closed-currency destination) alongside a per-traveller currency. Defaults to USD when unset; set to your home currencies, e.g. `CAD,USD` |
| `TX_FRANKFURTER_URL` | `https://api.frankfurter.dev/v2` | Currency-rate source for Discovery (public, keyless; overridable for a self-hosted mirror) |
| `TX_OPEN_METEO_URL` | `https://api.open-meteo.com/v1` | Forecast weather source for Discovery |
| `TX_OPEN_METEO_ARCHIVE_URL` | `https://archive-api.open-meteo.com/v1` | Historical weather source for Discovery (trips beyond the forecast window) |
| `TX_SEARXNG_URL` | `http://searxng:8080` | Metasearch backend for Discovery's LLM tier (visa/safety/events/food/regional language). Empty = those topics degrade to absent findings |
| `TX_FORWARDED_ALLOW_IPS` | `*` | Which proxies uvicorn trusts for `X-Forwarded-Proto`. Read by the image's start command (`Dockerfile`), not by `app/config.py`. Narrow it to your proxy's address if you need to |
| `TX_AUTH_REQUIRED` | `true` | Sign-in for everything but the public pages (D-203). `false` only for a trusted single-user LAN; start-up warns while it's off |
| `TX_JWT_SECRET` (or `_FILE`) | `""` | Signs sessions and tokens: `openssl rand -base64 32`. Empty = a key that changes on every restart, which signs everyone out (start-up logs an error) |
| `TX_LOGIN_RATE_LIMIT`, `TX_LOGIN_RATE_WINDOW_SECONDS` | `10`, `900` | Failed password sign-ins allowed per address and per username in the window, then refused until it passes |
| `TX_FORGOT_RATE_LIMIT` | `5` | Password-reset requests per address in the same window |
| `TX_MAX_REQUEST_MB` | `32` | Largest request body accepted; bigger ones get 413. `0` = no limit |
| `TX_*_FILE` | — | Read a secret from a file (a Docker secret) instead of the environment: `TX_JWT_SECRET_FILE`, `TX_SECRETS_KEY_FILE`, `TX_DATABASE_URL_FILE`, `TX_SMTP_PASSWORD_FILE`, `TX_INBOX_IMAP_PASSWORD_FILE`, `TX_OIDC_CLIENT_SECRET_FILE` |
| `TX_PUBLIC_BASE_URL` | `""` | Public https base (e.g. `https://rundere.example.net`) for email links and the OAuth/MCP metadata. Set it when using the Claude connector |
| `TX_INBOX_ADDRESS`, `TX_INBOX_IMAP_*` | off | Email-forward capture: a mailbox you own (e.g. `trips@example.com`) read over IMAP; each person forwards to their own `trips+<token>@…`. See [docs/email-forward.md](email-forward.md) |
| `TX_CLAMAV_URL` | `""` | A [clamav-rest](https://github.com/ajilach/clamav-rest) service (e.g. `http://clamav-rest:9000`) that scans every captured email, attachments included, before anything is extracted. An infected email is quarantined to review; if the scanner is unreachable, attachments are skipped (the body is still read). Empty = no antivirus. Static PDF checks (no scripts, launch actions, embedded files; 25 MB / 200 pages) always run |
| `TX_DOCUMENTS_DIR` | `""` | File capture (PDF, .ics, .eml): a local folder for the stored files — any disk you like; mount it into the container. Empty = file capture off. Needs `TX_CLAMAV_URL` too. Rundere writes a `.rundere-documents` marker there and stops storing files if it later goes missing (a drive that didn't mount). A local folder backed up to a NAS is safer than an NFS share mounted `hard`, which hangs uploads while the NAS is away |
| `TX_DOCUMENT_MAX_MB` | `25` | Largest file accepted |
| `TX_DOCUMENT_QUOTA_MB` | `1024` | Each person's file storage; an admin can raise one person's in Administer → Users. Full = new files refused, nothing deleted |
| `TX_RETENTION_MONTHS` | `12` | Stored files and raw captures are removed this long after their trip ends (after they arrived, with no trip); what was extracted stays (SPEC-8 D-196) |
| `TX_INBOX_DROPPED_DAYS`, `TX_INBOX_TRASH_FOLDER` | `30`, auto | Email forward: mail in the dropped folder goes to Trash after this many days; the Trash folder is found automatically unless named |
| `TX_OAUTH_ACCESS_TTL_SECONDS` | `3600` | OAuth access-token lifetime (Claude connector) |
| `TX_OAUTH_REFRESH_IDLE_TTL_SECONDS` | `7776000` | A connected app's grant expires after this long unused (90 days) |
| `TX_OAUTH_APP_REDIRECT_URIS` | `https://rundere.app/auth/callback` | The iOS app's built-in OAuth client `rundere-ios` (D-194). Change only for a fork that ships its own app; empty turns app OAuth sign-in off |

## Single sign-on (OIDC, e.g. Authentik)

| Var | Default | Purpose |
|---|---|---|
| `TX_OIDC_ENABLED` | `false` | Turn SSO on (also needs issuer + client id) |
| `TX_OIDC_ISSUER` | `""` | Issuer URL; `/.well-known/openid-configuration` lives under it. Authentik: `https://auth.example.net/application/o/<slug>/` |
| `TX_OIDC_CLIENT_ID` / `TX_OIDC_CLIENT_SECRET` | `""` | From the IdP's OAuth2/OpenID provider |
| `TX_OIDC_DISPLAY_NAME` | `SSO` | Button label: "Sign in with …" |
| `TX_OIDC_ADMIN_GROUP` | `""` | A `groups` claim value that makes the user an admin (upgrade only) |
| `TX_OIDC_AUTO_CREATE` | `true` | Create an account on first SSO sign-in. An existing account with the same username is adopted |
| `TX_OIDC_HOSTS` | `""` | Hosts that offer "Sign in with SSO" (comma-separated). Empty = every host |
| `TX_OIDC_FORCE_HOSTS` | `""` | SSO-only hosts: `/login` goes straight to the IdP; password sign-in, sign-up and "forgot password" are refused there |
| `TX_OIDC_FORCE_API_LOGIN` | `false` | Also refuse the app's password sign-in on forced hosts. Leave off while the iOS app signs in with a password |
| `TX_OIDC_REDIRECT_URL` | `""` | Fixed callback URL. Leave empty when using the host lists — each listed host then uses `https://<host>/oidc/callback` |

**Force SSO on the public name, keep the LAN as the fallback.** One Authentik redirect URI, and a way in when Authentik is down:

```
TX_OIDC_HOSTS=rundere.example.net
TX_OIDC_FORCE_HOSTS=rundere.example.net
```

`rundere.example.net` always signs in through Authentik (redirect URI `https://rundere.example.net/oidc/callback`); `rundere.home.example` shows only the username/password form. To offer SSO on the LAN too, leave `TX_OIDC_HOSTS` empty and add the LAN callback as a second redirect URI in Authentik. Emailed password-reset links (built from `TX_PUBLIC_BASE_URL`) still open on a forced host; the new password is used on the LAN.
