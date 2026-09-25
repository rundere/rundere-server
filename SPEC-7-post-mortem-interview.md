# SPEC (planning) — Trip Post-Mortem, AI Interview module

**Status: PLANNING — DEFERRED (2026-09-07).** A post-4.0 major pillar, and the
**deliberately deferred** part of the post-trip work: the AI interview is
**not** being built in the current cycle. It stays a spec-driven backlog item,
picked up later behind its prerequisite (D-145). Decisions D-136+ are
**proposed**. **Extends** the post-mortem *framework* already proposed in
`SPEC-5-inventory-postmortem.md` (D-130); this spec is the **interview /
reviews / lessons** module, a sibling to that spec's packing-review module —
not a competing shell.

> **Why deferred.** SPEC-6's journal (the interview's raw material) shipped
> first — backend + `/api/v1` + web surface. The AI interview is the highest-
> risk, highest-complexity piece, and per D-144 it cannot start until SPEC-5's
> deterministic packing-review module introduces the post-mortem shell. So the
> ordered backlog is: **SPEC-5 packing module → this interview module**, both
> future, spec-driven cycles. Nothing here is built yet.

Turns "the trip is over" into a **structured reflection**: what happened, the
good stories, what could be better next time — captured as an **AI-led
interview** that already knows the itinerary and reads the traveller's journal
(SPEC-6), so it asks specific questions, not a blank form.

## Relationship to SPEC-5 (no second shell)

SPEC-5 D-130 defines `TripPostMortem` as a per-trip, per-user **shell with
modular sections**, explicitly reserving room for "**events** (planned vs actual
timeline), **cost**, and **reviews** (ratings/notes)" beyond the v1 packing
review. **This spec builds the reviews/lessons module inside that shell.** One
`TripPostMortem` per (trip, user); packing review and the interview are two
sections of the same object, offered when a trip becomes `past`.

**Build order (D-144): packing-review first.** SPEC-5's deterministic
packing-review module ships first and **introduces the shell**; this interview
module attaches to that existing shell afterward. Chosen to front-load the
lower-risk, no-LLM module and de-risk the shell before layering AI on top. The
shell is the shared contract either way.

## 1. The interview is itinerary- and journal-aware (D-136)

The point of doing this **inside Yonder** — rather than a generic "how was your
trip?" prompt — is that Yonder holds the trip. The interviewer is grounded in:
- the **itinerary**: the `TripActivity` timeline (flights, lodging, the planned
  days) — the spine of "what was supposed to happen";
- the **journal** (SPEC-6): the traveller's own dated entries — "what actually
  happened / how it felt", read **own-only** (SPEC-6 D-134);
- the **packing plan** and, when SPEC-5 is present, its post-trip outcomes;
- the **discovery findings** that shaped the trip (what was recommended).

From these it asks **specific** questions — "You'd planned the coast road on the
Tuesday but your journal mentions a detour — what happened?" — never a generic
questionnaire. Grounding is a hard rule (§4): the interviewer may only reference
what these sources contain; it must not invent events.

## 2. Interview format & flow (D-137)

A **turn-based, adaptive but deliberately tight** interview, not a fixed form
and not an open-ended chat. v1 is bounded hard (D-137):
1. **Seed.** From the itinerary + journal, the engine drafts a **small** opening
   set — a handful of targeted questions (a few day/activity-level, a couple
   trip-level), not one per day. Order by signal (days the journal actually
   mentions come first).
2. **Converse.** The traveller answers in free text (voice→text is v1.1+, D-143,
   not v1). Each answer may spawn **at most one** follow-up, and only when it
   clearly adds something ("the fish tacos — worth a note for next time?").
   A "wrap up now" control is always present. The interview is **finite by
   construction** — a small seed set × ≤1 follow-up each — so it never feels
   like an endless chat and stays cheap on the LLM.
3. **Skip freely.** Every question is skippable; a sparse interview still
   produces a valid (thinner) post-mortem. Never blocks.
4. **Synthesise.** On finalize, the engine writes the structured result (§3).

The interview is **resumable** — a durable run (§5), not a single request — so a
long, thoughtful reflection survives closing the app.

## 3. What it produces — the post-mortem sections (D-138)

Structured output, not just a transcript. The `TripPostMortem` reviews section
holds:
- **Summary** — a short narrative of the trip (grounded recap + the traveller's
  framing).
- **Highlights / stories** — the good moments worth keeping, each optionally
  tied to a day or an activity.
- **Lessons learned** — what to do differently, as **reusable, forward-looking**
  items. These are the payoff: a lesson can carry a `target` hint (packing,
  discovery-profile, planning) so a future trip's prep can surface it (§6).
- **Ratings (optional)** — light per-dimension scores (lodging, food, pace) if
  the traveller wants them; never required.
- **Transcript** — the raw Q&A retained for provenance and re-synthesis.

The synthesis is **grounded and attributed**: every claim traces to a journal
entry, an activity, or a specific interview answer — the same "cite your source"
discipline the discovery resolvers enforce (D-66 lineage).

## 4. AI methodology — reuse the discovery engine (D-139)

**No new AI infrastructure.** The interview reuses the proven Discovery LLM
stack (`docs/discovery-llm-tier.md`):
- **Ollama synthesis** via the `synthesize_json` / `extract/llm.py` pattern —
  schema-constrained generation, `temperature: 0`, thinking off — for both the
  question-generation and the final structured synthesis.
- **Grounding.** The interviewer's context window is **only** the trip's own
  data (itinerary + own journal + packing + findings). No web search, no
  SearXNG — this is reflection on known data, not research. The grounding check
  becomes: a generated question/claim must reference an id that exists in the
  supplied context; ungrounded output is dropped (the `ground_items` analogue).
- **Degrade, never 500.** Ollama unreachable → the interview falls back to a
  **static question set** derived deterministically from the itinerary (day-by-
  day "how was X?"), so the feature works without the LLM, just less adaptively
  — mirroring how a discovery run still carries deterministic findings when the
  LLM tier is down.
- **Local-first.** Same self-hosted Ollama posture as Discovery; nothing leaves
  the deployment. No third-party AI dependency (consistent with the app's
  no-external-commerce / local-first stance).

## 5. Durable background run (D-140)

The synthesis pass reuses the **`DiscoveryRun` durability pattern**: a long,
quality-paced LLM job whose state is a **DB row**, not in-memory, so it survives
a container restart and self-polls in the UI.

```
TripPostMortem (per trip, per user — the SPEC-5 shell)
- id, trip_id (soft link), owner_id
- status: open | in_progress | done
- sections (JSON)          # packing (SPEC-5) + reviews/lessons (this spec)
- transcript (JSON)        # the Q&A turns
- created_at, finalized_at

PostMortemRun (peer of DiscoveryRun) — the synthesis job
- id, post_mortem_id, trip_id
- status: running | done | failed
- phase, done, total, error, timestamps
```

The interview *conversation* is interactive (turn endpoints); only the
**synthesis** is a background run. Restart detection and one-run-per-post-mortem
guarding follow `app/trips/discovery_jobs.py` exactly.

## 6. The forward loop — lessons feed the next trip (D-141)

A post-mortem that only records the past is half the value. Lessons carry an
optional `target`:
- `target: packing` → surfaces in a future trip's packing plan warnings
  ("last cold trip you noted: not enough base layers").
- `target: discovery_profile` → a suggested tweak to the DiscoveryProfile
  (a cuisine to skip, an interest to add) the traveller can accept.
- `target: planning` → a note pinned to trip planning for similar future trips.

v1 **records** targeted lessons and shows them; **wiring them into** each engine
is a follow-on (each is a small, isolated read of "past lessons for a similar
trip"). This keeps v1 shippable while reserving the high-value loop.

## 7. Isolation & scope

- One `TripPostMortem` **per (trip, user)** — each traveller reflects on their
  own trip; a shared trip does **not** share post-mortems (consistent with the
  personal journal, SPEC-6 D-134). A trip's members each get their own.
- The interview reads the interviewee's **own** journal only (SPEC-6 D-134) and
  the trip-shared activities/findings they can already see.
- `scope.py`: a per-user-per-trip filter (own post-mortem on visible trips).

## 8. API & MCP (additive)

- `POST /api/v1/trips/{id}/postmortem` (open the shell), the interview turn
  endpoints (`.../postmortem/turn`), `POST .../postmortem/synthesize` (launch
  the background run), `GET .../postmortem` (read), `GET .../postmortem/status`
  (poll the run) — the Discovery route shape.
- **Online-only** (D-113): the interview is a live LLM conversation, inherently
  connected; no offline story needed (unlike the journal).
- MCP: `read_post_mortem` and a `run_post_mortem` trigger over the same service
  layer, own-scoped — a natural home for an agent-driven "debrief my last trip".
- `/api/v1` stays additive; no v2.

## 9. Impact on the 4.0 build

**Additive; no rework of completed 4.0 work** — same assessment as SPEC-5.
- **DB:** the SPEC-5 `trip_post_mortems` shell + one `post_mortem_runs` table.
- **API/MCP:** new endpoints/tools; `/api/v1` unchanged for existing clients.
- **AI:** **zero new infrastructure** — reuses Ollama + the discovery run/ground
  machinery (D-139/D-140). The deepest reuse in the plan.
- **iOS:** a later post-trip screen + the "your trip ended — reflect?" prompt
  (the SPEC-5 reserved IA); not a change to the core tabs.
- **Dependency:** materially better **with** SPEC-6 (journal) as input, but
  degrades gracefully to an itinerary-only interview without it.

**Only "act now" item:** reserve the post-trip prompt + a post-mortem area in
the iOS IA (already a SPEC-5 guard); confirm the interview is scoped online-only
so it needs no offline design.

## Open questions (for the design phase)

- **Exact seed/follow-up budget.** The tight bound is decided (D-137); the exact
  numbers — how many seed questions, the "adds something" test for the single
  follow-up — get tuned against real transcripts (as the events resolver was
  tuned against live output). A design-phase tuning task, not a reopening.
- **Multi-traveller trips.** v1 is strictly per-user post-mortems; a future
  "shared trip recap" that (consensually) merges members' highlights is out of
  scope and would need an explicit sharing model.
- **Re-synthesis.** Editing an answer after finalize → re-run synthesis
  (upsert, like a steerable discovery re-run) vs locked. Lean re-runnable.
- **Version placement.** A post-4.0 major (SPEC-5 floated 5.0); the shell could
  land with whichever of the two modules ships first.

## Decisions (proposed, D-136–D-145)

- **D-136 — The interview is grounded in the itinerary + own journal.** It reads
  the `TripActivity` timeline, the interviewee's own journal (SPEC-6),
  packing, and discovery findings, and may reference only what those contain —
  no invented events. (proposed, 2026-09-06)
- **D-137 — Adaptive, turn-based, skippable, resumable — and deliberately tight
  in v1.** Not a fixed form and not an open-ended chat: a **small** seed set
  (a handful of questions), **at most one** follow-up per answer and only when it
  adds something, an always-present "wrap up", every question skippable, and the
  conversation resumable via a durable record. Finite by construction — cheap on
  the LLM and never an endless chat. Exact counts tuned in design. (proposed)
- **D-138 — Structured output, not a transcript.** Synthesises summary,
  highlights/stories, forward-looking lessons (optionally targeted), optional
  ratings; the transcript is retained for provenance and re-synthesis. (proposed)
- **D-139 — Reuse the Discovery LLM stack; no new AI infra.** Ollama synthesis +
  a grounding check over the trip's own data (no web search); degrade to a
  deterministic itinerary-derived question set when the LLM is down; local-first.
  (proposed)
- **D-140 — Synthesis is a durable background run.** Peer of `DiscoveryRun`
  (DB-row job state, restart detection, self-polling); the interview turns are
  interactive, only the synthesis is backgrounded. (proposed)
- **D-141 — Lessons carry a forward target.** A lesson may target packing /
  discovery-profile / planning; v1 records and displays targeted lessons, wiring
  them into each engine is a follow-on — reserving the past→future loop without
  blocking v1. (proposed)
- **D-143 — Text-only in v1; voice→text is v1.1+.** The first interview is text
  entry only. Mobile voice-to-text (iOS speech) makes a field-adjacent debrief
  more natural but adds an Apple dependency and scope; deferred to v1.1 once the
  flow is proven. (proposed, 2026-09-07)
- **D-144 — Packing-review module ships before the interview module.** Within the
  shared `TripPostMortem` shell (SPEC-5 D-130), build SPEC-5's deterministic
  packing-review module first (lower AI risk, no LLM dependency), then this
  interview/stories/lessons module. The shell lands with the packing module; the
  interview attaches to it. Supersedes the earlier "interview-first" lean.
  (proposed, 2026-09-07)
- **D-145 — The AI interview is deferred; the journal shipped first.** In the
  current cycle only SPEC-6's journal is built (backend + API + web). This
  AI-driven module is deliberately held as a spec-driven backlog item, to be
  picked up in a later cycle behind its prerequisite (SPEC-5's packing-review
  module, D-144). Deferral is a sequencing choice, not a scope cut — the plan
  above stands. (accepted, 2026-09-07)
