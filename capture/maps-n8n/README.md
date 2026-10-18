# Google Maps directions capture (n8n form)

> **⚠️ Deprecated as of v1.2 — kept for reference.** Google Maps directions are
> now captured natively in Yonder's built-in **`/capture`** page: paste a
> directions link and set a travel date. It runs internally on the private route
> (no n8n, no API key, no HMAC signing). This workflow is no longer needed; the
> files remain here only to document the original approach.

The capture client for the `maps_url` channel. Google Maps has no Apps Script
add-on surface (unlike Gmail/Calendar), so capture is a small **n8n hosted form**:
paste a directions link + a travel date, and it makes a signed POST to the same
public `/ingest` the add-ons use. n8n is only the capture surface here — it parses
nothing; the server's `extract/maps_url.py` does that. (This is a *thin capture*
role, not the extraction role D-01/D-02 removed from n8n.)

## What it sends

```json
{
  "capture_channel": "maps_url",
  "url": "https://maps.app.goo.gl/…  (or a full /maps/dir/… or ?api=1 link)",
  "capture_context": {
    "travel_date": "2026-08-08",        // REQUIRED — a directions URL has no date (RULE-31)
    "travel_time": "09:30",             // optional
    "end_date": "2026-08-10",           // optional — multi-day drive
    "timezone": "America/Toronto",
    "collection_id": null,              // optional override (D-05)
    "title": null, "notes": null,
    "submitted_by": "maps-n8n"
  }
}
```

The server accepts three URL shapes (D-61): the `/maps/dir/A/B/C` path, the
`?api=1&origin=…&destination=…&waypoints=A|B` share format, and a
`maps.app.goo.gl` / `goo.gl/maps` short link (expanded server-side by following
its redirect — Google hosts only). City/region stops (`Toronto, ON`) are split so
they geocode; coordinate stops become lat/lon directly.

## Setup

1. **Create an API key** for this client:
   ```bash
   docker exec -it yonder-yonder-1 python -m app.cli create-key maps-n8n
   ```
   Copy the key it prints once.

2. **Import the workflow.** In n8n: *Workflows → Import from File →*
   [`workflow.json`](workflow.json). It creates four nodes:
   `Trip form` (Form Trigger) → `Config` → `Sign & build` (Code) → `POST to /ingest`.

3. **Fill in the Config node** (the only edit needed):
   - `apiKey` → the key from step 1
   - `ingestUrl` → leave as `http://yonder:8000/ingest` if n8n shares the
     docker network with the app; otherwise your public
     `https://rundere.example.com/ingest`.

4. **Activate** the workflow. The Form Trigger gives you a hosted form URL
   (*Trip form* node → *Production URL*). Open it on desktop or phone.

The `Sign & build` Code node is [`code-node.js`](code-node.js) in this repo (kept
in sync there for review). It signs `HMAC_SHA256(key, "<ts>." + body)` exactly like
`app/auth.py`, and the HTTP node sends the **raw** signed string — do not switch
its body type away from *Raw*, or a re-serialization will break the signature.

## Using it

1. In Google Maps, get directions (origin → waypoints → destination) and **Share
   → Copy link** (or copy the address-bar URL on desktop).
2. Open the form, paste the link, pick the **travel date** (the one required
   field), optionally add a time / end date / title / notes, and submit.
3. A batch lands on `/ingest`. Click **Process new captures** in the review queue
   (or wait for the scheduled worker, once it exists) → it becomes a
   `ground_transport` road item you review, approve, and log.

## Notes & limits

- **Travel date is mandatory.** A directions URL is a plan with no date; the
  server refuses to guess one (RULE-31) and would send the item to review.
- **Collection is a free-text override, not a picker.** An n8n form can't list
  your AdventureLog collections, so leave `Collection ID` blank in the normal
  case — the resolver matches the trip by date automatically (D-05 is the manual
  override for when it can't).
- **Home drives are suppressed.** A drive that stays within your home radius
  (GTA, ~50 km — OQ-02) is not logged as a trip; Toronto→Ottawa is, a hop to
  Hamilton isn't.
- **Round trips** (same origin and destination) are detected and flagged.

## Verifying the plumbing

Signing parity and ingest acceptance were verified against the live services
(Node HMAC == `app/auth.py`; a signed maps POST returns 202). To watch your own
capture land:

```bash
docker exec yonder-yonder-db-1 psql -U travel -d travel \
  -c "SELECT id, received_at, payload->>'capture_channel' AS ch FROM ingest_batches ORDER BY received_at DESC LIMIT 5;"
```

A row with `ch = maps_url` right after you submit the form is the whole client
working.
