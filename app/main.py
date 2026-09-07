"""yonder Phase 0 — FastAPI skeleton (bandit-core / finance-analysis
mold). Surface: /health, /ingest, /admin (SQLAdmin; internal-only in prod via
Traefik routing). Pipeline stages arrive in later phases.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI

import rules as rules_module
from app.config import settings
from app.db import SessionLocal, engine
from app.ingest import router as ingest_router
from app.models import RuleSetting

log = logging.getLogger("yonder")

app = FastAPI(title="Yonder", version=settings.app_version,
              docs_url=None, redoc_url=None)   # no public API docs surface
app.include_router(ingest_router)

# Auth (Yonder 4.0 Phase 1a): login/logout + the /api/v1/auth token flow, plus
# onboarding (first-run setup, register, password reset, Administer → Users).
from app.accounts_routes import router as accounts_router  # noqa: E402
from app.authn_routes import router as authn_router  # noqa: E402
from app.oidc_routes import router as oidc_router  # noqa: E402

app.include_router(authn_router)
app.include_router(accounts_router)
app.include_router(oidc_router)

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
_PUBLIC_EXACT = {"/health", "/login", "/logout", "/favicon.ico",
                 "/setup", "/register", "/forgot"}
_PUBLIC_PREFIX = ("/ingest", "/capture", "/review/static", "/trips/static",
                  "/api/v1/auth/", "/reset/", "/oidc/")
_ADMIN_PREFIX = ("/admin", "/users")


def _is_public(path: str) -> bool:
    return path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIX)


@app.middleware("http")
async def auth_gate(request, call_next):
    from urllib.parse import quote

    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

    if not settings.auth_required:
        return await call_next(request)
    path = request.url.path
    if _is_public(path):
        return await call_next(request)

    from app import authn
    with SessionLocal() as session:
        user = authn.resolve_user(session, request)
        if user is not None:
            session.expunge(user)
        no_users = user is None and authn.user_count(session) == 0
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
    request.state.user = user
    return await call_next(request)

# Review queue (D-18 / OQ-07): server-rendered, reached only via the internal
# Traefik router — the public router exposes /ingest and nothing else.
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.capture import router as capture_router  # noqa: E402
from app.dashboard import router as dashboard_router  # noqa: E402
from app.journal_web import router as journal_router  # noqa: E402
from app.review.home import router as home_router  # noqa: E402
from app.review.routes import router as review_router  # noqa: E402
from app.trips.routes import router as trips_router  # noqa: E402

app.include_router(review_router)
app.include_router(home_router)
app.include_router(capture_router)
app.include_router(trips_router)
app.include_router(journal_router)
# Root ("/") is the dashboard — registered last so it never shadows a router
# above that also owns "/" behaviour; there is none today, but this keeps the
# one true landing route obvious to find.
app.include_router(dashboard_router)
app.mount("/review/static",
          StaticFiles(directory=str(Path(__file__).parent / "review" / "static")),
          name="review-static")
app.mount("/trips/static",
          StaticFiles(directory=str(Path(__file__).parent / "trips" / "static")),
          name="trips-static")


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


@app.on_event("startup")
def startup() -> None:
    with SessionLocal() as session:
        seed_rule_settings(session)
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
    try:
        from app.admin import mount_admin
        mount_admin(app, engine)
    except Exception:                       # pragma: no cover
        log.exception("SQLAdmin failed to mount; API still serving")


@app.get("/health")
def health():
    return {"status": "ok", "version": settings.app_version,
            "rules_registered": len(rules_module.RULES)}
