# Capture

How travel documents get into Run·dere. Every way in ends in the same place: the raw
capture is stored as it arrived (content-addressed, D-15), owned by the person who sent it
(D-120), then read into items that wait in the **Inbox** for review. Nothing is written to a
trip until you approve it (D-31).

In the app, **Capture Hub → Set up** has a step-by-step guide for each channel, with your own
keys and addresses filled in. This page is the overview.

## Ways in

| Channel | Use it for | Needs |
|---|---|---|
| **Capture Hub** (`/capture`) | Pasting a booking page's source, a public URL, or a Google or Apple Maps directions link with a travel date | Nothing extra |
| **Bookmarklet** | One click on any booking page in your browser, including pages behind a login | Nothing extra |
| **File upload** | PDF confirmations, `.ics` invites, saved `.eml` emails | `TX_DOCUMENTS_DIR` and a virus scanner (`TX_CLAMAV_URL`), see [configuration.md](configuration.md) |
| **Email forward** | Forwarding confirmations from any email account to your own `trips+<token>@…` address | A mailbox Run·dere reads over IMAP, see [email-forward.md](email-forward.md) |
| **Gmail add-on** | Capturing the email you're reading, from Gmail's side panel | A public HTTPS route to `/ingest` |
| **Google Calendar add-on** | Capturing an open calendar event | A public HTTPS route to `/ingest` |
| **iPhone and iPad app** | Capturing from the share sheet or inside the app | The app, see the README |
| **AI assistants (MCP)** | Asking an assistant such as Claude to capture a link | The `/mcp` connector, see [claude-connector.md](claude-connector.md) |

Extraction tries structured readers first (schema.org JSON-LD, iCal, PDF text, Maps URL
formats) and falls back to the local LLM only for free-form text (D-10, D-11).

## The Capture Hub and bookmarklet

`/capture` needs you to be signed in, like the rest of the app (D-203), and every capture
is yours. There's no key to set up.

- **Bookmarklet.** Drag it from **Capture Hub → Set up → Bookmarklet** to your bookmarks bar.
  On a booking page it opens a Run·dere tab and hands the page over (D-159). Your browser
  has already loaded the page, so this works for pages behind a login. If pop-ups are
  blocked it copies the page to your clipboard to paste instead.
- **Paste or URL.** Paste a page's HTML source, or a **public** URL that Run·dere fetches
  itself. Only public hosts are fetched (the SSRF policy, D-152), so use the bookmarklet
  for pages behind a login.
- **Maps directions.** Paste a directions link (`maps.app.goo.gl`, `/maps/dir/…`,
  `?api=1&origin=…&destination=…`, or Apple Maps) and set a **travel date**, since a route
  has no date of its own (RULE-31).

## The Google add-ons

The add-ons post from Google's servers, which can't sign in interactively, so they use
`/ingest` with a per-person key and a signature (below). Keep `/ingest` as the only path
on your public route if the rest of the app should stay on your LAN.

**Make your key** in **Capture Hub → Set up → Gmail** (or **Calendar**). It's shown once and
belongs to you, so what the add-on sends is yours. Making another replaces the old one.
An admin can also make keys from the shell:

```bash
docker compose exec rundere python -m app.cli create-key gmail-addon --user <username>
```

Each add-on has a detailed README in `capture/`. Condensed steps follow.

### Gmail add-on (Apps Script)

Full guide: [`capture/gmail-addon/README.md`](../capture/gmail-addon/README.md).

1. **script.google.com → New project.**
2. Project Settings (⚙️) → check **"Show appsscript.json"** → paste [`capture/gmail-addon/appsscript.json`](../capture/gmail-addon/appsscript.json).
3. Replace `Code.gs` with [`capture/gmail-addon/Code.gs`](../capture/gmail-addon/Code.gs).
4. **Deploy → Test deployments → Install add-on** (installs for your account only; approve the "unverified app" screen — expected for your own script). Scope: `gmail.addons.current.message.readonly` (it only reads the open email; no labels are touched, D-164).
5. In Gmail, open the add-on panel → paste your key on the homepage card → **Save**.

### Google Calendar add-on (Apps Script)

Full guide: [`capture/gcal-addon/README.md`](../capture/gcal-addon/README.md).

Identical flow to Gmail, with Calendar scopes:

1. **script.google.com → New project** (or merge into the Gmail project — see that README).
2. Show `appsscript.json` → paste [`capture/gcal-addon/appsscript.json`](../capture/gcal-addon/appsscript.json). It enables the **Advanced Calendar Service** (needed to read timezone / iCalUID / attendees / recurrence).
3. Replace `Code.gs` with [`capture/gcal-addon/Code.gs`](../capture/gcal-addon/Code.gs).
4. **Deploy → Test deployments → Install add-on.** Scopes: `calendar.addons.current.event.read`, `calendar.readonly`, `script.external_request`.
5. In Google Calendar, open the add-on panel → paste your Calendar key → **Save**.

## `/ingest` authentication

Revocable per-person API keys (stored sha256-hashed) plus a signed timestamp. Any
client that can sign a request can use it, so a script can too:

```
X-Api-Key:   tx_<hex>
X-Timestamp: <unix seconds>          # rejected outside a 5-minute window
X-Signature: HMAC_SHA256(key, "<timestamp>." + body) as hex
```

Returns `202 {"batch_id": ...}`; the batch is stored and content-addressed
(D-15) for async processing. Rejections are explicit: `401` (bad key/signature/
skew), `413` (body over `TX_INGEST_MAX_BODY_BYTES`), `429` (rate-limited).

## Home profiles

**Home profiles** decide which places are never logged as "visited" and which
local drives aren't trips. Manage them in the admin UI (**`/admin → Home
Profiles`**, full CRUD with per-field help), or bootstrap from a file:

```bash
# copy docs/home-profiles.example.json, fill it in, then:
docker compose exec rundere python -m app.cli import-home /data/home-profiles.json
```
