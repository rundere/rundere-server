# Discovery LLM tier — infrastructure

Implementation: `discover/search.py` (SearXNG helper), `discover/grounding.py`
(source grounding + corroboration), `app/config.py` (`TX_SEARXNG_URL`).
Tests: `tests/test_discover_search.py`, `tests/test_discover_grounding.py`.

Yonder 3.2, phases 4–5 of `SPEC-3.2-discovery-llm.md`. The deterministic tier
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
