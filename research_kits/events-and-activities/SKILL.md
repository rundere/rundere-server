---
name: rundere-events-and-activities
description: Research what's on during a Run·dere trip and things to do in each city — dated events and festivals, heads-ups like strikes or holiday closures, and anytime activities — and save them to the trip's Discover tab with the Run·dere MCP tools. Use when asked about events, festivals, things to do or activities for a Run·dere trip.
---

# Run·dere research kit — Events & Activities

You are researching events and activities for a trip in **Run·dere** (the
user's travel planner), and saving them to the trip's **Discover → Events &
Activities** section, one city at a time. You need the Run·dere MCP connector.

## 1. Read the trip and the person

1. Call `get_trip` with the trip id.
   - `places` lists the cities; each `key` (e.g. `MA:Harar`) is the
     `place_key` you save under.
   - `trip.start_date` / `trip.end_date` are the **date window**. Only list
     events that happen inside it.
   - `activities` is the timeline (flights, stays) — use it to see which days
     they're in which city, and match events to those days.
   - `discovery_profile` is **this person's** preferences, with `how_to_use`:
     their **activity interests** add to the mix (museums, live music,
     outdoor…); **mobility** rules out anything they can't do.
   - `discovery_items` shows what's already there. Don't bring back `hidden`
     rows; rows with `activity_id` are already on the timeline.
2. If the user named a city, do only that one. Otherwise every city in `places`.

## 2. Research each city

Use web search: official city and tourism sites, venue calendars, festival
sites, reputable local listings. Always check the **dates are this year's**.

Three kinds of row:

- **Events** — `kind: "event"`, happening on specific dates: festivals,
  concerts, exhibitions, sports, markets on a given day. Set `starts_on`
  (`YYYY-MM-DD`, or `YYYY-MM-DDTHH:MM` if the time is known) and `ends_on`
  for multi-day events; `venue` and `address`.
- **Heads-ups** — also `kind: "event"` with `heads_up: true`: public or
  religious holidays that close things, strikes, marathons that shut roads,
  big crowds. Put the effect in `details` ("Most shops closed", "Trains
  limited").
- **Activities** — `kind: "activity"`, doable any day: tours, hikes, hammams,
  classes, viewpoints, day trips. Set `address`, `duration` ("3 h", "full
  day"), `booking: true` if it must be booked.

For every row: `name`, `details` (one or two lines — what it is, why go,
tips), and a `source_url`. Put free wording that isn't a clean date
("Fridays in October") in `when_text` instead of `starts_on`.

Aim for the city's marquee events in the window (never omit a famous one),
3–8 activities that fit the person's interests, and every heads-up that
affects their plans.

## 3. Write the city summary

`summary_md`: two or three short paragraphs — what's on while they're there,
what you'd prioritise and on which days, and anything to plan around.

## 4. Save it

```
save_city_guide(trip_id, topic="events", place_key="MA:Harar",
                summary_md="…", items=[…], sources=[{title, url}, …])
```

- It lands as a **draft** for review; re-saving replaces your earlier rows but
  keeps what the person hid, edited or scheduled. `replace_items=false` only
  adds.
- If it's refused as **locked**, stop and tell the person.

## 5. Report back

Per city: the events on their dates, the heads-ups, and the activities you'd
pick. Offer `add_item_to_timeline` for any of them — for an event, its date is
filled in; ask for the time.
