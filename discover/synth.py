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
import logging
import socket
import urllib.error
import urllib.request

DEFAULT_MODEL = "qwen3:14b"

log = logging.getLogger("yonder.discovery.llm")


def _post_chat(base_url: str, messages: list[dict], schema: dict, *,
               model: str, timeout: int, num_ctx: int) -> dict:
    """One /api/chat call, returning Ollama's full response body (the message
    content is a JSON string when the format grammar holds; the rest —
    done_reason, eval counts, thinking — is kept for failure diagnostics).
    Separated so tests monkeypatch here."""
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
        return json.loads(resp.read())


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
    where = f"{model} at {base_url}"
    try:
        data = _post_chat(base_url, messages, schema, model=model,
                          timeout=timeout, num_ctx=num_ctx)
    except urllib.error.HTTPError as e:
        # 404 here usually means the model isn't pulled.
        body = e.read().decode(errors="replace")[:200]
        log.warning("synth: Ollama HTTP %s from %s: %s", e.code, where, body)
        return None
    except (TimeoutError, socket.timeout):
        log.warning("synth: Ollama timed out after %ss (%s)", timeout, where)
        return None
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            log.warning("synth: Ollama timed out after %ss (%s)", timeout, where)
        else:
            log.warning("synth: Ollama unreachable (%s): %s", where, e.reason)
        return None
    except (OSError, ValueError) as e:
        log.warning("synth: bad response from Ollama (%s): %s", where, e)
        return None

    if not isinstance(data, dict):
        log.warning("synth: unexpected Ollama response from %s: %r", where, data)
        return None
    msg = data.get("message") or {}
    content = msg.get("content") or ""
    stats = (f"done_reason={data.get('done_reason')} "
             f"prompt_tokens={data.get('prompt_eval_count')} "
             f"out_tokens={data.get('eval_count')} num_ctx={num_ctx} "
             f"thinking_chars={len(msg.get('thinking') or '')}")
    if not content.strip():
        # The gpt-oss failure mode: it ignores think=false and spends the
        # context on reasoning, leaving no content.
        log.warning("synth: %s returned empty content (%s)", where, stats)
        return None
    try:
        parsed = json.loads(content)
    except ValueError as e:
        log.warning("synth: %s returned invalid JSON (%s; %s): %r…",
                    where, e, stats, content[:200])
        return None
    if not isinstance(parsed, dict):
        log.warning("synth: %s returned JSON %s, not an object (%s)",
                    where, type(parsed).__name__, stats)
        return None
    return parsed
