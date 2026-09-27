# Discovery LLM tier — infrastructure

Implementation: `discover/search.py` (SearXNG helper), `discover/grounding.py`
(source grounding + corroboration), `app/config.py` (`TX_SEARXNG_URL`).
Tests: `tests/test_discover_search.py`, `tests/test_discover_grounding.py`.

Run·dere 3.2, phases 4–5 of `SPEC-3.2-discovery-llm.md`. The deterministic tier
(plugs / weather / currency / language) covers stable facts from free APIs. The
LLM tier adds the four topics that need web search + synthesis — food & drink,
events & activities, visa & entry, safety & transport. Every LLM resolver runs
the same pipeline:

```
  BUILD QUERIES  ->  SEARCH (SearXNG)  ->  SYNTHESIZE (Ollama, schema)  ->
  GROUND (drop ungrounded)  ->  ASSEMBLE Finding
```

These two modules are the SEARCH and GROUND steps. No resolver consumes them
yet — those are phases 6+.

## SearXNG search (`discover/search.py`)

`searxng_search(query, *, base_url, ...) -> list[SearchResult]` hits SearXNG's
JSON API (`GET {base_url}/search?q=...&format=json`) and returns a flat list of
`{url, title, snippet}`. SearXNG names the snippet field `content`; that is the
only field-name translation.

- **Config.** `TX_SEARXNG_URL` (default `http://searxng:8080`, matching the
  `TX_OLLAMA_URL` / `TX_PHOTON_URL` convention). SearXNG must have
  `search.formats: [html, json]` enabled in `settings.yml`.
- **Degrade, never 500.** Returns `[]` on any failure — no base_url, blank query,
  unreachable host, non-JSON body. A resolver that can't reach SearXNG produces a
  flagged/absent finding, not an error.
- **No dedup here.** A resolver issues several queries per place and dedups by URL
  before feeding Ollama (§5.2), so the helper returns exactly what SearXNG gave,
  capped at `max_results`, empty-URL rows dropped.
- Reuses `get_json` / `qs` from `discover/resolvers/base.py` — stdlib HTTP, no new
  dependency.

## Grounding (`discover/grounding.py`)

Two mechanical checks applied between the Ollama response and Finding assembly.
Both are pure set/count logic — no judgment, no network.

### 1. Source grounding — `ground_items(items, allowed_urls)`

Drops any item whose `source_url` is not in that resolver call's SearXNG result
set. This kills invented URLs and fabricated claims: the model can only cite what
it was given. Returns `GroundingResult(kept, dropped)` — `dropped` feeds the
run's transparency log.

URLs are normalised first (`normalize_url`: lowercase scheme+host, drop the
fragment and a trailing slash) so a real-but-trivially-reformatted URL is not
falsely dropped. The normalisation is deterministic, so this stays a pure
set-membership test, not a fuzzy match.

### 2. Corroboration — `corroborate(source_urls, min_domains=2)`

Visa and safety are high-stakes (D-66), so a claim there needs **≥ 2 distinct
source domains** or it renders under a visible verify banner. `corroborate`
counts distinct domains (`domain_of`: host lowercased, `www.` and `:port`
stripped — not a full public-suffix parse, just enough to tell one site from
another) and returns `Corroboration(corroborated, domains)`. `verify_banner`
turns a failed check into the markdown banner the resolver bakes into `body_md`;
a passing check yields `""`.

Corroboration is opt-in per resolver: the food and events resolvers don't call
it, visa and safety do.

## Synthesis (`discover/synth.py`)

`synthesize_json(prompt, schema, *, base_url, system=…) -> dict | None` is the
SYNTHESIZE step every LLM resolver shares. It reuses the `extract/llm.py` pattern
— `/api/chat` with a JSON-schema `format` grammar, `temperature: 0`,
`num_ctx: 16384`, thinking off — but drops the validate-and-retry loop:
grounding is the safety net here, not a second schema gate, and the grammar
already constrains generation. Returns `None` on any failure (no base_url,
unreachable, non-JSON content) so a resolver degrades to an absent finding.

## Food & drink resolver (`discover/resolvers/food.py`)

The first LLM resolver and the template for the rest. Scope: city. Confidence:
medium (LLM-synthesized, source-grounded). Its endpoints come from the
constructor (`build_llm`, deployment config); the DiscoveryProfile comes from
`ResolverContext.discovery_profile` (per-trip). Missing either endpoint → `None`.

The five pipeline steps:

1. **BUILD QUERIES** — `best local restaurants {city} {country}` and
   `street food {city}` always; a budget modifier (`cheap eats` at `$`,
   `fine dining` at `$$$$`); one query per checked drink interest (§2.2).
2. **SEARCH** — every query through `searxng_search`, deduped by normalised URL,
   capped at `max_snippets` (24).
3. **SYNTHESIZE** — `synthesize_json` with a per-topic schema (`places[]` with
   `name/type/neighborhood/price_tier/why/local_specialty/source_url`,
   `local_dishes[]`, `allergy_warnings[]`). The prompt carries the profile's
   dietary restrictions, cuisine dislikes, allergies, and budget ceiling, and
   presents the SearXNG snippets as the ONLY source of truth, numbered with their
   URLs so the model can cite `source_url` exactly.
4. **GROUND** — `ground_items` drops any place whose `source_url` was not in the
   SearXNG set; a defensive budget post-filter drops places tiered above the
   ceiling. If nothing survives and there are no local dishes → `None`.
5. **ASSEMBLE** — a `Finding` (14-day TTL) whose `body_md` leads with local
   specialties, groups places by type (street food first), stars local
   specialties, and appends a visible `⚠️` allergy block when present.

Each place also renders its **address** (when a snippet gives one) and a **`map ↗`
deeplink** — a Google Maps *search* URL built from name + address/city. It is a
plain formatted URL, not an API call (no quota, no geocoding), so it costs nothing
and opens the Maps app on mobile. A baseline `best bars and cafes {city}` query
always runs so a finding is never food-only even when no drink interests are
checked. Markdown links are rendered by `_render_md`, which linkifies only
`http(s)` URLs (a `javascript:` URL is left inert).

## Events & activities resolver (`discover/resolvers/events.py`)

Same pipeline as food. Scope: city. Confidence: medium. Short 7-day TTL — the
most date-sensitive resolver. Output is **two tiers** (revised in 3.2.1 after the
three-tier model produced a wall of calendar-website noise):

- **Impacts**: specific dated public/religious holidays, closures, or strikes
  during the travel window that affect any traveller — rendered as `> ⚠️`
  callouts. The prompt insists on the actual holiday + date ("Columbus Day —
  Oct 12"), never the calendar website.
- **Notable**: specific named events and attractions worth a traveller's time
  (a festival, a landmark, a market). Grouped under a heading per matched
  activity interest when one applies, otherwise a single "Notable events &
  activities" list — so a blank profile still gets the notable stuff, not an
  empty section.

The key fix is in the **prompt**: the model must EXTRACT named events/holidays
from the results, never list a calendar/aggregator/listing website, ad, or school
calendar as an item — and de-duplicate (a `_dedupe` pass by normalised name is the
backstop). The noisy "other" tier was dropped entirely.

Two further refinements (3.2.2), from live output:

- **Exact date window.** The SEARCH queries use the broad `{month} {year}`, but
  the SYNTHESIS prompt is given the trip's exact `start–end` dates ("October 2,
  2026 to October 11, 2026") and told impacts must fall INSIDE that window — a
  holiday the day after they leave is omitted. Without this the model dumped every
  holiday for the whole month/year.
- **No padding.** The prompt tells the model to prefer a short, real list to a
  long guessed one — an empty `notable` list is fine, never invent a vague "local
  festival". The grounded event-listing links still surface as the finding's
  sources, so "where to find events" is covered even when specific events aren't
  extractable.

## Safety & transport resolver (`discover/resolvers/safety.py`)

Scope: city. Confidence: medium; 30-day TTL (slow-changing). **Transport-first** —
the resolver's priority is the practical "how do I get around" answer: what public
transit exists and how to pay for it, which ride-hailing apps actually operate in
that city, the apps to install, and the airport transfer. Safety is the secondary
block: a brief, non-alarmist summary, concrete advisories, and common scams.

- **Corroboration (D-66).** Safety advisories are grounded per-item and the block
  is corroborated — `corroborate()` counts distinct advisory domains; under 2 and
  the safety section renders under the `verify_banner()`. Transport is medium-
  confidence: the block is kept only when at least one of its `source_urls` is in
  the SearXNG set.
- **Accuracy guard.** The prompt insists ride-hailing lists name only apps that
  operate in *that* city (no Bolt/Grab for a US city) — a live failure that the
  prompt fixed.
- Queries: `travel safety {country} {year}`, `scams to avoid {city}`,
  `getting around {city} public transit`, `ride hailing apps {city}`,
  `{city} airport to city center transport`, `best transport apps {city}`.

**Visa & entry (§3.3) is intentionally not built.** Entry rules are high-liability,
change constantly, and are passport/purpose-specific — they belong to official
government sources. `TravelerIdentity.nationality` stays for possible future use.

## Durable background run + wiring (`app/trips/discovery_jobs.py`)

"Run Discovery" now runs BOTH tiers on a single background worker (§6.1). The
request returns immediately with a self-polling panel; deterministic findings
land in the first few seconds and the LLM findings stream in as each resolver
finishes.

- **The `DiscoveryRun` row IS the job state.** The worker (own DB session, off
  the request) writes `phase/done/total` and commits after each finding so the
  poll sees them appear; it finalizes `status=done`. This is durable, unlike the
  in-memory review job registry.
- **Restart detection.** A row that says `running` but whose id is not in this
  process's `_LIVE` set was orphaned by a restart; `running_run()` marks it
  `failed` on the next poll (re-run to recover). One worker + a per-trip guard
  means one run per trip at a time.
- **Both tiers, one run, one prune.** Deterministic and LLM tasks are planned
  together (`run.total` = both), executed in sequence, and the stale-draft prune
  runs once at the end over the union of produced keys — so the LLM phase never
  prunes the deterministic drafts, and a resolver raising mid-run is logged and
  skipped, never a run failure.
- **Wiring.** `POST /trips/{id}/discover` launches (guarding on a live run),
  `GET /trips/{id}/discover/status` is the poll target, and both render the
  extracted `_discovery_section.html`. The route threads the DiscoveryProfile
  (`values`) and the `self` traveller's nationality into the run's
  `ResolverContext`. The LLM endpoints come from config (`TX_SEARXNG_URL`,
  `TX_OLLAMA_URL`); if SearXNG/Ollama are unreachable the resolvers return None
  and the run simply carries the deterministic findings.

`_render_md` (the finding-body renderer) grew to match the richer LLM output:
alongside `**bold**` and `- ` bullets it now renders `### ` headings and `> `
callouts (the allergy block, and the coming visa/safety verify banner).
