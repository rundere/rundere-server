"""Schema-constrained Ollama synthesis (SPEC-3.2 §3/§4).

The SYNTHESIZE step shared by every LLM resolver: hand Ollama the profile
constraints + the SearXNG snippets and a per-topic JSON schema, get back
structured JSON. Reuses the OllamaExtractor pattern from extract/llm.py —
`/api/chat` with a JSON-schema `format` grammar, `temperature: 0`,
`num_ctx: 16384`, thinking off — but without the validate-and-retry loop:
grounding (discover/grounding.py) is the safety net here, not a second schema
gate, and the `format` grammar already constrains generation.

Stdlib HTTP (urllib), like the rest of discovery. Returns None on ANY failure
(no base_url, unreachable, non-JSON content) so a resolver degrades to an
absent finding, never a 500.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_MODEL = "qwen3:14b"


def _post_chat(base_url: str, messages: list[dict], schema: dict, *,
               model: str, timeout: int, num_ctx: int) -> str:
    """One /api/chat call, returning the raw message content (a JSON string when
    the format grammar holds). Separated so tests monkeypatch here."""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "format": schema,
        "think": False,
        "options": {"temperature": 0, "num_ctx": num_ctx},
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/chat", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data.get("message", {}).get("content", "")


def synthesize_json(prompt: str, schema: dict, *, base_url: str | None,
                    system: str | None = None, model: str = DEFAULT_MODEL,
                    timeout: int = 600, num_ctx: int = 16384) -> dict | None:
    """Run one schema-constrained synthesis. Returns the parsed object, or None
    on any failure. Callers read the result defensively — a missing key is not an
    error, and grounding filters what survives."""
    if not base_url or not prompt.strip():
        return None
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        content = _post_chat(base_url, messages, schema, model=model,
                             timeout=timeout, num_ctx=num_ctx)
        parsed = json.loads(content)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None
