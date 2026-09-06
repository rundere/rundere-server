# Yonder 4.0 — Directions capture (Google + Apple Maps; car / transit / walking)

Status: **PROPOSED (spec for discussion)**. Written against the checked-in
`extract/maps_url.py`, `extract/pipeline.py`, `rules.py`, `app/capture.py`,
`app/api_v1.py`, `app/processing.py`, and `schemas/` on `feat/4.0-companion`.

> Supersedes the earlier Apple-only draft (`SPEC-4-apple-maps-url.md`). Apple is
> now **one provider** of a multi-provider, multi-mode directions capture — not a
> standalone, driving-only, permanently-capped feature. Decisions D-87/D-88/D-91
> from that draft are revised or reversed below and called out where they change.

---

## 0. The one-paragraph pitch

Someone in the household pastes a maps directions link — Google **or** Apple —
and Yonder captures it as a `ground_transport` road-trip payload. Capture is no
longer content-sniffed: the capture surface offers an explicit **selection**
(generic page / Google Maps directions / Apple Maps directions), and within a
directions capture the human picks the **travel mode** (car / public transit /
walking). Scope per link is still **origin + destination only** — the share-URL
formats carry no multi-stop syntax — but the mode is now first-class, because a
walk or a transit leg is as much a travel plan as a drive.

---

## 1. Scope

**In:**
- **Google Maps directions** (existing `extract/maps_url.py`, D-10): `/maps/dir/…`
  path, `?api=1&origin=…&destination=…` query, and `maps.app.goo.gl` / `goo.gl/maps`
  short links (expanded via redirect, allowed Google hosts only).
- **Apple Maps directions**:
  `https://maps.apple.com/?saddr=<origin>&daddr=<destination>[&dirflg=…][&ll=…]`.
  Both `saddr` and `daddr` must be present and stable (see §4).
- **Three travel modes** on the directions capture surface: car, public transit,
  walking (§3). Coordinate stops (`saddr=37.7749,-122.4194`) parse the same as
  place names for either provider.

**Out, permanently (not deferred):**
- **`maps.apple/p/...` short links** (revised D-88). As of iOS 26 Apple's share
  sheet stopped embedding coordinates in these, with no documented replacement.
  On the explicit Apple path a pasted short link is an **inline capture error**
  ("this Apple share link carries no coordinates — paste the full directions
  link"), not a silent `needs_review`. Revisit only if Apple publishes a stable
  resolution method.
- **Multi-waypoint chains** (D-87, unchanged). Neither provider's share URL
  carries a third stop to parse. Deriving stops from a screenshot or an LLM read
  is a different feature, not an extension of this one.
- **`maps://` native-scheme links.** Capture is paste/web based; add later if it
  shows up — a one-line host check.
- **Booked, ticketed travel** (train, ferry, coach with a ticket). That is not a
  directions capture — it arrives through the ticket/booking extractors and keeps
  its own `mode` (`train`, `ferry`, …). See §3 on why transit here is `other`.

---

## 2. Capture surface & routing

### 2.1 The three-path selection

The capture surface replaces content-sniffing with an explicit choice:

1. **Capture a page (generic)** — existing web/JSON-LD/LLM path. Unchanged.
2. **Google Maps directions** — provider = Google.
3. **Apple Maps directions** — provider = Apple.

Paths 2 and 3 share one directions sub-form (§2.2). The current single paste box
(`app/review/templates/capture.html`, field `content`) and the sniffing checks in
`app/capture.py:247` and `app/api_v1.py:359` (`looks_like_maps_url`) are replaced
by path-based routing: the selected path sets the provider directly. `looks_like_*`
becomes a per-path *validator* (is this actually a Google / Apple directions URL?)
that produces an inline error on the wrong path, rather than a router.

### 2.2 Shared directions sub-form

Fields common to both directions paths (car/transit/walking):

| Field | Required | Notes |
|---|---|---|
| URL | yes | validated against the path's provider host at entry |
| Travel mode | yes | car / public transit / walking (§3) |
| Travel date | yes | a route carries no date (RULE-31) |
| Start time | optional | departure; extractor stamps it as departure datetime |
| End date | optional | multi-day drive → arrival on end_date (§5) |
| Timezone | optional | stamped onto the datetime when present (§5) |
| Trip / collection | required to commit | picker, not free text; **travellers inherit from it** (§6) |
| Round trip | optional toggle | overrides string-match inference (§7) |
| Title, notes | optional | as today |

**Travellers are NOT captured here** — they are a trip-level concept
(`TravelerIdentity` / `TripTraveler`, `app/api_v1.py`) and are inherited from the
selected trip/collection. No new `capture_context` traveller field, no extractor
plumbing to `party/role`.

### 2.3 Pipeline wiring

`extract/pipeline.py:61` currently special-cases:
```python
if channel == "maps_url" or (ingest_payload.get("url") or "").find("/maps/dir/") >= 0:
```
Give Apple its **own channel** (`apple_maps_url`) so provider is unambiguous and
ordering can't collide. Add a distinct branch producing `extractor == "apple_maps_url"`
(needed for §6 and §8), and keep the Google branch on `maps_url`. Do **not** route
an Apple link through the existing `maps_url` channel — it would hit the Google
extractor first and raise "not a Google Maps directions URL."

`app/capture.py` needs an Apple queue (or a `provider` arg on `_queue_maps`) and,
critically, must **plumb the fields the extractor already reads but the current
`_queue_maps` drops**: `end_date` and `timezone` (see §5). The new sub-form also
sends `mode`.

---

## 3. Travel mode (car / transit / walking)

Replaces the earlier "reject non-driving" posture (revised D-91). The mode is a
**capture selection**, cross-checked against the URL:

| Selection | Payload | Provenance |
|---|---|---|
| Car | `ground_transport`, `mode: "car"` | Google mode / Apple `dirflg=d` or absent |
| Public transit | `ground_transport`, `mode: "other"`, notes marks it a transit route | Apple `dirflg=r` |
| Walking / hiking | `ground_transport`, `mode: "walk"` (**new enum value**) | Apple `dirflg=w` |

- **Transit → `other`, not a ticketed mode.** The `ground_transport.mode` enum's
  `train`/`bus`/`ferry` are for *booked* travel. A transit *directions* link is a
  plan, and its share URL carries origin/destination/`dirflg` only — **no
  leg-by-leg steps**. So notes records *that it is a public-transit route A→B via
  provider*; it does not (and cannot from the URL) hold step text. Downstream can
  tell a transit plan from a drive by `mode == "other"` + the notes marker.
- **Walking/hiking → new `mode: "walk"`.** One value covers urban walk and trail
  hike; the provider and notes carry the nuance. Stays `item_type: ground_transport`
  (self-powered ground movement); no new item_type. Requires adding `walk` to the
  `mode` enum in both schema files (§8).
- **Cross-check, reject on contradiction (D-91, revised).** If the URL's mode
  signal is **present and contradicts** the selection (user picks Car, link is
  `dirflg=w`), raise `ExtractionError` → `needs_review` (or an inline capture
  error). An **absent** signal is not a conflict: Apple with no `dirflg` defaults
  to driving, which is compatible with a Car selection and with a coordinate-only
  link. Never reject on a missing signal, only on a present-and-disagreeing one.

---

## 4. Field mapping

| Param | Yonder field | Notes |
|---|---|---|
| Google `/maps/dir/A/B` or `origin`/`destination` | `departure.place` / `arrival.place` | existing `_segments` + `_parse_place` |
| Apple `saddr` | `ground_transport.departure.place` | via shared `_parse_place`, §5 |
| Apple `daddr` | `ground_transport.arrival.place` | via shared `_parse_place`, §5 |
| Apple `dirflg` / Google mode | drives the §3 cross-check | not written directly as `mode` |
| Apple `ll=<lat>,<lon>` | ignored | map-center hint, not a stop |

**Apple `saddr` present but unstable.** "Both params present" is not enough. A link
with only `daddr` implies "current location" as origin — that's whoever shared it,
not a stable start — so it is treated like Google's "fewer than 2 stops":
`ExtractionError` → `needs_review`. **Also reject a literal placeholder origin**
(`saddr=Current%20Location`), which passes the presence check but is the same
unstable origin. Never guess it.

---

## 5. Place, date & coordinate handling — reuse, don't duplicate

`extract/maps_url.py`'s `_parse_place()` (City/Region split, D-59) and
`_capture_dt()` (capture-context date stamping, RULE-31/34) are generic over *any*
directions URL. **Share them, don't copy** — they live in
`extract/directions_common.py`. In fact the whole payload assembly is shared:
each provider module only parses its own URL shape into ordered `segments` + a
mode signal, then calls `build_directions_payload(...)`, which owns date stamping,
mode resolution, the unstable-origin guard, titles, the transit notes marker,
round-trip inference and the route block. Parity is structural (D-96): neither
provider can drift because there is only one builder.

- **Dates.** Neither provider's link carries a date; `capture_context.travel_date`
  is required (RULE-31). `start time`, `end_date`, `timezone` flow from the
  sub-form. **Note the current bug:** the extractor already reads `cc.get("end_date")`
  and `cc.get("timezone")` (`maps_url.py:139-140`) but `_queue_maps` never stores
  them — so multi-day drives silently collapse to same-day today. This spec plumbs
  them through (§2.3).
- **Coordinates (accepted gap, unchanged reasoning).** Google embeds exact per-stop
  coords (`!1d<lon>!2d<lat>`) in `route.source_url`, applied at resolve by
  `_apply_maps_coords` (`app/resolving.py:323`). **Apple's `saddr`/`daddr` has no
  equivalent blob.** A named Apple stop resolves through the D-59 geocoder ladder
  like any address; a bare `saddr=37.77,-122.41` still parses via `_COORD`. The gap
  only bites Apple place-name stops with awkward formatted addresses — acceptable at
  two stops.

---

## 6. Rule reuse — generalize, don't mint new IDs

`rules.py` gates capture-context checks on the literal string `"maps_url"`
(`rules.py:361`, `rules.py:380`), reading `ext = item.extraction.extractor`
(`rules.py:358`), which is set from `result.extractor` at
`app/processing.py:59`. If the Apple extractor reports `apple_maps_url`, these
checks silently don't fire — a dateless Apple capture would sail past RULE-31.

**Generalize:**
```python
DIRECTIONS_EXTRACTORS = {"maps_url", "apple_maps_url"}
...
if ext in DIRECTIONS_EXTRACTORS:
    if not cc.get("travel_date"):
        e(("RULE-31", "directions-URL capture with no capture_context.travel_date — "
                      "a maps directions URL carries no date; it is unusable without one"))
```
Apply the same to RULE-32 and RULE-34/34b/34c. Reword the inline `e(...)` messages
that say "Google Maps directions URL" (`rules.py:363-364`, `381-382`) and the
matching `RULES.md` rows to the provider-neutral "a maps directions URL" — same
rule IDs, same registry entries. The RULE-31 *registry* line (`rules.py:119`) is
already provider-neutral. This is D-25's one-failure-mode-one-rule reasoning.

**No change:** RULE-27/27b key off `item_type == "ground_transport"` and the `route`
dict, not extractor name or mode (`rules.py:538-542`) — they hold for car, transit,
and walk alike (all `ground_transport`, all `route.is_estimate = True`).

**Verify before implementing** — confirm the literal-string checks haven't moved.

---

## 7. Route payload shape

Identical to Google's shape; provider and mode vary:

```python
"route": {"provider": "apple_maps", "source_url": url, "is_estimate": True}
```
- `route.provider` — `apple_maps` **already exists** in the enum
  (`schemas/*.json`); `google_maps` for the Google path. AllTrails-class providers
  are a future enum addition, not built here.
- `mode` — `car` / `other` (transit) / `walk` per §3.
- No `waypoints` are ever produced (empty list); RULE-26/26b/26c are inert by
  construction, not suppressed.
- `round_trip` — inferred when origin == destination, **overridable** by the
  sub-form toggle for a one-way paste that is actually a round trip.

---

## 8. Schema changes required

Both `schemas/travel-item.schema.json` and the derived
`schemas/travel-extraction.schema.json` (marked "DERIVED — do not hand-edit";
regenerate with the repo's tooling):

1. **`extraction.extractor` enum** — add `"apple_maps_url"`. It flows from
   `result.extractor` into `extraction.extractor` (`app/processing.py:59`) and is
   validated at commit; without this a committed Apple item fails schema validation
   *and* the §6 rule check never sees it. (This was the load-bearing gap in the
   Apple-only draft.)
2. **`ground_transport.mode` enum** — add `"walk"` (§3). Current values:
   `train, bus, ferry, car, car_rental, rideshare, transfer, other`.
3. **`route.provider`** — no change; `apple_maps` already present.

`app/processing.py:63` `_CONFIDENCE` should get a deliberate `apple_maps_url`
entry (matching `maps_url`) rather than defaulting to `0.5`.

---

## 9. Decisions

- **D-87** (unchanged) — Directions capture is origin/destination only,
  **permanently**. Neither provider's share URL carries waypoint syntax.
- **D-88** (revised) — `maps.apple/p/...` short links are out of scope; on the
  explicit Apple path a pasted short link is an inline capture error, not a silent
  drop. Revisit only on a stable Apple resolution method.
- **D-89** (unchanged) — `_parse_place` / `_capture_dt` are shared via
  `extract/directions_common.py`, never copied.
- **D-90** — Generalize the RULE-31/32/34/34b/34c extractor check to a
  `DIRECTIONS_EXTRACTORS` set and make the messages provider-neutral, rather than
  minting a parallel Apple rule series.
- **D-91** (revised) — Travel mode is a capture selection (car / transit / walk),
  cross-checked against the URL's mode signal. Reject only on a **present,
  contradicting** signal; an absent signal is compatible with a Car selection.
  Non-driving is supported, not rejected.
- **D-92** — Public transit is `mode: "other"` with a notes marker, distinct from
  booked `train`/`ferry`. The share URL has no leg steps to store.
- **D-93** — Walking/hiking is a single new `mode: "walk"` under
  `ground_transport`; no new item_type. Provider-extensible to AllTrails-class
  sources later via `route.provider`.
- **D-94** — Travellers inherit from the selected trip/collection; they are never
  captured per directions item.
- **D-95** — The capture surface is selection-based, not content-sniffed: three
  paths (web page / Google Maps / Apple Maps). `looks_like_*` is a per-path
  validator that rejects a URL pasted on the wrong path, and `_queue_maps` is
  fixed to plumb `end_date`, `timezone`, and `mode`. Sniffing is retained only as
  a fallback for clients that send no path (the bookmarklet, older callers).
- **D-96 (parity principle)** — Providers with a common use case are kept at
  feature parity *by construction*, not by remembering to mirror changes. Google
  and Apple share one `extract/directions_common.build_directions_payload`, so
  mode handling, the transit notes marker, the unstable-origin guard, titles,
  round-trip inference and the route block are identical for both — a new
  directions capability is added to the shared builder once and both providers
  gain it together. This is the standing rule for any future solution built on a
  shared use case: update the common path, not one provider.

---

## 10. Test cases to add to `tests/samples.json`

- Google car link (existing shapes) → unchanged behavior, `mode: "car"`.
- Apple `saddr`+`daddr` place names, no `dirflg`, Car selected → `mode: "car"`, passes.
- Apple `dirflg=w` + Walking selected → `mode: "walk"`, passes.
- Apple `dirflg=r` + Transit selected → `mode: "other"`, notes carries transit marker.
- **Contradiction:** `dirflg=w` + Car selected → `ExtractionError`, `needs_review`.
- **Absent signal:** no `dirflg` + Car selected → passes (not a conflict).
- Apple `daddr` only (no `saddr`) → `ExtractionError`, "fewer than 2 stops" family.
- Apple `saddr=Current Location` literal → `ExtractionError`, unstable origin.
- Coordinate `saddr` (`37.7749,-122.4194`) + place-name `daddr` → coord via `_COORD`,
  place via `_parse_place`.
- No `capture_context.travel_date`, either provider → RULE-31 fires (post-D-90).
- **Commit-level:** an Apple item validates against the full `travel-item` schema —
  locks in the `apple_maps_url` extractor and `walk` mode enum additions (§8), not
  just extraction-time schema.
- `maps.apple/p/...` on the Apple path → inline capture error, never a route.

---

## 11. Non-goals restated

No multi-waypoint chains. No `maps://` scheme. No Apple short-link resolution. No
leg-by-leg transit steps (the URL has none). No Google-style embedded-coordinate
extraction for Apple (the format doesn't offer it). No per-item traveller capture.
This is a two-stop, multi-mode, best-effort-geocode directions capture across two
providers — matched to what each share URL actually carries.
