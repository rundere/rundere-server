# SPEC (planning) — Packing Inventory & Trip Post-Mortem

**Status: 5.0 (inventory, questionnaire, templates) being built on `next`;**
**5.x (consumption, reorder, post-mortem) planned.** D-127 is accepted
(2026-11-07) and D-229–D-231 record the 5.0 design (§6). D-128–D-130 are still
proposed: they wait for 5.x, after the inventory has been used. §§1–5 are the
original plan, kept for the 5.x parts; where they differ, §6 wins.

Turns packing from a per-trip checklist into a **closed-loop personal inventory**
that maintains itself across trips, and introduces a **Trip Post-Mortem**
framework (packing review first).

## The loop
```
Inventory  →  Packing plan draws from it  →  Trip  →  Packing post-mortem
   ↑                                                         │
   └──────  Inventory recomputed + Reorder list  ←───────────┘
```

## 1. Per-user inventory is the master list (D-127)
There is **no shared master packing list**. Each user owns a personal inventory
of real products. The existing deployment `PackingCatalogItem` (shared, D-84) is
demoted from "the list" to just **seed data + the archetype engine's generic
type vocabulary** ("toothpaste", "swimwear") — generation *rules*, not a
user-facing list. A new user's inventory is **seeded** from that generic set,
then personalised (brand, size, quantity, thresholds). Households / shared
inventory are deferred (per-user to start).

`InventoryItem` (per-user):
- `user_id`, `label`, `category`, `tags[]` (beach, formal, cold, modest… — the
  hooks archetypes match on)
- `kind`: **consumable** | **durable** (drives all post-mortem math)
- `brand`, `characteristics` (e.g. "25 ml mini", "tablets, any brand")
- `quantity_on_hand`, `unit` (each | pack | …), `reorder_threshold`
- `usage_rate` (consumables): `{basis: per_day|per_trip, amount}` — the
  auto-estimate input (D-128); durables = 0
- `reorderable`, `reorder_url` (saved product link, D-129), `notes`, `enabled`
- `type_key` (optional): the generic catalog type it fulfils, so archetypes match

## 2. Consumables vs durables
- **Consumable** (toothpaste, sunscreen): depletes; a trip **decrements** stock
  by the amount used.
- **Durable** (jacket, charger): **returns** to inventory after the trip; only a
  loss decrements it.
This single flag decides how the post-mortem reconciles each line.

## 3. Packing plan ↔ inventory
Generation (archetypes + profile, D-123) now resolves each needed *type* to the
user's **inventory** item(s) by `type_key`/tags. Two payoffs:
- **Pre-trip acquire flag:** if the trip needs more than `quantity_on_hand`
  (e.g. out of sunscreen before a beach trip), flag it now — reorder can trigger
  *before* a trip, not only after.
- `PackingItem` gains `inventory_item_id` so the post-mortem can reconcile the
  exact product.

## 4. Trip Post-Mortem framework (D-130)
A per-trip, per-user **`TripPostMortem`** shell, offered when a trip becomes
`past` (or on demand), with modular sections. **v1 = the packing review only**;
the shell is designed so later versions add **events** (planned vs actual
timeline), **cost** (actuals vs budget), and **reviews** (ratings/notes) as
sibling modules.

### Packing review flow (auto-estimate, correct exceptions — D-128)
1. Yonder **pre-fills estimated consumption** per packed line from `usage_rate`
   × trip length (capped at packed qty); durables default to "returned".
2. The user **corrects only the exceptions** (used more/less, unused, lost).
3. On finalize: consumables `-= actual_used`, losses `-= 1`, durables unchanged;
   then any item now `< reorder_threshold` lands on the **reorder list**.

## 5. Reorder (flag + saved link — D-129)
The reorder list is **derived**: inventory below threshold, each row showing the
product's saved `reorder_url` to buy manually. **No purchasing inside Yonder**
(consistent with the local-first / no-external-commerce posture). A model hook is
reserved for a future shopping-list export, but nothing automated in v1.

## Data model sketch
- New `inventory_items` (per-user, as above).
- `packing_items` += `inventory_item_id` (nullable link).
- New `trip_post_mortems` (trip_id, user_id, status open|done, sections JSON).
- New `packing_outcomes` (post_mortem_id, packing_item_id, inventory_item_id,
  estimated_used, actual_used, returned, lost) — or fold outcomes into
  `packing_items` post-trip fields; TBD in design.
- Reorder list is computed (optionally snapshotted per post-mortem).

## Open questions (for the design phase)
- **Seed usage-rates:** defaults by category (toiletries=consumable w/ a nominal
  per-day rate; clothing/tech=durable) so auto-estimate works before the user
  tunes anything.
- **Units:** keep coarse (each/pack + a "consumable fraction") vs a real unit
  system (ml/g). Lean coarse for v1.
- **Migration:** one-time "seed my inventory from the catalog" per user; how
  archetypes reference types once the shared catalog is only seed/vocabulary.
- **Cost/events modules** need a trip budget/actuals model and planned-vs-actual
  activity diffing — scoped when those post-mortem modules are built.
- **Version placement:** a post-4.0 major (candidate 5.0). TBD.

## Impact on the 4.0 build (assessed 2026-08-30)
**Additive; no rework of completed 4.0 work.** 4.0's per-user packing (D-123) is
exactly the foundation this needs, so the hard dimension is already de-risked.
By element: DB = new tables + one nullable `packing_items.inventory_item_id`
(additive); API = new endpoints, `/api/v1` unchanged; `scope.py` = one new
"own-only user resource" filter (inventory is personal, not trip-shared); MCP =
new tools; trip lifecycle/post-mortem trigger reads existing "trip is past".
The **one deep change is the planning engine's item resolution**
(archetype→shared-catalog becomes archetype→user-inventory, D-127) — a
future-version concern that never touches 4.0.

**Only "act now" item — the iOS app (Phase 5, not yet built):** inventory adds
*later* screens (inventory, post-mortem, reorder), not changes to the core
screens, provided the app is built with four cheap forward-compat guards (see the
Phase-5 notes): lenient offline decoding (tolerate unknown fields), packing kept
a self-contained module, reserved IA space for an Inventory area + a post-trip
prompt, and staying on additive `/api/v1` (no v2). Follow those and no Phase-5
work is wasted.

## 6. The 5.0 design: questionnaire, inventory, templates (YONDER-62)

Agreed with the owner on 2026-11-07 and built on `next` in waves 4b–4e. This
section is the contract for those waves. D-127 is accepted; D-229–D-231 below
record the rest. D-128–D-130 (consumption, reorder, post-mortem) wait for 5.x,
after this has been used for a while, but the inventory model gets their
columns now (§6.6) so they don't need another migration.

### 6.1 What a person sees

1. A new account has **no packing profile** and **no inventory**.
2. Settings → Packing profile opens the **questionnaire** (§6.2): a few pick
   lists, no free text except the optional extras.
3. Saving it **seeds the inventory** (§6.3). The person then renames, adds,
   deletes and sets quantities. Every category is shown, even an empty one.
   Any item can be marked **must-have** (packed on every trip).
4. The same page holds the **packing preferences** (§6.4) and the person's
   **templates** (§6.5).
5. On a trip, **Generate packing list** builds a list for the signed-in person
   only, from their inventory (§6.7). Until the questionnaire has been saved it
   refuses with a link to it (§6.8).

### 6.2 The questionnaire

Each question is a closed pick list (D-86). Answers are stored on the
`PackingProfile` (§6.6) and can be changed later.

| # | Question | Kind | Options (stored key) |
|---|---|---|---|
| Q1 | Which clothing style should your list use? | one | Menswear (`masc`) · Womenswear (`fem`) · Generic / mixed (`neutral`) |
| Q2 | What clothing do you already travel with? | many | Everyday basics (`basics`, always on) · Smart casual (`smart`) · Business / formal (`formal`) · Outdoor and active (`outdoor`) · Swim and beach (`swim`) · Cold weather (`cold`) · Modest / cover-up (`modest`) |
| Q3 | Footwear | one | Minimalist: one or two pairs that do everything (`minimal`) · Varied: the right shoe for each thing (`varied`) |
| Q4 | Personal care you always take | many | Basic toiletries (`toiletries`, always on) · Skincare (`skincare`) · Hair care and styling (`hair`) · Shaving (`shaving`) · Make-up (`makeup`) · Glasses or contact lenses (`eyewear`) · Sun care (`sun`) · Regular medication (`meds`) |
| Q5 | Bags and organisers you own | many, with a count for bags | Daypack (`daypack`) · Travel backpack (`backpack`) · Carry-on suitcase (`carry_on`) · Checked suitcase (`checked`) · Personal item / tote (`personal_item`) · Packing cubes (`cubes`) · Compression bags (`compression`) · Toiletry bag (`toiletry_bag`) |
| Q6 | Tech you travel with | many | Phone (`phone`, always on) · Laptop (`laptop`) · Tablet or e-reader (`tablet`) · Camera (`camera`) · Headphones (`headphones`) · Smartwatch or fitness tracker (`watch`) · Travel adapter (`adapter`) · Car charger (`car`) |

**Q1 is a clothing style the person picks, never their gender** (D-229, and the
4.13.5 rule that nothing is inferred from identity). It maps one to one onto
the catalogue's existing `variants` (`masc` / `fem` / `neutral`), which choose
an item's label ("Smart-casual shirt" or "Smart-casual top"), and onto a new
per-item `styles` list (§6.3) that says which styles an item is seeded for.
Q4 asks about care items directly instead of deriving them from Q1.

### 6.3 From answers to a seeded inventory

The seed is **every enabled catalogue item whose seed tags match an answer**,
plus the always-on rows, filtered by Q1's style. Seed tags are a new catalogue
field, `seed` (4b), kept apart from the engine's existing descriptive `tags`
(`warm`, `revealing`, …), which the generator still matches on.

| Answer | Seeds catalogue items with `seed` tag | Examples from today's catalogue (4b adds the rest) |
|---|---|---|
| always | `essential` | passport, tickets, reservations, phone charger, power bank, prescription meds, pain relief, sunglasses |
| Q2 `basics` (always) | `basics` | T-shirt, jeans, underwear, socks, sleepwear, cardigan, light jacket, walking shoes |
| Q2 `smart` | `smart` | smart-casual top, chinos |
| Q2 `formal` | `formal` | blazer, dress shirt, business trousers, evening outfit, dress shoes / smart flats |
| Q2 `outdoor` | `outdoor` | quick-dry top and trousers, waterproof jacket, hiking boots, daypack, water bottle, headlamp |
| Q2 `swim` | `swim` | swimwear, rash guard, sandals, beach towel, waterproof pouch |
| Q2 `cold` | `cold` | thermal base layer, wool sweater, insulated jacket, wool socks, beanie, gloves, winter boots |
| Q2 `modest` | `modest` | modest long-sleeve top, long skirt, loose trousers, modest scarf, headscarf |
| Q3 `minimal` | footwear: only items with `seed_minimal` | walking shoes, plus sandals if Q2 has `swim` |
| Q3 `varied` | footwear: every footwear item matched by Q2 | as Q2 matches |
| Q4 each option | `care:<key>` | `care:sun` → sunscreen, after-sun; `care:toiletries` → toiletry basics; 4b adds skincare, hair, shaving, make-up and eyewear items |
| Q5 bags | — | each bag picked becomes a **`Bag` row** owned by the person, with the count given (`kind` from the answer) |
| Q5 organisers | `gear:<key>` | packing cubes, compression bags, toiletry bag |
| Q6 each option | `tech:<key>` | `tech:laptop` → laptop + charger, `tech:adapter` → universal adapter, `tech:car` → car charger |

**Style filter (Q1).** Each catalogue item has `styles`, a subset of
`masc` / `fem` / `neutral`, default all three. `masc` seeds items listing
`masc`; `fem` seeds items listing `fem`; `neutral` seeds **every** item
(Generic / mixed is the union). An item's label comes from `variants[Q1]`,
else its `label`. Example: a sundress lists `fem` and `neutral`, so Menswear
doesn't seed it; anyone can still add it by hand.

**Quantities.** Each catalogue item has a `seed_qty` (default 1). Clothing
the generator counts by formula seeds enough for a week: tops 5, bottoms 3,
underwear 7, socks 7, sleepwear 2. The inventory quantity is what the person
owns, and it caps the generated quantity (§6.7).

**Re-running the questionnaire** adds items for newly matched seed tags that
aren't already in the inventory (matched by `type_key`). It never deletes,
renames or re-counts what is there, so it's safe to re-run. Removing an answer
removes nothing; the person deletes items themselves.

### 6.4 Packing preferences (the rest of the page)

| Preference | Values (stored key) | Default | Change from 4.x |
|---|---|---|---|
| Laundry on trip preference | No (`none`) · Hand wash (`hand_wash`) · Machine / laundromat (`machine`) | No | Renamed from "Laundry comfort"; same keys and effect |
| Packing style | One bag only, strict (`one_bag`) · Minimalist (`minimalist`) · Leisurely (`balanced`) · Well prepared (`prepared`) | Leisurely | `one_bag` moves here from the bag mode: a hard cap of one bag plus a personal item |
| On trips my body tends to… | I run hot (`runs_hot`) · I run cold (`runs_cold`) · No preference (`neutral`) | No preference | Renamed from "Climate comfort"; same keys |

**Removed:** style constraints ("No heels", "Always dress modestly",
"Business-casual only") and the code that applies them; cultural strictness;
the clothing-cut select (replaced by Q1); the bag mode select; the carry-on and
luggage weight fields and the engine's weight warning. Must-haves move from a
profile list to a flag on inventory items. Cultural modesty becomes the
**Religious sites** template (§6.5).

### 6.5 Templates

A template is the person's own named set of inventory items for one kind of
activity or trip, with a **type**. Templates replace the engine's archetypes as
the thing people see (D-230); archetype and modifier detection stays, but only
to **suggest** which template types fit a trip.

| Type (key) | Suggested for a trip when… | Pre-ticked inventory when you create one |
|---|---|---|
| Beach (`beach`) | archetype `beach` or modifier `beach` | items seeded by `swim`, plus sun care |
| Hiking (`hiking`) | modifier `hiking` | `outdoor` items, trail snacks, poles |
| Swimming (`swimming`) | modifier `watersport` or `beach` | swimwear, towel, waterproof pouch |
| Fine dining (`fine_dining`) | never automatically: pick it on the trip | evening outfit, dress shoes / smart flats |
| Formal events (`formal_events`) | never automatically | `formal` items |
| Cultural events (`cultural_events`) | never automatically | `smart` items |
| Business meetings (`business`) | archetype or modifier `business` | `formal` items, laptop, business cards |
| Road trip (`road_trip`) | archetype `road_trip` or modifier `road` | car charger, cooler, daypack |
| Long flight (`long_flight`) | a flight of 6 hours or more (a new signal, 4e) | headphones, power bank, sleepwear-style comfort items |
| Sightseeing (`sightseeing`) | archetypes `city_break`, `backpacking` | walking shoes, daypack, water bottle |
| Cold weather / ski (`cold_ski`) | archetype `cold_ski`, modifiers `cold_weather` or `ski` | `cold` items, ski goggles |
| Camping (`camping`) | modifier `camping` | tent, sleeping bag, headlamp |
| City break (`city_break`) | archetype `city_break` | `smart` items, walking shoes |
| Wellness / spa (`wellness`) | never automatically | swimwear, sandals |
| Festival / concert (`festival`) | never automatically | daypack, power bank, rain gear |
| Cruise (`cruise`) | never automatically | `formal` and `swim` items |
| Family / theme park (`family`) | never automatically | walking shoes, daypack, water bottle |
| Wedding (`wedding`) | never automatically | `formal` items |
| Rainy / tropical (`rainy_tropical`) | modifiers `rain_gear`, `humid` or `tropical` | rain gear, insect repellent, quick-dry items |
| Religious sites (`religious_sites`) | archetype `cultural`, modifier `temple`, or a destination whose `culture.json` entry is `modest` | `modest` items, plus the country's `add_items` (slip-on shoes, scarf) |
| Your own keyword (`custom`) | the keyword appears in the trip's title, notes or an activity name | nothing |

- **Create, rename, delete** a template; pick its type, or a keyword for
  `custom`. One template per type per person, except `custom`, which can have
  several keywords.
- Creating one **pre-ticks** the matching inventory items from the table; the
  person adjusts. Pre-ticking only proposes, and the person saves.
- **Adding an item by hand** to a template also adds it to the inventory
  (YONDER-62 item 15), so a template only ever points at inventory.
- A template line may carry its own quantity; otherwise the generator's
  formula or the item's quantity applies.
- `culture.json` stays the source for **which destinations** suggest Religious
  sites and what they add. The `modest` / `strict` filtering of revealing items
  goes with cultural strictness.

### 6.6 Data model (built in 4c)

- **`inventory_items`** (per user, own-only): `id`, `user_id`, `type_key`
  (the catalogue item it came from, nullable for hand-made items), `label`,
  `category`, `tags[]`, `quantity` (owned, default 1), `must_have` (bool),
  `brand`, `notes`, `enabled`, `created_at`, `updated_at`. Unused until 5.x
  (P14, D-128/D-129): `kind` (`consumable` | `durable`), `usage_rate` (JSON),
  `reorder_threshold`, `reorder_url`.
- **`packing_templates`** (per user, own-only): `id`, `user_id`, `type_key`,
  `name`, `keyword` (for `custom`), timestamps.
- **`packing_template_items`**: `id`, `template_id`, `inventory_item_id`,
  `quantity` (nullable). Soft links, no foreign keys (house rule 3); deleting
  an inventory item removes its template lines.
- **`packing_items.inventory_item_id`**: nullable soft link, so a line knows
  which owned item it is.
- **`bags.user_id`**: `Bag` becomes the luggage part of the person's inventory
  (P6). Existing rows stay ownerless, so only admins see them (house rule 2),
  until the owner takes the questionnaire.
- **`PackingCatalogItem`** gains `seed` (list), `styles` (list) and `seed_qty`
  (int). It stays the deployment's editable vocabulary (D-84); nobody packs
  from it directly (D-127).
- **`PackingProfile.values`** becomes
  `{"questionnaire": {"style", "clothing": [], "footwear", "care": [], "gear": [], "tech": []},
  "questionnaire_saved_at", "laundry_comfort", "packing_style", "climate_preference"}`.
  The old keys are ignored on read, not migrated: everyone takes the
  questionnaire (P4).
- **Scoping** (P15): a new own-only filter in `app/scope.py` for inventory,
  templates and bags, like the journal's (D-134). Trip sharing never exposes
  them. Admin act-for (D-211) works and is audited. Every route gets a test
  that another user can't see or change them.

### 6.7 Generating a list (built in 4e)

1. **Gate:** no `questionnaire_saved_at` → refuse (§6.8).
2. **Signals:** the existing context pass (climate bands, activity tags,
   accommodation, transport, destination culture) gives the archetype scores
   and modifiers, as today.
3. **Templates:** each of the person's templates whose type the signals
   suggest, plus any the person ticked on the trip (this replaces
   `manual_archetypes`, and is kept across re-generates the same way), adds
   **all** its items (`source = template`).
4. **Needs:** the essentials and the active modifiers' items are needs. Each
   resolves to the person's inventory by `type_key`, then by tag. Found → added
   (`source = engine`). Not found → a **"Suggested — not in your inventory"**
   line with **Add to inventory**. It's shown, never hidden (D-83), and never
   added silently (D-231).
5. **Must-haves:** every inventory item marked must-have is added.
6. **De-dupe** by `inventory_item_id`: one line per owned item, the largest
   quantity, the reasons merged.
7. **Quantities:** the laundry and packing-style formula as today, **capped
   by the inventory quantity**. Short → a warning ("7 pairs of socks needed,
   you have 5").
8. The page says **who the list is for**: "Packing list for <name>". Lists are
   always one person's (D-123).

Re-generating keeps packed, removed and hand-edited lines, as today (D-63).

### 6.8 The gate, and the apps

`POST /trips/{id}/packing/generate` without a saved questionnaire returns
**409** with
`{"detail": "Set up your packing profile first: answer the questions in Settings → Packing profile.", "code": "packing_profile_required", "url": "/trips/settings/packing-profile"}`.
`detail` **stays a string**: the iOS and Android apps decode it as one and show
it, so today's builds handle the 409 unchanged. A test pins it. The web page
shows the same sentence with a link. Plans made before 5.0 stay readable, with a
banner linking to the questionnaire.

The apps need no change in 5.0. The "Packing list for <name>" label is optional
polish. In 5.x the apps gain an "Open questionnaire" button that opens `url`,
then the questionnaire and inventory screens. In 5.0 the questionnaire and
inventory are web only.

### 6.9 Upgrading an existing install

- Migrations add the tables and columns. No packing data is converted.
- Old profile keys stay in the JSON and are ignored.
- Existing plans and their lines stay readable, with the banner.
- Each person takes the questionnaire once before they can generate again.
- The upgrade note in the release says so.

## Decisions (D-127–D-130, D-229–D-231)
- **D-127 — Inventory is per-user; no shared master list.** The per-user
  `InventoryItem` set is the master list; the shared `PackingCatalogItem` becomes
  seed data + the archetype engine's generic type vocabulary, not a user-facing
  list. Households/shared inventory deferred. (proposed 2026-08-30; **accepted
  2026-11-07** for 5.0: a seed catalogue is the vocabulary, the questionnaire
  seeds each person's inventory, §6.)
- **D-128 — Post-trip consumption is auto-estimated, user corrects exceptions.**
  Consumption pre-filled from a per-item `usage_rate` × trip length; the user
  adjusts only the exceptions. Requires seed usage-rates by category. (proposed)
- **D-129 — Reorder = flag + saved link, no in-app commerce.** Below-threshold
  items surface with a stored product link to buy manually; no purchasing inside
  Yonder. (proposed)
- **D-130 — Trip Post-Mortem is a framework; packing review is v1.** A per-trip
  post-mortem shell offered when a trip ends; packing first, with events/cost/
  reviews as future modules. (proposed)
- **D-229 — A questionnaire seeds each person's inventory; it asks for a
  clothing style, never gender.** Six closed pick lists (§6.2) map to catalogue
  `seed` tags (§6.3); Q1 is Menswear / Womenswear / Generic-mixed, mapped to the
  catalogue's `masc` / `fem` / `neutral` variants and filtered by each item's
  `styles`. Re-running it only adds. Generating a list needs it to have been
  saved once. The old style constraints, cultural strictness, clothing cut, bag
  mode and weight fields go. (accepted 2026-11-07, YONDER-62)
- **D-230 — Templates replace archetypes as what people see.** A template is the
  person's own set of inventory items with a type (§6.5). Archetype and modifier
  detection only suggests template types for a trip. Cultural dress becomes the
  Religious sites template, driven by `culture.json`. (accepted 2026-11-07)
- **D-231 — A packing list draws only from the person's inventory.** Templates
  add all their items; the engine's needs resolve to owned items, and a need
  with no owned item is shown as "Suggested — not in your inventory" with Add
  to inventory, never added silently (D-83). The owned quantity caps the
  formula. Without a saved questionnaire, generation answers 409 with a string
  `detail` and `code: packing_profile_required` (§6.8). (accepted 2026-11-07)
