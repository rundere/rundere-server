"""Run·dere — admin notifications (YONDER-253, SPEC-8 D-197).

Notification is a function of the server, with pluggable targets: Pushover
first (app/notify/pushover.py), more over time. Admins manage the targets in
Settings → Notifications, not in env variables. Credentials are encrypted at
rest (secretbox, D-151) and never shown again.

What gets watched: anything that can quietly stop working — the email inbox
(IMAP), outgoing mail (SMTP), people's AirTrail/AdventureLog connections, the
documents store folder, the virus scanner, the retention sweep, the LLM. Each
is an `AlertState` keyed like "inbox" or "integration:<user>:<kind>". Code
calls `report(key, ok, …)`; an alert goes out only on a change:

- **failing** — after `threshold` failures in a row (so one dropped
  connection doesn't page anyone),
- **reminder** — every 24 hours while it's still failing,
- **recovered** — when it works again (quiet priority).

Messages carry a service name, a short reason and at most a username — never
a credential, an address or anyone's content. Sending never blocks or breaks
the caller: it happens on a worker thread, and a target that fails records
its error on the Notifications page.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

log = logging.getLogger("rundere.notify")

# Alert categories a target can pick (empty = all).
CATEGORIES = {
    "mail": "Email — the forwarding inbox (IMAP) and outgoing mail (SMTP)",
    "integrations": "AirTrail and AdventureLog connections",
    "storage": "The file store and the virus scanner",
    "jobs": "Background jobs — the daily retention sweep",
    "ai": "The LLM (Ollama) used to read bookings and research trips",
    "research": "Trip research sources — SearXNG, Open-Meteo, Frankfurter",
}
REMIND_AFTER = timedelta(hours=24)

FAILING, REMINDER, RECOVERED, TEST = "failing", "reminder", "recovered", "test"

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notify")
_lock = threading.Lock()
ASYNC = True                          # tests send synchronously


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def kinds() -> dict:
    """The target kinds this server can send to."""
    from app.notify import pushover
    return {pushover.KIND: pushover}


# --------------------------------------------------------------------------- health
def report(key: str, ok: bool, *, category: str, label: str, detail: str | None = None,
           threshold: int = 1, session_factory=None) -> str | None:
    """Record one check of `key`. Returns the alert sent, if any. Never raises."""
    try:
        return _report(key, ok, category=category, label=label, detail=detail,
                       threshold=threshold, session_factory=session_factory)
    except Exception:                                  # pragma: no cover - must not break callers
        log.exception("notify: couldn't record %s", key)
        return None


def _report(key, ok, *, category, label, detail, threshold, session_factory):
    from app.models import AlertState
    if session_factory is None:
        from app.db import SessionLocal as session_factory
    now = _now()
    event = None
    with _lock, session_factory() as s:
        st = s.get(AlertState, key)
        if st is None:
            st = AlertState(key=key, category=category, failing=False, fail_count=0,
                            alerted=False)
            s.add(st)
        st.category, st.label, st.checked_at = category, label[:120], now
        if ok:
            if st.failing:
                if st.alerted:
                    event = RECOVERED
                    since = _aware(st.since)
                    detail = _duration(now - since) if since else None
                st.failing, st.fail_count, st.alerted = False, 0, False
                st.since, st.detail = None, None
        else:
            if not st.failing:
                st.failing, st.since, st.fail_count, st.alerted = True, now, 0, False
            st.fail_count = (st.fail_count or 0) + 1
            st.detail = (detail or "")[:300] or None
            if not st.alerted and st.fail_count >= max(1, threshold):
                event, st.alerted, st.last_alert_at = FAILING, True, now
            elif st.alerted and now - (_aware(st.last_alert_at) or now) >= REMIND_AFTER:
                event, st.last_alert_at = REMINDER, now
        since = _aware(st.since)
        s.commit()
    if event:
        title, message, quiet = _compose(event, label, detail, since)
        dispatch(category, title, message, quiet=quiet, session_factory=session_factory)
    return event


def _duration(d: timedelta) -> str:
    mins = int(d.total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)} min"
    hours = mins // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} days"


def _compose(event, label, detail, since) -> tuple[str, str, bool]:
    when = since.strftime("%Y-%m-%d %H:%M UTC") if since else ""
    if event == RECOVERED:
        return (f"✅ {label} recovered",
                f"{label} is working again" + (f" after {detail}." if detail else "."), True)
    if event == REMINDER:
        return (f"⚠️ {label} still failing",
                f"{detail or 'Still failing.'}\nFailing since {when} "
                f"({_duration(_now() - since) if since else '?'}).", False)
    return (f"⚠️ {label} is failing",
            f"{detail or 'It stopped working.'}\nSince {when}. You'll hear again when it "
            "recovers, or in 24 hours if it doesn't.", False)


# --------------------------------------------------------------------------- sending
def _aad(target) -> bytes:
    return f"notify|{target.id}|{target.kind}".encode()


def secrets_of(target) -> dict:
    import json

    from app import secretbox
    return json.loads(secretbox.decrypt(target.secret_ct or "", _aad(target)))


def set_secrets(target, values: dict) -> None:
    import json

    from app import secretbox
    target.secret_ct = secretbox.encrypt(json.dumps(values), _aad(target))


def wants(target, category: str) -> bool:
    cats = target.categories or []
    return target.enabled and (not cats or category in cats or category == TEST)


def send_to(session, target, title: str, message: str, *, quiet: bool = False) -> tuple[bool, str]:
    """Send one message to one target now; records the outcome on it."""
    kind = kinds().get(target.kind)
    if kind is None:
        ok, note = False, f"unknown kind {target.kind!r}"
    else:
        try:
            ok, note = kind.send(secrets_of(target), target.config or {}, title, message,
                                 quiet=quiet, url=_settings_url())
        except Exception as ex:                        # a SecretError says why, never the value
            ok, note = False, f"{type(ex).__name__}: {str(ex)[:200]}"
    target.last_error = None if ok else note[:300]
    if ok:
        target.last_sent_at = _now()
    session.commit()
    if not ok:
        log.warning("notify: %s target %r failed: %s", target.kind, target.name, note)
    return ok, note


def dispatch(category: str, title: str, message: str, *, quiet: bool = False,
             session_factory=None) -> None:
    """Send to every enabled target that wants this category — off the
    caller's thread."""
    def work():
        from app.models import NotificationTarget
        sf = session_factory
        if sf is None:
            from app.db import SessionLocal as sf
        with sf() as s:
            for t in s.query(NotificationTarget).all():
                if wants(t, category):
                    send_to(s, t, title, message, quiet=quiet)
    if ASYNC:
        _executor.submit(_safe, work)
    else:
        work()


def _safe(fn) -> None:
    try:
        fn()
    except Exception:                                  # pragma: no cover
        log.exception("notify: dispatch failed")


def _settings_url() -> str | None:
    from app.config import settings
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/settings/notifications" if base else None
