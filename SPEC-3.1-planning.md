# Yonder 3.1 — Planning (the Packing Plan)

Status: **PLANNED (spec for discussion)**. Distilled from `planning/*.md` (the
owner's packing research) and re-cast onto Yonder's real architecture.

---

## 0. The one-paragraph pitch

**Discovery scouts; Planning packs.** Discovery (3.0) answers *"what is it like
there?"* — weather, currency, plugs, language — as a set of briefing findings.
Planning answers the other half of trip prep: *"what do I bring?"* It is not a new
app and not another resolver — it's an **aggregator** that sits *downstream* of
Discovery and the trip, turning everything Yonder already knows into one
structured, right-sized, bag-assigned **Packing Plan** you review and correct.

The source docs assume a **form** the traveller fills (destination climate,
cultural norms, accommodation, activities with venue types, weather). Yonder's
whole thesis is the opposite: **derive, don't ask.** Almost every input those docs
demand, Yonder can compute first-party from the trip it already holds. What's left
— genuine *preferences* — is asked **once** and remembered. That reframe is the
spine of this spec.

> **Why Planning is a capability, not Discovery topic #9.** Packing was the 9th
> topic in the original Discovery list. It doesn't fit: every other topic is a
> *place → markdown briefing*. Packing is a *whole-trip → structured checklist
> with quantities, bag assignments, and warnings*, computed by scoring templates
> and running modifier passes. It outgrew the resolver model. So it graduates into
> its own pillar — **Planning** — with the Packing Plan as its first (3.1)
> deliverable, leaving the name room to grow (budget, day-plan optimisation) later.

---

## 1. Derive, don't ask (the input map)

The source `Trip Context` schema has ~12 fields the user must supply. Here's where
each actually comes from in Yonder:

| Source-doc input | Yonder's answer | How |
|---|---|---|
| destination city/country | **derived** | Discovery location derivation (`discover/places.py`) — already built |
| duration_days | **derived** | `trip.end_date − trip.start_date` |
| weather_forecast (temp/precip/humidity) | **derived** | the Discovery **weather finding's `data`** — no second weather API |
| climate_zone | **derived** | from destination coords + the weather numbers |
| cultural_norms | **derived** | country → shared `culture.json` (see §4) |
| transport_mode | **derived** | presence of flight / train / drive / carRental / cruise activities |
| accommodation_type | **derived** | a NEW `type` field on the lodging activity (hotel/hostel/resort/bnb/campground/cabin/…), **shared with AdventureLog's `Lodging.type`** so the push populates it too (D-81) |
| activities + venue_type | **derived** | TripActivity **types** → archetype tags, no re-entry of what's already captured (D-80); LLM only sharpens ambiguous free-text names later (3.2) |
| special_events (wedding/conference) | **asked per-trip (optional)** | one small "planning hints" field, or inferred from `event`/`dining` activity names |
| laundry_access | **profile default, per-trip override** | rarely knowable from data |
| **the whole User Profile** | **asked once** | a persistent **Packing Profile** (§3) — real preferences, not derivable |

**Principle (D-73): the plan is a draft you correct, not a form you fill.** Same
loop as Discovery — derive → review → adjust — so the two pillars feel identical.

---

## 2. The pipeline — eight deterministic passes

The engine is a composable line of pure passes. Context in, plan out; each pass
only *adds/annotates/scales*, never hides the reasoning (every item carries a
`reason`, echoing Discovery's grounding discipline).

```
 trip + activities + Discovery findings + Packing Profile
        │
  1. READ THE TRIP        derive duration, destinations, weather, transport,
        │                 accommodation, activity tags, cultural norms
        ▼
  2. CHOOSE THE BLUEPRINT score the 7 archetypes; pick top 1–2 (merge if mixed)
        │
  3. LAY OUT THE KIT      instantiate base items from the archetype(s), dedupe
        │
  4. ADAPT TO CONDITIONS  modifier passes, additive:
        │                   · climate  (from the weather finding)
        │                   · cultural (country + per-venue overrides)
        │                   · activity (hiking/beach/business/temple…)
        │                   · event/seasonal (Ramadan, cherry-blossom…)
        ▼
  5. RIGHT-SIZE           quantity scaling: duration × laundry × style × one-bag
        │
  6. MAKE IT YOURS        profile pass: force must-haves, filter style
        │                 constraints, pick gendered variants, downweight
        │                 past-unused items
        ▼
  7. LOAD OUT             bag assignment (carry-on essentials vs checked) +
        │                 weight estimate
        ▼
  8. PRE-FLIGHT CHECK     warnings: overpacking, forgotten essentials,
        │                 cultural/weather mismatch, over-weight
        ▼
     Packing Plan  →  review on Yonder  →  (3.2) push as an AdventureLog Checklist
```

Passes 2–8 are the "rules engines" from the docs, kept **100% deterministic**.
The LLM only ever *sharpens inputs to pass 1* and *narrates outputs of pass 8*
(§7) — it is never in the load-bearing path, exactly as in Discovery.

---

## 3. Data model (durable, Yonder-native)

Reuses the Discovery persistence pattern (durable rows, JSON envelopes, app-enforced
idempotency, human-edits preserved across re-runs).

Scaffolded for multiple travellers (a `traveler_id` FK from day one), but Yonder
has no multi-user yet, so 3.1 operates on the single `self` traveller — build and
test for one, ready to grow (D-82).

```
PackingProfile        the "ask once" preferences — attaches to a TravelerIdentity
  id, traveler_id (self by default), values (JSON):
    climate_preference   runs_cold | neutral | runs_hot
    packing_style        minimalist | balanced | prepared
    bag                  { mode: one_bag|carry_on|checked, weight_kg }
    laundry_comfort      none | hand_wash | machine
    style_constraints    [no_heels, modest_dress, business_casual_only, …]
    must_have_items      [cpap, insulin, contacts, …]
    cultural_strictness  strict | flexible
    clothing_variant     masc | fem | neutral   (drives template variant ONLY)
  Managed in the admin UI, same as HomeProfile / TravelerIdentity (never hardcoded).

PackingPlan           one generated plan for a trip (peer of DiscoveryRun)
  id, trip_id, status(running|done|failed), archetypes(JSON: chosen + scores),
  context(JSON: the derived inputs snapshot), warnings(JSON[]),
  tasks(JSON[]),       IN-TRIP actions the packing math implies, e.g.
                       {do: "laundry", around_day: 7, note: "5 tops for 12 days"}
  generated_at, finished_at

PackingItem           one checklist line (peer of DiscoveryFinding)
  id, plan_id, trip_id,
  item_key,            idempotency handle, e.g. "clothing:quick_dry_top"
  label, category,     "Quick-dry top", "clothing"
  quantity, bag,       4, "checked"
  reason,              "12 days, laundry mid-trip → 5 not 12"
  source,              template | modifier | must_have | activity | cultural
  swap_for,            when omitted: the suggested substitute (nullable)
  status               draft | packed | removed | omitted
                       (packed = ticked off; removed = you dropped it;
                        omitted = engine left it behind, with a reason — the
                        "can't/shouldn't bring" list, shown not hidden)
  A re-generate upserts on (trip_id, item_key) and PRESERVES packed/removed —
  your check-offs and edits survive, only draft rows refresh (same rule as D-63).
```

One migration (`0012`) adds the three tables. Migration `0011` is the current head
after 3.0.1.

---

## 4. Reference datasets — the "template database"

This is the packing template DB the owner asked for in 3.1. Three curated JSON
files, versioned in the repo like `plugs.json` / `phrases.json`:

- **`planning/data/archetypes.json`** — the 7 trip archetypes (Weekend City Break,
  Beach, Cold/Ski, Business, Backpacking, Road Trip, Cultural/Pilgrimage) with
  trigger conditions (activities/accommodation/transport/duration), base items,
  cross-archetype modifiers, and per-archetype quantity overrides. (Straight from
  `rule_sets_templates.md`.)
- **`planning/data/items.json`** — the item catalog: `{id, label, category, default_bag,
  weight_g, tags, variants{masc,fem,neutral}}`. The single source of truth items
  reference, so quantities/bags/weights aren't scattered across templates.
- **`planning/data/culture.json`** — cultural norms by country + venue overrides
  (dress code, shoes-off locations, add/remove items, taboos, seasonal/event
  flags). From `rule_sets_cultural.md`.

**Culture is shared, not duplicated (D-74).** `culture.json` is *also* the natural
backing for a future Discovery **"culture" briefing topic** (etiquette/dress as a
country-scope finding, deterministic base + LLM enrichment in the LLM tier). For
3.1, Planning reads it directly; when the Discovery culture topic lands it reads
the same file — one dataset, two consumers, never a second drifting copy.

---

## 5. A worked trace — Kyoto, 7 days

The docs' running example, but produced the Yonder way (nothing typed except the
one-time profile).

**Derived by pass 1 (zero user input):** duration 7; destination Kyoto/JP;
weather finding says highs ~28 °C / lows ~20 °C, humidity ~70 % → `lightweight` +
`humid`; activities include a `tour` named "Fushimi Inari" and a `dining` named
"kaiseki dinner" and `lodging` "…Ryokan" → tags `temple/hiking`, `formal_dining`,
accommodation `ryokan`; transport = flight + train; culture.json/JP → shoes-off at
temples & ryokan, modest shoulders/knees, add slip-on shoes + scarf.

**Profile (asked once):** balanced · one-bag carry-on 10 kg · hand-wash · no_heels
· modest_dress · must-have contacts · strict.

**Pipeline:**
- **2 Blueprint:** Cultural/Pilgrimage 5, Business 4 → merge both.
- **3 Kit:** modest tops, long bottoms, slip-on shoes, day bag, toiletry bag, chargers…
- **4 Adapt:** climate `lightweight+humid` → quick-dry fabrics, drop heavy layers;
  cultural JP → slip-on loafers + scarf, cover shoulders/knees, socks-no-holes for
  ryokan; activity `formal_dining` → one smart layer; `temple` → scarf (already).
- **5 Right-size:** one-bag + hand-wash → tops `min(7,4)+1 = 5`, bottoms 3,
  underwear 9, shoes 2.
- **6 Yours:** `no_heels` filters the business dress-shoe → smart flats;
  `modest_dress` keeps long/loose; contacts force-added (solution + backup glasses);
  variant selects the right clothing cut.
- **7 Load-out:** contacts/chargers/docs → carry-on; everything fits one bag; est.
  8.4 kg < 10 kg tolerance.
- **8 Pre-flight:** *"Humidity ~70 % — quick-dry chosen over cotton."* · *"Ryokan &
  temples remove shoes — slip-on loafers packed."* · no over-weight warning.

Output: ~24 items, categorised, bag-assigned, each with a one-line reason — a
checklist you tick off, not a wall of prose.

---

## 5.5 The intelligence — why a plan ≠ a list (D-83)

A dumb list scales linearly: 14 days → 14 of everything. **A real plan is the
opposite** — you don't fly home mid-trip to repack, so one merged plan must cover
the whole span *efficiently*. Three behaviours make it smart, and all three come
out of the same quantity math in passes 5–8:

- **Pack few, launder — as a derived in-trip task.** Quantity is
  `f(duration, laundry_comfort)`, not duration. When the packed count can't cover
  the trip on its own, the engine doesn't just pack less — it **emits an in-trip
  task**: *"do laundry around day 7 (5 tops for a 12-day trip)"*, and sizes a small
  detergent / laundry-kit item to match. A 14-day trip yields ~5–7 underthings **+
  a laundry task**, never 14 pairs. The plan tells you what to *do on the trip*,
  not only what to carry.
- **"Can't / shouldn't bring", shown not hidden.** Items filtered out — full-size
  liquids on a carry-on, heels for a temple trip, an over-weight third pair of
  shoes — become **omitted** rows with a reason and, where possible, a `swap_for`
  ("full-size shampoo → TSA 100 ml", "heels → smart flats"). Same transparency as
  Discovery's "couldn't place": the traveller sees the decision, never a silent
  drop.
- **One wardrobe across many climates.** A merged plan for a divergent trip (Dubai
  + Alps) layers rather than duplicates — a base of versatile pieces plus the
  minimum climate-specific adds for the coldest/hottest leg — and the reasons cite
  which leg drove each add. One bag, whole trip.

This is the payoff the owner named: *smart, efficient, whole-trip*, with
recommendations on what to bring, what not to, and **what to do en route**.

## 6. The Planning tab (UI)

A **Planning** section on the trip detail, sibling to Discovery, reusing every
pattern already shipped:

- **"Generate plan"** → engine runs (synchronous; deterministic tier is fast) →
  an interactive checklist grouped by the 7 categories, each a **collapsible
  group** with a count (reuse the 3.0.2 `<details>` + expand/collapse-all).
- Each item row: label · **quantity stepper** · **bag chip** (carry-on/checked) ·
  reason · a **pack/keep/remove** control (`packed` = ticked off; `removed` = drop
  it; both survive a re-generate).
- A **Warnings** panel at the top (over-weight, forgotten essentials, cultural /
  weather mismatch) — the complaint-driven value the docs centre on.
- A **To-do en route** strip (the derived in-trip tasks — laundry cadence, etc.)
  and a **Left behind / swapped** list (the `omitted` rows with reasons), so the
  intelligence in §5.5 is visible, not buried.
- **Re-generate** (preserves your check-offs/removals, refreshes drafts — same
  contract as a Discovery whole-trip re-run).
- Profile lives in the admin UI beside Home Profiles / Travellers.
- **Push** the finished plan to an AdventureLog **Checklist** — reuses the
  trip-prep skill's checklist-write path — lands with the Discovery push in 3.2.

---

## 7. Where the LLM helps (and only there)

Deterministic core ships first (3.1); the LLM is enrichment (3.1b / 3.2), always
optional, never load-bearing — same posture as the Discovery LLM tier:

1. **Activity sharpening** — free-text activity names → archetype tags
   ("Fushimi Inari sunrise hike" → `hiking`; "Gion tea ceremony" → `cultural`).
   Deterministic keyword rules handle the obvious; the LLM catches the rest.
2. **Edge-case culture** — country absent from `culture.json` → a *flagged* LLM
   suggestion (grounded/verify-labelled, like Discovery's flagged findings).
3. **Post-trip feedback (learning loop)** — "never wore the blazer, too heavy" →
   `{item: blazer, reason: too_heavy}` → downweight next time. Needs a history +
   care against overfitting → **deferred to 3.2**.
4. **Plain-English rationale** — an optional narrative summary of the plan.

---

## 8. What I'd change from the source docs (opinionated)

- **No new weather API.** The docs call OpenWeatherMap; reuse the Discovery
  weather finding. (D-75)
- **No duplicated cultural DB.** Share `culture.json` with the future Discovery
  culture topic. (D-74)
- **Drop `age_range` as a rules driver.** It stereotypes (age → "mobility",
  "trendy") for weak signal. Capture what actually matters — comfort/mobility,
  slip-on preference — explicitly via `style_constraints` / `special_needs`. Keep
  age only if ever wanted, non-driving. (D-76)
- **Gender → a single `clothing_variant` field** that only selects template cut
  (dress vs suit, swimwear), with a neutral default; never surfaces in a
  user-facing "reason", never drives anything else. (D-77)
- **Drop `preferred_brands`.** Yonder builds a checklist, not a shopping cart.
- **Defer the 0–100 item-weight learning loop** to 3.2 (needs trip history).
- **Derive context, don't collect it** — no climate/culture/accommodation trip
  form; infer from first-party data and let the review UI correct it (D-73).

The keepers, essentially intact: the **7 archetypes**, **archetype scoring**,
**climate/cultural/activity modifiers**, **quantity formulas**, **bag assignment +
weight**, and the **warning taxonomy** — those are the docs' real substance and map
cleanly onto passes 2–8.

---

## 9. Decisions

- **D-73** — Derive, don't ask: Planning computes trip context first-party
  (Discovery + activities + dates) and treats the plan as a draft to correct, not a
  form to fill. Only true preferences are asked, once, in a Packing Profile.
- **D-74** — `culture.json` is one shared dataset feeding both Planning and a
  future Discovery "culture" briefing topic — never duplicated.
- **D-75** — Climate comes from the existing Discovery weather finding; no second
  weather integration.
- **D-76** — `age_range` is dropped as a rules driver (stereotyping / low signal);
  mobility & comfort are captured explicitly.
- **D-77** — Gender collapses to a `clothing_variant` (template cut only), neutral
  default, never in reasons.
- **D-78** — Packing is a **Planning** capability (aggregator), not a Discovery
  resolver; it consumes Discovery outputs. The Packing Plan is a structured,
  durable checklist (`PackingPlan`/`PackingItem`), reviewed on Yonder, pushed to an
  AdventureLog Checklist in 3.2.
- **D-79** — Deterministic engine first; LLM only sharpens pass-1 inputs and
  narrates pass-8 output; the feedback-learning loop is 3.2.
- **D-80** — Archetype tags derive from TripActivity **types**; nothing already
  captured is re-entered (no per-activity tag field in 3.1).
- **D-81** — Accommodation comes from a new lodging-activity `type` field, whose
  values align to AdventureLog's `Lodging.type`, so the push populates it too.
- **D-82** — `PackingProfile` is scaffolded per-traveller (`traveler_id`) but 3.1
  operates on the single `self` traveller — Yonder has no multi-user yet.
- **D-83** — One merged plan per trip, made *smart*: quantities are
  `f(duration, laundry)` with a derived **in-trip laundry task** rather than
  one-per-day; filtered items become transparent **omitted** rows with reasons and
  swaps; divergent-climate trips layer rather than duplicate.

## 10. Phasing

- **3.1 (this build)** — the three reference datasets, the `PackingProfile` +
  `PackingPlan`/`PackingItem` model (migration 0012), the 8-pass deterministic
  engine, and the Planning tab (interactive checklist + warnings + re-generate).
  Context fully derived; zero LLM.
- **3.2** — LLM activity sharpening + edge-case culture, the post-trip feedback
  learning loop, the AdventureLog Checklist push, and the Discovery "culture"
  briefing topic that shares `culture.json`.

## 11. Open questions — resolved 2026-08-15

All four closed by the owner → decisions above:
- Activity semantics → **D-80** (derive from types, no re-entry).
- Accommodation → **D-81** (new lodging `type`, shared with AdventureLog).
- Profile scope → **D-82** (scaffold per-traveller, run single-user now).
- Multi-country → **D-83** (one merged, intelligent plan; see §5.5).

None currently open.
