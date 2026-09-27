# Run·dere research kits

Reusable instructions that let an AI agent research part of a trip and save it
into Run·dere's Discover tab, using the Run·dere MCP connector
([docs/claude-connector.md](../docs/claude-connector.md)).

| Kit | Saves to | MCP tool it uses |
|---|---|---|
| [`food-and-drink`](food-and-drink/SKILL.md) | Discover → Food & Drink | `save_city_guide(topic="food")` |
| [`events-and-activities`](events-and-activities/SKILL.md) | Discover → Events & Activities | `save_city_guide(topic="events")` |
| [`local-phrases`](local-phrases/SKILL.md) | Discover → Language → Local phrases | `save_phrases` |
| [`safety-and-transport`](safety-and-transport/SKILL.md) | Discover → Safety & Transport | `save_safety` |
| [`visa-and-entry`](visa-and-entry/SKILL.md) | Discover → Practical → Visa | `save_research(topic="visa")` |

Every kit starts from `get_trip`, researches with the person's
`discovery_profile` (dietary needs, cuisines to avoid, budget, interests,
mobility, their own notes, and what they've hidden as "not my thing"), and
lands as a **draft** they review — nothing goes live unreviewed, and anything
they locked is refused. With a blank profile, the kits ask the person first.

## Three ways to use them

1. **As MCP prompts (easiest in Claude).** The Run·dere connector serves each kit
   as a prompt — `research_food_and_drink`, `research_events_and_activities`,
   `research_local_phrases`, `research_safety_and_transport`,
   `research_visa_and_entry` — taking the trip id and optionally a city key. Pick
   it from the connector's prompts and send.
2. **As a skill.** Each folder is a skill: `SKILL.md` with a `name` and
   `description` header. Download it as a zip (`GET /api/v1/research-kits/<kit>.zip`)
   and add it to an agent that supports skills; the agent then uses it whenever
   you ask it to research that part of a trip.
3. **As a plain prompt.** Paste the body of `SKILL.md` into any agent that has
   the Run·dere MCP tools, and add "for trip <id>".

The same kits are listed at `GET /api/v1/research-kits` (and read one with
`GET /api/v1/research-kits/<kit>`), so the Capture page can offer them in-app.

## Writing a new kit

Add `research_kits/<name>/SKILL.md` with the `name`/`description` header. Keep
the shape: read the trip and profile → research → save with the matching tool →
report back. It's picked up automatically by the API and the MCP prompts.
