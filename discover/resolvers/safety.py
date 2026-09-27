"""Safety & transport resolver (SPEC-3.2 §3.4).

Scope: city. Confidence: medium. Safety claims are corroborated (≥2 distinct
source domains, D-66) — an under-corroborated safety block renders under a visible
verify banner. Transport is the priority: which apps to install, what public
transit is like, ride-hailing options, and the airport transfer.

Visa & entry (§3.3) is deliberately NOT built: entry rules are high-liability and
change constantly — they belong to official government sources, not Yonder.
"""
from __future__ import annotations

import logging

from discover.grounding import (corroborate, ground_items, normalize_url,
                                verify_banner)
from discover.places import DerivedPlace
from discover.resolvers.base import Finding, ResolverContext
from discover.search import searxng_search
from discover.synth import DEFAULT_MODEL, synthesize_json

log = logging.getLogger("yonder.discovery.llm")

_SCHEMA = {
    "type": "object",
    "properties": {
        "safety": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "advisories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"},
                                       "source_url": {"type": "string"}},
                        "required": ["text", "source_url"],
                    },
                },
                "scams": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "advisories", "scams"],
        },
        "transport": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "public_transit": {"type": "string"},
                "ride_hailing": {"type": "array", "items": {"type": "string"}},
                "apps": {"type": "array", "items": {"type": "string"}},
                "from_airport": {"type": ["string", "null"]},
                "tips": {"type": "array", "items": {"type": "string"}},
                "source_urls": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "public_transit", "ride_hailing", "apps",
                         "tips", "source_urls"],
        },
    },
    "required": ["safety", "transport"],
}

_SYSTEM = (
    "You are a practical travel safety & transport guide. Use ONLY the search "
    "results — never invent a claim or a URL, and copy each source_url exactly. "
    "SAFETY: a brief, factual, non-alarmist summary; concrete advisories (each with "
    "its source_url); the common scams to watch for. Do not overstate danger or "
    "give legal/entry advice. TRANSPORT is the priority — be specific and useful: "
    "what public transit exists and HOW to pay for it (a card? an app?); which "
    "ride-hailing apps ACTUALLY operate in THIS city — only list ones that really "
    "work here (in most of the US that is just Uber and Lyft; Bolt/Grab/Free Now are "
    "elsewhere — do not list an app from another region unless the results say it "
    "works here); the apps a traveller should install (transit + payment + ride); "
    "and how to get from the airport into the city. Name the actual apps, lines, and "
    "providers, not generic advice.")


class SafetyResolver:
    topic = "safety"
    scope = "city"

    def __init__(self, *, searxng_url: str | None, ollama_url: str | None,
                 model: str = DEFAULT_MODEL, timeout: int = 600,
                 max_snippets: int = 24):
        self.searxng_url = searxng_url
        self.ollama_url = ollama_url
        self.model = model
        self.timeout = timeout
        self.max_snippets = max_snippets

    # -- query construction -------------------------------------------------
    def _queries(self, city: str, country: str | None, year: str) -> list[str]:
        c = country or ""
        yr = f" {year}" if year else ""
        return [
            f"travel safety {c}{yr}".strip(),
            f"scams to avoid {city}",
            f"getting around {city} public transit",
            f"ride hailing apps {city}",
            f"{city} airport to city center transport",
            f"best transport apps {city}",
        ]

    def _search(self, queries: list[str]):
        seen: set[str] = set()
        results = []
        for q in queries:
            for r in searxng_search(q, base_url=self.searxng_url, timeout=self.timeout):
                key = normalize_url(r.url)
                if key and key not in seen:
                    seen.add(key)
                    results.append(r)
                    if len(results) >= self.max_snippets:
                        return results
        return results

    def _prompt(self, place: DerivedPlace, results) -> str:
        lines = [f"City: {place.label}", "",
                 "Search results (your ONLY source of truth):"]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] {r.title} — {r.url}")
            if r.snippet:
                lines.append(f"    {r.snippet}")
        lines += ["", "Return JSON with safety{} and transport{}. Every safety "
                  "advisory and every transport source_url must be copied exactly "
                  "from the list above. Prioritise concrete transport specifics — "
                  "name the apps, cards, lines, and airport options."]
        return "\n".join(lines)

    # -- resolve ------------------------------------------------------------
    def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None:
        if not self.searxng_url or not self.ollama_url:
            return None
        if not place.city:
            return None
        year = (ctx.today or "")[:4]
        results = self._search(self._queries(place.city, place.country, year))
        if not results:
            log.warning("safety: no SearXNG results for %s — is SearXNG reachable at "
                        "%s with JSON format enabled?", place.label, self.searxng_url)
            return None
        out = synthesize_json(self._prompt(place, results), _SCHEMA,
                              base_url=self.ollama_url, system=_SYSTEM,
                              model=self.model, timeout=self.timeout)
        if not out:
            log.warning("safety: Ollama returned nothing for %s — is Ollama reachable "
                        "at %s and is %s pulled?", place.label, self.ollama_url, self.model)
            return None

        allowed = {normalize_url(r.url) for r in results}
        safety = out.get("safety") or {}
        transport = out.get("transport") or {}
        advisories = ground_items(safety.get("advisories") or [],
                                  [r.url for r in results]).kept
        corr = corroborate([a.get("source_url") for a in advisories])
        scams = [s for s in (safety.get("scams") or [])
                 if isinstance(s, str) and s.strip()]
        t_sources = [u for u in (transport.get("source_urls") or [])
                     if normalize_url(u) in allowed]
        has_transport = bool((transport.get("summary") or "").strip()) and bool(t_sources)
        has_safety = bool(advisories) or bool((safety.get("summary") or "").strip() and scams)
        if not (has_transport or advisories):
            log.info("safety: nothing grounded for %s (%d results)",
                     place.label, len(results))
            return None
        log.info("safety: %s → %d advisories (corroborated=%s), transport=%s",
                 place.label, len(advisories), corr.corroborated, has_transport)

        title_by_url = {normalize_url(r.url): r.title for r in results}
        src_urls = [a.get("source_url") for a in advisories] + t_sources
        sources = self._sources(src_urls, title_by_url)
        return Finding(
            topic=self.topic, scope=self.scope, place_key=place.city_key,
            resolver="safety.searxng+ollama", confidence="medium",
            ttl_seconds=2_592_000,                # 30 days — slow-changing
            sources=sources,
            data={"safety": {"summary": safety.get("summary", ""),
                             "advisories": advisories, "scams": scams,
                             "corroborated": corr.corroborated},
                  "transport": {**transport, "source_urls": t_sources} if has_transport else None},
            body_md=self._render(place, safety, advisories, scams, corr,
                                 transport if has_transport else None))

    @staticmethod
    def _sources(urls, title_by_url: dict) -> list[dict]:
        seen, out = set(), []
        for u in urls:
            key = normalize_url(u or "")
            if key and key not in seen:
                seen.add(key)
                out.append({"title": title_by_url.get(key, u), "url": u})
        return out

    def _render(self, place, safety, advisories, scams, corr, transport) -> str:
        return render_safety(place.label, safety, advisories, scams, corr, transport)


def render_safety(label, safety, advisories, scams, corr, transport) -> str:
    """Safety & Transport as markdown — shared by the local resolver and agents' save_safety (4.5)."""
    out = [f"**{label}** — safety & transport", ""]
    # Transport first (the priority).
    if transport:
        out.append("### Getting around")
        if transport.get("summary"):
            out.append(transport["summary"].strip())
        if transport.get("public_transit"):
            out.append(f"- **Public transit:** {transport['public_transit'].strip()}")
        if transport.get("ride_hailing"):
            out.append(f"- **Ride-hailing:** {', '.join(transport['ride_hailing'])}")
        if transport.get("apps"):
            out.append(f"- **Apps to get:** {', '.join(transport['apps'])}")
        if transport.get("from_airport"):
            out.append(f"- **From the airport:** {transport['from_airport'].strip()}")
        for t in (transport.get("tips") or []):
            out.append(f"- {t}")
        out.append("")
    if advisories or scams or (safety.get("summary") or "").strip():
        out.append("### Safety")
        banner = verify_banner(corr)
        if banner:
            out.append(banner)
        if safety.get("summary"):
            out.append(safety["summary"].strip())
        for a in advisories:
            out.append(f"- {a.get('text', '').strip()}")
        if scams:
            out.append("")
            out.append("**Common scams:**")
            for s in scams:
                out.append(f"- {s}")
    return "\n".join(out).rstrip()
