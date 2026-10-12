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

- **Lodging** — the room for the nights they're there, or the booked total.
- **Transport** — flights and long-distance trains not yet booked, local
  transit passes, taxis, fuel, tolls, parking.
- **Food** — breakfast, lunch, dinner, coffee and snacks at their budget level,
  plus any special meals they've planned.
- **Activities** — entry tickets, tours and events.
- **Fees** — visas, eTAs, travel insurance, tourist taxes, SIM/eSIM, tips if
  customary.
- **Shopping / other** — only if they've asked for it.

Price in the **local currency** and let Run·dere convert. Use the middle of the
range and put the range and your arithmetic in `notes` ("3 nights × ¥18,000–30,000;
mid-range double, Shinjuku").

**Every `amount` is one total, and Run·dere never multiplies it.** A room for
two people is one cost (the room's price), tagged with both. Three days of
lunches for one person is the three-day total, tagged with that person. If two
people each need their own ticket, either save one item with both tagged and
the combined price, or one item per person with their own price.

## 3. Save it

One call per trip (or per city), every item a planned cost:

```
save_budget(trip_id, items=[
  {title: "Tokyo hotel (3 nights)", category: "lodging", amount: "72000",
   currency: "JPY", traveler_ids: [12, 14],
   start_date: "2027-04-02", end_date: "2027-04-04", place_key: "JP:Tokyo",
   notes: "3 nights × ~¥24,000 (¥18,000–30,000) for a double, mid-range, Shinjuku",
   sources: [{title, url}]},
  {title: "Lunches in Tokyo", category: "food", amount: "4200", currency: "JPY",
   traveler_ids: [12], notes: "3 days × ~¥1,400 …"},
  …
])
```

- `traveler_ids` (required): who it's for. A shared room is one item tagged
  with both people; a ticket for only one of them is tagged with just that one.
- `amount`: the single total for them — see above. In the per-traveller view
  Run·dere splits it evenly between the people tagged.
- `start_date`–`end_date`: when it applies, if not the whole trip (for the
  record; it doesn't change the amount).
- Everything lands as a **draft** the person reviews and keeps. Saving again
  with `replace: true` replaces your earlier drafts in those categories; it
  never touches what they've kept.
- Keep it to the lines that matter (roughly 8–25 items), not every coffee.

## 4. Report back

The planned total in the trip's currency (from the `plan` the tool returns),
the split by category and by traveller (`by_traveler`), the two or three biggest costs, and where the estimate is
least certain. Remind them they can keep, edit or drop each line in Costs →
Plan, and that actual spending goes in as expenses, which is what settles up.
