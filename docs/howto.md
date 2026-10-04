# Run·dere (formerly Yonder) — Setup & Usage Guide

> Current for **v4.7.0**. Screenshots are being recaptured against the current
> UI; `_[screenshot: …]_` marks where each one belongs. The older
> `docs/howto.html` predates the trip pickers and Run·dere's first-party writes —
> treat it as historical, not current.

## 1 What you need

- Docker Engine + Compose
- PostgreSQL 16 (bundled as `yonder-db` in the stack)
- A reverse proxy that exposes **only** `POST /ingest` over public HTTPS
  (e.g., Traefik). The Run·dere container must reach the proxy and any tools it
  calls; put it on whatever existing Docker network(s) those use — one or several.
- Google account for the Gmail/Calendar add-ons
- Optional external targets: AirTrail (flights), AdventureLog (trips/places).
  Without them Run·dere still records everything to its own built-in trips.
- Optional services: Ollama + an LLM model (extractor for free-form text, and
  Discovery's LLM tier), Photon (geocoder), SearXNG (Discovery's LLM-tier web
  search), AirTrail's read-only Postgres (flights to never-flown airports),
  CrowdSec (hardens `/ingest`)

> _[screenshot: Run·dere stack overview]_

## 2 Deploy Run·dere

1. Pull the image (built by CI on each version tag), or build locally:

   ```bash
   docker pull ghcr.io/jeeves5454/rundere-server:latest
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
- A traveller can carry a nationality (used by Discovery's visa resolver) and
  is the person a Discovery/Packing profile ultimately hangs off of.

## 6 Connect capture surfaces

### Gmail & Google Calendar add-ons

Personal Apps Script projects installed for your own account. They POST matched
items to `/ingest`.

### Web pages & Maps directions (built in)

The built-in Capture page requires no external client.

## 7 The dashboard — your starting point

Open Run·dere's root URL (e.g. `https://yonder.home.example/`) and you land on the
**dashboard**, not a 404. It's the front door for daily use:

- **What's next** — the trip currently under way, or the soonest upcoming one,
  with a day countdown and a link straight into it.
- **Review pipeline** — a live snapshot of the same buckets the review queue
  uses (Needs you / Waiting for a trip / Ready to Approve / Approved / Logged /
  Total), each one a link straight into that bucket.
- **Capture activity** — how much has come in today, what's still waiting to
  be processed, and what couldn't be read.
- **Go to** — one-click links into Capture, Review, Trips, and every settings
  page (Home profiles, Packing profile, Discovery profile, Travellers, Rules,
  Tables).

> _[screenshot: the dashboard — next trip, pipeline snapshot, quick links]_

## 8 Capture a web page or drive

### Bookmarklet (primary, for web pages)

Use the bookmarklet on the Capture page to capture the current page.

### Paste source, a public URL, or a Maps link

You can paste raw HTML source, provide a public URL, or paste a Google Maps
directions link into the Capture form.

> _[screenshot: the built-in Capture page — bookmarklet and paste/URL/Maps form]_

## 9 Process & review

- Click **Process new captures** to run extraction and resolution on demand.
- The review queue sorts items into buckets you work left to right:

  `Needs you → Waiting for a trip → Ready to Approve → Approved → Logged → Everything`

> _[screenshot: the review queue, items sorted into buckets]_

## 10 Correct & approve

1. Open an item to see what was read. Every field is editable, and **anything
   you change is locked** so a later capture can't overwrite your correction.
2. Choose who flew.
3. Use the two independent trip pickers:
   - **AdventureLog** — file the item into a specific collection; candidates
     whose dates contain the item are marked.
   - **Run·dere** — link it to one of your in-house Run·dere trips (independent of
     AdventureLog). If exactly one Run·dere trip's dates contain the item, it is
     linked automatically.
4. Approving only **stages** the item; nothing is written until you log the batch.
5. Editing or re-assigning re-checks the item.

> _[screenshot: reviewing an item — what was read, what it will write, Approve]_

## 11 Log to your trackers

Two-step and irreversible:

1. A **dry-run preview** shows exactly what each approved item will write:
   - **AirTrail** — the flight
   - **AdventureLog** — transportation/lodging/note + locations, filed under the trip
   - **Run·dere** — a trip activity on the linked trip

   with a "cannot be undone" warning.
2. Confirm to execute.
3. Re-running is safe: an already-written record is skipped (each target dedups),
   so nothing double-writes.

> _[screenshot: the two-step log — dry-run preview, then confirm]_

## 12 Your trips — timeline, Discovery, Planning

Open **Trips** to work with Run·dere's own trip system directly — it exists
alongside AirTrail/AdventureLog, not only as a mirror of them.

### Timeline

Every trip has a day-by-day timeline of activities (flights, lodging, drives,
notes, and more). Captured items linked to the trip appear automatically;
anything else you add by hand from the trip page.

### Who's traveling

Add companions from your saved Travellers, or quick-add a generic guest by
name. The `self` traveller is linked automatically when you create a trip.

### Discovery — the pre-trip briefing

Click **Run Discovery** on a trip to generate a local research pass over nine
topics: weather, currency (with a per-trip currency calculator), plugs &
voltage, visa & vaccination, safety & transport, events & activities, food &
drink, language, and packing pointers. Weather and currency come back in
seconds from free public APIs; visa/safety/events/food and regional language
take longer — they search the web through your self-hosted SearXNG and
synthesize with your local Ollama model, so nothing leaves your network.

Every finding is reviewable: edit the text, re-run a single topic, or leave it
as-is. Set your **Discovery profile** (**Administer → Discovery profile**) once
— dietary restrictions, drink/activity interests, a budget ceiling, mobility
needs — so the searches reflect what you'd actually want to see.

> _[screenshot: a trip's Discovery findings, grouped by topic]_

### Planning — the Packing Plan

Click **Generate packing plan** on a trip to turn its dates, derived locations,
Discovery findings, and detected trip type(s) (beach, business, hiking, …)
into a categorized, bag-assigned checklist. Tick items off as you pack, remove
ones you don't need (bring them back any time), or add a trip type by hand if
auto-detection missed one.

Set your **Packing profile** (**Administer → Packing profile**) once — bag
size, laundry comfort, packing style, must-have items — so every plan starts
from your real habits.

> _[screenshot: a trip's Packing Plan, categorized and bag-assigned]_

### Syncing with AdventureLog

Push a trip to AdventureLog (**Push to AdventureLog** on the trip page) to
create a linked collection. From then on, `/trips/{id}/adventurelog` is a
two-way sync page:

- **Push** selected timeline activities and approved Discovery findings into
  the collection. Re-pushing is safe — Run·dere marks what it wrote and updates
  those records in place instead of duplicating them; a note that would
  overwrite something already in AdventureLog asks you to confirm first.
- **Pull** AdventureLog's own transportation/lodging records into the Run·dere
  timeline, in case someone edited the trip directly in AdventureLog.

> _[screenshot: the AdventureLog sync page — push/pull selections]_

## 13 Scenarios & edge cases

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

### Discovery without SearXNG/Ollama configured

Weather, currency, plugs, and language still generate (the deterministic
tier). Visa, safety, events, and food simply don't produce findings — nothing
errors, those topics are just absent until both services are configured.

## 14 Version & About

The **Administer → About** page shows the running version (currently 3.8.1),
the capture surfaces, the source repository, and the license. The
**dashboard** (the root URL) is the faster day-to-day check: what's next, and
what's moving through the pipeline right now.

> _[screenshot: the About page]_
