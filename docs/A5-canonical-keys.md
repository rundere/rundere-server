# A5 — Canonical Key Specification

Implementation: `resolve/keys.py` (pure functions, stdlib only, no I/O).
Tests: `tests/test_keys.py` (pytest-compatible; also runs standalone via
`python3 tests/test_keys.py`). Resolves OQ-10 through OQ-13.

## The model: a key SET, not a single key

Every item gets a `KeyResult`: one **primary** (stored in
`TravelItem.canonical_key`, unique-indexed) plus zero or more **alternates**.
Two items denote the same real-world thing **iff their key sets intersect**
(`matches()`).

Why: the cross-channel requirement — airline email, Google calendar entry, and
SingleFile manage-booking capture must collapse to the same flight — cannot be
met by a single key, because channels disagree about which fields they carry
(the calendar entry often lacks the PNR). A stable identifier when present,
plus a weaker content-anchored form always, gives collapse without guessing.

**Storage:** alternates are resolver-internal. They live in an `item_keys`
table (`item_id, key, kind`), NOT in the payload schema — no schema change,
consistent with the A4 outcome. Matching = `SELECT` on incoming keys against
that table.

## Key grammar

`{type}:{...segments}` — colon-separated; segments are normalized and never
empty. Codes (PNR, IATA, operator, station) are uppercase alphanumeric; name
signatures are lowercase hyphen-joined token sets.

| Type | Primary (when id present) | Content form (always present) |
|---|---|---|
| flight | `flight:pnr:{PNR}:{carrier}:{number}:{origin}` | `flight:{carrier}:{number}:{date}:{origin}` |
| lodging | `lodging:conf:{CONF}` | `lodging:{name_sig}:{check_in}` |
| event | — (gcal id only ever an alternate) | `event:{date}:{title_sig}` |
| road | — | `road:{date}:{gh5(origin)}:{gh5(dest)}` (+ `road4:` gh4 alternate) |
| rail/bus/ferry | `{prefix}:pnr:{PNR}:{op}:{svc}:{origin}` | `{prefix}:{op}:{svc}:{date}:{origin}` |
| note | — | `note:{date}:{title_sig}` |
| unknown | `None` → needs_review | — |

## Per-type decisions

### Flight (OQ-10)
- **Anchor: PNR + carrier + normalized number + origin IATA.** Survives the
  past-midnight schedule change that breaks a date key — the exact case
  `Revision.action=update` exists for (tested).
- Date form always rides along as alternate → a PNR-less calendar capture
  still collapses against the email capture (tested).
- A PNR is reused across legs, so number+origin stay in the key; leg 1 and
  leg 2 of one booking never collide (tested).
- **Repeated-segment edge** (same PNR, same flight number, different dates —
  weekly commuter multi-city): both legs produce the same PNR-form key.
  RULE-17c already flags duplicate keys within a batch; the resolver responds
  by re-keying those items with `force_date=True`. Deliberate: rare case
  routed through an existing guard instead of complicating the common key.
- Number normalization: strip carrier prefix and leading zeros, keep suffix
  letter (`AC 0652` → `652`, `652A` stays).

### Lodging (OQ-12)
- `name_sig` = tokens of `property_name`, minus articles (multilingual),
  lodging boilerplate (hotel/inn/resort/…), and the place's own city/country
  tokens; deduped, **sorted**, hyphen-joined. Sorting buys word-order
  insensitivity. The three Hoxton variants from HANDOFF collapse to
  `hoxton-shoreditch` (tested), diacritics fold (`Hôtel` → `hotel`).
- Confirmation number, when present, becomes the primary and bridges a
  **date-change modification** — the name form alone cannot, since check-in is
  in it (tested).
- Known trade: aggressive stopwords could collapse two genuinely different
  properties ("Shoreditch Inn" / "Shoreditch Hotel"). Accepted; the resolver's
  geographic proximity check is the backstop, and RULE-15/field-locks protect
  against bad merges overwriting human edits.

### Event (OQ-13)
- `title_sig` drops ticketing boilerplate ("Your tickets:", "presents",
  "tour"…) and venue/city tokens, so email and invite wordings collapse
  (tested). Date is the local start date.
- Venue is **not** in the key — its rendering varies more across channels than
  titles do. Same-title-same-date distinct events at different venues are
  accepted as a collision risk; vanishingly rare in personal travel data.
- `gcal_event_id`, when present, is an alternate: exact identity across
  recaptures of the same calendar entry even if the title was edited (tested).

### Road trip (OQ-11)
- Endpoints truncated to **geohash-5** (~4.9 km cell) absorb coordinate jitter
  between captures of the same route (tested). A **geohash-4** alternate
  (~39 km) absorbs most cell-boundary straddle; the resolver proximity match
  is the final backstop.
- Waypoints deliberately unkeyed: re-planning a stop is still the same drive.
- Date comes from `capture_context.travel_date` (RULE-31/34). No date → no
  key → needs_review, never a guess. Key stability across captures therefore
  depends on the human entering the same date — documented limitation, matches
  the HANDOFF note.

### Scheduled ground (rail/bus/ferry)
Mirrors the flight shape with operator + service number + station code
(`rail:EUR:9024:2026-09-20:GBSPX`, backward-compatible with the existing
sample). PNR form first when a booking reference exists.

## Dispatcher

`keys_for(payload, capture_context) -> KeyResult | None` reads a schema-shaped
`Payload`. `None` means unkeyable (unknown type, missing required fields) and
signals `needs_review` — key generation never throws on malformed input and
never invents an anchor.

## Sample-data note

`samples.json` predates this spec and carries old-format keys
(`flight:AC:0652:2026-09-14:YYZ` — zero-padded number, no PNR form). Rules only
test key *presence/uniqueness*, not format, so the 35 cases still pass
(verified). Migrate the sample keys opportunistically when samples are next
regenerated.
