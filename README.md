<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="design/logo/rundere-lockup-horizontal-silver.svg">
    <img src="design/logo/rundere-lockup-horizontal-night.svg" alt="Run·dere" width="360">
  </picture>

# Run·dere

A self-hosted travel planner for you and the people you travel with.

[![CI](https://github.com/rundere/rundere-server/actions/workflows/ci.yml/badge.svg)](https://github.com/rundere/rundere-server/actions/workflows/ci.yml)
[![CodeQL](https://github.com/rundere/rundere-server/actions/workflows/codeql.yml/badge.svg)](https://github.com/rundere/rundere-server/actions/workflows/codeql.yml)
[![Release](https://img.shields.io/github/v/release/rundere/rundere-server?sort=semver&display_name=tag)](https://github.com/rundere/rundere-server/releases/latest)
[![Image](https://img.shields.io/badge/GHCR-rundere--server-2496ED?logo=docker&logoColor=white)](https://github.com/rundere/rundere-server/pkgs/container/rundere-server)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-6B7280.svg)](LICENSE)
[![Demo](https://img.shields.io/badge/demo-try_it-111827)](https://demo.rundere.app)

**Version 4.12.11** · [Documentation](docs/) · [Demo](https://demo.rundere.app) · [Releases](https://github.com/rundere/rundere-server/releases) · [Docker image](https://github.com/rundere/rundere-server/pkgs/container/rundere-server) · [rundere.app](https://rundere.app)
</div>

Forward a booking email, drop in a PDF or click a bookmarklet, and Run·dere reads the
flight, hotel, train or tour out of it, asks you to check it, and puts it on the right trip.
Each trip gets a timeline, a map, research for the destination, a packing list, shared
costs and a travel journal. It runs on your own server, and the reading is done by
structured parsers and a local LLM, so your bookings stay with you.

Say it "run there". Plain-text spelling: Rundere.

<p align="center">
  <img src="docs/images/home.png" alt="Run·dere's Home page: the next activity on an upcoming trip, the Inbox and upcoming trips" width="49%">
  <img src="docs/images/trip-timeline.png" alt="A trip's day-by-day timeline with a flight, a hotel and a museum visit" width="49%">
</p>

## What it does

- **Capture from anywhere.** Email forwarding, file upload (PDF, `.ics`, `.eml`), a
  bookmarklet, Gmail and Google Calendar add-ons, the iPhone and iPad app, and AI
  assistants over MCP.
- **Read it reliably.** Structured readers go first (schema.org booking markup, calendar
  invites, PDF text, Google and Apple Maps links). Only free-form text goes to the LLM,
  which runs on your own hardware.
- **Review before anything changes.** Everything lands in the **Inbox**, sorted by what
  needs you. Nothing reaches a trip until you approve it, and your corrections stick.
- **Plan the trip.** A timeline and map, **Discover** research (weather, currency, plugs,
  visa, safety, food and drink, local phrases), packing lists, budgets and shared expenses,
  and a private travel journal.
- **Keep places for later.** Share a place from Google or Apple Maps and it waits in your
  saved places until it goes on a trip, or on several.
- **Travel together.** Several people on one server, each seeing only their own trips and
  the ones shared with them. Sharing can be view-only.
- **Use what you already have.** Optionally also log flights to
  [AirTrail](https://github.com/johanohly/AirTrail) and trips to
  [AdventureLog](https://github.com/seanmorley15/AdventureLog). Run·dere works fully
  without them.

## Try the demo

**[demo.rundere.app](https://demo.rundere.app)** is a read-only server with three invented
trips that move forward with the calendar. The sign-in page shows the shared login.

## Install

Run·dere is one container plus a Postgres database, behind a reverse proxy that handles
HTTPS.

```bash
curl -O https://raw.githubusercontent.com/rundere/rundere-server/main/deploy/docker-compose.example.yml
# replace every CHANGE_ME (database password, TX_JWT_SECRET, TX_SECRETS_KEY:
# openssl rand -base64 32) and the example hostnames
docker network create proxy   # unless your reverse proxy already has one
docker compose -f docker-compose.example.yml up -d
```

Then open the server in a browser. The first visit creates the admin account (D-203), so
do that before anyone else can reach it. The image is built for Linux on amd64.

- **[Self-hosting guide](docs/self-hosting.md)**: secrets, the proxy, sign-in, optional
  services, backups and upgrades.
- **[Running a demo server](docs/demo-server.md)** of your own.
- To work on the code, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Updating

1. Read the [release notes](https://github.com/rundere/rundere-server/releases), especially
   **Before you upgrade**: it lists migrations, new settings and anything to run once.
2. Back up the database, the artifacts folder and `TX_SECRETS_KEY`
   ([backups](docs/self-hosting.md#6-backups)).
3. Change the image tag and redeploy. Database migrations run when the container starts.

Pin a version (`ghcr.io/rundere/rundere-server:4.12`, or an exact one) rather than `latest`
if you want to choose when to upgrade and be able to roll back.

## A few things to know

- **It needs HTTPS in front of it.** Run·dere expects a reverse proxy such as Traefik or
  Caddy. The iPhone app and AI assistants also need a public hostname.
- **A local LLM does most of the reading.** Without one, Run·dere still reads what has
  structure (booking markup, calendar invites and Maps links), but most booking emails are
  free-form, so they wait in the Inbox for you to fill in by hand. Geocoding, web search,
  email and file scanning are optional too: each adds a feature, and nothing breaks
  without it.
- **Captures are kept on purpose.** The original of every booking is kept until 12 months
  after its trip, so a capture can be re-read and checked. Plan disk space for it.
- **It isn't encrypted against the server's operator.** Sharing settings decide what
  admins see in the app; whoever runs the server can still read the database.
- **Old names show through.** The project was called Yonder, and the `TX_*` settings, the
  Python package and some code keep that name (D-206).

## Privacy

- **Your data stays on your server.** Run·dere has no cloud service and sends nothing
  home.
- **Extraction is local.** Bookings are read by parsers and by the LLM you host (Ollama).
  No booking is sent to a cloud AI unless you connect an assistant yourself.
- **A few public lookups, all replaceable.** By default the server fetches map tiles from
  OpenStreetMap, weather from Open-Meteo and exchange rates from Frankfurter. Each can be
  pointed at your own mirror ([configuration](docs/configuration.md)).
- **People are kept apart.** Each person sees their own trips and the ones shared with
  them, and journals are never shared. New accounts are private: an admin sees only the
  trips you put them on, unless you choose to share more with admins (D-220).
- **Less is kept.** No photos, receipts or audio are stored (journal photos are links).
  Original captures and uploaded files are deleted 12 months after their trip ends, by
  default (D-196).
- Integration tokens are encrypted at rest, and URLs the server fetches for you are
  checked so they can't reach your private network (D-151, D-152).

## Capture

| Way in | Setup |
|---|---|
| Capture page and bookmarklet | Nothing. Paste a page, a URL or a Maps directions link, or click the bookmarklet on a booking page |
| File upload | A folder for files and a ClamAV scanner |
| Email forward | A mailbox the server reads; each person forwards to their own address |
| Gmail and Calendar add-ons | Your own Apps Script copy and a personal key |
| iPhone and iPad app, AI assistants | Sign in from the app, or connect `/mcp` |

In the app, **Capture → Set up** walks through each one. **[docs/capture.md](docs/capture.md)**
covers them all, including the signed `/ingest` endpoint for scripts.

## Workflow

1. **Capture** a booking.
2. Run·dere **reads it on its own** and matches it to a trip.
3. **Check it in the Inbox**, fix anything, and approve.
4. It appears on the trip's **timeline**, and in AirTrail or AdventureLog if you've
   connected them.
5. **Plan** with Discover, packing, costs and the journal.

**[docs/workflow.md](docs/workflow.md)** has the details and alerts for admins.
**[docs/howto.md](docs/howto.md)** walks through each screen.

## Configuration

Everything is set with `TX_*` environment variables, and secrets can come from Docker
secret files. The essentials:

| Setting | What it's for |
|---|---|
| `TX_DATABASE_URL` | Postgres in production (SQLite for development) |
| `TX_JWT_SECRET`, `TX_SECRETS_KEY` | Signing sign-ins, and encrypting each person's integration tokens |
| `TX_PUBLIC_BASE_URL` | Your public address, for email links, the app and AI assistants |
| `TX_OLLAMA_URL`, `TX_PHOTON_URL`, `TX_SEARXNG_URL` | Optional local LLM, geocoder and web search |
| `TX_OIDC_*` | Optional single sign-on (Authentik, Keycloak, …) |

**[docs/configuration.md](docs/configuration.md)** is the full reference.

## Apps and assistants

- **iPhone and iPad.** The native app is in its own repository and talks to this
  server's API. [rundere.app](https://rundere.app) has the details.
- **AI assistants.** The server has a built-in MCP endpoint at `/mcp` with OAuth sign-in,
  so Claude and other MCP clients can read your trips, add to them and save research,
  acting as you. Nothing is connected unless you connect it
  ([docs/claude-connector.md](docs/claude-connector.md)).

## Documentation

Guides are in [`docs/`](docs/) until the project has a wiki:

| | |
|---|---|
| [Self-hosting](docs/self-hosting.md) · [Configuration](docs/configuration.md) | Running a server |
| [Capture](docs/capture.md) · [Email forward](docs/email-forward.md) · [Email](docs/email.md) | Getting bookings in |
| [Workflow](docs/workflow.md) · [How-to](docs/howto.md) | Using it day to day |
| [AI assistants](docs/claude-connector.md) · [Demo server](docs/demo-server.md) | Extras |
| [Architecture](docs/architecture.md) · [Decisions](DECISIONS.md) · [Rules](RULES.md) | How it works and why |

## AI usage

Much of Run·dere is written with AI coding assistants, mainly Claude Code, and the
repository says so: [AGENTS.md](AGENTS.md) holds the rules every contributor and agent works
to, and commits made with an assistant carry a `Co-Authored-By` line. The maintainer
decides what gets built, writes down the decisions behind it ([DECISIONS.md](DECISIONS.md)
and the specs), and reviews every change. Every change goes through a pull request and
needs the full test suite, the extraction regression set and the pre-publish checks to
pass before it merges.

## Contributing and security

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) and the
[code of conduct](CODE_OF_CONDUCT.md). Please report vulnerabilities privately as
[SECURITY.md](SECURITY.md) describes.

Images are published as `ghcr.io/rundere/rundere-server` from the first public release
on; earlier ones were private (D-207).

## Licence

[GNU Affero General Public License v3.0](LICENSE). If you run a modified Run·dere for
others, you must offer them its source; the **About** page links to it (D-202).
Run·dere talks to AirTrail and AdventureLog over their APIs and doesn't include their code.
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) lists the work it builds on.
