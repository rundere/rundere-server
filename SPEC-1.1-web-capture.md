# Yonder 1.1 — Web Capture surface (LOCAL working spec)

> **Local-only.** Gitignored on purpose: this spec assumes an Authentik-fronted
> deployment, which is an operator choice, not a Yonder requirement. The public
> repo stays auth-agnostic — anyone can front the private route with whatever
> auth they run (Authelia, Tinyauth, basic-auth, mTLS, a trusted LAN, …). If any
> of this graduates into repo docs, genericize the auth section first.

Status: **DELIVERED in v1.1.0** (commit 1390ebb). Kept for reference; the notes
below describe what was built. Deviations from the original plan: the shared
helper is `store_batch()` in `app/ingest.py`; proxy-authed batches use an inert
internal `web-capture` api key to satisfy the non-null FK (no schema migration).

## Goal
A native capture page in the Yonder stack that turns any travel web page
(hotel/restaurant/event/airline confirmation) into an ingest batch — no n8n, no
HMAC. This is the last item on the capture roadmap: Gmail ✅, GCal ✅, Maps ✅,
**web = the remaining "send-a-web-page" surface.**

Primary input: a **bookmarklet** that sends the current tab's rendered HTML;
**paste-HTML** textarea as manual fallback; **URL-fetch** as a secondary
convenience for public pages. (Decision locked: bookmarklet + paste.)

## Why this is cheap (already in the codebase)
- `extract/jsonld.py` **already parses HTML** — it matches `<script
  type="application/ld+json">` and maps schema.org flight/lodging/event/train.
  Web pages are exactly where JSON-LD lives, so extraction is essentially free.
- `"web"` is **already in the channel enum** in `schemas/`.
- The review UI is **Jinja2 + HTMX** (`app/review/templates/`, `app/review/routes.py`) —
  a capture form is a natural sibling page.
- Batch storage in `app/ingest.py` (sha256 → `Artifact` → `IngestBatch`) is reusable.

## Auth model (the crux — and why native beats n8n)
- New route lives on the **private router only**, behind whatever auth the
  operator runs. **Never on the public `/ingest` route.**
- **No API key, no HMAC.** The private route is already authenticated by the
  operator's proxy; the session cookie is the auth. This deletes the class of
  HMAC-signing bugs that made the n8n Maps node painful.
- If the proxy passes an identity header (e.g. Authentik's `X-authentik-username`),
  read it → `capture_context.submitted_by`. Treat as optional/best-effort so the
  feature doesn't hard-depend on any one auth product.
- On the old n8n-behind-Authentik 302: that was an *embedded* form POSTing across
  an auth boundary. A same-origin HTMX POST from a page already behind the proxy
  carries the cookie natively — the problem doesn't arise.

### Operator note (this deployment)
Internal Traefik router currently has only step-ca TLS. To front it with Authentik,
add the forward-auth middleware to the internal router. This deployment's middleware
chain is:

```
authentik-auth@docker,plex-geoblock@file,crowdsec-bouncer@file
```

So the internal router label becomes (order matters — auth before geoblock/bouncer):

```
traefik.http.routers.<router>-int.middlewares=authentik-auth@docker
```

(Keep the public `/ingest` router as-is — it deliberately skips Authentik because
Google's Apps Script egress can't do interactive login; it uses API-key + HMAC.)

## Routes & UI (`app/review/`)
- `GET /capture` — Jinja2 page: a textarea (paste HTML *or* a URL — auto-detected),
  plus optional `travel_date` / `title` / `notes` / `collection_id` fields
  (mirrors the Maps form's context).
- `POST /capture/web` — HTMX handler. Detects input type:
  - **HTML** (contains `<html`/`<script`) → store as-is.
  - **URL** → server-fetch **only if** it resolves to a public host; on
    auth-wall / redirect-to-login / fetch failure, return an HTMX partial:
    *"Couldn't fetch — use the bookmarklet or paste the page source."*
  - Stores an `IngestBatch` with `capture_channel: "web"` via the shared helper
    below; returns the `batch_id` (or the reason) as an HTMX partial.
- **Bookmarklet** served from `/capture`:
  ```
  javascript:(()=>{fetch('https://<your-private-host>/capture/web',{method:'POST',credentials:'include',headers:{'Content-Type':'text/html'},body:document.documentElement.outerHTML}).then(r=>r.text()).then(t=>alert(t))})()
  ```
  `credentials:'include'` reuses the proxy session cookie. This is the web analog
  of the Gmail add-on and is the primary path — it captures logged-in booking
  pages the server can't fetch.

## Extraction (mostly free)
- New thin `extract/web.py`: normalize stored HTML → call existing
  `extract_jsonld(html)`; fall through to `extract/llm.py` for pages with no JSON-LD.
- Register `web` in `extract/pipeline.py` channel dispatch.
- Optional cheap middle tier before the LLM: `<meta property="og:...">` / microdata.

## Refactor (small, enables the above)
- Factor the batch-storage tail of `app/ingest.py` (sha256 → `Artifact` →
  `IngestBatch`) into a shared `store_batch(session, body, channel, context)`.
  `/ingest` (HMAC path) and `/capture/web` (proxy-auth path) both call it — one
  storage path, two front doors. Preserves D-15 content-addressing for both.

## Testing
- `tests/test_web_capture.py`: a real booking page's saved HTML → asserts the same
  `TravelItem` a JSON-LD email produces (reuse the `samples.json` contract style).
- Auth: `/capture/web` returns 401/redirect without the proxy identity; 202 with it.
- URL path: public URL fetches; a login-walled URL returns the "use bookmarklet"
  partial, not a crash.

## Out of scope for 1.1 (design for, don't build)
- Folding the **Maps** channel into this page to retire n8n later — keep the form
  field generic so a Maps URL could route to `maps_url` in 1.2.
- Browser-extension version of the bookmarklet.

## Task list
1. `store_batch()` refactor out of `app/ingest.py`.
2. `extract/web.py` + pipeline dispatch for `web`.
3. `GET /capture` page + `POST /capture/web` handler + HTMX partials.
4. Bookmarklet snippet on the page.
5. Add the operator's auth middleware to the internal Traefik router (local; not repo).
6. Tests.

Estimate: small — extractor and storage already exist; mostly one route, one
template, and a proxy label.
