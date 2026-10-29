# SPEC-11 — The Places Library (YONDER-314)

**Status: BUILDING (2026-10-25).** Decisions D-212–D-219. D-212 to D-216 are being built in
phase 1, the data foundation. D-217 to D-219 are proposed for later phases. This spec revises
the saved places of SPEC-9 §6b (D-166 to D-170).

## Intent

Saved places began as an inbox: share a Maps link, file it onto one trip, and it was gone.
People keep places for years, though: "I heard of this, I should go", or "I must go, and here
is what I'll do there". Google, Foursquare and Yelp all keep such places as lists. Run·dere's
version is a **library**. A place is kept independent of any trip. It can be filed onto any
number of trips, kept in lists, and it feeds the trip timeline and Discover.

The data is small: text rows, maybe a few thousand per person. The hard part is showing it
without overwhelming anyone. The library is never shown as one long table. People come in
through a **lens** that matches the question they arrive with, and each lens shows a small,
relevant set:

| Lens | The question | Shape |
|---|---|---|
| Unsorted | What did I just save, and what needs fixing? | New, unlisted or pinless places, sorted to zero (not "Inbox", which is the captures queue, D-200) |
| Atlas | What have I got in Japan? | Continent → country → city boards with counts and kinds |
| Lists | My ramen list; Want to go | The person's own boards (Google lists come over intact) |
| Map | What's around here? | A clustered map that follows the visible area (optional) |
| Search | That bar with the tiki thing | One box over name, address, city, notes and to-do |

The library also shows up in two places without being asked. The trip page offers "From your
library: N places near …", and Discover lists the person's own saved places first.

## Model

- **SavedPlace** (`saved_places`, SPEC-9 D-166): the row gains what makes it a library entry.
  - *What to do there:* `todo`. `notes` keeps the why, or who recommended it.
  - *How much:* `priority`, `someday` or `must`.
  - *Been there:* `visited_on`.
  - *Where, in words:* `locality`, `region`, `country_code` and `locality_source`.
  - *Where, for lookups:* `geohash`.
  - *For search:* `search_text`.
- **SavedPlaceFiling** (`saved_place_filings`): one row for each time a place went onto a trip,
  holding `place_id`, `trip_id` and `activity_id`. All three are soft links (house rule 3).
  Deleting the activity or the trip clears the row.
- **PlaceList** (`place_lists`): a person's own list, holding `name`, `emoji`, `description`,
  `position` and `source` (`google:<list name>` for an imported one).
- **PlaceListMember** (`place_list_members`): a place in a list, many-to-many.

Everything is own-only, with no admin view (D-166).

## Decisions

- **D-212 — Filing doesn't consume a place (amends D-169).** A saved place is a library entry.
  - *Many trips.* Filing it onto a trip makes the timeline activity and records a filing row.
    The place stays in the library, and it can be filed onto other trips later.
  - *Same trip twice.* Filing it onto a trip it is already on is a `409`. A unique index on
    `(place_id, trip_id)` backs this, so two filings at once make one activity.
  - *Unfiling.* Deleting the activity, or the trip, clears the filing row, so the place can go
    on that trip again.
  - *Status.* `status` is `saved` or `dismissed`, and `filed` is no longer used. The migration
    turns existing `filed` rows back into `saved`. Where it can find the activity a row became
    (same owner's trip, same Maps link and name), it also records the filing.
  - *Outcomes.* A save no longer reports `already_filed` for a new filing. The value stays
    valid in the API for older clients.
- **D-213 — Lists are the person's own, and many-to-many.**
  - A place can be in any number of lists. Deleting a list never deletes its places.
  - Lists are own-only, like the journal (D-134). Sharing a list is left for a later decision.
  - Order is kept by `position`, for lists and for places within a list.
- **D-214 — What, where and what to do are columns, not free text.**
  - *What* is the name and the kind (`category`, `activity_type`).
  - *Where* is the address, the pin, and the locality for grouping.
  - *What to do* is `todo`.
  - The library is searched, grouped and filtered on these, so they are columns. A future
    view never has to parse notes.
  - *The type is the person's to set.* A Google link carries no category, so every such
    place used to arrive as a Tour (owner, 2026-10-26). The place panel has a type picker,
    limited to the types that are places (tour, restaurant, lodging, event, note). When no
    category arrives, the name gives a first guess ("Café …", "… Inn"). A category always
    beats the name, and the guess applies only to a new save, so a type the person chose is
    never replaced.
- **D-215 — Locality is worked out once and kept as Run·dere's own data (D-146).** The city,
  region and country of a place come from the first source that has them:
  1. what the client sent (MapKit on iOS knows them);
  2. a Photon reverse lookup of the pin;
  3. offline, the city and country of the nearest bundled airport within 75 km, or the
     country alone within 300 km (`resolve/airports.py`).

  `locality_source` records which one it was (`client`, `photon`, `airport`). A later run with
  Photon available upgrades an `airport` guess and leaves the rest alone.
  *The address can place the pin (owner, 2026-10-26; amended 2026-10-29, YONDER-339).*
  - *Saving.* The address, city and country are filled in together, so they save with a
    **Save address** button (or Enter), not on change like the rest of the panel. The
    result shows under that button, and a pinless place keeps showing "Not on the map
    yet" with **Find on map** to try again.
  - *When it's looked up.* On that Save, on Find on map, and when a place arrives with an
    address and no pin. A Google share link (`maps?q=Name, street, city …&ftid=…`) carries
    no coordinates, so this is how every Google-shared place gets its pin. The name is the
    first part of `q=` and the address is the rest.
  - *What counts as a match.* The query is the name, street, city and the country spelled
    out; the postcode is left out, because measured against a real Photon it moved the top
    hit to another building. With a house number, a hit must have that number on that
    street, since a chain's other branch has the same name. With no street, a hit must
    bear the place's own name. A street, a town or a city centre is never a pin, since a
    wrong pin is worse than none.
  - *An existing pin.* It stays unless the person presses "Move the pin to this address",
    because a Maps link's own pin is usually exact.
  - *Without Photon* the address still saves, and the panel says there's no lookup.
  - `places-backfill` re-reads older `q=` names and pins pinless places the same way, with
    `--dry-run` to preview.

  The continent comes from the country, through a small table in the code with no
  dependency. So the Atlas works on a server with no Photon at all.
- **D-216 — Search without a search engine.**
  - `search_text` is the name, address, locality, country, notes and to-do, folded for case
    and accents the way `discover.items.name_key` folds names.
  - A search is a per-owner `LIKE` on it, ranked in Python: name prefix, then name, then the
    rest.
  - At a few thousand rows per person this is instant, and it behaves the same on SQLite and
    Postgres. Full-text search (FTS5, tsvector) waits until a real library is slow.
  - The pin is also kept as a `geohash`. Map clusters at low zoom are a `GROUP BY` on a geohash
    prefix, and nearby lookups are a bounding box on `lat`/`lon`. Neither needs PostGIS.
- **D-217 — Google Takeout is imported by the server (proposed; amends D-170).**
  - The import reads `Saved/*.csv` (one file per Google list) and `Saved Places.json`
    (starred places, GeoJSON). The file is parsed in memory and never stored (D-153).
  - Each CSV becomes a list with `source = google:<name>`.
  - A CSV row's link usually has no coordinates. Such places come in without a pin and wait in
    the Unsorted view's "Needs a pin" tray, with a Photon name match offered as a suggestion. Nothing
    is placed automatically: a wrong pin is worse than none.
  - Repeating the import is safe (D-169 identity), and `dry_run` previews it.
  - A shared Google list link is not supported: the page is drawn in the browser and carries
    no data. The UI points to Takeout instead.
- **D-218 — The map is optional (proposed; D-146).**
  - Leaflet and markercluster are vendored into the static folder, like htmx. No CDN.
  - Tiles go through the server, from the same `map_tile_url` and tile cache the PDF maps use.
    The browser never talks to a tile server, and tiles are cached.
  - With no `map_tile_url` the Map lens isn't shown, and the other lenses work as before.
  - At low zoom the server sends geohash clusters, never thousands of pins.
- **D-219 — The library is Discover's first source (proposed).**
  - Saved places near a trip's destinations appear in Discover as the person's own items,
    marked "You saved this". They are sorted into Discover's topics through the topic and kind
    that `extract/place_url.category_for` already gives each category.
  - A Discover item can be saved to the library.
  - The MCP `get_trip` reports the saved places nearby, and the research kits start from them.

## Phases

0. Shared Maps links that never reached Saved Places (a fix; SPEC-9 §9 note).
1. **Data foundation:** the model, migration, services and filing rewrite (D-212 to D-216)
   (built, 4.12.11).
2. The library web page: Search, Unsorted, Atlas, Lists and the place panel, with the type
   picker and Atlas map thumbnails (built).
3. The Map lens (D-218).
4. Takeout import (D-217).
5. Trip and Discover integration (D-219).
6. The mobile API and MCP surface: filters, lists, Atlas, clusters, import, and
   `updated_since` sync for the app's offline cache.

## Out of scope

- Sharing lists with others.
- Photos of places (D-153).
- Server-side geofencing or location tracking. "Near me" runs on the phone from its synced
  copy.
- Reading a shared Google list link.
- PostGIS.
