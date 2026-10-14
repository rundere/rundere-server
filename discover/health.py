"""How the trip-research sources are behaving (YONDER-253, SPEC-8 D-197).

SearXNG, Open-Meteo and Frankfurter are outside services that can fail
quietly — SearXNG most of all: it answers 200 with *no results* when the
engines it asks time out or rate-limit it, and a resolver then just drops a
city. Every call records its outcome here, so the admin page can show how
reliable each source has been, and a Discovery run can tell the admin when
a source mostly failed during it.

A LEAF module (like discover/http.py): no imports from the app or the
resolvers. In memory, per process; counts restart with the server.
"""
from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

SEARXNG, OPEN_METEO, FRANKFURTER = "searxng", "open-meteo", "frankfurter"
LABELS = {SEARXNG: "SearXNG (web search)", OPEN_METEO: "Open-Meteo (weather)",
          FRANKFURTER: "Frankfurter (exchange rates)"}
OK, EMPTY, DOWN = "ok", "empty", "down"          # empty = answered, but nothing usable

_lock = threading.Lock()


@dataclass
class SourceStats:
    ok: int = 0
    empty: int = 0
    down: int = 0
    engines: Counter = field(default_factory=Counter)     # SearXNG engines that didn't answer
    last_failure_at: datetime | None = None
    last_failure: str | None = None

    @property
    def calls(self) -> int:
        return self.ok + self.empty + self.down

    @property
    def failed(self) -> int:
        return self.empty + self.down


_stats: dict[str, SourceStats] = {}
_since = datetime.now(timezone.utc)


def record(source: str, outcome: str, detail: str | None = None,
           engines: list[str] | None = None) -> None:
    with _lock:
        st = _stats.setdefault(source, SourceStats())
        setattr(st, outcome, getattr(st, outcome) + 1)
        if engines:
            st.engines.update(engines)
        if outcome != OK:
            st.last_failure_at = datetime.now(timezone.utc)
            st.last_failure = (detail or outcome)[:200]


def snapshot() -> dict[str, SourceStats]:
    """A copy of every source's counts so far."""
    with _lock:
        return {k: SourceStats(v.ok, v.empty, v.down, Counter(v.engines),
                               v.last_failure_at, v.last_failure) for k, v in _stats.items()}


def since() -> datetime:
    return _since


def delta(before: dict[str, SourceStats], after: dict[str, SourceStats]) -> dict[str, SourceStats]:
    """What happened between two snapshots (one Discovery run)."""
    out = {}
    for k, a in after.items():
        b = before.get(k, SourceStats())
        out[k] = SourceStats(a.ok - b.ok, a.empty - b.empty, a.down - b.down,
                             a.engines - b.engines, a.last_failure_at, a.last_failure)
    return out


def unresponsive(data) -> list[str]:
    """The engine names a SearXNG JSON answer lists as not answering."""
    rows = data.get("unresponsive_engines") or [] if isinstance(data, dict) else []
    out = []
    for row in rows:
        name = row[0] if isinstance(row, (list, tuple)) and row else row
        if isinstance(name, str) and name:
            out.append(name)
    return out


def reset() -> None:                     # tests
    global _since
    with _lock:
        _stats.clear()
        _since = datetime.now(timezone.utc)
