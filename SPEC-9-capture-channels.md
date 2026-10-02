# SPEC-9 — Capture channels and the one-step capture flow

**Status: PLANNING (2026-10-02).** Decisions D-158–D-160 are **proposed**. Nothing here is
built yet; section 2 documents what exists today (read from the code), sections 3–5 are
the proposal. Related Plane items: YONDER-159–163 (capture page redesign), YONDER-199
(email forward), YONDER-54 (iOS Share Extension), YONDER-179 (file capture API),
YONDER-200 (Maps place link).

## 1. Problem

Getting a booking into Run·dere takes too many steps: capture it, go to the review queue
to press **Process new captures**, wait, then review and approve. Capture and processing
are separate because D-40 deferred a scheduled worker ("button first"). Separately, the
capture page's bookmarklet has never worked for the owner, and each channel is documented
in a different place (add-on READMEs, the capture page copy, SPEC-4).

Goal: one action from the person's side. Capture → extract → resolve happens by itself;
the person's only remaining job is the human approval (D-31).

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
   polling). Moves the thread between `trip-planning/untracked`, `tracked` and `toreview`
   labels (D-26). Needs the labels to exist, one install per Google account, and an API key
   created with `python -m app.cli create-key`. Can pin a trip via `collection_id` (D-05).
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
| **Email forward** | YONDER-199 | Admin-configured mailbox, polled; "forward your confirmation" like TripIt. Needs a sender-to-user mapping (an unmapped sender must not create ownerless rows) and is the first channel that runs without anyone clicking, so it needs D-158 |
| **iOS Share Extension** | YONDER-54 | Calls `POST /api/v1/capture`; offline outbox queues captures and sends them later |
| **Maps place link** | YONDER-200 | A Google/Apple Maps *place* (not directions) → name, address, category → offer a trip |
| **File capture** | YONDER-179 | PDF, `.ics`, `.eml` from the app (iPad drag-and-drop) |

## 3. Proposed: capture runs Process (D-158)

- **D-158 — A capture starts processing by itself.** After `store_batch` commits, the server
  starts extract + resolve for that owner's received batches on the existing background job
  (`app/review/jobs.py`). It applies to every channel, not only the page, so the add-ons,
  the app and email forward behave the same. Capture never depends on processing: if the LLM
  is down or the job fails, the batch stays `received`, the capture still succeeds, and the
  existing "to process" count and button remain as the retry path. (Supersedes the "button
  first" part of D-40.)
  - *Concurrency:* the executor runs one job at a time. A capture that arrives while a job
    runs sets a per-owner "run again" flag, and the job re-runs once when it finishes, so a
    batch that landed mid-run is never stranded.
  - *Scope:* processing is filtered to the capturing user's batches (`scope.batch_filter`).
    Ownerless captures process under the admin filter.
  - *Cost control:* `TX_AUTO_PROCESS` (default on) turns it off; `/ingest` keeps its rate
    limit; the LLM tier already only runs when the deterministic extractors find nothing.
- **Capture page result becomes the review step.** The form posts, shows a progress panel
  (reusing the job panel), then shows what was extracted as cards: type, title, dates, the
  trip it will file to, and what still needs the person ("no time on the page"). Each card
  has **Approve** and **Open in review**. The queue stays the place to fix things.
- **D-31 stays.** Approval remains a human act: this proposal adds an inline button, not
  auto-approval. Auto-approving high-confidence, deterministic items (JSON-LD, iCal,
  Calendar) would remove the last step but changes D-31; it is left as an open question.
- **Bookmarklet and add-ons** can't show a panel, so they get a short reply
  ("Captured — processing; open Review") and the result lands in the queue.

## 4. Proposed: a bookmarklet that works (D-159)

- **D-159 — The bookmarklet hands the page to Run·dere by `window.open` + `postMessage`,
  not a cross-origin `fetch`.** It opens `/capture/handoff` in a new tab, waits for a ready
  message, and posts the page HTML. The Run·dere tab (same-origin, cookie attached, no CORS,
  unaffected by the booking site's CSP) submits it through the normal path and shows the
  result cards from section 3. The handoff page accepts a message from any origin because
  the payload is untrusted page HTML either way; it never executes it, and `/capture/web`
  keeps its size cap. Popup blockers allow it because it follows a click on the bookmark.
  Keep the clipboard fallback for browsers that block `window.open`.
- Mobile Safari has no bookmarklet worth supporting: the iOS Share Extension is the mobile
  answer.

## 5. Capture page redesign

Built on `design/README.md` and the design tokens (YONDER-202); one Beacon primary action.

1. **Choose, don't configure.** Three equal choices, each with one line saying when to use
   it: *A page I'm looking at* (bookmarklet), *A link or its source* (paste), *A route*
   (Google/Apple directions). Only the directions choice shows the travel date and mode.
2. **One field first.** A single box that accepts a URL or pasted HTML and detects
   directions links, as the server already does; trip, title and notes sit under "More".
3. **Status per channel (YONDER-162).** A checklist: Gmail add-on, Calendar add-on, bookmarklet,
   email forward, app. Each shows connected or not and the last capture time, with a
   setup guide (YONDER-160) and a token generated inline (YONDER-161).
4. **Recent captures** below the form, each with its state (processing, ready to approve,
   needs you, approved), so a capture is never "lost between pages".
5. **Per-channel guides** are the section 2.1 text, in-app, with copy buttons.

## 6. Decisions (proposed)

- **D-158** — A capture starts processing by itself, on every channel (section 3).
- **D-159** — The bookmarklet hands the page over by `window.open` + `postMessage` (section 4).
- **D-160** — The capture page shows extracted results and an inline Approve; approval stays
  human (D-31).

## 7. Open questions

1. Auto-approve deterministic captures (JSON-LD, iCal, Calendar) and leave only the
   uncertain ones for review? It would make capture one step end to end, but contradicts D-31.
2. Email forward: one shared mailbox with sender-to-user mapping, or a per-user address
   (`name+token@…`)? The second is simpler to scope but needs mail-server support.
3. Should the add-ons' label workflow (D-26) change once capture is automatic, since
   `toreview` is now decided after processing rather than at capture?
4. Does the iOS Share Extension get the same inline result cards, or only a notification?
