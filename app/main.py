"""yonder Phase 0 — FastAPI skeleton (bandit-core / finance-analysis
mold). Surface: /health, /ingest, /admin (SQLAdmin; internal-only in prod via
Traefik routing). Pipeline stages arrive in later phases.
"""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI

import rules as rules_module
from app.config import settings
from app.db import SessionLocal, engine
from app.ingest import router as ingest_router
from app.models import RuleSetting

log = logging.getLogger("yonder")



@contextlib.asynccontextmanager
async def lifespan(_app):
    """Startup seeding, then the remote MCP server's session manager (YONDER-108)
    for the app's lifetime. (A lifespan replaces @app.on_event: FastAPI ignores
    on_event handlers once one is set.)"""
    startup()
    from app import mcp_server
    async with mcp_server.lifespan():
        yield


# No PUBLIC schema or docs: the /api/v1 contract is served behind auth at
# /api/v1/openapi.json and /api/v1/docs (YONDER-58, app/api_v1.py).
app = FastAPI(title="Run·dere", version=settings.app_version,
              docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)
app.include_router(ingest_router)

# Auth (Yonder 4.0 Phase 1a): login/logout + the /api/v1/auth token flow, plus
# onboarding (first-run setup, register, password reset, Administer → Users).
from app.accounts_routes import router as accounts_router  # noqa: E402
from app.authn_routes import router as authn_router  # noqa: E402
from app.oauth_routes import router as oauth_router  # noqa: E402
from app.oidc_routes import router as oidc_router  # noqa: E402
from app.settings_routes import router as settings_router  # noqa: E402

app.include_router(authn_router)
app.include_router(accounts_router)
app.include_router(settings_router)
app.include_router(oidc_router)
app.include_router(oauth_router)

# The JSON API (Phase 3). Every /api/v1/* route (except /api/v1/auth/*) requires
# a bearer access token via Depends(authn.current_user) — always authenticated,
# independent of TX_AUTH_REQUIRED — and is user-scoped through app/scope.py.
from app.api_v1 import router as api_router  # noqa: E402

app.include_router(api_router)


# --- Auth gate (SPEC-4 Workstream B) --------------------------------------
# Master switch is TX_AUTH_REQUIRED. OFF => the app behaves exactly as it did
# pre-4.0 (LAN-only, no login), so existing deployments are untouched until the
# operator creates a user and opts in. ON => every human-UI and /api route
# needs an authenticated user; /admin additionally requires is_admin. /ingest,
# /capture (self-authenticated), static, and the login/token endpoints stay open.
# Onboarding endpoints (setup/register/reset) are public; /users is admin-only.
# YONDER-107: the OAuth metadata/token/register/revoke endpoints are public
# (they authenticate clients themselves); /oauth/authorize resolves the signed-in
# user itself so it can send them to /login with the WHOLE request as `next`.
# YONDER-108: /mcp answers its own 401 (with the OAuth resource-metadata pointer).
# YONDER-278 (D-202): /about is public, so a visitor who lands on a server (the demo
# included) can find out what it is, how to say it and how to get the app.
_PUBLIC_EXACT = {"/health", "/login", "/logout", "/favicon.ico",
                 "/setup", "/register", "/forgot", "/mcp", "/about"}
_PUBLIC_PREFIX = ("/ingest", "/capture", "/review/static", "/trips/static", "/brand/",
                  "/api/v1/auth/", "/reset/", "/oidc/", "/oauth/", "/.well-known/")
_ADMIN_PREFIX = ("/admin", "/users")


def _is_public(path: str) -> bool:
    return path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIX)


def _resolve_for_gate(request):
    """(user or None, no users exist yet) for the auth gate. Sync: call it off
    the event loop."""
    from app import authn
    with SessionLocal() as session:
        user = authn.resolve_user(session, request)
        if user is not None:
            session.expunge(user)
        no_users = user is None and authn.user_count(session) == 0
    return user, no_users


@app.middleware("http")
async def auth_gate(request, call_next):
    from urllib.parse import quote

    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

    if not settings.auth_required:
        return await call_next(request)
    path = request.url.path
    if _is_public(path):
        return await call_next(request)

    from starlette.concurrency import run_in_threadpool

    # YONDER-178: the lookup is blocking DB I/O — run it in a worker thread.
    # Done on the event loop, a drained pool froze the loop for the whole
    # checkout timeout, which also stopped finished requests from returning
    # their connections: the server never recovered, /health included.
    user, no_users = await run_in_threadpool(_resolve_for_gate, request)
    is_api = path.startswith("/api/")
    if user is None:
        # First-run: nobody exists yet → send humans to create the first admin.
        if no_users and not is_api:
            return RedirectResponse("/setup", status_code=303)
        if is_api:
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
    if path.startswith(_ADMIN_PREFIX) and not user.is_admin:
        msg = "admin only"
        return (JSONResponse({"detail": msg}, status_code=403) if is_api
                else HTMLResponse(f"<h1>403 — {msg}</h1>", status_code=403))
    # 4.1: a self-registered/SSO-provisioned user first chooses whether to share
    # their data with admins. Browser only — the JSON API is never redirected.
    if (not is_api and getattr(user, "sharing_prompt", False)
            and not path.startswith(("/account/sharing", "/logout"))):
        return RedirectResponse(f"/account/sharing?next={quote(path)}", status_code=303)
    request.state.user = user
    return await call_next(request)

# --- Cross-site request guard (YONDER-293) -------------------------------
# The session cookie is SameSite=Lax, which stops other SITES from riding on it
# but not another host on the same site (any app on a sibling subdomain of a
# self-hoster's domain). A browser names the page a request came from in Origin,
# so a state-changing request whose Origin isn't this server is refused. A
# request with no Origin (scripts, the iOS app, add-ons) or with its own
# Authorization header carries no ambient credential to abuse, and the machine
# endpoints (/ingest, /mcp, /oauth/token, …) authenticate every call themselves.
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _foreign_origin(request) -> bool:
    from urllib.parse import urlsplit

    from app.authn import is_machine_path
    if request.method not in _UNSAFE_METHODS:
        return False
    path = request.url.path
    if path.startswith("/ingest") or is_machine_path(path):
        return False
    if request.headers.get("authorization"):
        return False
    origin = request.headers.get("origin")
    if not origin:
        return False
    if origin == "null":                  # sandboxed frames, opaque redirects
        return True
    own = {request.headers.get("host", "").lower()}
    if settings.public_base_url:
        own.add(urlsplit(settings.public_base_url).netloc.lower())
    return urlsplit(origin).netloc.lower() not in own


@app.middleware("http")
async def same_origin_writes(request, call_next):
    if _foreign_origin(request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
    return await call_next(request)


# Requests currently inside the app (YONDER-178). Only touched on the event
# loop, so a plain int needs no lock.
_inflight = 0
_SHED_EXEMPT_PREFIX = ("/review/static", "/trips/static", "/brand/")


@app.middleware("http")
async def shed_load(request, call_next):
    """Answer 503 straight away once too many requests are in flight, rather
    than queueing work nobody may be waiting for. /health and static files are
    exempt. Registered after auth_gate, so it runs first (outermost)."""
    global _inflight
    limit = settings.max_inflight_requests
    path = request.url.path
    if not limit or path == "/health" or path.startswith(_SHED_EXEMPT_PREFIX):
        return await call_next(request)
    if _inflight >= limit:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "server busy, retry shortly"}, status_code=503,
                            headers={"Retry-After": "2"})
    _inflight += 1
    try:
        return await call_next(request)
    finally:
        _inflight -= 1


@app.middleware("http")
async def demo_guard(request, call_next):
    """D-198 (YONDER-181): on a demo server, closed surfaces (MCP, OAuth, SSO,
    registration, admin, …) answer 404 and every write other than signing in or
    out is refused. Registered after shed_load, so it runs first (outermost):
    nothing behind it ever sees a demo write."""
    from app import demo
    if not demo.enabled():
        return await call_next(request)
    path = request.url.path
    if demo.is_closed(path):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "not available on the demo server"}, status_code=404)
    if demo.refuses(request.method, path):
        return demo.refusal(request)
    return await call_next(request)


# Review queue (D-18 / OQ-07): server-rendered, reached only via the internal
# Traefik router — the public router exposes /ingest and nothing else.
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.capture import router as capture_router  # noqa: E402
from app.dashboard import router as dashboard_router  # noqa: E402
from app.journal_web import router as journal_router  # noqa: E402
from app.places_web import router as places_router  # noqa: E402
from app.review.home import router as home_router  # noqa: E402
from app.review.routes import inbox_router  # noqa: E402
from app.review.routes import router as review_router  # noqa: E402
from app.trips.routes import router as trips_router  # noqa: E402
from app.trips.costs import router as costs_router  # noqa: E402

app.include_router(review_router)
app.include_router(inbox_router)          # D-200: /inbox
app.include_router(home_router)
app.include_router(capture_router)
app.include_router(trips_router)
app.include_router(costs_router)
app.include_router(journal_router)
app.include_router(places_router)
# Root ("/") is the dashboard — registered last so it never shadows a router
# above that also owns "/" behaviour; there is none today, but this keeps the
# one true landing route obvious to find.
app.include_router(dashboard_router)
# Remote MCP (YONDER-108): MCP Streamable HTTP at exactly /mcp (a Mount would
# 307 /mcp → /mcp/, which connectors don't follow for POST). Bearer-only.
from starlette.routing import Route  # noqa: E402

from app import mcp_server  # noqa: E402

app.router.routes.append(Route("/mcp", endpoint=mcp_server.endpoint,
                               methods=["GET", "POST", "DELETE"]))
app.mount("/review/static",
          StaticFiles(directory=str(Path(__file__).parent / "review" / "static")),
          name="review-static")
app.mount("/trips/static",
          StaticFiles(directory=str(Path(__file__).parent / "trips" / "static")),
          name="trips-static")

@app.middleware("http")
async def _revalidate_static(request, call_next):
    """Static files revalidate (ETag) on every load instead of living on heuristic
    freshness, so a deploy reaches browsers even where a page links an unversioned URL."""
    resp = await call_next(request)
    if request.url.path.startswith(("/review/static/", "/trips/static/", "/brand/tokens/", "/brand/fonts/")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# Brand assets straight from the design system (design/, PR #77): logos and
# icons are served as-is so the app never carries its own copies.
_DESIGN = Path(__file__).parent.parent / "design"
if (_DESIGN / "logo").is_dir():
    app.mount("/brand/logo", StaticFiles(directory=str(_DESIGN / "logo")), name="brand-logo")
    app.mount("/brand/icons", StaticFiles(directory=str(_DESIGN / "icons")), name="brand-icons")
if (_DESIGN / "tokens").is_dir():     # D-161: the design tokens are the palette's source
    app.mount("/brand/tokens", StaticFiles(directory=str(_DESIGN / "tokens")), name="brand-tokens")
if (_DESIGN / "fonts").is_dir():      # self-hosted brand fonts (no third-party CDN)
    app.mount("/brand/fonts", StaticFiles(directory=str(_DESIGN / "fonts")), name="brand-fonts")
if (_DESIGN / "audio").is_dir():
    app.mount("/brand/audio", StaticFiles(directory=str(_DESIGN / "audio")), name="brand-audio")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import FileResponse, Response
    ico = _DESIGN / "icons" / "favicon.ico"
    return FileResponse(ico, media_type="image/x-icon") if ico.is_file() else Response(status_code=404)


def seed_rule_settings(session) -> None:
    """D-25: registry is truth for WHICH rules exist; DB is truth for MODE.
    New rules appear as enforce; critical rules not enforcing log loudly."""
    existing = {r.rule_id: r for r in session.query(RuleSetting).all()}
    for rule_id, rule in rules_module.RULES.items():
        if rule_id not in existing:
            session.add(RuleSetting(rule_id=rule_id, mode="enforce"))
    stale = set(existing) - set(rules_module.RULES)
    for rule_id in stale:
        log.warning("rule_settings row %s has no registry entry (renamed rule?)",
                    rule_id)
    session.commit()
    for rule_id, row in existing.items():
        if rule_id in rules_module.RULES and rules_module.RULES[rule_id].critical \
                and row.mode != "enforce":
            log.warning("CRITICAL rule %s is %s — this rule encodes a past "
                        "real-world failure (see RULES.md)", rule_id, row.mode)


def load_enablement(session) -> dict[str, str]:
    """The map rules.check_item/check_batch consume."""
    return {r.rule_id: r.mode for r in session.query(RuleSetting).all()
            if r.rule_id in rules_module.RULES}


def ownerless_counts(session) -> dict[str, int]:
    """Rows with no owner, per table. With sign-in on these are drift (only
    admins can see them — YONDER-186); with it off they're normal."""
    from app.models import IngestBatch, JournalEntry, TravelItem, Trip
    return {name: n for name, n in (
        ("trips", session.query(Trip).filter(Trip.owner_id.is_(None)).count()),
        ("captures", session.query(TravelItem).filter(TravelItem.owner_id.is_(None)).count()),
        ("batches", session.query(IngestBatch).filter(IngestBatch.user_id.is_(None)).count()),
        ("journal", session.query(JournalEntry).filter(JournalEntry.owner_id.is_(None)).count()),
    ) if n}


def _warn_risky_auth_config() -> None:
    """Say so at start-up when the auth settings leave a server exposed
    (YONDER-293). Warnings, not refusals: a LAN-only install may mean it."""
    if not settings.auth_required:
        log.warning("TX_AUTH_REQUIRED is off: anyone who can reach this server can use it "
                    "without signing in. Turn it on for any server others can reach.")
        return
    if not settings.jwt_secret:
        log.error("TX_JWT_SECRET is unset: sessions use a key that changes on every restart. "
                  "Set it (openssl rand -base64 32).")
    if not settings.public_base_url:
        log.warning("TX_PUBLIC_BASE_URL is unset: password-reset links are built from the "
                    "request's Host header. Set it to this server's public address.")


def startup() -> None:
    from app import demo
    with SessionLocal() as session:
        if demo.enabled():
            demo.check_startup(session)     # D-198: raises, and the app doesn't start
        seed_rule_settings(session)
        _warn_risky_auth_config()
        if settings.auth_required:
            try:
                drift = ownerless_counts(session)
                if drift:
                    log.warning("ownerless rows with sign-in on (visible to admins only; "
                                "assign an owner): %s", drift)
            except Exception:               # pragma: no cover
                log.exception("ownerless-row check failed")
        try:
            # SPEC-8 D-150: move the deprecated server-wide AirTrail/AdventureLog
            # settings into the deployment owner's own connections, once.
            from app.integrations import seed_from_env
            seed_from_env(session)
        except Exception:                   # pragma: no cover
            log.exception("connection migration failed; integrations stay as they were")
        try:
            # D-194: the iOS app's built-in OAuth client, on every server.
            from app.oauth import ensure_app_client
            ensure_app_client(session)
        except Exception:                   # pragma: no cover
            log.exception("app OAuth client seed failed; the app can still use passwords")
        try:
            from planning.catalog import seed_catalog
            seed_catalog(session)
        except Exception:                   # pragma: no cover
            log.exception("packing catalog seed failed; Planning may be empty")
        try:
            from discover import langdata
            langdata.seed_phrasebook_rows(session)
            langdata.seed_regional_rows(session)
        except Exception:                   # pragma: no cover
            log.exception("language data seed failed; discovery uses JSON fallback")
    if demo.enabled():
        # D-198: no background work on a demo server (no mail, no purges, no
        # alerts). Only the nightly roll-forward of the sample trips runs.
        _start_demo_jobs()
        _mount_admin()
        return
    try:
        # Email-forward capture (D-190): poll the admin's mailbox, if configured.
        from app import inbox
        inbox.start(SessionLocal)
    except Exception:                       # pragma: no cover
        log.exception("inbox poller failed to start; email forward is off")
    try:
        # SPEC-8 D-196: the daily retention sweep (files, raw captures, mailbox).
        from app import retention
        retention.start(SessionLocal)
    except Exception:                       # pragma: no cover
        log.exception("retention job failed to start; nothing is removed until it does")
    try:
        # YONDER-253 (D-197): the health monitor behind admin notifications.
        from app.notify import monitor
        monitor.start()
    except Exception:                       # pragma: no cover
        log.exception("health monitor failed to start; admins get no alerts until it does")
    _mount_admin()


def _mount_admin() -> None:
    try:
        from app.admin import mount_admin
        mount_admin(app, engine)
    except Exception:                       # pragma: no cover
        log.exception("SQLAdmin failed to mount; API still serving")


def _start_demo_jobs() -> None:
    """D-198: seed the sample trips if they're missing, then keep them current
    (app/demo_seed.py: at startup, then 03:00 daily)."""
    from app import demo_seed
    demo_seed.start(SessionLocal)


@app.get("/health")
async def health():
    # async + no DB on purpose (YONDER-178): answers on the event loop, so it
    # stays up even when every worker thread and pooled connection is busy.
    return {"status": "ok", "version": settings.app_version,
            "rules_registered": len(rules_module.RULES)}
