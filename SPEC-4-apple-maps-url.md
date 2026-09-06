# Yonder 4.0 — Apple Maps directions extractor (origin/destination only)

Status: **PROPOSED (spec for discussion)**. Written against the checked-in
`extract/maps_url.py`, `extract/pipeline.py`, and `rules.py` on `feat/4.0-companion`.

---

## 0. The one-paragraph pitch

A narrower sibling of the Google Maps extractor (D-10, `extract/maps_url.py`):
someone in the household pastes an **Apple Maps** share link instead of a Google
one, and it captures as a `ground_transport` road-trip payload the same way.
Scope is deliberately capped at **origin + destination, driving directions
only** — that's not a phase-1 cut toward future parity, it's permanent. Apple's
share-link format has no multi-stop syntax to parse, so there's nothing to build
toward.

---

## 1. Scope

**In:**
- `https://maps.apple.com/?saddr=<origin>&daddr=<destination>[&dirflg=d]`
- Coordinate stops (`saddr=37.7749,-122.4194`) same as place names.
- Both `saddr` and `daddr` must be present. A link with only `daddr` implies
  "current location" as the start — that's whoever shared it, not a stable
  origin — so it's treated the same as Google's "fewer than 2 stops" case:
  `ExtractionError` → `needs_review`, never guessed.

**Out, permanently (not deferred):**
- **`maps.apple/p/...` short links.** As of iOS 26, Apple's own share sheet
  stopped putting coordinates in these — it's an open, unresolved thread on
  Apple's developer forums as of this writing, with no documented replacement
  API. Following an undocumented redirect chain Apple could reshape again is
  exactly the kind of integration this repo avoids building on. Revisit only if
  Apple publishes a stable resolution method.
- **Multi-waypoint chains.** Apple's URL scheme has no `/dir/A/B/C` path or
  `waypoints=` query param — there is no data in the link to parse into a third
  stop. A future "Apple multi-stop" capability would have to derive stops from
  something other than the URL (e.g. an LLM read of a screenshot) — a different
  feature, not an extension of this one.
- **`maps://` native scheme links.** Capture here is paste/web based; add this
  later if it actually shows up, it's a one-line host check.
- **Non-driving directions** (`dirflg=w` walking, `dirflg=r` transit). See §3 —
  rejected, not coerced.

---

## 2. URL shape & routing

Single accepted shape:

```
https://maps.apple.com/?saddr=<origin>&daddr=<destination>[&dirflg=d][&ll=<lat>,<lon>]
```

`looks_like_apple_maps_url(url)`:
- host is `maps.apple.com` (reject `maps.apple` short-link host outright — let
  it fall through to the generic web extractor rather than silently failing
  here)
- query has **both** `saddr` and `daddr`

This mirrors `looks_like_maps_url()`'s job in the Google extractor: let the
capture page route a pasted link to the right extractor without an n8n form.

**Pipeline wiring** — `extract/pipeline.py` currently special-cases:
```python
if channel == "maps_url" or (ingest_payload.get("url") or "").find("/maps/dir/") >= 0:
```
Add an `apple_maps_url.looks_like_apple_maps_url(url)` branch alongside it,
producing a distinct `extractor` trace value (`"apple_maps_url"`) — needed for
§6 below.

---

## 3. Field mapping

| Apple param | Yonder field | Notes |
|---|---|---|
| `saddr` | `ground_transport.departure.place` | via shared `_parse_place()`, §4 |
| `daddr` | `ground_transport.arrival.place` | via shared `_parse_place()`, §4 |
| `dirflg=d` or absent | `mode: "car"` | the only supported value |
| `dirflg=w` / `dirflg=r` | **reject** | see decision D-91 below |
| `ll=<lat>,<lon>` | ignored | that's a map-center hint, not a stop |

**Decision — reject, don't coerce non-driving directions.** Apple's `dirflg=w`
(walking) or `=r` (transit) means the link isn't a road-trip capture at all.
Silently treating a walking-directions link as `mode: "car"` would write a
wrong payload with no signal anything was off. Raise `ExtractionError` instead
and let it land in `needs_review` — same posture as every other "this isn't
what I think it is" case in the extractor chain. (`dirflg=c` for cycling
appears in some third-party writeups but isn't in Apple's own documented
parameter list — treat any unrecognized `dirflg` the same as `w`/`r`: reject,
don't guess.)

---

## 4. Place & date handling — reuse, don't duplicate

`extract/maps_url.py`'s `_parse_place()` (City/Region splitting, D-59) and
`_capture_dt()` (capture-context date stamping, RULE-31/34) are generic over
*any* directions URL — nothing in either function is Google-specific. **Import
them into the new module rather than copying them**, so the two providers
can't drift on shared logic:

```python
from extract.maps_url import _parse_place, _capture_dt
```

If that import shape feels wrong (both are private/underscore names), promote
them to a small shared `extract/directions_common.py` and have both `maps_url.py`
and the new module import from there instead. Either way: one implementation,
two callers.

Apple links carry no date, same as Google's — `capture_context.travel_date`
is required, same `ExtractionError` message pattern as the existing RULE-31.

---

## 5. Coordinate resolution — a real capability gap, accepted

Google's extractor pulls exact per-stop coordinates out of the `!1d<lon>!2d<lat>`
blob Google embeds in `route.source_url`, applied later at resolve
(`_apply_maps_coords`) — because Google's own formatted addresses
("100 … Pkwy, … ON Z9Z 9Z9") often don't geocode. **Apple's `saddr`/`daddr`
query-param shape has no equivalent embedded blob to extract.** A named stop
resolves exactly the way a plain address does today — through the D-59 geocoder
tier ladder, no shortcut.

This is a real quality gap versus the Google path, not an oversight. It's
accepted here because: (a) scope is two stops, not a multi-leg route where
geocode misses compound, and (b) a bare `saddr=37.77,-122.41` coordinate still
parses directly via the same `_COORD` regex — the gap only bites on place-name
stops with awkward formatted addresses.

---

## 6. Rule reuse — not new rule IDs

`rules.py`'s capture-context checks gate on a **literal string**:

```python
if ext == "maps_url":
    if not cc.get("travel_date"):
        e(("RULE-31", "maps_url capture with no capture_context.travel_date — a Google "
                      "Maps directions URL carries no date; it is unusable without one"))
    ...
if ext == "maps_url" and o not in ("capture_context", "collection_default"):
    e(("RULE-34", f"...maps_url date origin={o!r}..."))
```

Same for RULE-32/34b/34c. If the new extractor reports `ext == "apple_maps_url"`,
these checks silently **don't fire** for it — a dateless Apple capture would
sail past RULE-31 uncaught. That's not a new failure mode to design around,
it's a bug in waiting if this ships without touching `rules.py`.

**Recommendation: generalize, don't duplicate.**

```python
DIRECTIONS_EXTRACTORS = {"maps_url", "apple_maps_url"}
...
if ext in DIRECTIONS_EXTRACTORS:
    if not cc.get("travel_date"):
        e(("RULE-31", "directions-URL capture with no capture_context.travel_date — "
                      "a maps directions URL carries no date; it is unusable without one"))
```

Reword the three messages that currently say "Google Maps directions URL"
specifically (RULE-31, RULE-34) to the provider-neutral "a maps directions URL"
— same rule ID, same registry entry, same `RULES.md` row, updated wording.
This is the same reasoning as D-25's single-toggle-per-rule design: one failure
mode, one rule, regardless of which provider triggered it.

**No change needed:** RULE-27/27b (`route.is_estimate`, `distance_km`) already
key off `item_type == "ground_transport"` and `mode`, not extractor name — fully
provider-agnostic as written.

**Verify before implementing** — this spec is written from the checked-in copy
of `rules.py`; confirm the literal string check hasn't moved before wiring this in.

---

## 7. Route payload shape

Identical to Google's output shape, provider field changed:

```python
"route": {"provider": "apple_maps", "source_url": url, "is_estimate": True}
```

No `waypoints` entries are ever produced (empty list) — RULE-26/26b/26c
(waypoint ordering) are inert for this extractor by construction, not
suppressed.

---

## 8. Decisions

- **D-87** — The Apple Maps extractor ships origin/destination only,
  **permanently**, not as a phase-1 step toward waypoint parity. Apple's URL
  scheme has no waypoint syntax to parse; there is no "later" version of this
  that adds stops from the same input.
- **D-88** — `maps.apple/p/...` short-link expansion is out of scope
  indefinitely. iOS 26 removed embedded coordinates from the share payload with
  no documented replacement; revisit only if Apple publishes a stable
  resolution method.
- **D-89** — Place-parsing and capture-date logic (`_parse_place`,
  `_capture_dt`) are shared with `extract/maps_url.py` via import, never
  copied, so the two providers can't drift on identical logic.
- **D-90 (proposed)** — Generalize RULE-31/32/34/34b/34c's extractor check from
  the literal `"maps_url"` string to a `DIRECTIONS_EXTRACTORS` set, and reword
  the Google-specific messages to be provider-neutral, rather than minting a
  parallel RULE-36+ series for Apple.
- **D-91** — `dirflg=w`/`dirflg=r` (and any unrecognized `dirflg`) are rejected
  with `ExtractionError`, not coerced to `mode: "car"`. A walking/transit share
  link is not a road-trip capture, and defaulting it into one would write a
  wrong payload silently.

---

## 9. Test cases to add to `samples.json`

- `saddr`+`daddr` place names, no `dirflg` → 2 movements, `mode: "car"`, passes.
- Same, with `dirflg=d` explicit → identical result.
- `dirflg=w` present → `ExtractionError`, `needs_review`.
- `daddr` only (no `saddr`) → `ExtractionError`, same message family as
  Google's "fewer than 2 stops."
- Coordinate `saddr` (`37.7749,-122.4194`) + place-name `daddr` → coordinate
  stop parses via `_COORD`, place stop via `_parse_place`.
- No `capture_context.travel_date` → RULE-31 fires (post-D-90) exactly as it
  does today for a dateless Google capture.
- `maps.apple/p/...` link pasted → **not** routed here; falls through to the
  generic web/LLM path (or `needs_review` if that path can't place it) — confirms
  the short-link exclusion in §2 is a routing decision, not a silent drop.

---

## 10. Non-goals restated

No transit/cycling support. No `maps://` scheme handling. No short-link
resolution. No attempt at Google-style embedded-coordinate extraction — the
format doesn't offer it. This is a two-stop, driving-only, best-effort-geocode
capability, matched to what Apple's URL actually carries — not a reduced
Google clone aiming to close the gap later.
