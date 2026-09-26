"""Who may replace a Discover finding, and what's kept when they do (owner
decision, 2026-09-26, YONDER-108 follow-up).

Two kinds of writer:
- **local** — the server's own Discover run (deterministic resolvers + the
  local LLM). Not agentic: it just re-derives, so a newer run should normally
  win.
- **external** — an AI agent (Claude, via the MCP `save_research` tool or
  `POST /api/v1/trips/{id}/findings`). Deliberate work, marked
  `resolver="claude"`.

Rules:
- **Locking** is per topic. Only stable facts and manual language picks can be
  locked (`LOCKABLE_TOPICS`); approving or editing one locks it. Time-sensitive
  topics (weather, currency, food, events, safety, packing, research) never
  lock — a newer result always supersedes, and the old version is kept.
- A **lock is absolute**: neither a local re-run nor Claude overwrites it until
  the person unlocks it (or explicitly re-runs that one finding).
- A **local** whole-trip run never overwrites an **external** finding, nor one
  the person **rejected**.
- Claude may write only `EXTERNAL_TOPICS` — not weather/currency/languages,
  whose structured data (forecast series, calculator rates, the language
  picker) only the local resolvers produce.
- Whatever is superseded goes into `data["history"]` (newest first, last
  `HISTORY_KEPT`), shown on the Discover card as "Earlier versions".
"""
from __future__ import annotations

from datetime import datetime

EXTERNAL_RESOLVER = "claude"
LOCKABLE_TOPICS = frozenset({"plugs", "visa", "language", "language_regional",
                             "language_local"})
EXTERNAL_TOPICS = ("research", "food", "events", "safety", "visa", "plugs", "packing")
LOCKING_STATUSES = ("approved", "edited")
HISTORY_KEPT = 10


def is_lockable(topic: str | None) -> bool:
    return topic in LOCKABLE_TOPICS


def is_locked(finding) -> bool:
    return is_lockable(finding.topic) and finding.status in LOCKING_STATUSES


def is_external(finding) -> bool:
    return (finding.resolver or "") == EXTERNAL_RESOLVER


def local_run_may_replace(finding) -> bool:
    """May a whole-trip local Discover run overwrite this finding?"""
    return not (is_locked(finding) or is_external(finding)
                or finding.status == "rejected")


def history_with(finding, new_body: str | None = None) -> list:
    """The finding's history with its CURRENT version pushed on top — call
    before overwriting it. Skipped (history unchanged) when the body would not
    change, so a routine re-run doesn't pile up identical copies."""
    data = finding.data if isinstance(finding.data, dict) else {}
    history = [h for h in (data.get("history") or []) if isinstance(h, dict)]
    if not finding.body_md or (new_body is not None and new_body == finding.body_md):
        return history[:HISTORY_KEPT]
    gen = finding.generated_at
    snap = {"title": data.get("title"), "body_md": finding.body_md,
            "sources": finding.sources or [], "status": finding.status,
            "resolver": finding.resolver,
            "generated_at": gen.isoformat() if isinstance(gen, datetime) else gen}
    return [snap, *history][:HISTORY_KEPT]


def with_history(data: dict | None, history: list) -> dict | None:
    """New `data` carrying the history forward (None stays None when empty)."""
    if not history:
        return data
    out = dict(data or {})
    out["history"] = history
    return out
