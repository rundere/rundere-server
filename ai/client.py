"""One way to ask an LLM for schema-shaped JSON (YONDER-354, SPEC-12).

Run·dere talks to two kinds of endpoint:

- **ollama**: Ollama's own `/api/chat`, with the JSON schema as its `format`
  grammar and thinking off. What every install used before 5.0.
- **openai**: any OpenAI-compatible `/chat/completions` (LiteLLM, vLLM,
  llama.cpp's server, a cloud provider), with the schema as
  `response_format: json_schema`. Not strict mode: OpenAI's strict mode rejects
  the extractor's per-type `anyOf` schema, and the callers keep their own
  schema gate (extraction) or grounding (Discover) anyway.

Which endpoint a feature may use, and whether AI is on at all, is decided in
app/ai.py (D-228). This module only makes the call. Stdlib HTTP (urllib), like
the rest of the pipeline. Transport errors (urllib.error.URLError,
TimeoutError, OSError) and bad bodies (ValueError) reach the caller unchanged,
so each keeps its own failure handling.

The API key travels in the Authorization header and nowhere else: never in a
URL, a log line, an exception message or a repr (house rule 6).
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field

PROVIDERS = ("ollama", "openai")


@dataclass(frozen=True)
class Endpoint:
    """Where one feature's LLM calls go. `cloud` is the operator's word for it
    (TX_AI_LOCATION), not something guessed from the address."""
    provider: str
    url: str
    model: str
    api_key: str = field(default="", repr=False)
    timeout: int = 600
    cloud: bool = False

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise ValueError(f"unknown AI provider {self.provider!r} (ollama or openai)")

    @property
    def where(self) -> str:
        """For log lines: the model and the address, never the key."""
        return f"{self.model} at {self.url}"


@dataclass
class ChatReply:
    """The message content (a JSON string when the schema held) plus what a
    failure diagnosis needs. Token counts are None when the server didn't say."""
    content: str
    thinking: str = ""
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    out_tokens: int | None = None


def chat_json(llm: Endpoint, messages: list[dict], schema: dict, *,
              num_ctx: int = 16384, name: str = "result") -> ChatReply:
    """One schema-constrained chat call at temperature 0. `num_ctx` sizes
    Ollama's context window; OpenAI-compatible servers size their own."""
    if llm.provider == "ollama":
        return _ollama(llm, messages, schema, num_ctx)
    return _openai(llm, messages, schema, name)


def _post(url: str, body: dict, timeout: int, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    return data


def _ollama(llm: Endpoint, messages: list[dict], schema: dict, num_ctx: int) -> ChatReply:
    data = _post(f"{llm.url.rstrip('/')}/api/chat", {
        "model": llm.model,
        "messages": messages,
        "stream": False,
        "format": schema,
        "think": False,
        "options": {"temperature": 0, "num_ctx": num_ctx},
    }, llm.timeout)
    msg = data.get("message") or {}
    return ChatReply(content=msg.get("content") or "", thinking=msg.get("thinking") or "",
                     finish_reason=data.get("done_reason"),
                     prompt_tokens=data.get("prompt_eval_count"),
                     out_tokens=data.get("eval_count"))


def _openai(llm: Endpoint, messages: list[dict], schema: dict, name: str) -> ChatReply:
    headers = {"Authorization": f"Bearer {llm.api_key}"} if llm.api_key else {}
    data = _post(f"{llm.url.rstrip('/')}/chat/completions", {
        "model": llm.model,
        "messages": messages,
        "temperature": 0,
        "stream": False,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": name, "schema": schema, "strict": False}},
    }, llm.timeout, headers)
    choices = data.get("choices") or [{}]
    msg = choices[0].get("message") or {}
    usage = data.get("usage") or {}
    return ChatReply(content=msg.get("content") or "",
                     # LiteLLM and vLLM put a reasoning model's thinking here.
                     thinking=msg.get("reasoning_content") or "",
                     finish_reason=choices[0].get("finish_reason"),
                     prompt_tokens=usage.get("prompt_tokens"),
                     out_tokens=usage.get("completion_tokens"))


def list_models(llm: Endpoint, timeout: int = 10) -> int:
    """For the health check: ask the endpoint which models it serves. Returns
    the HTTP status; raises like chat_json on transport errors."""
    path = "/api/tags" if llm.provider == "ollama" else "/models"
    headers = {"User-Agent": "Rundere"}
    if llm.provider == "openai" and llm.api_key:
        headers["Authorization"] = f"Bearer {llm.api_key}"
    req = urllib.request.Request(llm.url.rstrip("/") + path, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status
