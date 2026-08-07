# Gmail capture add-on

Turns "open a booking email in Gmail" into a stored, content-addressed batch
on the travel-extractor `/ingest` endpoint. This is **capture only** — it does
not parse anything. It sends the raw RFC 2822 message source (D-15: keep the
original artifact forever) so the extractor stage (not built yet) has the
real thing to work from, and reprocessing later never loses information.

Why an add-on and not a background script: D-02 explicitly removed polling
from the architecture. This only runs when you click the button while reading
an email — no trigger, no scheduled job, no standing Gmail access beyond the
one message you have open.

## What it sends

```json
{
  "capture_channel": "gmail",
  "message_id": "...", "thread_id": "...",
  "subject": "...", "from": "...", "internal_date": "2026-08-03T12:00:00.000Z",
  "raw_message_base64": "<the entire raw email, base64>",
  "capture_context": {
    "submitted_by": "you@gmail.com",
    "notes": null,
    "collection_id": null
  }
}
```

`collection_id`, if you fill it in, becomes `capture_context.collection_id` —
the human-selected-collection short-circuit (D-05): the resolver will attach
this item to that trip and never override your choice (RULE-35b).

## Label workflow (D-26)

This matches the owner's existing manual triage setup — three Gmail labels
that must **already exist** before you use the add-on (it never creates them):

- **`trip-planning-untracked`** — your intake queue. You label candidate
  emails into this yourself; the add-on doesn't touch how things get here.
- **`trip-planning-tracked`** — where a thread "moves" to on a successful
  capture.
- **`trip-planning-toreview`** — where it "moves" to instead if the capture
  fails.

Gmail has no real folders, so a "move" is two operations: remove
`trip-planning-untracked` (a no-op if it isn't actually on the thread) and add
the outcome label. The email is never archived — this is purely a label swap,
and it happens on **both** outcomes, not just success.

If `trip-planning/tracked` or `trip-planning/to-review` doesn't exist in your
Gmail, the add-on fails immediately with a clear message — including a full
list of your actual label names — rather than silently creating a new one.

**Nested labels:** these three live under a `trip-planning` parent. Gmail's
internal name for a nested label is `Parent/Child` (forward slash) — the
label sidebar's indentation hides this, so the code must use the slash form
(`trip-planning/untracked`, not `trip-planning-untracked`) or the lookup
silently fails to match a label you can plainly see. If the constants in
`Code.gs` (`LABEL_UNTRACKED`/`LABEL_TRACKED`/`LABEL_TOREVIEW`) ever stop
matching your actual setup, the "missing label" error message lists every
label name Gmail actually has — copy the exact string from there.

A label-move failure (e.g. a transient API hiccup) is reported as a side note
and never rolls back the capture itself: the ingest result is already durably
stored server-side by the time label-moving runs.

## Setup (one time, per Google account)

1. Go to **script.google.com** → New project.
2. In the editor, open the manifest (⚙️ Project Settings → check "Show
   appsscript.json") and replace its contents with
   [appsscript.json](appsscript.json).
3. Replace `Code.gs`'s contents with [Code.gs](Code.gs) from this repo (copy
   the file itself — a script.google.com project is your own copy; there is
   nothing to `clasp push` unless you set that up separately).
4. **Deploy → Test deployments → Install add-on.** This installs it for your
   own account only — no Google review, no public listing. Scopes:
   `gmail.addons.current.message.readonly` (reading the open email) and
   `gmail.modify` (moving labels — there's no narrower Gmail Add-on scope for
   label mutation, see D-26). Both are *sensitive*, not *restricted*, so this
   still matches D-03's approach — no CASA/verification needed for personal
   use. You'll still see Google's "this app isn't verified" screen on first
   authorization since it's your own unpublished script — click
   **Advanced → Go to (unsafe)**; that screen is expected and does not mean
   anything is wrong.
5. Open Gmail, refresh, open the add-on's side panel (plane icon), and on the
   **homepage card** paste the API key you created with:
   ```bash
   docker exec -it travel_extractor-travel-extractor-1 python -m app.cli create-key <client-name>
   ```
   Leave the Ingest URL as the default unless you changed the public hostname
   in `portainer-stack.yml`. Click **Save** — the key is stored in your
   script's User Properties, never in code.

## Using it

Open any email → the add-on panel shows Subject/From/Date → optionally add a
note or force a collection ID → **Capture to Travel Extractor**. A toast shows
the resulting `batch_id`, or the exact rejection reason (401 variants, 413 too
large, 429 rate-limited) straight from the server.

## Troubleshooting

**"The script does not have permission to perform that action. Required
permissions: gmail.addons.execute"** — the manifest was missing the base scope
every Gmail add-on needs just to run at all (fixed 2026-08-03; if you copied
`appsscript.json` before this fix, re-copy it now). After updating the
manifest:
1. Save the project (⌘/Ctrl+S).
2. Reload Gmail and reopen the add-on panel — Apps Script should prompt for
   re-authorization since the scope set changed.
3. If it doesn't prompt, force it: in the script editor, select the
   `buildHomepageCard` function and click **Run** once — that triggers the
   consent dialog directly. Re-approve (you'll see the "unverified app"
   click-through again — expected, see step 4 in Setup above).

**"Exception: Access denied: : Expired access token"** — fixed 2026-08-03;
re-copy `Code.gs`. Root cause: the button used to capture the message-scoped
`accessToken` at the moment the card first rendered and carry it forward as an
action parameter. If any time passed (typing a note, switching emails and
coming back, just thinking about it) before clicking Capture, that snapshotted
token had already expired. The fix reads `e.gmail.accessToken` /
`e.gmail.messageId` fresh inside the button's own handler instead — Gmail
reissues both on every action event fired from a contextual card, so there's
nothing left to go stale.

**"Permission failure" on label move, even though reading the message
works** — the Executions log (clock icon, left sidebar in the script editor —
always check this, not the toast; toasts can vanish before you finish reading
them) will show something exact like:

> Required permissions: (https://mail.google.com/ || .../auth/gmail.modify)

This means `gmail.modify` genuinely isn't authorized for the **installed
add-on** yet — confirmed 2026-08-03. Running a function from the editor's
**Run** button re-authorizes the editor's own execution context, but that is
tracked SEPARATELY from the Test Deployment's (installed add-on's)
authorization, and does not reliably refresh it. What actually works:

1. In the script editor: **Deploy → Test deployments** → find the existing
   install → **Uninstall** (or remove it).
2. **Deploy → Test deployments → Install add-on** again, fresh.
3. This should trigger a full new consent screen listing every scope
   currently in `appsscript.json`, including `gmail.modify` — approve all of
   them (with the expected "unverified app" click-through).
4. If step 2 still doesn't prompt for anything new, go to
   [myaccount.google.com/permissions](https://myaccount.google.com/permissions),
   find "Travel Extractor Capture," remove its access entirely, then repeat
   steps 1–2 for a guaranteed clean consent flow.

Separately (2026-08-03): the label-move calls now fetch the thread via
`GmailApp.getThreadById()` rather than reusing the message object obtained
through the narrow "current message" token, on the theory that token might
only ever carry read permission regardless of what's granted at the script
level. This is good hygiene either way, but the confirmed root cause above is
the missing authorization — re-copy `Code.gs` and `appsscript.json` together,
then do the uninstall/reinstall.

**"Capture FAILED (401): bad signature"** on a real capture, even with a valid
key — fixed 2026-08-03; re-copy `Code.gs`. Root cause: the payload was signed
as a JS *string* via `computeHmacSha256Signature`, then handed to
`UrlFetchApp.fetch` as that same string — two independent string→bytes
conversions that aren't guaranteed to agree, and didn't for a real ~110KB
email with non-ASCII header content (subject/sender names). The fix converts
the JSON to explicit UTF-8 bytes once (`Utilities.newBlob(...).getBytes()`)
and reuses that identical byte array both for the signature and as the literal
`payload` sent over the wire — nothing left to re-encode differently on
either path.

## Known limitations (MVP, not bugs)

- **5 MB cap** (`TX_INGEST_MAX_BODY_BYTES`, server-side): an email with large
  attachments (PDF boarding passes, etc.) can exceed this after base64
  overhead. The server returns 413 and the add-on surfaces it directly; raise
  the limit if this bites you.
- **No segment-count declaration.** The card doesn't ask "how many flight
  segments does this PNR have" (`declared_segment_count` / RULE-16) — the
  capturing human rarely knows reliably. Left null; a future enhancement if
  dropped legs turn out to be a real problem in practice.
- **Not yet processed.** `/ingest` stores the batch and returns 202. Nothing
  parses it until the extractor stage (`extract/jsonld.py` etc.) exists.

## Verifying it end-to-end right now

Even before extractors exist, you can confirm the plumbing:

```bash
docker exec travel_extractor-travel-extractor-db-1 psql -U travel -d travel \
  -c "SELECT id, status, received_at, declared_segment_count FROM ingest_batches ORDER BY received_at DESC LIMIT 5;"
```

A row appearing right after you click Capture is the whole add-on working
correctly — capture → stored, exactly per the pipeline diagram in HANDOFF.md.
