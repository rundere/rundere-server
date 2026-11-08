"""Is AI on, and where may each feature send its calls? (YONDER-354, SPEC-12 D-228.)

Every LLM caller asks here and gets an `Endpoint`, or None, which means "no AI
for this feature": extraction then reads only the structured sources and sends
the rest to review, and Discover shows its deterministic findings. Nothing
below makes a network call.

D-228 (amends D-11): AI is off unless the operator turns it on. A *cloud*
endpoint (TX_AI_LOCATION=cloud) may serve Discover, whose prompts hold public
web snippets and the person's own profile constraints, but email and file
extraction stays local unless TX_AI_CLOUD_EXTRACTION=true as well: travel
emails carry full names, addresses and passenger lists.
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta, timezone

from ai import client
from ai.client import PROVIDERS, CallRecord, Endpoint
from app.config import settings

log = logging.getLogger("rundere.ai")

EXTRACTION = "extraction"       # email, file and web-page captures
DISCOVER = "discover"           # Discover research and the phrasebook generator
FEATURES = (EXTRACTION, DISCOVER)
ADMIN_TEST = "admin_test"       # the admin page's Test connection (call log only)

KEEP_CALLS_DAYS = 90            # SPEC-12 §6


def _legacy() -> bool:
    """True when this install still names its model the 4.x way: TX_AI_URL is
    unset and TX_OLLAMA_URL was set explicitly (its built-in default doesn't
    count, or every install would look configured). Read for one release."""
    return not settings.ai_url.strip() and "ollama_url" in settings.model_fields_set \
        and bool(settings.ollama_url.strip())


def configured() -> Endpoint | None:
    """The configured endpoint, whatever the feature, or None if AI is off or
    incomplete. Raises nothing: config_problems() says what's wrong."""
    if not settings.ai_enabled:
        return None
    from app import demo
    if demo.enabled():                          # D-198: a demo never calls AI
        return None
    if _legacy():
        provider, url = "ollama", settings.ollama_url.strip()
        model = settings.ai_model_chat.strip() or settings.ollama_model.strip()
    else:
        provider, url = settings.ai_provider.strip().lower(), settings.ai_url.strip()
        model = settings.ai_model_chat.strip()
    cloud = settings.ai_location.strip().lower() == "cloud"
    if provider not in PROVIDERS or not url or not model:
        return None
    if cloud and not url.lower().startswith("https://"):
        return None                             # a cloud endpoint must be https
    return Endpoint(provider=provider, url=url, model=model,
                    api_key=settings.ai_api_key.strip() if provider == "openai" else "",
                    timeout=settings.llm_timeout_seconds, cloud=cloud)


def endpoint(feature: str, user_id: str | None = None) -> Endpoint | None:
    """The endpoint `feature` may use, or None (D-228). `user_id` is whom the
    calls are for, recorded in the call log."""
    if feature not in FEATURES:
        raise ValueError(f"unknown AI feature {feature!r}")
    llm = configured()
    if llm is None:
        return None
    if feature == EXTRACTION and llm.cloud and not settings.ai_cloud_extraction:
        return None
    return dataclasses.replace(llm, feature=feature, user_id=user_id)


def extractor():
    """The LLM extractor for captures, or None when extraction has no AI
    (off, unconfigured, or a cloud endpoint without TX_AI_CLOUD_EXTRACTION).
    app/processing.py points it at each capture's owner (for_user)."""
    llm = endpoint(EXTRACTION)
    if llm is None:
        return None
    from extract.llm import LLMExtractor
    return LLMExtractor(llm)


def config_problems() -> list[str]:
    """What stops the configured AI from being used, in words an operator can act
    on. Empty when AI is off on purpose or fully set up."""
    if not settings.ai_enabled:
        return []
    problems = []
    if _legacy():
        problems.append("TX_OLLAMA_URL is deprecated: set TX_AI_PROVIDER=ollama, "
                        "TX_AI_URL and TX_AI_MODEL_CHAT instead (it stops working in 5.1)")
    else:
        provider = settings.ai_provider.strip().lower()
        if provider not in PROVIDERS:
            problems.append(f"TX_AI_PROVIDER must be ollama or openai, not {provider!r}")
        if not settings.ai_url.strip():
            problems.append("TX_AI_URL is empty")
        if not settings.ai_model_chat.strip():
            problems.append("TX_AI_MODEL_CHAT is empty")
    location = settings.ai_location.strip().lower()
    if location not in ("local", "cloud"):
        problems.append(f"TX_AI_LOCATION must be local or cloud, not {location!r}")
    url = settings.ai_url.strip() or (settings.ollama_url.strip() if _legacy() else "")
    if location == "cloud" and url and not url.lower().startswith("https://"):
        problems.append("a cloud AI endpoint must use https (TX_AI_URL)")
    if location == "cloud" and not settings.ai_cloud_extraction:
        problems.append("note: captures are read without AI because the endpoint is cloud "
                        "(set TX_AI_CLOUD_EXTRACTION=true to allow it, D-228)")
    return problems


def _shown(url: str) -> str:
    """An address for display: any user:password@ in it is dropped."""
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def describe() -> dict:
    """For the admin and health pages: on or off, and per feature where calls go.
    Never includes the key."""
    llm = configured()
    return {
        "enabled": bool(settings.ai_enabled),
        "provider": llm.provider if llm else None,
        "model": llm.model if llm else None,
        "url": _shown(llm.url) if llm else None,
        "cloud": llm.cloud if llm else False,
        "features": {f: endpoint(f) is not None for f in FEATURES},
        "problems": config_problems(),
    }


def log_startup() -> None:
    """One line at start-up saying what AI will do, plus any problem."""
    d = describe()
    if not d["enabled"]:
        log.info("AI is off (TX_AI_ENABLED): captures use the structured readers, "
                 "the rest waits in review")
    elif d["provider"]:
        log.info("AI: %s %s (%s); extraction %s, Discover %s", d["provider"], d["model"],
                 "cloud" if d["cloud"] else "local",
                 "on" if d["features"][EXTRACTION] else "off",
                 "on" if d["features"][DISCOVER] else "off")
    for p in d["problems"]:
        log.warning("AI: %s", p)


# ------------------------------------------------------------------ call log
def _record(rec: CallRecord) -> None:
    """client.observe hook: one AiCall row per call, in its own session so a
    caller's transaction is never touched. Prompts and replies are never
    stored, and the error is its class name only (SPEC-12 §6)."""
    from app.db import SessionLocal
    from app.models import AiCall
    llm = rec.endpoint
    with SessionLocal() as session:
        session.add(AiCall(feature=(llm.feature or "other")[:24], function=rec.name[:64],
                           provider=llm.provider, model=llm.model[:120],
                           started_at=rec.started_at, duration_ms=rec.duration_ms,
                           ok=rec.ok, error=(rec.error or None) and rec.error[:64],
                           prompt_tokens=rec.prompt_tokens, out_tokens=rec.out_tokens,
                           user_id=llm.user_id))
        session.commit()


client.observe(_record)


def purge_calls(session, now: datetime | None = None) -> int:
    """Delete call-log rows older than KEEP_CALLS_DAYS (the daily retention
    sweep). Returns how many went."""
    from app.models import AiCall
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=KEEP_CALLS_DAYS)
    n = session.query(AiCall).filter(AiCall.started_at < cutoff).delete(
        synchronize_session=False)
    session.commit()
    return n


def call_summary(session, limit: int = 50) -> dict:
    """For the admin page: the latest calls, and per-feature totals over the
    last 7 days."""
    from sqlalchemy import case, func

    from app.models import AiCall
    since = datetime.now(timezone.utc) - timedelta(days=7)
    rows = (session.query(AiCall.feature, func.count(), func.sum(case((AiCall.ok.is_(False), 1), else_=0)),
                          func.avg(AiCall.duration_ms))
            .filter(AiCall.started_at >= since).group_by(AiCall.feature).all())
    week = [{"feature": f, "calls": n, "failed": int(bad or 0),
             "avg_s": round((avg or 0) / 1000, 1)} for f, n, bad, avg in rows]
    recent = (session.query(AiCall).order_by(AiCall.started_at.desc()).limit(limit).all())
    return {"week": sorted(week, key=lambda w: w["feature"]), "recent": recent}


def test_connection() -> tuple[bool, str]:
    """The admin page's Test connection: list the models, then one tiny JSON
    call, which lands in the call log as admin_test. Returns (ok, a sentence);
    errors are named by class and HTTP status only."""
    import urllib.error
    llm = configured()
    if llm is None:
        return False, "AI is off or not fully configured, so there's nothing to test."
    llm = dataclasses.replace(llm, feature=ADMIN_TEST)
    try:
        client.list_models(llm)
        reply = client.chat_json(llm, [{"role": "user", "content":
                                        'Reply with the JSON object {"ok": true}.'}],
                                 {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                                 num_ctx=2048, name="connection_test")
    except urllib.error.HTTPError as ex:
        return False, f"The endpoint answered HTTP {ex.code}."
    except Exception as ex:
        return False, f"The call failed ({type(ex).__name__})."
    if '"ok"' not in reply.content:
        return False, "The endpoint answered, but not with the JSON asked for."
    return True, f"{llm.model} answered."
