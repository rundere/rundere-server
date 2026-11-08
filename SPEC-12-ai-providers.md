# SPEC-12 — AI providers, and AI on or off

Status: **5.0, in progress** (written 2026-11-07, YONDER-354). Part one, the
providers and the switch, is built. Part two, manual entry in review and the AI
call log, is next (§6).

Before 5.0, Run·dere called Ollama directly in two places: the extractor
(`extract/llm.py`) and Discovery's synthesis step (`discover/synth.py`). D-11
said inference is local only. 5.0 lets an operator use any OpenAI-compatible
server instead, such as LiteLLM, vLLM, llama.cpp's server or a cloud provider.
It also makes "no AI" a mode Run·dere is built for, not a broken setup.

## 1. Decision

**D-228 — AI is off unless the operator turns it on. A cloud endpoint may serve
Discovery, but booking emails and files go to one only when the operator allows
that separately.** It amends D-11.

- **Off by default** (`TX_AI_ENABLED=false`). Extraction still runs its
  deterministic readers: JSON-LD, iCal and .ics, PDFs with text, and Maps links.
  A capture none of them can read waits in review, and Discovery shows its
  deterministic tier (weather, currency, plugs, language basics). Nothing tries
  to call a model, so nothing times out.
- **Local or cloud is the operator's statement** (`TX_AI_LOCATION`), not a guess
  from the address. A cloud endpoint must be https.
- **Discovery may use a cloud endpoint.** Its prompts hold public web snippets
  and the person's own profile constraints.
- **Extraction may not, unless `TX_AI_CLOUD_EXTRACTION=true`.** Travel emails
  carry full names, addresses, passenger lists and partial payment data, which
  is the reason for D-11. With a cloud endpoint and the switch off, extraction
  behaves as if AI were off and the start-up log says so.
- **A demo server never calls AI** (D-198), whatever its settings say.
- **The configuration lives in the environment only.** The key comes from
  `TX_AI_API_KEY` or a `_FILE` secret and is never stored in the database
  (in the spirit of D-151), never logged, and never shown.

D-11's validation gate and its single retry still apply to every provider.

## 2. Settings

| Variable | Default | |
|---|---|---|
| `TX_AI_ENABLED` | `false` | The switch |
| `TX_AI_PROVIDER` | `ollama` | `ollama` or `openai` (any OpenAI-compatible API) |
| `TX_AI_URL` | | `http://ollama:11434`, or a base ending in `/v1` |
| `TX_AI_MODEL_CHAT` | | The model for chat-style calls. Other model roles (vision) are reserved and not wired yet |
| `TX_AI_API_KEY` / `_FILE` | | Bearer token, `openai` only |
| `TX_AI_LOCATION` | `local` | `local` or `cloud` |
| `TX_AI_CLOUD_EXTRACTION` | `false` | D-228's second switch |
| `TX_LLM_TIMEOUT_SECONDS` | `600` | Unchanged |

These are operator configuration, not user-supplied URLs, so the SSRF policy
(D-152) doesn't apply. The same reasoning covers `TX_WATCH_URLS`.

## 3. Providers

One call, `ai.client.chat_json(endpoint, messages, schema)`, at temperature 0:

- **ollama:** `POST {url}/api/chat` with the schema as the `format` grammar,
  `think: false`, and `num_ctx` for the context window.
- **openai:** `POST {url}/chat/completions` with
  `response_format: {type: json_schema, strict: false}`. Strict mode is off
  because OpenAI's strict mode rejects the extractor's per-type `anyOf`
  schema. The schema gate (extraction) and grounding (Discovery) stay the real
  checks. A reasoning model's `reasoning_content` is kept for diagnostics only.

Anthropic, Google and other native APIs are out of scope: put LiteLLM in front
of them.

`app/ai.py` decides, per feature (`extraction`, `discover`), which endpoint a
call may use, or none. Every caller asks it: the review queue's Process button
and automatic processing, `app.cli process`, and the Discovery resolvers and
phrasebook generator.

## 4. Health

When AI is on, the health monitor asks the endpoint for its models: Ollama's
`/api/tags`, or `/models` on an OpenAI-compatible server. When AI is off, it
isn't watched.

## 5. Upgrading from 4.x

- `TX_OLLAMA_URL` and `TX_OLLAMA_MODEL` still work in 5.0, with a start-up
  warning, when `TX_AI_URL` is unset and `TX_OLLAMA_URL` is set explicitly.
  They're removed in 5.1.
- **An existing install must add `TX_AI_ENABLED=true` to keep reading booking
  emails with AI.** Without it, 5.0 reads only the structured sources and the
  rest waits in review. The 5.0 release notes say this first.

## 6. Part two (next)

- **AI off in review:** a capture that needs AI says "AI is off: add the
  details by hand" rather than "no LLM configured", and review gets a
  manual-entry form (pick the type, fill in the fields, then the same schema
  gate and rules as an extracted item).
- **The AI call log:** an `ai_calls` table holding feature, function,
  provider, model, start time, duration, outcome and error class (never the
  error body, which can echo the prompt), token counts when given, and the
  user. Kept 90 days. Prompts and replies are never stored.
- **A read-only admin page:** on or off, the endpoint and model, where AI is
  used, a Test connection button, the call log, and how many captures waited
  for manual entry because AI was off.
