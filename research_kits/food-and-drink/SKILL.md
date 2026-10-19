---
name: rundere-food-and-drink
description: Research where to eat and drink in each city of a Run·dere trip — a short city write-up plus restaurants, cafés, bars, pubs, markets and street food with what to order — and save it to the trip's Discover tab with the Run·dere MCP tools. Use when asked to research food, restaurants, bars or "where to eat" for a Run·dere trip.
---

# Run·dere research kit — Food & Drink

You are researching food and drink for a trip in **Run·dere** (the user's travel
planner), and saving it where they'll use it: the trip's **Discover → Food & Drink**
section, one city at a time. You need the Run·dere MCP connector.

## 1. Read the trip and the person

1. Call `get_trip` with the trip id.
   - `places` lists the trip's cities; each `key` (e.g. `JP:Kyoto`) is the
     `place_key` you save under.
   - `trip.start_date` / `trip.end_date` are the dates.
   - `discovery_profile` is **this person's** preferences, with `how_to_use`.
     Follow it strictly:
     - every place must fit their **dietary restrictions**;
     - **never** recommend an **avoided cuisine**;
     - stay at or under the **budget ceiling** (1 = $, 4 = $$$$; 0 = no limit);
     - **flag** any **allergen** risk in `details` — don't just drop the place;
     - their **drink interests** add bars, breweries, wineries or cafés to the mix;
     - **`notes`** is them in their own words — likes, dislikes, past
       favourites. Treat a dislike there like an avoided cuisine, and use the
       favourites to judge what they'd enjoy;
     - **`not_my_thing`** lists places they hid as not their thing, on this or
       earlier trips. Never suggest those again, and skip places like them.
   - If `discovery_profile.blank` is true, **ask the person first** what they
     like and want to avoid (diet, budget, favourite kinds of places), and
     suggest they fill in their Discovery profile in Run·dere so next time you
     won't need to.
   - `discovery_items` shows what's already there. Rows marked `hidden` were
     dismissed; don't bring them back. Rows with `activity_id` are already
     booked into the timeline.
2. If the user named a city, do only that one. Otherwise do every city in
   `places` (skip their home city).

## 2. Research each city

Use web search. Prefer recent, specific, local sources: respected local food
writers, city guides, reputable publications, the places' own sites. Avoid
aggregator spam, chains and anything that looks permanently closed.

Aim for **8–15 places per city**, a real mix, not all restaurants:

| `kind` | Use for |
|---|---|
| `restaurant` | Sit-down meals |
| `street_food` | Stalls, food markets' hot food, snack vendors |
| `cafe` | Coffee, pastries, breakfast |
| `bakery`, `dessert` | Bread, sweets, ice cream |
| `bar`, `pub` | Drinks; cocktails, wine bars, pubs |
| `brewery`, `distillery`, `winery` | Makers worth a visit |
| `market` | Food markets and halls |

For each place fill in what you can verify:

- `name` (required), `kind` (required)
- **`what_to_get`** — the dish or drink it's known for. This is the most useful
  field; set it whenever a source says what to order.
- `neighbourhood`, `address`
- `price_tier` 1–4 ($ cheap … $$$$ splurge)
- `details` — one or two lines: why go, when to go, booking, allergen flags
- `booking: true` if you must reserve
- `website` (the place's own site) and `source_url` (where you read about it)

## 3. Write the city summary

`summary_md`: two or three short paragraphs of markdown —

- what eating and drinking is like in this city (rhythm, hours, local
  specialties worth trying),
- your recommendation for **this** traveller given their profile,
- anything practical (tipping, cash, reservations, dress).

## 4. Save it

Call `save_city_guide` once per city:

```
save_city_guide(trip_id, topic="food", place_key="JP:Kyoto",
                summary_md="…", items=[…], sources=[{title, url}, …])
```

- It lands as a **draft** the person reviews. Re-saving the same city replaces
  your earlier rows but keeps anything they hid, edited or scheduled.
- To add a few more places later without replacing, pass `replace_items=false`.
- If it's refused as **locked**, stop and tell the person — it's theirs.

## 5. Report back

Tell the person, per city, how many places you saved and the two or three you'd
book first. Offer to put any of them on the timeline with
`add_item_to_timeline` (they'll give the day and time).
