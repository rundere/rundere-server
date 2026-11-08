# SPEC-12 — AI providers, and AI on or off

Status: **5.0, built** (written 2026-11-07, YONDER-354). Part one is the
providers and the switch; part two (§6) is manual entry in review, the AI call
log and the admin page.

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

## 6. Part two: review, the call log, the admin page

- **AI off in review.** A capture that needs AI leads with the reason code
  `ai_off`: "AI is off, so this wasn't read automatically. Add the details by
  hand." The capture belongs to the person who captured it. Before 5.0 a
  capture extraction couldn't read was saved with no owner, so only an admin
  could see it.
- **Manual entry** (`app/review/manual.py`). For any capture with no details
  (AI off, or AI couldn't read it), review shows the original next to a form:
  the email's subject, sender and text, a page's text, a calendar file, or a
  PDF's text. The person picks the type and fills in review's usual fields.
  The form then:
  - says in plain words what is still missing;
  - passes the result through the extraction schema gate (D-11);
  - stores it as `extractor: manual`, with every typed field locked (D-13), as
    a review edit is;
  - re-resolves it, so the rules run.

  A journey's end with no time becomes "no date yet" (D-210), never a made-up
  one.
- **The AI call log** (`ai_calls`). `ai.client.chat_json` reports every call,
  successful or not. Each row holds the feature, the call (the extractor, a
  Discover topic, `phrasebook`, `connection_test`), the provider and model,
  the start time and duration, the outcome, the error's class name (never its
  text, which can echo the prompt), the token counts when the server gives
  them, and the user. The feature and user travel on the `Endpoint`, because
  Discover calls from a worker thread. Prompts and replies are never stored.
  The daily retention sweep deletes rows after 90 days, and a failure to log
  never fails the call.
- **The admin page**, Settings → AI (`/settings/ai`), admins only and
  read-only. It shows:
  - whether AI is on or off, the endpoint (without any credentials in the
    address) and the model;
  - whether captures and Discover use AI;
  - any configuration problems;
  - how many captures are waiting for manual entry because AI was off;
  - the last 7 days per feature, and the latest calls.

  Test connection lists the endpoint's models, then makes one tiny call,
  which is logged as `admin_test`.
