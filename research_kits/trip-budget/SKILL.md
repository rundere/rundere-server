---
name: rundere-trip-budget
description: Plan what a Run·dere trip will cost — lodging, transport, food, activities, fees — for its travellers, dates and cities, priced from real sources and saved as the trip's planned costs (Costs → Plan) with the Run·dere MCP tools. Use when asked to budget, estimate or plan the cost of a Run·dere trip.
---

# Run·dere research kit — Trip budget

You are planning the **budget** for a trip in **Run·dere** (the user's travel
planner), and saving it to the trip's **Costs → Plan**. You need the Run·dere
MCP connector.

Planned costs are only a budget. They never count towards who owes whom: that
comes from the actual expenses the travellers log on the trip. So aim for a
realistic plan the person can compare against, not a quote.

## 1. Read the trip and the people

Call `get_trip` with the trip id, then `list_expenses`.

- `trip.start_date` / `trip.end_date` — the number of days. If either is
  missing, ask for the dates before pricing anything per day.
- `travelers` — who's on the trip, by `id`. **Every cost you save is tagged
  with the traveller(s) it's for** (`traveler_ids`), and it counts for them only.
  Don't assume everyone shares everything: use each activity's `traveler_ids`
  (empty = everyone on it) and ask if you can't tell who a cost is for. Never
  extrapolate a cost to people it isn't for — if only one person asked for a
  plan, plan for that person.
- `places` — the cities (`JP:Tokyo`). Price each city on its own; costs differ.
- `activities` — what's **already booked** (flights, hotels, cars, tours). Use a
  booking's price if its `values` show one; otherwise price it, pass its
  `activity_id` so the plan links to it, and tag the same travellers it's for.
- `discovery_profile` — budget level, dietary needs, interests, mobility and
  their own notes. **If it's blank, ask** what kind of trip this is (budget,
  mid-range, luxury) before you start.
- `discovery` — a `currency` finding has the rates Run·dere will convert with.
- `list_expenses` → `plan.items` — anything with `status: "kept"` is the
  person's own number. Don't re-price it; mention it in your summary instead.
  You only see costs the user is on, so other travellers' private budgets are
  not yours to fill in.

## 2. Price it

Use current, specific sources: the hotel or operator's own site, booking
platforms' typical nightly prices for these dates, the local transit
authority's fares, official attraction prices, and recent cost-of-living or
restaurant-price sources for meals. Note the date you checked.

Cover what applies to this trip:

- **Lodging** — per night for the room (basis `day`), or the booked total.
- **Transport** — flights and long-distance trains not yet booked (`person`),
  local transit passes (`person` or `person_day`), taxis, fuel, tolls, parking.
- **Food** — breakfast, lunch, dinner, coffee and snacks at their budget level
  (`person_day`), plus any special meals they've planned.
- **Activities** — entry tickets, tours and events, per person.
- **Fees** — visas, eTAs, travel insurance, tourist taxes, SIM/eSIM, tips if
  customary.
- **Shopping / other** — only if they've asked for it.

Price in the **local currency** and let Run·dere convert. Use the middle of the
range as `amount` and put the range in `notes` ("¥1,000–1,800; mid-range
ramen or set lunch").

## 3. Save it

One call per trip (or per city), every item a planned cost:

```
save_budget(trip_id, items=[
  {title: "Tokyo hotel (3 nights)", category: "lodging", amount: "24000",
   currency: "JPY", basis: "day", traveler_ids: [12, 14],
   start_date: "2027-04-02", end_date: "2027-04-04", place_key: "JP:Tokyo",
   notes: "¥18,000–30,000 per night for a double, mid-range, Shinjuku",
   sources: [{title, url}]},
  {title: "Lunches", category: "food", amount: "1400", currency: "JPY",
   basis: "person_day", traveler_ids: [12], notes: "…"},
  …
])
```

- `traveler_ids` (required): who it's for. A shared room is one item tagged
  with both people; a per-person ticket for only one of them is tagged with
  just that one.
- `basis`: `trip` (once, for the tagged people together), `person` (each tagged
  traveller), `day` (each day, shared) or `person_day` (each tagged traveller
  each day). Run·dere multiplies it by the **tagged** travellers only, over
  `start_date`–`end_date` when someone is there for part of the trip (else the
  trip's dates).
- Everything lands as a **draft** the person reviews and keeps. Saving again
  with `replace: true` replaces your earlier drafts in those categories; it
  never touches what they've kept.
- Keep it to the lines that matter (roughly 8–25 items), not every coffee.

## 4. Report back

The planned total in the trip's currency (from the `plan` the tool returns),
the split by category and by traveller (`by_traveler`), the two or three biggest costs, and where the estimate is
least certain. Remind them they can keep, edit or drop each line in Costs →
Plan, and that actual spending goes in as expenses, which is what settles up.
