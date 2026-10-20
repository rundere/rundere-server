---
name: rundere-local-phrases
description: Research how locals actually talk in each city of a Run·dere trip — register, real greetings, everyday expressions and etiquette, with pronunciation — and save it to the trip's Discover → Language section with the Run·dere MCP tools. Use when asked for local phrases, slang, greetings or etiquette for a Run·dere trip.
---

# Run·dere research kit — Local phrases & register

You are researching **how locals really speak** in each city of a trip in
**Run·dere** (the user's travel planner), and saving it to the trip's
**Discover → Language → Local phrases & register**. You need the Run·dere MCP
connector.

This is the local layer only. The national language and the vetted phrasebook
are Run·dere's own — you can't change them, and you shouldn't repeat them.

## 1. Read the trip

Call `get_trip` with the trip id.

- `places` lists the cities; each `key` (e.g. `JP:Kyoto`) is the
  `place_key` you save under.
- `discovery` includes the `language` finding for each country — read it so
  you know the national language(s) and don't duplicate the phrasebook.
- `discovery_profile.travel.nationality` hints at the traveller's own language;
  write pronunciations for an English speaker unless told otherwise.

If the user named a city, do only that one; otherwise every city in `places`.

## 2. Research each city

Use web search: language-learning sites, locals' guides, travel forums with
native speakers, dialect resources. Colloquial and regional forms are the
point — this is where Darija differs from Modern Standard Arabic, or Québécois
from Parisian French.

Collect:

- **`register_summary`** — one short paragraph: how formal or casual locals
  are, how to address people (titles, first names), what comes across as rude,
  which language to try first.
- **`greetings`** — 4–8 of how locals actually greet and say goodbye. Each:
  - `phrase` (as written locally; add a transliteration if the script isn't Latin),
  - `meaning`,
  - `pronunciation` respelled for an English speaker (`sa-LAAM a-LAY-kum`),
  - `note` — when and with whom (elders, shopkeepers, friends).
- **`expressions`** — 6–12 useful beyond greetings: thanks, no thanks (vital
  in markets), how much, the bill, sorry, cheers, a bit of friendly slang.
  Same fields.
- **`etiquette`** — 3–6 short notes (right hand, shoes, tipping talk,
  bargaining manners).

Keep it accurate over long: every phrase should be something a local would
recognise and not wince at.

## 3. Save it

```
save_phrases(trip_id, place_key="JP:Kyoto",
             register_summary="…", greetings=[…], expressions=[…],
             etiquette=[…], sources=[{title, url}, …])
```

- It lands as a **draft** under Language for the person to review; saving again
  replaces your earlier version (kept as history).
- If it's refused as **locked**, stop and tell the person — they've kept
  their own version.

## 4. Report back

Per city: the one greeting and the one "no thank you" they should learn first,
with pronunciation.
