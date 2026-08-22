# Yonder 3.2 — Discovery LLM tier + Discovery Profile

Status: **SPEC (for discussion)**. Extends SPEC-3-discovery.md §3/§6/§7/§11;
supersedes the 3.2 bullet in §11 "Deferred" with a concrete design.

---

## 0. The one-paragraph pitch

**3.0 told you the facts; 3.2 tells you what to eat, what to see, and what to
watch out for.** The deterministic tier covers plugs, weather, currency, and
language — stable data from free APIs. The LLM tier adds the four topics that
require web search and synthesis: food & drink, events & activities, visa &
entry, and safety & transport. It also introduces the **Discovery Profile** — a
constraint-first, user-entered preference model that shapes what the food and
activity resolvers search for — and promotes **TravelerIdentity as the spine**
of the entire person model, so every Yonder capability (homes, packing, discovery,
trip membership) hangs off one record.

> **Design principle (D-87): capture what to skip, not what to seek.** Affinities
> are hard to articulate, change by mood, and a good discovery engine should
> surface variety. Constraints and dislikes are stable, fast to enter, and the
> cost of ignoring them is a bad recommendation. The profile is a filter, not a
> recommendation engine.

---

## 1. TravelerIdentity as the person spine (D-94)

`TravelerIdentity` already exists (D-22) with `kind=self|guest`, aliases, and an
AirTrail link. Today it serves only the flight-review identity matcher. This spec
promotes it to Yonder's **central person record** — the one row everything else
hangs from.

### 1.1 Why now

Three profile tables already exist or are proposed, each with a soft
`traveler_id` FK:

| Table | `traveler_id` today | Status |
|---|---|---|
| `HomeProfile` | **missing** | deployment-wide, no person link |
| `PackingProfile` | nullable (D-82) | scaffolded but single-user |
| `DiscoveryProfile` (new) | nullable | this spec |

And a fourth need is emerging: **who is on this trip?** — required for per-trip
`traveling_with` overrides, visa lookups (which passport?), and multi-person
packing plans. Without a spine, each table invents its own person link and they
drift.

### 1.2 The model

`TravelerIdentity` gains:

```
nationality    str(2)|null   -- ISO-2, used by the visa resolver
```

`HomeProfile` gains:

```
traveler_id    int|null      -- FK to traveler_identities.id, indexed
                             -- NULL = deployment-wide (backward compatible)
```

`Trip` gains a structured association:

```
TripTraveler (new association table)
  trip_id        str  FK trips.id
  traveler_id    int  FK traveler_identities.id
  role           str  "self"|"companion"   -- who planned it vs who came along
  PK (trip_id, traveler_id)
```

The trip detail page shows who is traveling. Discovery and Planning read the
trip's travelers to:

- Look up each person's `DiscoveryProfile` and `PackingProfile`
- Determine `nationality` for the visa resolver (from the `self` traveler)
- Derive `traveling_with` from the companion roster (children, elderly — see §1.5)

### 1.3 Fan-out: what hangs off the spine

```
TravelerIdentity (the person)
  ├── HomeProfile[]          home locations over time (effective-dated)
  ├── PackingProfile         one per person — bag, laundry, style
  ├── DiscoveryProfile       one per person — food/activity preferences
  ├── TripTraveler[]         which trips this person is on
  └── nationality, aliases   identity facts
```

One person, one packing style, one discovery profile. People don't typically have
multiple personalities (D-95). HomeProfiles are the exception — they're
time-series by design (you moved from DC to Toronto).

### 1.4 Migration path

- `HomeProfile.traveler_id` is nullable — existing rows keep `NULL` (the
  deployment-wide default). A future UI lets you assign homes to a person; until
  then the `self` traveler inherits all `NULL` homes (same behavior as today).
- `PackingProfile.traveler_id` already exists and is nullable. No change.
- `DiscoveryProfile` is new; `traveler_id` is nullable from birth.
- `TripTraveler` is a new table — no existing data to migrate. Existing trips
  have no travelers assigned; the UI defaults to "add yourself" on first visit.

### 1.5 Per-trip `traveling_with` — derived from the roster (D-96)

`traveling_with` is no longer a profile field or a trip hint. It is **derived
from the trip's TripTraveler roster**: if the trip has companions, the resolver
reads each companion's own `DiscoveryProfile` for their constraints (mobility,
dietary). No age fields, no birthdates, no per-trip overrides — each person is
responsible for keeping their own profile current (D-99).

### 1.6 Rules: system vs person (D-97)

Today all rules (home filter, packing style constraints, cultural strictness)
are system-wide. As the person spine takes shape, some rules will need to move
to per-person scope. The principle:

- **System rules** stay system-wide: geocoding policy (Photon-only), grounding
  thresholds, corroboration requirements, push-to-AdventureLog mechanics.
- **Person rules** migrate to the traveler record: dietary restrictions, budget
  ceiling, packing style, clothing variant, must-have items, mobility needs.

No person-scoped rules exist today — everything is system-wide or profile-based.
When a rule is identified as person-specific during build, it targets the
`TravelerIdentity` fan-out (profile tables), not a new system config. This is a
**design guideline**, not a migration task for 3.2.

---

## 2. Discovery Profile (the "ask once" preferences)

A new `DiscoveryProfile` table, peer of `PackingProfile`, following the same
proven pattern: nullable `traveler_id` soft-linked to `TravelerIdentity`
(NULL = deployment-wide default while single-user), preferences stored in a
`values` JSON blob so adding a field is a data change, never a migration (D-82).

Managed through a structured form at `/trips/settings/discovery-profile` (peer of
the existing `/trips/settings/packing-profile` page), linked from the Administer
nav (D-86 pattern).

### 2.1 Schema

```
DiscoveryProfile
  id            str  pk
  traveler_id   int|null  (FK to traveler_identities.id, indexed)
  values        json
  created_at, updated_at
```

Exactly one per person (D-95). The `self` traveler's profile is the default for
all resolvers; companions' profiles are used when the resolver supports
multi-person awareness (future — 3.2 reads only the trip owner's profile).

### 2.2 `values` shape

The JSON blob is the profile. Every field is optional — a blank profile means
"show me everything, no constraints." The resolver reads the profile and adjusts
its SearXNG queries and Ollama prompt accordingly.

```json
{
  "food": {
    "dietary_restrictions": [],
    "cuisine_dislikes": [],
    "allergies": "",
    "budget_ceiling": 0
  },
  "drink": {
    "interests": []
  },
  "activity": {
    "interests": []
  },
  "practical": {
    "mobility": "no_limits"
  }
}
```

Note: `traveling_with` is gone from the profile — it's derived from the trip's
traveler roster (§1.5). `mobility` stays on the profile because it's a durable
personal constraint, not a trip-by-trip variable.

#### Food constraints

These **subtract** from search results. The system includes everything by default;
the profile tells it what to filter out or flag.

| Field | Type | Values | Resolver effect |
|---|---|---|---|
| `dietary_restrictions` | `str[]` | `vegetarian`, `vegan`, `halal`, `kosher`, `gluten_free`, `dairy_free`, + custom strings | Appended to every food SearXNG query; Ollama prompt requires results to accommodate them |
| `cuisine_dislikes` | `str[]` | free-text cuisine names (e.g. `"sushi"`, `"fast food"`) | Ollama prompt: "exclude recommendations for these cuisines" |
| `allergies` | `str` | free text | Flagged as a **warning** on results that mention the allergen — not a hard filter (search can't reliably detect allergens) |
| `budget_ceiling` | `int` | `0` = no limit, `1`–`4` = $/$$/$$$/$$$$ | Ceiling, not floor — see §2.3 |

#### Budget ceiling (D-88)

Follows the universal $/$$/$$$/$$$$ convention (Yelp, Google Maps, TripAdvisor).
The value is a **ceiling**: selecting `$$` means "show me $ and $$ results, skip
$$$ and $$$$." `0` = no limit (show everything).

| Value | Label | Meaning | Resolver behavior |
|---|---|---|---|
| `0` | No limit | All price tiers | No budget modifier on SearXNG queries |
| `1` | $ | Street food, budget eats | Queries add "cheap eats", "budget restaurants", "street food" |
| `2` | $$ | Casual to mid-range | Queries add "best restaurants" (no modifier — this is the natural default) |
| `3` | $$$ | Mid-range to upscale | Queries include "best restaurants" + "upscale dining" |
| `4` | $$$$ | All including fine dining | Queries include "fine dining", "tasting menu", "best restaurants" |

The Ollama prompt receives the ceiling and filters its synthesis accordingly.
A `$` ceiling user who searches "Lisbon" gets pastéis de nata and tascas, not
Michelin-starred tasting menus. A `$$$$` user gets both.

#### Drink interests

These **add** search queries. Checked items cause the resolver to issue
per-destination SearXNG queries for that drink category. Unchecked items are not
searched for, but if a brewery appears in a general food result it is not
suppressed — this is additive, never subtractive.

| Interest | SearXNG query template |
|---|---|
| `craft_beer` | "best craft beer {city}", "brewery {city}" |
| `distilleries` | "distillery tour {city}", "whiskey bar {city}" |
| `wineries` | "winery near {city}", "wine tasting {city}" |
| `coffee` | "specialty coffee {city}", "best coffee shop {city}" |
| `tea` | "tea house {city}", "tea room {city}" |
| `non_alcoholic` | "mocktail bar {city}", "juice bar {city}" |

#### Activity interests

Same opt-in pattern as drink interests. Each checked item generates dedicated
SearXNG queries per city-level destination. Unchecked = not searched, not blocked.
Activity interests also serve as the **curation lens** for the events & activities
resolver (§3.2) — see the three-tier model below.

| Interest | SearXNG query template |
|---|---|
| `markets` | "local market {city}", "street food market {city}" |
| `hidden_gems` | "hidden gems {city}", "off the beaten path {city}" |
| `museums` | "best museums {city}", "art gallery {city}" |
| `outdoor` | "hiking near {city}", "outdoor activities {city}" |
| `historical` | "historical sites {city}", "landmarks {city}" |
| `nightlife` | "nightlife {city}", "best bars {city}" |
| `sports` | "sports events {city}", "activities {city}" |
| `shopping` | "local shopping {city}", "souvenirs {city}" |
| `wellness` | "spa {city}", "wellness center {city}" |
| `cooking_classes` | "cooking class {city}", "food workshop {city}" |
| `live_music` | "live music {city}", "concerts {city}" |
| `architecture` | "architecture tour {city}", "walking tour {city}" |

### 2.3 What the profile is NOT

- **Not inferred.** Nothing is derived from check-in history, Foursquare data,
  origin, or name. The profile is what the user explicitly enters. Period. (D-93)
- **Not a recommendation engine.** It shapes search queries and filters output.
  The SearXNG results are the recommendations; the profile is the lens.
- **Not required.** A blank profile produces the generic "best of {city}" results
  for every topic — still useful, just not personalized.
- **Not per-trip.** The profile is a durable preference. Per-trip overrides flow
  through the trip's traveler roster (§1.5) or the per-finding steer note (D-71).

---

## 3. LLM resolvers (SearXNG → Ollama)

Four new resolvers, all behind the same `Resolver` interface (§3 of the base
spec). Each follows the same pipeline:

```
  Profile + Place + Dates
       │
  1. BUILD QUERIES     profile shapes the SearXNG search terms
       │
  2. SEARCH            SearXNG returns {url, title, snippet}[]
       │
  3. SYNTHESIZE        Ollama (qwen3:14b, schema-constrained, temp 0)
       │                 prompt = profile constraints + search snippets
       │                 output = per-topic JSON schema
       ▼
  4. GROUND            drop any claim whose source_url was NOT in the
       │                 SearXNG result set (§4)
       ▼
  5. ASSEMBLE          Finding(body_md, data, sources, confidence)
```

### 3.1 Food & drink resolver (`discover/resolvers/food.py`)

**Scope:** city. **Confidence:** medium (LLM-synthesized, source-grounded).

Queries:
- Base: `"best local restaurants {city} {country}"` (always)
- `"street food {city}"` (always — local street food is universal value)
- Per-drink-interest: one query each (see §2.2 table)
- Budget modifier: if ceiling is `$`, add `"cheap eats {city}"`; if `$$$$`, add
  `"fine dining {city}"`; `$$`/`$$$`/`0` use only the base query (mid-range is
  the natural SearXNG default)

Ollama prompt includes:
- The profile's dietary restrictions, cuisine dislikes, allergies, budget ceiling
- `"Do not recommend cuisines from the dislikes list"`
- `"Flag any result that may contain: {allergies}" ` (warning, not exclusion)
- The SearXNG snippets as the ONLY source of truth

Schema:
```json
{
  "places": [
    {
      "name": "str",
      "type": "restaurant|cafe|bar|brewery|distillery|market|street_food",
      "neighborhood": "str|null",
      "price_tier": 1,
      "why": "str (one sentence)",
      "local_specialty": "bool",
      "source_url": "str"
    }
  ],
  "local_dishes": ["str"],
  "allergy_warnings": ["str"]
}
```

The `body_md` rendering groups places by type, leads with local specialties, and
appends allergy warnings as a visible `⚠️` block when present.

### 3.2 Events & activities resolver (`discover/resolvers/events.py`)

**Scope:** city. **Confidence:** medium (date-bounded, source-grounded).

This resolver produces a **three-tier output** (D-98):

#### Tier 1 — Impact events (always shown, regardless of profile)

Events that affect every traveler: religious observances (Ramadan, Diwali,
Semana Santa), national holidays, political events, strikes, seasonal closures.
These surface as **impact warnings** — "it's Ramadan; expect limited food
options and no bars" — not as recommendations. They go into the finding's
`impacts` list and render prominently at the top.

The resolver searches for these unconditionally:
- `"holidays {city} {month} {year}"`
- `"{country} public holidays {month} {year}"`
- `"religious observances {country} {month} {year}"`

Ollama is prompted: *"Identify events during these dates that would impact any
traveler regardless of their interests — religious observances, national holidays,
closures, strikes, or safety events. These are not recommendations; they are
things the traveler needs to know."*

#### Tier 2 — Suggested (matches your activity interests)

Events and activities that match the traveler's checked activity interests in
the Discovery Profile. A food festival surfaces for someone who checked
`markets`; a live music venue surfaces for someone who checked `live_music`.
These are **curated recommendations** — the profile is the filter.

Queries — one per checked activity interest:
- `"{interest} {city} {month} {year}"` (for date-bound events)
- `"{interest} near {city}"` (for persistent activities/venues)

These render as the main recommendation list, grouped by interest category.

#### Tier 3 — Other (available but not matching your interests)

Activities and events that turned up in the search results but don't match any
checked interest. A water park surfaces here if the traveler didn't check
`sports` or `wellness`. These render in a collapsed **"Other activities"**
section — visible and browseable, but not highlighted.

The Ollama prompt receives the full list of checked interests and classifies
each result:
- *"Classify each event/activity: does it match one of these interests:
  [{interests}]? If yes, mark it 'suggested' with the matching interest. If no,
  mark it 'other'."*

Schema:
```json
{
  "impacts": [
    {
      "name": "str",
      "date_range": "str|null",
      "effect": "str (one sentence — what it means for the traveler)",
      "source_url": "str"
    }
  ],
  "suggested": [
    {
      "name": "str",
      "date_range": "str|null",
      "type": "event|venue|activity",
      "matched_interest": "str (the activity interest it matched)",
      "why": "str (one sentence)",
      "source_url": "str"
    }
  ],
  "other": [
    {
      "name": "str",
      "date_range": "str|null",
      "type": "event|venue|activity",
      "category": "str (best-guess category)",
      "why": "str (one sentence)",
      "source_url": "str"
    }
  ]
}
```

The `body_md` rendering:
1. **Impacts** at the top with a `⚠️` prefix — these are not optional
2. **Suggested** as the main list, grouped by matched interest
3. **Other** in a collapsible section ("Other activities")

This is the most date-sensitive resolver — a re-run closer to the trip date
yields fresher results. The finding's `ttl_seconds` should be short (7 days).

### 3.3 Visa & entry resolver (`discover/resolvers/visa.py`)

**Scope:** country. **Confidence:** flagged (high-stakes; D-66).

Requires the traveler's **nationality** — sourced from `TravelerIdentity`
(the `self` traveler on the trip, or the only `TravelerIdentity` with
`kind=self`). NULL = skip this resolver with a visible "set your nationality
to enable visa research" message.

Queries:
- `"{nationality} passport visa requirements {country}"`
- `"vaccination requirements {country} {year}"`

**Corroboration (D-66):** visa/safety claims require N≥2 distinct source domains
in the SearXNG results. Claims with only one source render under a visible
`⚠️ Verify at official source` banner baked into `body_md`. This is mechanical
(count distinct domains per claim), not a judgment call.

Schema:
```json
{
  "visa": {
    "required": "bool|null",
    "type": "str|null (e-visa, visa on arrival, visa-free, etc.)",
    "max_stay": "str|null",
    "source_urls": ["str"],
    "notes": "str"
  },
  "vaccinations": [
    {
      "name": "str",
      "required_or_recommended": "required|recommended",
      "source_url": "str"
    }
  ]
}
```

### 3.4 Safety & transport resolver (`discover/resolvers/safety.py`)

**Scope:** country (safety) + city (transport tips). **Confidence:** medium,
corroborated for safety; medium for transport.

Queries:
- `"travel safety {country} {year}"`
- `"getting around {city} public transit"`
- `"taxi app {city}"`
- `"scams to avoid {city}"` (practical, not alarmist)

Corroboration applies to safety claims (same rule as visa). Transport tips are
medium-confidence (practical, lower stakes).

Schema:
```json
{
  "safety": {
    "summary": "str",
    "advisories": [{"text": "str", "source_url": "str"}],
    "scams": ["str"]
  },
  "transport": {
    "summary": "str",
    "apps": ["str"],
    "tips": ["str"],
    "source_urls": ["str"]
  }
}
```

---

## 4. LLM grounding (D-66, expanded)

Reuses the `OllamaExtractor` pattern from `extract/llm.py`: `/api/chat` with a
JSON-schema `format` grammar, `temperature: 0`, `num_ctx: 16384`.

Two mechanical checks, applied to every LLM resolver's output:

1. **Source grounding** — drop any item whose `source_url` was NOT in that
   resolver call's SearXNG result set. This kills invented URLs and fabricated
   claims. No judgment, pure set-membership check.

2. **Corroboration** (visa + safety only) — count distinct source domains per
   claim. Claims with < 2 distinct domains get a visible `⚠️ Verify at official
   source` banner in `body_md`. Claims with ≥ 2 pass without a banner.

Both checks are in a shared `discover/grounding.py` module, called between the
Ollama response and the Finding assembly. The grounding module also records what
was dropped (for the run's transparency log).

---

## 5. SearXNG integration

SearXNG is already running in the stack (`searxng-core` on `travel-net`). Config:

- `TX_SEARXNG_URL` = `http://searxng:8080` (matches `TX_OLLAMA_URL` /
  `TX_PHOTON_URL` convention).
- SearXNG JSON output must be enabled in `settings.yml` (`search.formats:
  [html, json]`).

### 5.1 Search helper

New `discover/search.py`:

```python
def searxng_search(query: str, *, categories: str = "general",
                   language: str = "en", max_results: int = 10,
                   base_url: str) -> list[SearchResult]:
    """Query SearXNG, return a flat list of {url, title, snippet}.
    Returns [] on any failure — a resolver that can't reach SearXNG degrades
    to a flagged/absent finding, never a 500."""
```

Uses `GET {base_url}/search?q={query}&format=json&categories={categories}`.
No API key. Same `get_json` / retry pattern from `discover/resolvers/base.py`.

### 5.2 Query construction

Each LLM resolver builds its SearXNG queries from three inputs:

1. **The place** — city name, country (from `DerivedPlace`).
2. **The profile** — adds/removes query terms per §2.2.
3. **The trip dates** — for events ("October 2026 {city}").

A resolver may issue **multiple SearXNG queries** per place — e.g. the food
resolver issues one base query plus one per checked drink interest. Results are
deduplicated by URL before feeding to Ollama.

---

## 6. Durable background job (D-63, expanded)

The deterministic tier runs synchronously (fast). The LLM tier runs
minutes-to-hours and must survive a restart. Design from SPEC-3-discovery.md §6:

- The `DiscoveryRun` row IS the job state. The background thread writes
  `phase/done/total` to the row; the poll panel reads the row.
- A restart mid-run → next poll marks the row `failed` (re-run to recover).
- Quality-paced: per-resolver Ollama timeout (600s) × topics × places.
  **No overall wall clock.**
- One run per trip at a time (guard on an existing `running` row for the trip).
- SearXNG queries are fast (~1s each); Ollama synthesis is the bottleneck.
- The HTMX poll panel reuses the existing `_job_panel.html` shape from the
  review queue (SSE or 3s poll).

### 6.1 Mixed-tier run

A "Run Discovery" click now runs BOTH tiers in sequence:

1. Deterministic resolvers (synchronous, fast) — plugs, weather, currency,
   language L1/L2.
2. LLM resolvers (background thread) — food, events & activities, visa, safety,
   language L3.

The deterministic findings appear immediately; the LLM findings stream in as the
background thread completes each. The poll panel shows which topic/place is in
progress.

---

## 7. Resolver interface changes

The `resolve()` signature is unchanged:

```python
def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None
```

`ResolverContext` gains:

```python
@dataclass
class ResolverContext:
    start_date: str | None = None
    end_date: str | None = None
    today: str | None = None
    base_currencies: tuple[str, ...] = ("CAD", "USD")
    timeout: int = 12
    # 3.2 additions:
    searxng_url: str | None = None
    ollama_url: str | None = None
    discovery_profile: dict | None = None   # the values blob from DiscoveryProfile
    nationality: str | None = None          # ISO-2, from TravelerIdentity
```

Deterministic resolvers ignore the new fields. LLM resolvers require
`searxng_url` and `ollama_url`; if either is None the resolver returns None
(degrades gracefully, same as a missing API). The `discovery_profile` is the
`values` JSON blob from §2.2 — resolvers read the sections they care about
(food resolver reads `food` + `drink`; events resolver reads `activity`).

---

## 8. Push to AdventureLog Notes

Approved findings push to AdventureLog Notes via the existing push path
(`app/trips/routes.py` → AdventureLog API). Each finding becomes one Note in the
trip's collection, keyed idempotently by topic + place_key — consistent with the
trip-prep skill's nine-note convention and the "no reconciliation engine" stance
(a push is one-shot at creation time; a re-push updates in place).

The Note content is the finding's `body_md` (already rendered as markdown by the
resolver). Ownership is marked by the same `<!-- yonder:discovery:{topic}:{place_key} -->`
HTML comment pattern the trip-prep skill uses, so a Yonder-pushed note and a
skill-pushed note for the same topic are distinguishable and don't collide.

---

## 9. Migration

One Alembic migration (**0014**, head is 0013) adds:

### New tables

```
DiscoveryProfile
  id            str  pk
  traveler_id   int|null  FK traveler_identities.id, indexed
  values        json  default {}
  created_at    datetime
  updated_at    datetime

TripTraveler
  trip_id       str  FK trips.id (CASCADE), PK
  traveler_id   int  FK traveler_identities.id, PK
  role          str(16)  "self"|"companion"
```

### Columns on existing tables

```
TravelerIdentity:
  nationality   str(2)|null   -- ISO-2 country code

HomeProfile:
  traveler_id   int|null      -- FK traveler_identities.id, indexed
```

All additions are nullable with no default — zero disruption to existing data.
`HomeProfile` rows with `traveler_id=NULL` continue to work as deployment-wide
defaults (the resolver falls through: trip traveler's homes → NULL homes).
`TripTraveler` starts empty; existing trips have no travelers assigned until the
user adds them.

---

## 10. Resolver registry update

`discover/resolvers/__init__.py` gains a `build_llm()` factory:

```python
def build_llm(*, searxng_url: str, ollama_url: str) -> list[Resolver]:
    return [
        FoodResolver(searxng_url=searxng_url, ollama_url=ollama_url),
        EventsResolver(searxng_url=searxng_url, ollama_url=ollama_url),
        VisaResolver(searxng_url=searxng_url, ollama_url=ollama_url),
        SafetyResolver(searxng_url=searxng_url, ollama_url=ollama_url),
    ]
```

`run_discovery()` receives both `build_deterministic()` and `build_llm()`
resolver lists. Deterministic resolvers run synchronously; LLM resolvers run in
the background thread. Both use the same `_upsert` / `_prune_stale_drafts` /
idempotency machinery.

---

## 11. UI additions

### 11.1 Discovery Profile form

New route: `GET/POST /trips/settings/discovery-profile`. Linked from the
Administer nav alongside the existing Packing Profile link.

Form structure (mirrors the §2.2 schema):

- **Food constraints** — dietary restrictions (multi-select checkboxes), cuisine
  dislikes (tag input with free text), allergies (text input), budget ceiling
  (radio group: no limit / $ / $$ / $$$ / $$$$, rendered with the dollar-sign
  labels).
- **Drink interests** — checkboxes (opt-in, unchecked = not searched).
- **Activity interests** — checkboxes (opt-in, unchecked = not searched).
- **Practical constraints** — mobility (dropdown).

Same HTMX form-save pattern as the Packing Profile (inline success message, no
page reload).

### 11.2 Trip travelers

The trip detail page gains a **"Who's traveling"** section — a list of
TravelerIdentity records associated via `TripTraveler`. Add/remove travelers via
a simple picker (dropdown of existing TravelerIdentity rows + "Add new"). The
`self` traveler auto-links on trip creation.

This replaces the old `traveling_with` profile/hint field with a first-class
data model.

### 11.3 Discovery tab updates

The Discovery tab on the trip detail page gains:

- **Food & drink**, **Events & activities**, **Visa & entry**, **Safety &
  transport** finding groups alongside the existing Weather / Currency / Plugs /
  Language groups.
- The events finding renders the three-tier layout: impacts at top (⚠️),
  suggested in the middle (grouped by interest), other in a collapsible section.
- LLM-produced findings show their **confidence** badge (medium / flagged) and
  their **source links** (the SearXNG URLs that survived grounding).
- Findings with allergy warnings render the `⚠️` block visibly.
- The `⚠️ Verify at official source` banner on under-corroborated visa/safety
  claims.

### 11.4 Nationality prompt

If `TravelerIdentity.nationality` is NULL when a visa finding is attempted, the
Discovery tab shows a one-line prompt: *"Set your nationality in Administer →
Travellers to enable visa research."* Not a blocker — Discovery runs without it;
the visa resolver simply produces no finding.

### 11.5 Traveler admin enrichment

The existing Administer → Travellers page gains:

- **Nationality** field (ISO-2 country picker) on the TravelerIdentity form.
- Links to that person's **Discovery Profile** and **Packing Profile** (one
  click to edit their preferences).

---

## 12. Decisions (new, continuing from D-86)

- **D-87** — Discovery Profile captures **constraints (what to skip) and
  interests (what to add)**, never affinities or inferences. Dislikes are short
  lists; interests are opt-in checkboxes. A blank profile produces generic "best
  of" results. No origin, ethnicity, or history is ever inferred.
- **D-88** — Budget ceiling uses the **$/$$/$$$/$$$$ convention** (integer 1–4,
  0 = no limit). It is a ceiling: selecting $$ means "show $ and $$, skip $$$
  and $$$$." The ceiling shapes SearXNG queries and the Ollama prompt.
- **D-89** — `DiscoveryProfile` follows the **`PackingProfile` pattern**: nullable
  `traveler_id`, `values` JSON blob, structured form in the Administer nav.
- **D-90** — LLM resolver output is **schema-constrained + source-grounded**
  (D-66 enforced). Every claim carries a `source_url`; claims whose URL was not
  in the SearXNG result set are dropped mechanically. Visa/safety claims
  additionally require corroboration (≥2 distinct source domains) or render
  under a visible verify banner.
- **D-91** — `TravelerIdentity` gains a nullable `nationality` (ISO-2) column.
  The visa resolver requires it; all other resolvers ignore it. A missing
  nationality skips the visa resolver with a visible prompt, never a failure.
- **D-92** — Mixed-tier run: deterministic resolvers run synchronously (immediate
  results), LLM resolvers run in the background thread (the durable job). Both
  tiers share the same idempotency, upsert, and prune machinery.
- **D-93** — The Discovery Profile is **not inferred from data**. It is what the
  user enters in the form. Foursquare history, check-in categories, and traveler
  origin are explicitly excluded as profile inputs. The profile is a distributable,
  portable preference model any Yonder user fills out in 5 minutes.
- **D-94** — **TravelerIdentity is the person spine.** Every profile table
  (`HomeProfile`, `PackingProfile`, `DiscoveryProfile`) hangs off it via
  `traveler_id`. Trip membership is a structured `TripTraveler` association, not
  a free-text hint. This is the foundation for multi-person trips, per-person
  packing, and per-person discovery — even though 3.2 operates single-user.
- **D-95** — **One Discovery Profile per person.** People don't have multiple
  personalities. The profile is a durable preference set, not a per-trip or
  per-mood configuration.
- **D-96** — **`traveling_with` is derived from the trip's traveler roster**, not
  entered on the profile or as a trip hint. Each companion's own profile carries
  their constraints; the resolver reads the union of all travelers' profiles.
- **D-99** — **People maintain their own profiles.** No age tracking, no
  birthdates, no per-trip detail overrides. If a child grows up, the person
  responsible updates that profile. Yonder does not model temporal personal
  attributes — the profile is current state, not a time series.
- **D-97** — **System rules vs person rules.** System rules (geocoding policy,
  grounding thresholds) stay system-wide. Person rules (dietary, budget, packing
  style, mobility) live on the person's profile tables. When a new rule is
  identified as person-specific, it targets the TravelerIdentity fan-out.
- **D-100** — **Generic guests are roster placeholders, ignored by discovery.**
  The trip's "add a guest" quick-add mints a minimal `TravelerIdentity`
  (`kind=guest`, `generic=True`, no profile, no nationality) so you can record
  "someone is coming" without building a person. Generic identities are
  **excluded** from discovery and planning (via `discovery_travelers()`), so a
  guest name never leaks into a recommendation and the identity table never
  becomes a dumping ground for considerations ("female guest", "old guest").
  `display_name` is UNIQUE, so quick-add reuses an existing row by name rather
  than duplicating; reusing a real (non-generic) identity never flips its flag.
  To make a companion actually count for discovery, give them a real profile —
  not a guest name.
- **D-98** — **Events & activities three-tier model.** Tier 1 (impacts): events
  that affect every traveler regardless of interests (Ramadan, holidays, strikes)
  — always shown, rendered as warnings. Tier 2 (suggested): events and activities
  matching the traveler's checked activity interests — the curated
  recommendations. Tier 3 (other): results that don't match any checked interest
  — shown in a collapsible section, available but not highlighted.

---

## 13. Phasing (3.2 delivery order)

1. **TravelerIdentity spine + migration** — `nationality` column,
   `HomeProfile.traveler_id` column, `TripTraveler` table, `DiscoveryProfile`
   table. The data model exists before any resolver ships.
2. **Trip travelers UI** — "Who's traveling" on the trip detail page.
3. **Discovery Profile form** — editable preferences before any LLM resolver.
4. **SearXNG helper** (`discover/search.py`) + SearXNG config (`TX_SEARXNG_URL`).
5. **Grounding module** (`discover/grounding.py`) — shared by all LLM resolvers.
6. **Food & drink resolver** — the proof-of-concept. Ships first because it has
   the strongest profile interaction (dietary, dislikes, budget, drinks) and the
   most immediate user value.
7. **Events & activities resolver** — the three-tier model (impacts / suggested /
   other). Date-sensitive, short TTL.
8. **Visa & entry resolver**.
9. **Safety & transport resolver**.
10. **Language L3** (regional register) — grounded overlay on the existing L1/L2.
11. **Durable background job** — the poll panel + background thread, wired once
    all LLM resolvers are ready.
12. **Push to AdventureLog Notes** — the payoff. Approved findings → Notes,
    idempotent by topic + place_key.

Steps 1–5 are infrastructure. Step 6 is the first user-visible result. Steps
7–10 are additional resolvers behind the same interface. Steps 11–12 are
orchestration and output.

---

## 14. Open questions

- **OQ-08** — *(Resolved → D-98.)* Activity interests feed the events & activities
  resolver as the curation lens for tier 2 (suggested). Events that impact
  everyone (tier 1) are searched unconditionally. The three-tier model is the
  rule for cross-resolver profile usage.
- **OQ-09** — *(Resolved → D-94/D-96/D-99.)* Per-trip `traveling_with` is derived
  from the `TripTraveler` roster. Each companion has their own profile; no age
  tracking or per-trip overrides. People maintain their own profiles.
