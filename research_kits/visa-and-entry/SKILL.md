---
name: rundere-visa-and-entry
description: Research visa and entry requirements for each country of a Run·dere trip, for the traveller's own passport — visa or eTA, passport validity, forms, fees, health and customs rules — with official sources, and save it to the trip's Discover → Practical → Visa with the Run·dere MCP tools. Use when asked about visas, entry rules, ETAs or passport requirements for a Run·dere trip.
---

# Run·dere research kit — Visa & entry

You are researching **entry requirements** for a trip in **Run·dere** (the
user's travel planner), and saving them to the trip's **Discover → Practical →
Visa**, one country at a time. You need the Run·dere MCP connector.

This is high-stakes: a wrong answer can mean a refused boarding. Run·dere
doesn't research visas itself, so this is the only research here. Be exact,
cite official sources, and say clearly what you couldn't confirm.

## 1. Read the trip and the person

Call `get_trip` with the trip id.

- `places` — the countries are the part of each `key` before the colon
  (`MA:Harar` → `MA`). Save one finding per **country**, with
  `place_key="MA"`.
- `trip.start_date` / `trip.end_date` — the length of stay per country
  (use `activities` to split a multi-country trip).
- `discovery_profile.travel.nationality` — the passport to check for.
  **If it's empty, ask the person which passport(s) they travel on before you
  research.** If `travelers` lists others, say your answer covers only this
  person's passport and offer to check the others'.
- `discovery` — if a `visa` finding for that country is **locked**, the person
  has confirmed their own version; don't try to replace it.

## 2. Research each country

Use **official sources first**: the destination government's immigration or
foreign-affairs site, its embassy in the traveller's country, and the
traveller's own government travel advice. Use airline or IATA (Timatic-based)
checkers only to cross-check. Note the date each rule was last updated.

Cover, for this passport and this length of stay:

- **Visa**: none / visa on arrival / e-visa / eTA / visa in advance — and the
  maximum stay.
- **How to apply**: the official link (warn about look-alike paid sites), fee,
  processing time, and when to apply by for these dates.
- **Passport**: validity required beyond the stay, blank pages.
- **On arrival**: arrival forms or digital cards, onward/return ticket, proof
  of funds or accommodation.
- **Transit**: anything needed for layovers on the way (from `activities`).
- **Health**: required vaccinations or certificates only if actually required.
- **Customs**: anything notable (cash limits, banned items, drones, medicines).

## 3. Save it

```
save_research(trip_id, topic="visa", place_key="MA",
              title="Morocco — entry for <nationality> passport holders",
              body_md="…", sources=[{title, url}, ...])
```

`body_md` layout:

```
**Visa:** Not required for stays up to 90 days.
**Passport:** Valid for the whole stay; …
**Apply by:** — (or the date)

### On arrival
- …

### Also check
- … (anything you couldn't confirm — say so plainly)

_Checked <today's date> against <official site>._
```

- It lands as a **draft** they review; once they approve or edit it, it locks
  and agents can't overwrite it.
- If it's refused as **locked**, stop and tell the person.

## 4. Report back

Per country: the one-line answer (visa or not, max stay), anything they must
do **before** travelling with its deadline, and what you couldn't confirm.
