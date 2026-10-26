"""Demo mode: a read-only showcase server (YONDER-181, SPEC-8 D-198).

`TX_DEMO_MODE=true` turns a Run·dere server into a public demo. App Review,
TestFlight testers and anyone curious sign in with one shared login
(`TX_DEMO_USERNAME` / `TX_DEMO_PASSWORD`) and look around pre-seeded trips.
Nothing they do changes anything, and nothing reaches the outside world:

- **Read-only.** Every write is refused (`demo_guard` in app/main.py), except
  signing in and out.
  `scope.can_edit_trip` answers False, so every edit check (web 403s, the API's
  `can_edit`, hidden controls) follows from one place. The demo user still owns
  the seeded trips, so `my_role` is `owner` and every tab shows (D-199's viewer
  is timeline-and-map only, the wrong thing for a showcase).
- **Closed doors.** No MCP, OAuth, OIDC, registration, password reset, ingest
  or admin. Sign-in is a plain username and password.
- **No request log.** uvicorn's access log (each request with the visitor's IP)
  is switched off (YONDER-277).
- **No background work.** The inbox poller, retention sweep and health monitor
  don't start. The seeded trips are kept current by the nightly roll-forward
  (`app.demo_seed`), which writes to the database directly.
- **Startup guards.** Refuse to start on a database holding any user other
  than the demo user (a real server must never be flipped into demo mode),
  without sign-in, or with mail or SSO configured. The self-hosted services
  (Ollama, Photon, SearXNG, ClamAV, AdventureLog, AirTrail, Valhalla) are
  switched off.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging

from app.config import settings

log = logging.getLogger("rundere.demo")

READ_ONLY_MESSAGE = "Demo is read-only: the sample trips can be viewed but not changed."

# Writes the demo still accepts: signing in and out (web and API).
_WRITE_ALLOWED = {"/login", "/logout", "/api/v1/auth/login", "/api/v1/auth/refresh",
                  "/api/v1/auth/logout"}
# Gone entirely on a demo server, whatever the method (404).
_CLOSED_EXACT = {"/mcp", "/setup", "/register", "/forgot"}
_CLOSED_PREFIX = ("/oauth/", "/oidc/", "/.well-known/oauth", "/ingest", "/reset/",
                  "/admin", "/users", "/settings/integrations", "/settings/mail",
                  "/settings/notifications", "/account/tokens", "/account/connected-apps")
_READ_METHODS = ("GET", "HEAD", "OPTIONS")


# True only inside `seeding()`: the seed and the nightly roll writing the sample
# data through the app's own functions. Never set by a request.
_seeding: contextvars.ContextVar[bool] = contextvars.ContextVar("demo_seeding", default=False)


def enabled() -> bool:
    return bool(settings.demo_mode)


def read_only() -> bool:
    """Is editing off right now? On a demo server, yes, except while the seed
    itself is writing (D-198)."""
    return enabled() and not _seeding.get()


@contextlib.contextmanager
def seeding():
    """Let the demo's own seed and roll write through the app's validated paths
    (save_research_finding, expenses, journal, …). Requests never pass through
    here; `demo_guard` refuses their writes before any of this runs."""
    token = _seeding.set(True)
    try:
        yield
    finally:
        _seeding.reset(token)


def is_closed(path: str) -> bool:
    return path in _CLOSED_EXACT or path.startswith(_CLOSED_PREFIX)


def refuses(method: str, path: str) -> bool:
    """True when demo mode turns this request away as a write."""
    return method.upper() not in _READ_METHODS and path not in _WRITE_ALLOWED


class DemoMisconfigured(RuntimeError):
    """Demo mode was asked for on a server that isn't safe to run as a demo."""


def check_startup(session) -> None:
    """Refuse to start a demo server that could expose or change real data, then
    make sure the demo login exists with the configured password. Raises
    DemoMisconfigured; the app does not start."""
    from app.models import User
    from app import oidc
    problems = []
    if not settings.auth_required:
        problems.append("TX_AUTH_REQUIRED must be true (the demo is signed in)")
    if not settings.demo_username.strip() or not settings.demo_password:
        problems.append("set TX_DEMO_USERNAME and TX_DEMO_PASSWORD")
    if settings.smtp_host or settings.inbox_imap_host:
        problems.append("mail must be off (unset TX_SMTP_HOST and TX_INBOX_IMAP_HOST)")
    if oidc.enabled():
        problems.append("single sign-on must be off (TX_OIDC_ENABLED=false)")
    others = (session.query(User.username)
              .filter(User.username != settings.demo_username.strip()).limit(3).all())
    if others:
        # Never name them in full: this is a log line on a public server.
        problems.append(f"the database has {len(others)}+ other user(s); a demo server "
                        "needs its own database")
    if problems:
        raise DemoMisconfigured("demo mode refused: " + "; ".join(problems))
    _cut_home_services()
    _quiet_request_log()
    ensure_demo_user(session)
    log.warning("DEMO MODE: read-only showcase server; every write is refused")


# The self-hosted services a demo must never reach (D-198): they live on the
# owner's network, which the demo's firewall blocks anyway. Blanking them here
# makes every lookup fail at once instead of waiting out a timeout, and doesn't
# rely on the compose file remembering to.
_HOME_SERVICES = ("ollama_url", "photon_url", "searxng_url", "clamav_url",
                  "adventurelog_url", "airtrail_url", "valhalla_url")


def _quiet_request_log() -> None:
    """A demo server writes no per-request log (YONDER-277). uvicorn's access
    line carries the visitor's real IP (the image runs `--proxy-headers`), and
    nobody can change anything on the demo, so there's nothing to debug from it.
    Warnings and errors still log. Traffic is counted by country and region,
    without IPs, in the proxy's pipeline instead (the privacy page says so)."""
    logging.getLogger("uvicorn.access").disabled = True


def _cut_home_services() -> None:
    for name in _HOME_SERVICES:
        if getattr(settings, name, ""):
            setattr(settings, name, "")


def ensure_demo_user(session):
    """The shared demo login: created if missing, its password kept in step with
    TX_DEMO_PASSWORD, never an admin, never asked the sharing question."""
    from app import authn
    from app.models import User
    name = settings.demo_username.strip()
    u = session.query(User).filter_by(username=name).one_or_none()
    if u is None:
        u = User(username=name, auth_source="local", share_with_admins=False)   # D-220
        session.add(u)
    if not u.password_hash or not authn.verify_password(settings.demo_password, u.password_hash):
        u.password_hash = authn.hash_password(settings.demo_password)
    u.is_admin, u.is_active, u.sharing_prompt = False, True, False
    session.commit()
    return u


def refusal(request):
    """The reply to a refused write: a 403 (JSON for the API, a plain page for a
    browser form), or for HTMX a message shown in the demo banner."""
    from fastapi.responses import HTMLResponse, JSONResponse
    path = request.url.path
    if path.startswith("/api/"):
        return JSONResponse({"detail": READ_ONLY_MESSAGE, "demo": True}, status_code=403)
    if request.headers.get("hx-request"):
        # htmx 2 doesn't swap error responses, so this one answers 200 and
        # retargets the message into the banner: nothing changed, and the person
        # sees why.
        return HTMLResponse(f'<p class="yn-demo-refusal" role="status">{READ_ONLY_MESSAGE}</p>',
                            headers={"HX-Retarget": "#demo-msg", "HX-Reswap": "innerHTML"})
    return HTMLResponse(f"<h1>Demo server</h1><p>{READ_ONLY_MESSAGE}</p>"
                        '<p><a href="/">Back to the trips</a></p>', status_code=403)
