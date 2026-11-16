"""One-time data steps a server runs after an upgrade, in the background.

Each step runs once per server: when it finishes, a `server_settings` row
(`upgrade:<name>` = "done", D-208) records it, so a restart doesn't repeat it.
A step that fails is logged and tried again at the next start. A step that
can't finish yet for want of an optional service (D-146) raises `NotYet`: it is
not recorded and is tried again at the next start, with an info line instead of
a traceback. Steps must be safe to re-run, because a restart mid-step runs it
again from the top.

- `locate-addresses` (SPEC-8 §13, D-233): map pins made before 5.0 kept no
  address, so the trip PDF had none to print for airports and places found by
  name. This is `locate-activities` once, which redoes only those pins. It is
  recorded only once a geocoder (Photon) has answered, or when there was
  nothing to locate: with Photon unset, starting or down nothing can be found,
  and "done" would leave old timeline items without pins for good. The
  operator can also run `python -m app.cli locate-activities`.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("rundere.upgrade")

class NotYet(Exception):
    """Raised by a step that can't finish until something it depends on is
    available (a geocoder, D-146). `run()` leaves it unrecorded and retries at
    the next start, quietly: a server that never configures the service must
    not log an error on every start."""


_FIRST_DELAY = 2 * 60                    # let startup settle first, like retention
_stop = threading.Event()
_thread: threading.Thread | None = None


def _locate_addresses(session) -> str:
    from app.trips.geo import backfill, geocoder_state, needs_locating
    # Ask first: with no geocoder answering a run gains nothing, and it would
    # only rescan every activity at each start (D-146). Nothing to locate is
    # done either way.
    todo = needs_locating(session)
    if not todo:
        return "nothing to locate"
    if (state := geocoder_state()) != "ok":
        raise NotYet(f"geocoder {state}; {todo} to locate")
    counts = backfill(session)
    # D-233 / D-146: backfill never raises and a down Photon looks like "not
    # found", so activities left unlocated are told apart here. Photon unset or
    # unreachable -> try again at the next start; Photon answering -> what is
    # left is genuinely unfindable and the step is done.
    if counts.get("unlocated") or counts.get("partial"):
        state = geocoder_state()
        if state != "ok":
            raise NotYet(f"geocoder {state}; {counts.get('unlocated', 0)} unlocated, "
                         f"{counts.get('partial', 0)} partly located")
    return ", ".join(f"{k}={v}" for k, v in counts.items())


STEPS = (("locate-addresses", _locate_addresses),)


def _key(name: str) -> str:
    return f"upgrade:{name}"


def pending(session) -> list[str]:
    from app.models import ServerSetting
    return [name for name, _ in STEPS if session.get(ServerSetting, _key(name)) is None]


def run(session_factory) -> dict[str, str]:
    """Run every step not yet done; returns {name: outcome}."""
    from app.models import ServerSetting
    out = {}
    for name, step in STEPS:
        if _stop.is_set():
            break
        with session_factory() as session:
            if session.get(ServerSetting, _key(name)) is not None:
                continue
            try:
                summary = step(session)
            except NotYet as why:                # quiet retry at the next start
                session.rollback()
                log.info("upgrade step %s isn't possible yet (%s); it runs again "
                         "at the next start", name, why)
                out[name] = f"not yet ({why})"
                continue
            except Exception as ex:              # retried at the next start
                session.rollback()
                log.exception("upgrade step %s failed; it runs again at the next start", name)
                out[name] = f"failed ({type(ex).__name__})"
                continue
            session.add(ServerSetting(key=_key(name), value="done"))
            session.commit()
            log.info("upgrade step %s done: %s", name, summary)
            out[name] = summary
    return out


def _loop(session_factory) -> None:
    if _stop.wait(_FIRST_DELAY):
        return
    try:
        run(session_factory)
    except Exception:                            # pragma: no cover - never kill the app
        log.exception("upgrade steps failed")


def start(session_factory) -> bool:
    """Start the steps in the background if any are pending (once per process)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    with session_factory() as session:
        if not pending(session):
            return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(session_factory,),
                               name="upgrade", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()
