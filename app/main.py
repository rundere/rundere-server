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

# Review queue (D-18 / OQ-07): server-rendered, reached only via the internal
# Traefik router — the public router exposes /ingest and nothing else.
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.capture import router as capture_router  # noqa: E402
from app.review.home import router as home_router  # noqa: E402
from app.review.routes import router as review_router  # noqa: E402
from app.trips.routes import router as trips_router  # noqa: E402

app.include_router(review_router)
app.include_router(home_router)
app.include_router(capture_router)
app.include_router(trips_router)
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
