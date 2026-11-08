"""Schema-constrained synthesis (SPEC-3.2 §3/§4).

The SYNTHESIZE step shared by every LLM resolver: hand the model the profile
constraints + the SearXNG snippets and a per-topic JSON schema, get back
structured JSON, through ai.client.chat_json: Ollama's `format` grammar or an
OpenAI-compatible `json_schema` response format, at temperature 0, with
thinking off where the server allows it (YONDER-354). There's no
validate-and-retry loop: grounding (discover/grounding.py) is the safety net
here, not a second schema gate.

Returns None on ANY failure (no endpoint, unreachable, non-JSON content) so a
resolver degrades to an absent finding, never a 500. No endpoint means AI is
off for Discover (app/ai.py, D-228).
"""
from __future__ import annotations

import json
import logging
import socket
import urllib.error

from ai.client import ChatReply, Endpoint, chat_json

log = logging.getLogger("yonder.discovery.llm")


def _post_chat(llm: Endpoint, messages: list[dict], schema: dict, *,
               num_ctx: int) -> ChatReply:
    """One chat call. Separated so tests monkeypatch here."""
    return chat_json(llm, messages, schema, num_ctx=num_ctx, name="discover_finding")


def synthesize_json(prompt: str, schema: dict, *, llm: Endpoint | None,
                    system: str | None = None, num_ctx: int = 16384) -> dict | None:
    """Run one schema-constrained synthesis. Returns the parsed object, or None
    on any failure. Callers read the result defensively — a missing key is not an
    error, and grounding filters what survives."""
    if llm is None or not prompt.strip():
        return None
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    where, timeout = llm.where, llm.timeout
    try:
        reply = _post_chat(llm, messages, schema, num_ctx=num_ctx)
    except urllib.error.HTTPError as e:
        # 404 here usually means the model isn't pulled (or isn't in LiteLLM's list).
        body = e.read().decode(errors="replace")[:200]
        log.warning("synth: HTTP %s from %s: %s", e.code, where, body)
        return None
    except (TimeoutError, socket.timeout):
        log.warning("synth: timed out after %ss (%s)", timeout, where)
        return None
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            log.warning("synth: timed out after %ss (%s)", timeout, where)
        else:
            log.warning("synth: unreachable (%s): %s", where, e.reason)
        return None
    except (OSError, ValueError) as e:
        log.warning("synth: bad response (%s): %s", where, e)
        return None

    content = reply.content or ""
    stats = (f"done_reason={reply.finish_reason} "
             f"prompt_tokens={reply.prompt_tokens} "
             f"out_tokens={reply.out_tokens} num_ctx={num_ctx} "
             f"thinking_chars={len(reply.thinking or '')}")
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
