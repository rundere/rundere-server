# SPEC-9 — Capture channels and the one-step capture flow

**Status: PLANNING (2026-10-02), revised after owner review.** Decisions D-158–D-160, D-162,
D-163 and D-164 are **proposed** (D-161 is the design-token decision in SPEC-8). Nothing here is
built yet; section 2 documents what exists today (read from the code), sections 3–7 are
the proposal. Related Plane items: YONDER-159–163 (capture page redesign), YONDER-199
(email forward), YONDER-54 (iOS Share Extension), YONDER-179 (file capture API),
YONDER-200 (Maps place link).

## 1. Problem

Getting a booking into Run·dere takes too many steps: capture it, go to the review queue
to press **Process new captures**, wait, then review and approve. Capture and processing
are separate because D-40 deferred a scheduled worker ("button first"). Separately, the
capture page's bookmarklet has never worked for the owner, and each channel is documented
in a different place (add-on READMEs, the capture page copy, SPEC-4).

Goal: from the capture page, one action. The person captures; extract and resolve start by
themselves and the item is waiting in their approval queue straight away. Approval stays a
human act (D-31): **no auto-approval, ever**. The capture page is where this has to feel
immediate, because the person is already in Run·dere when it happens.

The page is split by how often it is used (section 3): the capture box is used all the time;
every other channel is set up once, so it is a click-through instruction manual.

## 2. What exists today

Every channel ends in the same place: `store_batch()` writes a content-addressed
`ingest_batches` row (status `received`, raw payload kept, D-15) owned by the submitting
user (D-120). Nothing is extracted until something runs `process_pending` + `resolve_pending`
(the **Process new captures** button, `python -m app.cli process`).

| # | Channel | Where it runs | Auth and owner | Payload (`capture_channel`) | Extraction |
|---|---|---|---|---|---|
| 1 | **Gmail add-on** | Apps Script side panel on an open email (`capture/gmail-addon`) | `POST /ingest`, API key + HMAC; the key's user owns it | `gmail`: raw RFC 2822 message, base64 | JSON-LD, iCal (`.ics` attachments), then LLM |
| 2 | **Google Calendar add-on** | Apps Script on an open event (`capture/gcal-addon`) | `POST /ingest`, same | `gcal`: the Event resource | `extract/gcal.py`, deterministic |
| 3 | **Capture page → bookmarklet** | A browser bookmark on any booking page | `POST /capture/web`, session cookie | `web`: page `outerHTML` | JSON-LD, then LLM on the page text |
| 4 | **Capture page → paste form** | `/capture` | `POST /capture/web`, session cookie | `web` (pasted HTML, or a URL the server fetches under the SSRF policy), `maps_url` or `apple_maps_url` (a directions link plus a travel date) | as above; directions: `extract/maps_url.py`, `apple_maps_url.py` |
| 5 | **App / API** | iOS in-app capture and share-to-app | `POST /api/v1/capture`, bearer token | `url` or `html`, same routing as the page | as above |
| 6 | **MCP `capture_url`** | A Claude connector on `/mcp` | OAuth bearer | a URL or directions link | as above |
| 7 | **Operator** | `python -m app.cli process`, `create-key` | shell on the server | n/a | n/a |

Two more are not channels: the n8n Maps form (`capture/maps-n8n`) is deprecated and
superseded by the page's directions input, and `/ingest` accepts any client that can sign
a request, so a script can use it.

**Answer to "any other surfaces?":** beyond the four you listed, there are the **app/API
capture** (5) and the **MCP `capture_url` tool** (6), plus the paste form's two extra inputs
(a URL, and Google/Apple directions links). The page therefore has three inputs, not one.

### 2.1 Channel notes

1. **Gmail add-on.** Capture-only; it runs only when you click the button (D-02, no
   polling). It only reads the open email: the old `trip-planning` label moves (D-26) were a
   personal organisation method and are dropped, with the `gmail.modify` scope (D-164). One
   install per Google account and an API key created with `python -m app.cli create-key`. Can
   pin a trip via `collection_id` (D-05).
2. **Calendar add-on.** Same install and key; sends one event. Gmail `.ics` invites and
   calendar events resolve to the same shape, so they dedupe.
3. **Bookmarklet — why it has never worked.** It does a cross-origin
   `fetch(POST, Content-Type: text/html, credentials: include)` from the booking site to
   `/capture/web`. Three independent problems, the first two verified against the code:
   - `text/html` is not a CORS-safelisted type, so the browser sends a preflight `OPTIONS`.
     The server has no CORS handling and answers **405** with no `Access-Control-Allow-Origin`
     (checked with a test client). The fetch always rejects.
   - The session cookie is `SameSite=Lax`, which a cross-site `fetch` never carries. Even if
     the POST got through, the capture would be unowned (admin-only per YONDER-186).
   - Many booking sites' Content-Security-Policy forbid `connect-src` to another origin, and
     an `https` site cannot call an `http` LAN host (mixed content).
   The clipboard fallback is what the person actually sees, which is channel 4 with an
   extra step.
4. **Paste form.** The reliable path today. Three inputs: *Web page* (paste source or a URL),
   *Google Maps directions*, *Apple Maps directions* (the last two need a travel date).
   Optional title, notes, trip. Returns "Captured. Run Process new captures in the review
   queue", which is the two-step problem.
5. **App/API.** Same routing as the page; returns 202 with a batch id. The iOS Share Extension
   (YONDER-54) will use this. A file upload (PDF, `.ics`, `.eml`) endpoint is YONDER-179.
6. **MCP.** Returns the batch id; the item appears in the queue after processing.

### 2.2 To be built

| Channel | Item | Notes |
|---|---|---|
| **Email forward** | YONDER-199 | One admin-configured mailbox, polled; each user forwards to their own plus address (D-162). Runs without anyone clicking, so it is the first channel that needs the auto-process of D-158 to be extended beyond the page |
| **iOS Share Extension** | YONDER-54 | Calls `POST /api/v1/capture`; offline outbox queues captures and sends them later |
| **Maps place link** | YONDER-200 | A Google/Apple Maps *place* (not directions) → name, address, category → offer a trip |
| **File capture** | YONDER-179 | PDF, `.ics`, `.eml` from the app (iPad drag-and-drop) |

## 3. Proposed: the capture page, by how often it is used (D-160)

**Immediate and constant: the capture box.** This is the one reusable interface, used every
time. One box takes a URL or pasted page HTML (and recognises a Google or Apple directions
link, which adds the travel date and mode), with trip, title and notes under "More". It is the
only part of the page that is always visible.

**Set up once: a click-through to "Other ways to capture".** Everything else is user-action work
that is built once on the user's side (an Apps Script project, a bookmark, a mail rule, an app
install). These do not belong on the working page. A link under the capture box opens a guide
page (`/capture/setup`) with **one tab per channel**, in the design board's segmented-tab
style. Each tab is an instruction manual: a short "what you get", numbered steps with copy
buttons where text is copied, a "check it works" step, and a troubleshooting note. The tab
header carries that channel's status (connected or not, last capture), which is YONDER-162.

| Tab | What the steps cover |
|---|---|
| **Gmail** | Create the Apps Script project at script.google.com; replace `appsscript.json` and `Code.gs` (copy buttons for both); Deploy → Test deployments → Install add-on; get past Google's "unverified app" screen; paste the Ingest URL and the user's key on the add-on home card (the key is generated inline, YONDER-161); open an email and capture it |
| **Calendar** | Same project steps with the Calendar add-on files; same key; open an event and capture it |
| **Bookmarklet** | Show the bookmarks bar; drag the button there; open a booking page and click it; a Run·dere tab opens with the page (D-159); what to do if the pop-up is blocked |
| **Email forward** | The user's own address (D-162) with a copy button; how to set a forwarding rule in Gmail and in Outlook; what happens to a forwarded email; regenerate the address if it leaks |
| **iPhone and iPad** | When built: install the app, sign in, Share → Run·dere from Safari or Mail; the confirmation notification (D-163) |
| **Claude** | Add the Run·dere connector from `/mcp`; ask Claude to capture a link |

The page's own capture box is not a tab, because it needs no setup.

### 3.1 After a capture: auto-process (D-158)

- **D-158 — Capturing on the capture page starts processing immediately.** After `store_batch`
  commits, the server starts extract + resolve for that user's received batches on the existing
  background job (`app/review/jobs.py`), and the page shows progress (the existing job panel)
  and then what landed: one card per extracted item (type, title, dates, the trip it will file
  to, and what still needs the person, such as "no time on the page") with **Open in review**.
  The item is in the user's approval queue straight away. Supersedes the "button first" part of
  D-40 for this surface.
  - *Approval is untouched:* nothing is approved, committed or written to Run·dere trips by
    this step (D-31). Whether the cards also carry an inline Approve button is not part of this
    decision; it would still be a human click, and is left for after the page is built.
  - *Capture never depends on processing:* if the LLM is down or the job fails, the batch stays
    `received`, the capture still succeeds, and the existing "to process" count and button are
    the retry path.
  - *Concurrency:* the executor runs one job at a time. A capture that arrives while a job runs
    sets a per-user "run again" flag and the job re-runs once when it finishes, so a batch that
    landed mid-run is never stranded.
  - *Scope:* processing is filtered to the capturing user's batches (`scope.batch_filter`).
  - *Cost control:* `TX_AUTO_PROCESS` (default on) turns it off; the LLM tier already runs only
    when the deterministic extractors find nothing.
  - *Other channels:* the add-ons, the app and the API keep today's behaviour (captured, then
    processed by the button) until it is decided otherwise. Email forward is the exception that
    will need it, because nobody is present to click (D-162).
- The bookmarklet is not exempt from this: its hand-off lands on the capture page (D-159), so
  the same progress and cards appear there.

## 4. Proposed: a bookmarklet that works (D-159)

- **D-159 — The bookmarklet hands the page to Run·dere by `window.open` + `postMessage`,
  not a cross-origin `fetch`.** It opens `/capture/handoff` in a new tab, waits for a ready
  message, and posts the page HTML. The Run·dere tab (same-origin, cookie attached, no CORS,
  unaffected by the booking site's CSP) submits it through the normal path and shows the
  progress and result cards from section 3.1. The handoff page accepts a message from any
  origin because the payload is untrusted page HTML either way; it never executes it, and
  `/capture/web` keeps its size cap. Popup blockers allow it because it follows a click on the
  bookmark. Keep the clipboard fallback for browsers that block `window.open`.
- Mobile Safari has no bookmarklet worth supporting: the iOS Share Extension is the mobile
  answer.

## 5. Proposed: email forward with a per-user plus address (D-162)

- **D-162 — Each user forwards to their own plus address.** The admin configures one mailbox
  (`captures@example.com`) that most providers can receive plus-addressed mail for. The server
  generates a random token per user and shows them `captures+<token>@example.com`; mail to that
  address is attributed to that user (owner set, no sender-to-user mapping needed). The token
  is a capability: it can be regenerated from the Email tab, which invalidates the old address.
  Because the address, not the sender, identifies the user, forwarding from any of their own
  accounts works.
  - *Safety:* mail to a token that matches no user is dropped and counted, never stored as an
    ownerless capture (YONDER-186); rate-limited per token; the raw message is kept (D-15).
  - *Processing:* nobody is present when mail arrives, so the poller runs the same auto-process
    as D-158 for that user's batch. The result is an item in their approval queue (D-31).
  - *Needs from the deployment:* a mailbox that supports plus addressing and IMAP access. The
    Email tab says so when the admin has not configured one.

## 6. Proposed: iOS Share Extension feedback (D-163)

- **D-163 — The Share Extension shows a notification that the capture was received, at
  minimum.** It calls `POST /api/v1/capture` (or queues it in the offline outbox until it can),
  and confirms "Captured for Run·dere" or "Saved, will send when online". Whether it also shows
  result cards (what was extracted) is unresolved: processing is asynchronous for the app, the
  extension has a short lifetime, and the app's own capture screen may be the better place. That
  needs a look at how the app shows results before it is decided.

## 7. Decisions (proposed)

- **D-158** — Capture-page captures start processing immediately; approval stays human (D-31).
- **D-159** — The bookmarklet hands the page over by `window.open` + `postMessage`.
- **D-160** — The capture page is the always-used capture box plus a click-through guide with a
  tab per channel; each tab is an instruction manual with status.
- **D-162** — Email forward uses a server-generated per-user plus address.
- **D-163** — The iOS Share Extension confirms receipt with a notification at minimum.
- **D-164** — The Gmail add-on no longer moves labels and no longer asks for `gmail.modify`;
  it only reads the open email. Supersedes D-26.

## 8. Open questions

1. Should the other channels (add-ons, app, API) also auto-process once the page proves it out?
   Email forward will need it regardless (D-162).
2. Inline Approve on the result cards: a human click, so it fits D-31, but it is a second place
   to approve. Leave it out of the first version?
3. iOS: result cards in the extension, in the app after the notification, or neither?
4. Email: if the admin's provider does not support plus addressing, is a per-user mailbox or a
   shared mailbox with a sender allow-list the fallback?
