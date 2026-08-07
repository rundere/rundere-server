"""In-memory registry for long-running operator actions (Process / Reprocess /
Log). The app is single-process and single-tenant, so a module-level dict guarded
by a lock is enough — no migration, no broker, no message bus (see the D-48 UX
review). A process restart orphans a running job (its work is dead anyway); the
poll endpoint reports the job as gone and the operator simply re-runs.

The route starts a job (which runs the real work on a background thread with its
OWN database session) and returns immediately; a self-polling panel reads the
job state each tick. One job runs at a time — the executor has a single worker
and the routes guard on `current_running()` so a second Reprocess can't race the
first (its reset is destructive).
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tx-job")
_KEEP_FINISHED = 12          # cap the registry so finished jobs don't accumulate


def current_running() -> dict | None:
    """The job in flight, if any — the routes show it instead of starting a
    second one."""
    with _LOCK:
        for j in _JOBS.values():
            if not j["finished"]:
                return dict(j)
    return None


def get(job_id: str) -> dict | None:
    with _LOCK:
        j = _JOBS.get(job_id)
        return dict(j) if j else None


def dismiss(job_id: str) -> None:
    with _LOCK:
        _JOBS.pop(job_id, None)


def _update(job_id: str, **fields) -> None:
    with _LOCK:
        j = _JOBS.get(job_id)
        if j:
            j.update(fields)


def start(kind: str, work) -> str:
    """Register a job and run `work(progress)` on the background thread.

    `work` returns (toast_text, error_or_None) or, when it has per-item detail
    worth showing (e.g. the exact reason each write failed), a third element:
    (toast_text, error_or_None, [detail_line, ...]). `progress(phase, done,
    total, current)` updates the live state. Any exception becomes a finished
    job with an error, never a dropped thread.
    """
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        done_ids = [k for k, v in _JOBS.items() if v["finished"]]
        for k in done_ids[:-_KEEP_FINISHED] if len(done_ids) > _KEEP_FINISHED else []:
            _JOBS.pop(k, None)
        _JOBS[job_id] = {
            "id": job_id, "kind": kind, "phase": "starting",
            "done": 0, "total": 0, "current": "",
            "started_at": time.monotonic(), "finished": False,
            "error": None, "toast": None, "toast_error": False,
            "details": [],
        }

    def progress(phase, done=None, total=None, current=None):
        upd: dict = {"phase": phase}
        if done is not None:
            upd["done"] = done
        if total is not None:
            upd["total"] = total
        if current is not None:
            upd["current"] = current
        _update(job_id, **upd)

    def runner():
        try:
            result = work(progress)
            toast, err = result[0], result[1]
            details = result[2] if len(result) > 2 else []
            _update(job_id, finished=True, toast=toast,
                    toast_error=bool(err), error=err, details=details or [])
        except Exception as ex:                     # pragma: no cover
            _update(job_id, finished=True, toast=f"Failed — {ex}",
                    toast_error=True, error=str(ex))

    _EXECUTOR.submit(runner)
    return job_id


def eta_text(job: dict) -> str:
    """Rough remaining-time from average per-item latency so far. 'estimating…'
    until a couple of items complete, since Ollama per-email time varies a lot."""
    done, total = job.get("done", 0), job.get("total", 0)
    if job.get("finished") or total <= 0 or done <= 0:
        return ""
    if done < 2:
        return "estimating…"
    elapsed = time.monotonic() - job["started_at"]
    rate = done / elapsed if elapsed > 0 else 0
    if rate <= 0:
        return "estimating…"
    remaining = max(0, total - done)
    secs = int(remaining / rate)
    return f"~{secs}s left · {remaining} to go"
