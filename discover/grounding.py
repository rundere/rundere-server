"""LLM grounding (SPEC-3.2 §4, D-66/D-90).

Two mechanical checks, applied to every LLM resolver's output between the Ollama
response and Finding assembly. Both are pure set/count logic — no judgment:

1. SOURCE GROUNDING — drop any item whose source_url was NOT in that resolver
   call's SearXNG result set. Kills invented URLs and fabricated claims. URLs are
   normalised first (scheme/host lowercased, trailing slash and fragment dropped)
   so a trivially reformatted-but-real URL is not falsely dropped; that
   normalisation is deterministic, not a judgment call.

2. CORROBORATION (visa + safety only) — a claim needs >= 2 distinct source
   DOMAINS or it renders under a visible "verify at official source" banner. This
   is counting distinct domains, nothing more.

Nothing here reaches the network; callers pass the SearXNG URLs they already
have. Dropped items are returned too, for the run's transparency log.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field


def normalize_url(url: str) -> str:
    """Canonical form for membership comparison: lowercase scheme+host, no
    fragment, no trailing slash on the path. Deterministic, dependency-free."""
    try:
        p = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return ""
    if not p.netloc:
        return ""
    path = p.path.rstrip("/")
    base = f"{p.scheme.lower()}://{p.netloc.lower()}{path}"
    return f"{base}?{p.query}" if p.query else base


def domain_of(url: str) -> str:
    """Host of a URL, lowercased, 'www.' and any :port stripped. Not a full
    public-suffix parse (no dependency) — enough to tell one source site from
    another for corroboration counting."""
    try:
        host = urllib.parse.urlsplit((url or "").strip()).netloc.lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]      # drop userinfo + port
    return host[4:] if host.startswith("www.") else host


@dataclass
class GroundingResult:
    kept: list = field(default_factory=list)      # source_url was in the result set
    dropped: list = field(default_factory=list)   # ungrounded — for the transparency log


def ground_items(items, allowed_urls, *, url_key: str = "source_url") -> GroundingResult:
    """Keep only items whose url_key (normalised) is among allowed_urls (the
    SearXNG result set). Items with no/empty/unknown url are dropped."""
    allowed = {normalize_url(u) for u in (allowed_urls or []) if u}
    allowed.discard("")
    res = GroundingResult()
    for it in (items or []):
        raw = it.get(url_key) if isinstance(it, dict) else None
        norm = normalize_url(raw or "")
        (res.kept if norm and norm in allowed else res.dropped).append(it)
    return res


@dataclass
class Corroboration:
    corroborated: bool
    domains: list[str]                            # distinct domains backing the claim


def corroborate(source_urls, *, min_domains: int = 2) -> Corroboration:
    """Count distinct source domains. corroborated iff >= min_domains (D-66)."""
    domains: list[str] = []
    seen: set[str] = set()
    for u in (source_urls or []):
        d = domain_of(u)
        if d and d not in seen:
            seen.add(d)
            domains.append(d)
    return Corroboration(corroborated=len(domains) >= min_domains, domains=domains)


VERIFY_BANNER = ("> ⚠️ **Verify at official source** — only one source backs this; "
                 "confirm before you rely on it.")


def verify_banner(corr: Corroboration) -> str:
    """The markdown banner an under-corroborated visa/safety claim renders under,
    or '' when the claim clears the corroboration bar."""
    return "" if corr.corroborated else VERIFY_BANNER
