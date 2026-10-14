"""The health monitor behind admin notifications (YONDER-253, D-197).

Every 15 minutes it checks what can't report for itself, and hands each
result to notify.report(); the inbox reader, outgoing mail and the
retention sweep also report as they run. A check that isn't configured
isn't watched (and its old state is left alone).

| key                           | category      | how                                   | every  |
|-------------------------------|---------------|---------------------------------------|--------|
| inbox                         | mail          | the reader's own results + staleness  | 15 min |
| smtp                          | mail          | sign in and hang up (sends nothing)   | 60 min |
| integration:<user>:<kind>     | integrations  | each enabled connection's check()     | 60 min |
| documents                     | storage       | the store folder (marker, writable)   | 15 min |
| antivirus                     | storage       | a tiny scan through clamav-rest       | 15 min |
| llm                           | ai            | Ollama's /api/tags                    | 15 min |
| retention                     | jobs          | reported by the daily sweep itself    | daily  |
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app import notify
from app.config import settings

log = logging.getLogger("rundere.monitor")

TICK = 15 * 60
HOURLY = 60 * 60
_last: dict[str, float] = {}           # check name → monotonic time it last ran


def _due(name: str, every: float, force: bool) -> bool:
    now = time.monotonic()
    if force or now - _last.get(name, -1e12) >= every:
        _last[name] = now
        return True
    return False


# --------------------------------------------------------------------------- the checks
def check_documents(session) -> tuple[bool, str] | None:
    if not (settings.documents_dir or "").strip():
        return None
    from app import documents
    why = documents.folder_problem(session)
    return (why is None, why or "")


def check_antivirus() -> tuple[bool, str] | None:
    if not (settings.clamav_url or "").strip():
        return None
    from extract import filescan
    try:
        filescan.clamav_scan(settings.clamav_url, "health-check.txt", b"Run-dere health check",
                             timeout=15)
        return True, ""
    except filescan.ScanUnavailable as ex:
        return False, f"the virus scanner isn't answering ({ex}) — file capture is refused " \
                      "and email attachments aren't read until it is"


def check_llm() -> tuple[bool, str] | None:
    if not (settings.ollama_url or "").strip():
        return None
    import urllib.request
    try:
        with urllib.request.urlopen(settings.ollama_url.rstrip("/") + "/api/tags",
                                    timeout=10) as r:
            return (r.status == 200, "" if r.status == 200 else f"HTTP {r.status}")
    except Exception as ex:
        return False, f"can't reach Ollama ({type(ex).__name__}) — bookings that need " \
                      "the LLM go to review instead of being read"


def check_inbox() -> tuple[bool, str] | None:
    """The reader reports each read itself; this catches a reader that has
    silently stopped (no read at all for a long while)."""
    from app import inbox
    if not inbox.configured():
        return None
    last = inbox.status.get("last_poll_at")
    limit = timedelta(seconds=max(45 * 60, 5 * settings.inbox_poll_seconds))
    if last is not None and datetime.now(timezone.utc) - last > limit:
        return False, f"the inbox hasn't been read since {last:%Y-%m-%d %H:%M} UTC"
    return None                                     # healthy or erroring: the reader says


def run_once(*, force: bool = False, session_factory=None) -> dict:
    """One sweep. `force` runs the hourly checks too (the "Check now" button)."""
    from app.db import SessionLocal
    sf = session_factory or SessionLocal
    out: dict[str, bool] = {}

    def rep(key, result, *, category, label, threshold):
        if result is None:
            return
        ok, detail = result
        out[key] = ok
        notify.report(key, ok, category=category, label=label, detail=detail,
                      threshold=threshold, session_factory=sf)

    with sf() as s:
        rep("documents", check_documents(s), category="storage",
            label="File store folder", threshold=1)
    rep("antivirus", check_antivirus(), category="storage", label="Virus scanner", threshold=2)
    rep("llm", check_llm(), category="ai", label="LLM (Ollama)", threshold=2)
    rep("inbox", check_inbox(), category="mail", label="Email inbox (IMAP)", threshold=1)
    if _due("smtp", HOURLY, force):
        from app import email as mail
        if mail.email_configured():
            mail.check_connection()                 # reports through email._record
    if _due("integrations", HOURLY, force):
        check_integrations(sf)
    return out


def check_integrations(sf) -> None:
    from app import integrations
    from app.models import User, UserIntegration
    with sf() as s:
        for c in s.query(UserIntegration).filter(UserIntegration.enabled.is_(True)).all():
            ok, msg = integrations.check(s, c)
            s.commit()
            user = s.get(User, c.user_id)
            who = user.username if user else "a removed user"
            notify.report(f"integration:{c.user_id}:{c.kind}", ok, category="integrations",
                          label=f"{integrations.LABELS.get(c.kind, c.kind)} for {who}",
                          detail=msg, threshold=2, session_factory=sf)


# --------------------------------------------------------------------------- the thread
_thread: threading.Thread | None = None
_stop = threading.Event()


def _loop() -> None:
    if _stop.wait(120):                       # let startup settle
        return
    while True:
        try:
            run_once()
        except Exception:                     # pragma: no cover - never kill the thread
            log.exception("health monitor sweep failed")
        if _stop.wait(TICK):
            return


def start() -> bool:
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="health-monitor", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()
