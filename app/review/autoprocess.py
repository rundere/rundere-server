"""Auto-process (SPEC-9 D-158): a capture on the Capture Hub starts extract + resolve by
itself, so the item is in the person's approval queue straight away. Approval stays a human
act (D-31): this only runs the same extract + resolve the Process button runs.

One background job runs at a time (app/review/jobs.py). A capture that arrives while this
module's job is running records a "run again" for its owner and the running job picks it up
before it ends, so a batch that landed mid-run is never stranded. If the job fails the batch
stays `received`: capture never depends on processing, and the review queue's "to process"
count and button remain the retry path. `TX_AUTO_PROCESS=false` turns it off."""
from __future__ import annotations

import logging
import threading

from app import scope
from app.config import settings
from app.db import SessionLocal
from app.review import jobs

log = logging.getLogger("yonder.autoprocess")

_LOCK = threading.Lock()
_ACTIVE = False                     # our job is running or queued
_AGAIN: dict[str, object] = {}      # owner key -> user snapshot, to run again


def _run_once(snap, progress) -> tuple[str, str | None]:
    from app.review.routes import _process_toast, _run_extract_resolve
    with SessionLocal() as s:
        summary, resolved, error = _run_extract_resolve(s, progress, user=snap)
        return _process_toast(summary, resolved, error), error


def kick(user) -> str | None:
    """Start (or queue) processing for `user`'s received batches. Returns the job id when a
    job was started, None when it was queued behind our own running job or switched off."""
    global _ACTIVE
    if not settings.auto_process:
        return None
    snap = scope.snapshot(user)
    key = snap.id if snap is not None else ""
    with _LOCK:
        if _ACTIVE:
            _AGAIN[key] = snap
            return None
        _ACTIVE = True

    def work(progress):
        global _ACTIVE
        current, last = snap, ("Done.", None)
        while True:
            try:
                last = _run_once(current, progress)
            except Exception as ex:                       # never lose the flag
                log.exception("auto-process run failed")
                last = (f"Failed — {ex}", str(ex))
            with _LOCK:
                if _AGAIN:
                    _, current = _AGAIN.popitem()
                    continue
                _ACTIVE = False
                return last

    try:
        return jobs.start("process", work, owner=snap.id if snap is not None else None)
    except Exception:                                      # pragma: no cover
        with _LOCK:
            _ACTIVE = False
        raise
