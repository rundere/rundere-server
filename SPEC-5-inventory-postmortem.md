# SPEC (planning) — Packing Inventory & Trip Post-Mortem

**Status: PLANNING** — a future major pillar, *after* the 4.0 mobile release.
Decisions D-127+ are **proposed** (captured 2026-08-30). Not scheduled/built.

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

## Decisions (proposed, D-127–D-130)
- **D-127 — Inventory is per-user; no shared master list.** The per-user
  `InventoryItem` set is the master list; the shared `PackingCatalogItem` becomes
  seed data + the archetype engine's generic type vocabulary, not a user-facing
  list. Households/shared inventory deferred. (proposed, 2026-08-30)
- **D-128 — Post-trip consumption is auto-estimated, user corrects exceptions.**
  Consumption pre-filled from a per-item `usage_rate` × trip length; the user
  adjusts only the exceptions. Requires seed usage-rates by category. (proposed)
- **D-129 — Reorder = flag + saved link, no in-app commerce.** Below-threshold
  items surface with a stored product link to buy manually; no purchasing inside
  Yonder. (proposed)
- **D-130 — Trip Post-Mortem is a framework; packing review is v1.** A per-trip
  post-mortem shell offered when a trip ends; packing first, with events/cost/
  reviews as future modules. (proposed)
