# Yonder — Setup & Usage Guide

> Current for **v2.2.1**. Screenshots are being recaptured against the current
> UI; `_[screenshot: …]_` marks where each one belongs. The older
> `docs/howto.html` predates the trip pickers and Yonder's first-party writes.

## 1 What you need

- Docker Engine + Compose
- PostgreSQL 16 (bundled as `yonder-db` in the stack)
- A reverse proxy that exposes **only** `POST /ingest` over public HTTPS
  (e.g., Traefik). The Yonder container must reach the proxy and any tools it
  calls; put it on whatever existing Docker network(s) those use — one or several.
- Google account for the Gmail/Calendar add-ons
- Optional external targets: AirTrail (flights), AdventureLog (trips/places).
  Without them Yonder still records everything to its own built-in trips.
- Optional services: Ollama + an LLM model (extractor for free-form text),
  Photon (geocoder), AirTrail's read-only Postgres (flights to never-flown
  airports), CrowdSec (hardens `/ingest`)

> _[screenshot: Yonder stack overview]_

## 2 Deploy Yonder

1. Pull the image (built by CI on each version tag), or build locally:

   ```bash
   docker pull ghcr.io/jeeves5454/yonder:latest
   ```

2. Deploy the reference Portainer/Compose stack.
3. Set configuration via `TX_`-prefixed environment variables.
4. Database migrations run automatically when the container starts.

## 3 Create capture keys

```bash
python -m app.cli create-key <client-name>
```

One `/ingest` API key per capture client; it is printed once, so keep it safe.

## 4 Set up home profiles

- One-time setup: **Administer → Home profiles**.
- Define your home airport(s) and area(s).
- Places inside home are never logged as somewhere you "visited" — only as a
  journey's endpoint; purely-home movement is not a trip.

> _[screenshot: Home profiles UI]_

## 5 Set up travellers

- Define traveller identities in **Administer → Travellers** (self or guest).
- On a flight, pick who flew. Only a *self* traveller logs to AirTrail; guests
  never do.

## 6 Connect capture surfaces

### Gmail & Google Calendar add-ons

Personal Apps Script projects installed for your own account. They POST matched
items to `/ingest`.

### Web pages & Maps directions (built in)

The built-in Capture page requires no external client.

## 7 Capture a web page or drive

### Bookmarklet (primary, for web pages)

Use the bookmarklet on the Capture page to capture the current page.

### Paste source, a public URL, or a Maps link

You can paste raw HTML source, provide a public URL, or paste a Google Maps
directions link into the Capture form.

> _[screenshot: the built-in Capture page — bookmarklet and paste/URL/Maps form]_

## 8 Process & review

- Click **Process new captures** to run extraction and resolution on demand.
- The review queue sorts items into buckets you work left to right:

  `Needs you → Waiting for a trip → Ready to Approve → Approved → Logged → Everything`

> _[screenshot: the review queue, items sorted into buckets]_

## 9 Correct & approve

1. Open an item to see what was read. Every field is editable, and **anything
   you change is locked** so a later capture can't overwrite your correction.
2. Choose who flew.
3. Use the two independent trip pickers:
   - **AdventureLog** — file the item into a specific collection; candidates
     whose dates contain the item are marked.
   - **Yonder** — link it to one of your in-house Yonder trips (independent of
     AdventureLog). If exactly one Yonder trip's dates contain the item, it is
     linked automatically.
4. Approving only **stages** the item; nothing is written until you log the batch.
5. Editing or re-assigning re-checks the item.

> _[screenshot: reviewing an item — what was read, what it will write, Approve]_

## 10 Log to your trackers

Two-step and irreversible:

1. A **dry-run preview** shows exactly what each approved item will write:
   - **AirTrail** — the flight
   - **AdventureLog** — transportation/lodging/note + locations, filed under the trip
   - **Yonder** — a trip activity on the linked trip

   with a "cannot be undone" warning.
2. Confirm to execute.
3. Re-running is safe: an already-written record is skipped (each target dedups),
   so nothing double-writes.

> _[screenshot: the two-step log — dry-run preview, then confirm]_

## 11 Scenarios & edge cases

### An item "Needs you"

It needs a human decision — usually which trip it belongs to (pick one in the
AdventureLog picker) or a missing detail to fix. A flight whose trip is still
unresolved is still approvable and logs to AirTrail; its AdventureLog trip record
simply waits until you pick or create a trip.

> _[screenshot: a "Needs you" item — a flight with no single matching trip]_

### Trains, buses & ferries keep their real time

Local departure/arrival times are preserved (recovered from the station's
timezone when the document omits one), not collapsed to date-only.

### Events with no listed time

An event with only a date logs as a date-only entry rather than a fake midnight.

### Heavy / strict-CSP pages

If a page blocks the bookmarklet, paste the source or the URL into the Capture
form instead.

## 12 Version & About

The **Administer → About** page shows the running version (currently 2.2.1), the
capture surfaces, the source repository, and the license.

> _[screenshot: the About page]_
