# Open Questions

Grouped by who can answer. Do not guess on OWNER items — ask.

---

## ~~Answered by the A4 probe~~ — ANSWERED 2026-08-02, see `A4-PROBE-NOTES.md`

Probe run against live AdventureLog v0.12.1 and AirTrail v3. **No schema revision
required.** Evidence in `probe-results.json`.

| # | Question | Answer |
|---|---|---|
| Q1 | AirTrail: carrier + flight number separate or combined? | **Combined** (`flightNumber: "AC9338"`) plus separate `airline` object — adapter concatenates |
| Q2 | AdventureLog: country as ISO alpha-2 or name? | **alpha-2 uppercase** — matches schema |
| Q3 | **Date-only Transportation accepted?** | **Accepted but silently coerced to `T00:00:00Z`** — adapter must mark synthesized times; naive datetimes are read as UTC |
| Q4 | `order` field collision behaviour | No rejections; running exactly v0.12.1 (the fixed release) — pin ≥0.12.1 |
| Q5 | Is Visit writable via API? | **Yes** — POST/DELETE round-trip verified; backfill unblocked |
| Q6 | Does AdventureLog dedup Locations server-side? | **No** — resolver must match name + proximity (<500m) before creating |

---

## Needs a decision from the OWNER

**OQ-01 — Is a city you have family ties to (or once lived in) home, home-adjacent, or a destination?** **DECIDED 2026-08-03 (owner)**
A **normal destination** — flights logged, trips created, nothing suppressed.
`home_profiles` seeding must include only places you actually live (D-08).

**OQ-02 — `radius_km` default.** **SET TO 50km 2026-08-03 (owner)** — a metro-sized
radius, so a drive to a neighbouring suburb is not a trip but a weekend away 100+ km
out is. Now a
per-profile editable field in `/admin → Home Profiles`, not a global constant,
so it can be tuned per home era without a deploy. Revisit once real road-trip
captures exist.

**OQ-03 — Retain `no_change` items, or count and discard?**
Currently retained. Deliberate — free fixture material — but a chatty airline generates rows
that never commit. Confirm.

**OQ-04 — Collection naming on mint.** ~~open~~ **DECIDED 2026-08-03 (owner)** — see D-23.
`{Primary destination country} — {Month Year}` where the month is the one containing
the **majority of the trip** (implemented as the month of the date-span midpoint;
inherently tie-proof). Example: Aug 29 → Sep 15 names as "September", not "August".
Rename-on-range-extension applies unless the human has renamed (field-lock semantics).

**OQ-05 — `deferred` TTL.** **DECIDED 2026-08-03 (owner)** — see D-24.
**2 days.** Owner: "if the collection doesn't exist yet, then I messed up badly in my
planning" — orphans are planning mistakes to surface fast, not things to sit on.

**OQ-06 — Round-trip drives where home is at both ends.**
`round_trip: true` with home at origin and destination. Naming now follows D-23
(destination country + majority month), which covers this case; remaining A6 work
is only the mechanics of picking "the destination" from waypoints.

**OQ-07 — Review UI stack.** **DECIDED 2026-08-03 (owner): HTMX + Jinja** — see
D-30. Design reviewed as a mockup first, then built and verified against a clone
of real data. Original text follows.


HTMX + Jinja (no build step, harder to make pretty) vs SvelteKit (matches AdventureLog, more
setup). Owner wanted Claude Design input on the review queue before a final build.
**Sequencing agreed 2026-08-03:** A5/A6 are settled, so the design pass happens
**after Task 5** (with a running app to design against) and **before any review-UI
code is written** — that is also when this stack choice lands. Not needed for
Tasks 4–5 (backend only, no visual surface).

**OQ-08 — Ollama model for extraction.** **DECIDED 2026-08-03 — `qwen3:14b`.**
Measured on the 16-email real corpus (see D-29): with Ollama structured
outputs + the typed-variant format grammar + prompt v2, qwen3:14b scored
**16/16 schema-valid with zero retries** and semantically correct
classification, segment fan-out (United = 4 legs), booking references (incl.
subject-line PNRs), statuses (modification → `changed`, cancellation →
`cancelled`), carriers, and D-06 date/precision discipline. ~7–38s per email.
`qwen3:4b-instruct` is DISQUALIFIED: under grammar constraint it classified
nearly everything as lodging (including airline emails) — schema-valid but
semantically wrong, the failure mode the validation gate cannot catch.
Structured outputs DO change the D-11 reliability picture, but only with the
grammar shaped per D-29; D-11's validation gate + single retry stay.

**OQ-09 — Repo name and license.**
Private repo still wants a LICENSE file for collaborators.

---

## ~~Design questions for A5 (canonical keys)~~ — RESOLVED 2026-08-02

See `docs/A5-canonical-keys.md` for the full spec; all four are decided,
implemented in `resolve/keys.py`, and covered by `tests/test_keys.py`:

- **OQ-10** — Flights key on **PNR + carrier + number + origin** (date-anchored
  form always present as an alternate for PNR-less channels; repeated-segment
  edge routed through RULE-17c + `force_date`).
- **OQ-11** — Road trips key on **geohash-5 endpoints** (+geohash-4 alternate);
  waypoints unkeyed; date from `capture_context` with the documented human-input
  stability caveat.
- **OQ-12** — Lodging uses a **sorted token signature** minus articles,
  boilerplate, and city/country tokens; confirmation number bridges date-change
  modifications.
- **OQ-13** — Events key on **date + title signature** (boilerplate and venue
  tokens dropped); `gcal_event_id` as an exact alternate. Venue deliberately
  excluded from the key.

---

## ~~Design questions for A6~~ — RESOLVED 2026-08-03, see D-24 + `docs/A6-collection-resolution.md`

**OQ-14 — Fuzzy window size.** **DECIDED (owner): there is no fuzzy rung.** Near-misses
are never auto-filed; they defer/escalate to review instead. The window-size question
is void.

**OQ-15 — `collection_hints` matching semantics.** **DECIDED (owner): hints are cut from
MVP entirely** — too fuzzy, maintenance-heavy, potentially disruptive. **Flagged as
future work.** The `collection_hints` table drops out of the Phase-0 scaffold.

**OQ-16 — `collection_default` date origin.** **RESOLVED 2026-08-03 (technical):
not wired in MVP.** RULE-34 already restricts maps_url dates to
`capture_context`/`document`, which was its only plausible consumer. The enum value
stays in the schema (harmless); revisit only if a concrete need appears.

---

## Known unmeasured risk

**The deterministic parse rate is unknown.** D-10 makes it the Phase 2 gate: what percentage
of real booking emails carry usable JSON-LD or iCal? If 70%, the LLM is a minor component. If
20%, extraction is the project. Nothing else will tell you which.

The corpus source is a Piler archive of 132K messages spanning 20 years — better than Gmail
for this: full-text searchable, no OAuth, no rate limits, and it contains carriers and formats
that no longer exist. Search terms: *booking confirmation*, *itinerary*, *e-ticket*,
*reservation confirmed*. Target 50–100 `.eml` files.

**Build the redactor before the corpus grows.** Retrofitting it across 200 files is miserable,
and none of those files can be committed until it exists (D-21).
