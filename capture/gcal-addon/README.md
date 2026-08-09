# Google Calendar capture add-on

The Calendar sibling of the [Gmail add-on](../gmail-addon/README.md). Turns "open
an event in Google Calendar" into a stored, content-addressed batch on the
yonder `/ingest` endpoint. **Capture only** — it does not decide
anything; the server maps the event in `extract/gcal.py`.

Why an add-on and not a background sync: same reason as Gmail (D-02 removed
polling). This runs only when you click the button on an event you have open — no
trigger, no standing calendar access beyond reading the one event.

## What it sends

The Google Calendar API **Event resource** (a curated subset), as JSON:

```json
{
  "capture_channel": "gcal",
  "event": {
    "id": "abc_20260901", "iCalUID": "abc@google.com", "status": "confirmed",
    "summary": "Dinner with Tricia", "location": "Silver Diner, 123 Main St",
    "description": "usually 6:30",
    "start": { "dateTime": "2026-09-01T18:30:00-04:00", "timeZone": "America/New_York" },
    "end":   { "dateTime": "2026-09-01T20:30:00-04:00", "timeZone": "America/New_York" },
    "organizer": { "email": "tricia@example.com", "displayName": "Tricia" },
    "attendees": [ { "email": "you@gmail.com" } ],
    "recurrence": null
  },
  "capture_context": { "submitted_by": "you@gmail.com", "notes": null, "collection_id": null }
}
```

The server (`extract/gcal.py`) maps this to the **same event Payload** a
Gmail-captured `.ics` invite produces, so both resolve and commit identically. All-day
events send `start.date` / `end.date` instead of `dateTime` and stay date-precision
(never a fabricated 00:00, D-06). Google Calendar always uses IANA zone names, so the
Windows-name problem that D-56 fixed for Outlook invites does not arise here.

`collection_id`, if you fill it in, becomes `capture_context.collection_id` — the
human-selected-collection short-circuit (D-05): the resolver attaches the item to that
trip and never overrides your choice.

## Setup (one time, per Google account)

Identical flow to the Gmail add-on, with Calendar scopes.

1. Go to **script.google.com** → New project. (You can keep this separate from the
   Gmail add-on, or later merge both into one project — see *Merging* below.)
2. Project Settings (⚙️) → check **"Show appsscript.json"**, then replace the
   manifest with [appsscript.json](appsscript.json). It enables the **Advanced
   Calendar Service** (`dependencies.enabledAdvancedServices`) — needed to read the
   full event (timezone, iCalUID, attendees, recurrence) that the trigger context
   alone doesn't carry.
3. Replace `Code.gs` with [Code.gs](Code.gs) from this repo.
4. **Deploy → Test deployments → Install add-on.** Installs for your account only —
   no Google review, no public listing. Scopes:
   `calendar.addons.current.event.read` (the open event),
   `calendar.readonly` (fetch its full resource), `script.external_request` (POST to
   `/ingest`). All *sensitive*, not *restricted* — no CASA/verification for personal
   use. You'll see the "this app isn't verified" screen on first authorization
   (your own unpublished script) → **Advanced → Go to (unsafe)**; expected.
5. Open Google Calendar, refresh, open the add-on side panel (event icon), and on the
   **homepage card** paste the API key created with:
   ```bash
   docker exec -it yonder-yonder-1 python -m app.cli create-key gcal-addon
   ```
   Leave the Ingest URL as default unless you changed the public hostname in
   `portainer-stack.yml`. **Save** — stored in your script's User Properties, never in
   code. If you already made a key for the Gmail add-on you can reuse it, but a
   distinct `gcal-addon` key makes the ingest logs easier to read.

## Using it

Open any calendar event → the add-on panel shows the event title → optionally add a
note or force a collection ID → **Capture to Yonder**. A toast shows the
resulting `batch_id`, or the exact rejection reason (401 variants, 413 too large,
429 rate-limited) straight from the server.

## Merging with the Gmail add-on (optional)

A single Apps Script project can host **both** the Gmail and Calendar add-ons: merge
the two `Code.gs` files (the config/homepage and `postToIngest_`/`bytesToHex_`/
`notify_`/`currentUserEmail_` helpers are identical — keep one copy) and combine the
manifests' `oauthScopes`, `addOns.gmail`, and `addOns.calendar` blocks plus the
Calendar advanced-service dependency. One install, one API key, both surfaces. Kept
separate here so adding Calendar can't disturb the already-working Gmail add-on.

## Troubleshooting

Most failure modes mirror the Gmail add-on's README (read it too). Calendar-specific:

**"The script does not have permission… calendar.addons.execute"** — the manifest was
missing the base scope every Calendar add-on needs to run. Re-copy `appsscript.json`,
save, reload Calendar, and reopen the panel to re-authorize (or Run `buildHomepageCard`
once from the editor to force the consent dialog).

**"Could not read the event: … Calendar is not defined"** — the Advanced Calendar
Service isn't enabled. Confirm `appsscript.json`'s `dependencies.enabledAdvancedServices`
has the `Calendar` v3 entry, save, and reinstall the test deployment so the new
dependency is authorized.

**"Capture FAILED (401): bad signature"** — the HMAC signing is byte-for-byte identical
to the Gmail add-on (sign the exact bytes sent). If you hand-edited `postToIngest_`,
re-copy it from this repo. See the Gmail README for the underlying reason.

## Known limitations (MVP, not bugs)

- **One event per capture.** A recurring series sends the single instance you have open;
  its `iCalUID` is the series identity, so re-capturing another instance is recognised as
  the same event by the resolver rather than duplicated.
- **Not auto-processed.** `/ingest` stores the batch and returns 202; it reaches the
  review queue when you click **Process new captures** (or the scheduled worker, once it
  exists).

## Verifying it end-to-end

```bash
docker exec yonder-yonder-db-1 psql -U travel -d travel \
  -c "SELECT id, status, received_at FROM ingest_batches ORDER BY received_at DESC LIMIT 5;"
```

A row appearing right after you click Capture — with `payload->>'capture_channel' =
'gcal'` — is the add-on working. Then **Process new captures** in the review queue turns
it into an `event` item.
