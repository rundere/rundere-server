---
name: rundere-safety-and-transport
description: Research how to get around and stay safe in each city of a Run·dere trip — transit, the airport run, ride-hailing and apps that actually work, current advisories and common scams — and save it to the trip's Discover → Safety & Transport with the Run·dere MCP tools. Use when asked about safety, scams, getting around, transit or airport transfers for a Run·dere trip.
---

# Run·dere research kit — Safety & Transport

You are researching **getting around and staying safe** for a trip in
**Run·dere** (the user's travel planner), and saving it to the trip's
**Discover → Safety & Transport**, one city at a time. You need the Run·dere MCP
connector.

## 1. Read the trip and the person

Call `get_trip` with the trip id.

- `places` lists the cities; each `key` (e.g. `JP:Kyoto`) is the
  `place_key` you save under.
- `trip.start_date` / `trip.end_date` — look for anything happening then
  (strikes, closures, big events that change how the city moves).
- `activities` is the timeline — how they arrive (flight, train) tells you
  which airport or station matters.
- `discovery_profile.practical.mobility` — if it isn't "No limits", say what
  is and isn't accessible (stairs in the metro, step-free routes, taxis).
- `discovery_profile.notes` — anything they've said about how they like to
  travel ("happy to walk", "hate buses").

If the user named a city, do only that one; otherwise every city in `places`.

## 2. Research each city

Use web search. Prefer official sources — the city's transit operator, the
airport's own site, government travel advisories (several countries'), and
recent, reputable travel reporting. Check everything is current this year.

**Transport** (the part people use most — be specific):

- `summary` — how to get around in a short paragraph: walkable or not, what
  locals use, what to avoid.
- `public_transit` — lines, tickets and passes, how to pay (contactless?), hours.
- `from_airport` — every sensible option with rough time and price.
- `ride_hailing` — the apps that actually work there (not the ones that don't).
- `apps` — others worth installing (transit, taxis, maps, tickets).
- `tips` — short, practical notes (fixed-price taxis, night buses, strikes).

**Safety** (factual, calm, never alarmist):

- `summary` — a short, balanced paragraph: overall feel, areas to be careful
  in and when, anything specific to their dates.
- `advisories` — concrete points, **each with its own `source_url`**. Use at
  least two different sites; with only one, Run·dere shows a "verify at an
  official source" banner.
- `scams` — the common ones there, one line each, with what to do.

## 3. Save it

```
save_safety(trip_id, place_key="JP:Kyoto",
            safety={summary, advisories: [{text, source_url}], scams: [...]},
            transport={summary, public_transit, from_airport,
                       ride_hailing: [...], apps: [...], tips: [...]},
            sources=[{title, url}, ...])
```

- It lands as a **draft** they review, shown as headed subsections; saving
  again replaces your earlier version (kept as history).
- If it's refused as **locked**, stop and tell the person.

## 4. Report back

Per city: the best way in from the airport, the one app to install, and the
one scam to watch for.
