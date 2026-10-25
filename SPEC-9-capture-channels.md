# SPEC-9 — Capture channels and the one-step capture flow

**Status: PLANNING (2026-10-02), revised after owner review.** D-162 and D-190 (email
forward) are built (2026-10-12). Decisions D-158–D-160, D-163 and D-164 were proposed here
(D-161 is the design-token decision in SPEC-8); several have shipped since, so check the code
and Plane for each. Section 2 documents what existed on 2026-10-02 (read from the code);
sections 3–7 are the proposal. Related Plane items: YONDER-159–163 (capture page redesign), YONDER-199
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
| 6 | **MCP `capture_url`** | Any MCP-capable assistant or client on `/mcp` (Claude is one) | OAuth bearer | a URL or directions link | as above |
| 7 | **Operator** | `python -m app.cli process`, `create-key` | shell on the server | n/a | n/a |

One more is not a channel: `/ingest` accepts any client that can sign a request, so a
script can use it. (The n8n Maps form, superseded by the page's directions input, was
removed, D-205.)

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
| **AI assistants (MCP)** | Add Run·dere as a remote MCP server by URL; sign in and allow; ask the assistant to capture a link. Generic to the MCP standard, with Claude and Claude Code as examples |

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
  - **Built 2026-10-12 (YONDER-199), with the details in D-190.** This supersedes the
    verified-sender idea in the original Plane item: the address alone decides.

- **D-190 — How the email inbox is built (YONDER-199; owner, 2026-10-12).**
  - *Configuration:* `TX_INBOX_ADDRESS` and `TX_INBOX_IMAP_*`, like SMTP. The mailbox
    password is read from the environment only, never stored in the database or shown in a
    log or status line. With no host, user or address the feature is off and the tab says so.
  - *Address:* `local+<token>@domain`. The token is 12 random characters from an alphabet
    without look-alikes; one per user (`inbox_addresses`); made the first time it is shown;
    regenerated from the Email tab or `POST /api/v1/capture/email-address/rotate`
    (first-party only).
  - *Recipient:* read from `Delivered-To`, `X-Original-To`, `Envelope-To`, `X-Forwarded-To`,
    `To` and `Cc`, so a forwarding rule that keeps the original `To` still works. A message
    addressed to several people's tokens becomes one capture each. A deactivated user's
    address matches no one.
  - *Capture:* an `email`-channel batch shaped like the Gmail add-on's (raw RFC 2822,
    D-15; `source.type` `email`). Mail forwarded as an attachment keeps the original's
    booking markup, because the MIME decoder walks into it. Then auto-process (D-158).
  - *Dropped, never stored:* no matching address, larger than `TX_INBOX_MAX_BYTES` (never
    downloaded), or over `TX_INBOX_RATE_LIMIT` captures per address per hour. Such mail is
    moved to the dropped folder, and only the outcome is recorded (`inbox_messages`: no
    sender, subject or body).
  - *Exactly once:* `inbox_messages.message_key` = sha256(Message-ID, or the raw bytes, +
    user) is unique, so a re-forward, a second poll or a second worker never captures twice.
  - *Mailbox housekeeping:* captured mail moves to `TX_INBOX_DONE_FOLDER`, everything else
    to `TX_INBOX_DROPPED_FOLDER` (IMAP MOVE, or copy + expunge). Admins see the read mode, the
    last read, the last error and a 7-day count on the Email tab.
  - *Real time (owner, 2026-10-12):* one reader thread per process holds an **IMAP IDLE**
    (RFC 2177), so the mail server tells Run·dere about new mail and it is read within
    seconds. The reader re-IDLEs every 20 minutes and reconnects after a drop. A server
    without IDLE, or `TX_INBOX_IDLE=false`, falls back to reading every
    `TX_INBOX_POLL_SECONDS`. Email is the only channel that needs this. Every other channel
    (the add-ons, the bookmarklet, the capture box, the API, MCP) is an HTTP request *to*
    Run·dere, answered at once. After capture, processing is the same for all (D-158).
  - *"Got it" (owner, 2026-10-12):* as soon as a forward is captured, an email goes to the
    capture owner's **own account address**, never to the sender, which nobody verified, so a
    forged sender can't make Run·dere mail a stranger. It needs SMTP and an email on the
    account; the Email tab says which address it goes to, or asks for one.
    `TX_INBOX_ACKNOWLEDGE=false` turns it off.
  - *TLS (YONDER-229, 2026-10-12):* the mailbox's certificate is always checked (imaplib's
    own default checks nothing). `TX_INBOX_IMAP_CA_FILE` pins one certificate (Proton Bridge's
    self-signed one), with the hostname check relaxed because the pinned certificate is the
    guarantee; `TX_INBOX_IMAP_VERIFY=false` is an explicit, logged opt-out.
  - *A shared mailbox is left alone (YONDER-229, 2026-10-12):* the reader may be pointed at
    someone's own account (Proton: one account, many addresses, one INBOX). It reads only the
    recipient headers (PEEK, nothing marked read) and handles only mail to its own plus
    addresses; anything else — personal mail, the bare address — is never fetched, moved or
    recorded. It remembers the highest UID it has looked at per folder, so a large mailbox
    isn't rescanned on every new email. A message is removed from the folder only by MOVE or
    UID EXPUNGE of that one message, never a folder-wide EXPUNGE. *Amended by D-208:* an
    admin can say the folder is Run·dere's alone.
  - *Not in v1:* a webhook front door for mail services that push mail over HTTP.

- **D-208 — An inbox folder can be marked Run·dere-only (YONDER-332; owner, 2026-10-23).**
  D-190 leaves mail that isn't sent to a forwarding address where it is, because the folder
  may be part of someone's own mailbox. When the folder holds only mail meant for Run·dere,
  that rule strands mail sent to the bare address, a mistyped address or spam, and the folder
  never empties. Common cases are a mailbox of its own, or a folder that only a mail filter
  fills (for example `Folders/Rundere` behind Proton Bridge).
  - *An explicit setting, off by default.* It is never inferred from the folder's name. When
    it is on, mail without a forwarding address moves to the dropped folder and is recorded
    as `unmatched`, "not sent to a personal address", as an outcome only, with no sender,
    subject or body (D-162). When it is off, D-190 holds unchanged.
  - *Set by an admin on Settings → Server mail.* The page has help text on when to use it
    (a mailbox or folder that only Run·dere reads) and when not to (any part of a personal
    inbox), and says that the dropped folder is emptied to Trash after
    `TX_INBOX_DROPPED_DAYS`. It is kept in the database (`server_settings`). It isn't a
    secret, so D-190's rule that the mailbox password lives only in the environment is
    untouched.
  - *Optional lock.* `TX_INBOX_DEDICATED=true|false` fixes the value from the deployment's
    configuration. The page then shows it as set by the server and can't change it. When the
    variable isn't set, the page decides.
  - *A preview before it goes on.* Turning it on first shows how many messages in the folder
    would move to the dropped folder, counted by the server's own SEARCH (YONDER-331), and
    asks for confirmation. An extra warning appears when the folder is `INBOX`, the folder
    most likely to be someone's own. Turning it off takes effect at once.
  - *Moves, never deletes.* Mail is only moved to the dropped folder. Deleting stays with that
    folder's existing retention.

## 6. Proposed: iOS Share Extension feedback (D-163)

- **D-163 — The Share Extension shows a notification that the capture was received, at
  minimum.** It calls `POST /api/v1/capture` (or queues it in the offline outbox until it can),
  and confirms "Captured for Run·dere" or "Saved, will send when online". Whether it also shows
  result cards (what was extracted) is unresolved: processing is asynchronous for the app, the
  extension has a short lifetime, and the app's own capture screen may be the better place. That
  needs a look at how the app shows results before it is decided.

## 6b. Proposed: saved places from Maps links (D-166, D-167, D-168; YONDER-200)

*Saved places became the Places Library in SPEC-11 (YONDER-314, D-212 to D-219).*

A Google or Apple Maps **place** link (a restaurant, museum, hotel, viewpoint) has two entry
points that share one parser (`extract/place_url.py`) and one category table:

1. **Saved place (no trip).** Shared from Google or Apple Maps to the app, or pasted into the
   capture page. It is kept as a *saved place* until the person files it to a trip and, later,
   a date.
2. **Add by link inside a trip.** In a trip, paste a Maps link instead of choosing a type first.
   The name, pin and a suggested type come from the link and a lookup, and it lands on the
   timeline directly (D-168).

- **D-166 — Saved places are their own owner-scoped table (`saved_places`), not captures.**
  Own-only like the journal, and with no admin view: a person's wishlist is theirs. Ownerless
  rows are closed (YONDER-186). Re-saving the same place returns the existing row. Filing it to
  a trip builds a timeline activity and marks the row filed (D-169). An undated activity is fine: it is scheduled later (see the unscheduled-items items,
  YONDER-207 to 209). A saved place can be renamed and edited before filing. The picker
  pre-selects a suggested trip, never files on its own: the trip whose located stops are nearest
  the pin (within 100 km), else the trip covering today, else the next one to start; past trips
  are never suggested. Only an http(s) link is kept as the place's link.
- **D-167 — Saved places do not go through the extractor chain or the approval queue.** A place
  link is a deterministic parse, not an extraction, so it skips the LLM, the schema, the rules
  engine and the review queue the bookings use (D-31). The server does no lookups for a share
  from iOS: the app resolves address and category on the phone with MapKit and sends them. The
  server's own path (URL parse, then the optional Photon step) is the fallback for web, MCP and
  other clients, and Rundere works without it (D-146).
- **D-169 — A saved place is never saved or filed twice, and what was handled stays handled.**
  One row per place per owner, backed by a unique index on `(owner_id, place_key)`, so two
  simultaneous saves make one row (the loser gets the winner's row). Filing and removing keep
  the row as a tombstone (`status` filed or dismissed, no trip link kept), so re-sending a list
  never re-adds a place that was filed or removed; `revive` re-adds a removed one, never a
  filed one. Filing claims the row atomically, so of two simultaneous filings one wins and the
  other gets a 409. A place is the same place by its provider id (Google's `0x…:0x…` feature id
  is read from the link), else by name plus pin to about 11 m; a same-name place within 150 m
  is reported as a near-duplicate (a Google entry and an Apple share of the same café). This
  revises D-166, which consumed the row on filing.
  *Amended by D-212 (SPEC-11):* filing no longer uses a place up. It stays in the library, can
  go onto other trips, and only filing it onto the same trip twice is a 409.
- **D-170 — A list can be pushed in one call, safely and reviewably.** `POST /api/v1/places/batch`
  and the MCP `save_places` take up to 200 places. Each is judged and committed on its own, so a
  bad entry or a lost race never costs the rest and re-sending the same list changes nothing.
  `dry_run` reports what each would do and writes nothing, so a list (for example a Google Maps
  list read from a Takeout export) can be reviewed before it is pushed. The outcome per place is
  one of created, revived, duplicate, near_duplicate, already_filed, removed_earlier or error. A
  batch never expands a short link or looks up an address unless asked (25 places at most): one
  network call per place is not one paste. The agent maps each Takeout entry's fields (title,
  note, address, coordinates, Maps URL) to the place fields; the server does not parse Takeout
  files.
  *Amended by D-217 (SPEC-11, proposed):* the server will import a Takeout export itself.
- **D-168 — Adding by link inside a trip pre-fills the type from the category and never
  guesses silently.** A confident category sets the type; otherwise it falls back to a generic
  Tour that the person can change before saving. *Amended by D-209:* only when no type has been
  chosen yet.
- **D-209 — A Maps link never overrides the person, and is never lost for want of a date
  (YONDER-315, 317, 318; owner, 2026-10-23).** Three bugs had one cause: Maps links were
  handled three different ways. A link pasted into a half-filled activity replaced the whole
  form with a Tour (315). A shared directions link with no date was refused, so it was lost
  (317). A place link in an activity's Map link field filled nothing unless the activity was a
  road trip (318).
  - *No default activity type.* The new-activity form starts with "Choose a type…", and saving
    without one is refused. Until now it started on Flight, and a Maps link then read as a
    flight. (Reading stored data keeps the existing fallback for an unknown type.)
  - *One Map link field per activity*, under the type picker. The separate "Maps link
    (optional)" field above the form is gone. With no type chosen, a pasted link suggests one:
    a place by its category (D-168), directions as a road trip. Once a type is chosen, the link
    never changes it and only fills fields that are still empty. A place link fills name,
    address or location, and the Map link. A directions link fills From, To, Route, Distance
    and the arrival time. Nothing the person typed is replaced. The fill happens as soon as the
    link is pasted, and the same rule runs again on save, for every type.
  - *Undated directions go to the Inbox.* A directions link captured without a date is no longer
    refused at capture. It is stored (D-15) and waits in the Inbox (D-200) as "needs a travel
    date" (`missing_travel_date`), the same way any capture the pipeline can't finish waits for
    review. *Revised by D-210 (SPEC-8 §12):* it is extracted into a normal ground-transport
    item with no date yet, so the person checks it in the usual Inbox editor, adds the date
    there or approves it onto a trip undated and dates it later. RULE-31 no longer refuses
    it. RULE-34 holds: the date comes only from the person, never the URL.
  - *The app routes links as the page does.* `POST /api/v1/capture` checks a Maps link's kind
    before anything else, expanding a Google short link (allowlisted hosts, D-152), exactly like
    the capture page. A place link becomes a saved place (D-166/D-167), and directions go to the
    pipeline. The reply keeps `batch_id` for older apps; for a place it is empty, with
    `status: "saved_place"` and the place's id.
  - *Saved places themselves* are being redesigned separately (YONDER-314, 316). This decision
    only makes sure a shared place reaches them.

## 7. Decisions (proposed)

- **D-158** — Capture-page captures start processing immediately; approval stays human (D-31).
- **D-159** — The bookmarklet hands the page over by `window.open` + `postMessage`.
- **D-160** — The capture page is the always-used capture box plus a click-through guide with a
  tab per channel; each tab is an instruction manual with status. *Layout revised by D-191.*
- **D-191 — The capture page shows every guide as a card (owner, 2026-10-12).** The single
  "See the guides" link was easy to miss, and reaching one guide took three clicks. Now the
  left column holds the capture box with **Recent captures as a second tab** (linkable as
  `#recent`). The right column is a prominent **"Other ways to capture"** with one card per
  channel: what it is, its status (last capture, "Coming soon", or "Needs your server's
  mailbox"), the person's email-forward address when they have one, and a link **straight to
  that channel's guide tab**. The guides themselves stay on `/capture/setup`, as D-160 set out.
- **D-162** — Email forward uses a server-generated per-user plus address.
- **D-208** — An admin can mark the inbox folder Run·dere-only (opt-in, with an optional env
  lock). Mail in it without a forwarding address then moves to the dropped folder.
- **D-163** — The iOS Share Extension confirms receipt with a notification at minimum.
- **D-164** — The Gmail add-on no longer moves labels and no longer asks for `gmail.modify`;
  it only reads the open email. Supersedes D-26.
- **D-166** — Saved places are an owner-scoped table; filing one consumes it.
- **D-167** — Saved places skip the extractor chain and the approval queue.
- **D-171** — `/capture/web` authenticates itself. `/capture` is a public path in the auth gate, so with sign-in required the route refuses a stranger (401, and an `HX-Redirect` to login for the form) before it fetches, parses or stores anything, and no capture is written ownerless (YONDER-186, YONDER-210). With sign-in off (the single-user default) it stays open.
- **D-169** — A saved place is never saved or filed twice; filed and removed ones are tombstones.
- **D-170** — A list is pushed with a batch call: per-place outcomes, dry run, safe to repeat.
- **D-209** — A Maps link never overrides the person: no default activity type, one Map link
  field that fills only empty fields; undated directions wait in the Inbox; the app routes
  links like the page. Amends D-168.
- **D-168** — Add-by-link in a trip pre-fills the type from the category, with a generic fallback.
  Address and category come from Photon only when it finds a hit within 250 m of the link's own
  pin whose name matches (the wrong-branch trap, resolve/geocode.py); name-only links are never
  looked up.

## 8. Open questions

1. Should the other channels (add-ons, app, API) also auto-process once the page proves it out?
   Email forward will need it regardless (D-162).
2. Inline Approve on the result cards: a human click, so it fits D-31, but it is a second place
   to approve. Leave it out of the first version?
3. iOS: result cards in the extension, in the app after the notification, or neither?
4. Email: if the admin's provider does not support plus addressing, is a per-user mailbox or a
   shared mailbox with a sender allow-list the fallback?


## 9. iOS Share Extension contract for saved places (D-166 to D-170)

What the Share Extension (and the app) can rely on. All calls use the app's bearer token.

- **Save:** `POST /api/v1/places` with `{url}` for a Maps link, or the details the phone
  resolved with MapKit: `{name, lat, lon, address, category, provider, source_url, source: "ios"}`.
  Fields sent alongside a `url` win over what the server parses. Only an http(s)
  `source_url` is kept.
- **Response:** the saved place with `status`, `suggested_trip_id` (a hint for the sheet's trip
  picker, never a decision) and `outcome`. `201` means it was added (`created`, `revived`);
  `200` means it was not (`duplicate`, `near_duplicate`, `already_filed`, `removed_earlier`).
- **Retries are safe.** The offline outbox can resend a save: the server saves it once, and
  tells a retry after the person filed or removed the place (`already_filed`,
  `removed_earlier`) instead of adding it back. Send `revive: true` only for an explicit "save
  it again".
- **Add to timeline now:** save, then `POST /api/v1/places/{id}/file/{trip_id}` with an optional
  `starts_at` (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`; omit for undated) and `activity_type`.
  Filing twice is a `409`. The trip picker's list is `GET /api/v1/places` (each carries its
  `suggested_trip_id`) plus the trips the app already has.
- **Rename or correct before filing:** `PATCH /api/v1/places/{id}`.
- **A link the app can't classify** (a Google `maps.app.goo.gl` short link looks the same for a
  place and for directions) can go to `POST /api/v1/capture` as before. The server expands it
  and routes it like the page (D-209). For a place, the reply is `status: "saved_place"` with
  `place_id`, and an empty `batch_id`. Directions without a `travel_date` are accepted and
  wait in the Inbox for one.
- **Open question for the app side:** what the Apple Maps and Google Maps share sheets actually
  hand to an extension on current iOS (URL only, a `.vcf` card, or text). The server accepts a
  Maps URL or resolved fields, so it works either way; the app decides how to get them.
  *YONDER-314:* `url` (here, on `POST /api/v1/capture` and on the capture page) may also be the
  shared text as it came (a few plain lines: name, address, link), and the Maps link in it is
  used. A place link wins even when the share also carries the page's `html`. A short link that
  lands on Google's consent page is read from its `continue` target.

## 10. Navigation: Capture everywhere, Review becomes the Inbox (D-200)

- **D-200: Capture from anywhere; the review queue is the Inbox (YONDER-228; owner,
  2026-10-12 and 2026-10-14).** Agreed first: keep Home as it is (Up Next) and Trips as
  its own page; a Capture button available everywhere; Review becomes an **Inbox**.
  - **Web sidebar:** Home · Trips · Journal, a divider, then Capture · Places · Inbox.
    The first group is your trips and stories; the second is getting things in and
    sorting them. Settings stays at the bottom. Capture keeps its sidebar link, since
    its page holds the guides (email, share sheet, AI assistant).
  - **Global Capture button**, at the top of the sidebar on every page (in the top bar
    on phones, where it stands in for the Capture link).
    - It's a *secondary* button, so a page keeps its one Beacon primary (Home's own
      Capture stays the primary there).
    - It opens a sheet over the current page: paste a link or text. It files straight
      to the Inbox (D-158) and shows the same self-polling result as the Capture page.
      Files, directions links and the guides stay on the Capture page.
  - **The Inbox is at `/inbox`.** `/review` redirects there (301, keeping the query), so
    bookmarks and older links work. The item and action URLs under `/review/…` are
    unchanged, as is the wording inside ("Needs you", "Ready", "Approve").
  - **Badge:** the Inbox's count is the items that need you (blocked on you), as
    Review's was, not everything that arrived.
  - **Per trip:** a trip with captures filed to it and still in the Inbox says
    "N waiting in Inbox" in its header. That links to the Inbox narrowed to the trip
    (`/inbox?trip=…`, only a trip you can see). There's no second queue: the Inbox is
    where you approve.
  - **Item detail opens as a sheet** on the web (already so, per the design board).
  - **API unchanged:** `/api/v1/review…` keeps its paths so the shipped iOS app keeps
    working. "Inbox" is a name in the interface; an alias can come later if it's worth it.
  - **iOS:** tabs Today · Trips · **Capture** (centre) · Journal · **Inbox** (with the
    badge), five being the iOS limit. Places stays where it is in the app. The share
    sheet stays the main way to capture on iPhone.
  - **Android (no app yet):** a Capture FAB, with tabs Today · Trips · Journal · Inbox.
