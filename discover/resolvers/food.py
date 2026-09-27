"""Food & drink resolver (SPEC-3.2 §3.1) — the first LLM resolver.

Pipeline (the template every LLM resolver follows):
  BUILD QUERIES  the profile shapes the SearXNG search terms
  SEARCH         SearXNG returns {url,title,snippet}[], deduped by URL
  SYNTHESIZE     Ollama (schema-constrained, temp 0) turns snippets into places
  GROUND         drop any place whose source_url was not in the SearXNG set
  ASSEMBLE       a medium-confidence Finding with grouped body_md

Scope: city. Confidence: medium (LLM-synthesized, source-grounded). Endpoints
come from the constructor (deployment config, via build_llm); the DiscoveryProfile
comes from the ResolverContext (per-trip). Missing either endpoint → None.
"""
from __future__ import annotations

import logging

from discover.grounding import ground_items, normalize_url
from discover.places import DerivedPlace
from discover.resolvers.base import Finding, ResolverContext
from discover.search import searxng_search
from discover.synth import DEFAULT_MODEL, synthesize_json

log = logging.getLogger("yonder.discovery.llm")

# One SearXNG query per checked drink interest (§2.2). Kept to one representative
# query each so a full profile doesn't fan out to dozens of searches (§5.2).
_DRINK_QUERIES = {
    "craft_beer": "best craft beer {city}",
    "distilleries": "distillery {city}",
    "wineries": "wine tasting {city}",
    "coffee": "specialty coffee {city}",
    "tea": "tea house {city}",
    "non_alcoholic": "mocktail bar {city}",
}
_BUDGET_LABELS = {0: "no limit", 1: "$ (budget / street food)",
                  2: "$$ (casual to mid-range)", 3: "$$$ (mid to upscale)",
                  4: "$$$$ (fine dining)"}
_PLACE_TYPES = ["restaurant", "cafe", "bar", "pub", "brewery", "distillery", "market",
                "street_food"]
_TYPE_HEADINGS = {"street_food": "Street food", "restaurant": "Restaurants",
                  "cafe": "Cafés", "bar": "Bars", "pub": "Pubs", "brewery": "Breweries",
                  "distillery": "Distilleries", "market": "Markets"}

_SYSTEM = (
    "You are a local food & drink guide. Recommend real, specific places using "
    "ONLY the search results provided — never invent a place or a URL. Every place "
    "you return MUST carry the exact source_url of the search result it came from. "
    "Honour the traveller's constraints: do not recommend cuisines on their avoid "
    "list, keep within their budget ceiling, and flag (do not exclude) anything "
    "that may involve a listed allergen. Prefer local specialties over chains. "
    "Return a MIX: not only restaurants — include the notable bars, breweries, "
    "distilleries, cafés, and markets the results surface (a well-known local "
    "distillery or brewery belongs on the list). Type each place accurately. "
    "For each place, set what_to_get to the specific dish or drink it is known "
    "for when the results say so (else null). Also write `summary`: two or three "
    "sentences on this city's food & drink scene for this traveller and what you "
    "would prioritise.")

_SCHEMA = {
    "type": "object",
    "properties": {
        "places": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": _PLACE_TYPES},
                    "neighborhood": {"type": ["string", "null"]},
                    "address": {"type": ["string", "null"]},
                    "price_tier": {"type": "integer", "minimum": 1, "maximum": 4},
                    "why": {"type": "string"},
                    "local_specialty": {"type": "boolean"},
                    "what_to_get": {"type": ["string", "null"]},
                    "source_url": {"type": "string"},
                },
                "required": ["name", "type", "price_tier", "why",
                             "local_specialty", "source_url"],
            },
        },
        "local_dishes": {"type": "array", "items": {"type": "string"}},
        "allergy_warnings": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["places", "local_dishes", "allergy_warnings"],
}


class FoodResolver:
    topic = "food"
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
    def _queries(self, city: str, country: str | None, profile: dict) -> list[str]:
        food = (profile or {}).get("food") or {}
        drink = (profile or {}).get("drink") or {}
        place = f"{city} {country}".strip() if country else city
        # Baseline: restaurants + street food + drink queries ALWAYS, so a
        # food & drink finding never comes back food-only and notable breweries /
        # distilleries surface without opting in. Checked drink interests add more
        # targeted queries on top.
        qs = [f"best local restaurants {place}", f"street food {city}",
              f"best bars and cafes {city}",
              f"best breweries and distilleries {city}"]
        budget = food.get("budget_ceiling") or 0
        if budget == 1:
            qs.append(f"cheap eats {city}")
        elif budget == 4:
            qs.append(f"fine dining {city}")
        for interest in (drink.get("interests") or []):
            tmpl = _DRINK_QUERIES.get(interest)
            if tmpl:
                qs.append(tmpl.format(city=city))
        return qs

    def _search(self, queries: list[str]):
        """Run every query, dedup by normalized URL, cap at max_snippets."""
        seen: set[str] = set()
        results = []
        for q in queries:
            for r in searxng_search(q, base_url=self.searxng_url,
                                    timeout=self.timeout):
                key = normalize_url(r.url)
                if key and key not in seen:
                    seen.add(key)
                    results.append(r)
                    if len(results) >= self.max_snippets:
                        return results
        return results

    # -- prompt -------------------------------------------------------------
    def _prompt(self, place: DerivedPlace, profile: dict, results) -> str:
        food = (profile or {}).get("food") or {}
        diet = food.get("dietary_restrictions") or []
        dislikes = food.get("cuisine_dislikes") or []
        allergies = (food.get("allergies") or "").strip()
        budget = food.get("budget_ceiling") or 0
        lines = [f"City: {place.label}", "", "Traveller preferences:"]
        lines.append(f"- Dietary: {', '.join(diet) if diet else 'no restrictions'} "
                     "(every place must accommodate these)")
        lines.append(f"- Avoid cuisines: {', '.join(dislikes) if dislikes else 'none'}")
        lines.append(f"- Budget ceiling: {_BUDGET_LABELS.get(budget, 'no limit')} "
                     "— do not recommend places above this tier")
        lines.append(f"- Allergies to flag (warn, do not exclude): "
                     f"{allergies or 'none'}")
        lines += ["", "Search results (your ONLY source of truth):"]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] {r.title} — {r.url}")
            if r.snippet:
                lines.append(f"    {r.snippet}")
        lines += ["", "Return JSON with places[], local_dishes[], allergy_warnings[]. "
                  "Copy each place's source_url exactly from the list above, and set "
                  "address to the street address when a snippet gives one (else null)."]
        return "\n".join(lines)

    # -- resolve ------------------------------------------------------------
    def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None:
        if not self.searxng_url or not self.ollama_url:
            return None                          # degrade: LLM tier not configured
        if not place.city:
            return None                          # city-scope only
        profile = ctx.discovery_profile or {}
        results = self._search(self._queries(place.city, place.country, profile))
        if not results:
            log.warning("food: no SearXNG results for %s — is SearXNG reachable at "
                        "%s with JSON format enabled?", place.label, self.searxng_url)
            return None                          # nothing to ground against
        out = synthesize_json(self._prompt(place, profile, results), _SCHEMA,
                              base_url=self.ollama_url, system=_SYSTEM,
                              model=self.model, timeout=self.timeout)
        if not out:
            log.warning("food: no usable LLM output for %s (see the synth: line above "
                        "for the cause)", place.label)
            return None
        allowed = [r.url for r in results]
        grounded = ground_items(out.get("places") or [], allowed).kept
        grounded = self._apply_budget(grounded,
                                      (profile.get("food") or {}).get("budget_ceiling") or 0)
        dishes = [d for d in (out.get("local_dishes") or []) if isinstance(d, str) and d.strip()]
        summary = out.get("summary").strip() if isinstance(out.get("summary"), str) else ""
        warnings = [w for w in (out.get("allergy_warnings") or [])
                    if isinstance(w, str) and w.strip()]
        if not grounded and not dishes:
            log.info("food: %d place(s) proposed but none grounded for %s "
                     "(model cited URLs not in the %d search results)",
                     len(out.get("places") or []), place.label, len(results))
            return None                          # everything hallucinated / empty
        log.info("food: %s → %d places, %d dishes", place.label, len(grounded), len(dishes))
        title_by_url = {normalize_url(r.url): r.title for r in results}
        sources = self._sources(grounded, title_by_url)
        return Finding(
            topic=self.topic, scope=self.scope, place_key=place.city_key,
            resolver="food.searxng+ollama", confidence="medium",
            ttl_seconds=1_209_600,               # 14 days
            sources=sources,
            data={"places": grounded, "local_dishes": dishes,
                  "allergy_warnings": warnings, "summary": summary},
            body_md=self._render(place, grounded, dishes, warnings, summary))

    @staticmethod
    def _map_url(p: dict, city: str | None) -> str:
        """A Google Maps SEARCH deeplink (a plain URL, no API call / no quota) —
        the friendliest 'find it' link on mobile. Query is the place name plus its
        address when known, else the city."""
        import urllib.parse
        loc = (p.get("address") or "").strip() or (city or "")
        q = ", ".join(x for x in (p.get("name", ""), loc) if x)
        return "https://www.google.com/maps/search/?api=1&query=" + \
            urllib.parse.quote(q)

    @staticmethod
    def _apply_budget(places: list[dict], ceiling: int) -> list[dict]:
        """Belt-and-suspenders on the prompt's budget instruction: with a ceiling
        set, drop places the model tiered above it."""
        if not ceiling:
            return places
        return [p for p in places
                if not isinstance(p.get("price_tier"), int)
                or p["price_tier"] <= ceiling]

    @staticmethod
    def _sources(places: list[dict], title_by_url: dict) -> list[dict]:
        seen, out = set(), []
        for p in places:
            key = normalize_url(p.get("source_url") or "")
            if key and key not in seen:
                seen.add(key)
                out.append({"title": title_by_url.get(key, p.get("source_url")),
                            "url": p.get("source_url")})
        return out

    def _render(self, place: DerivedPlace, places: list[dict],
                dishes: list[str], warnings: list[str], summary: str = "") -> str:
        out = [f"**{place.label}** — food & drink", ""]
        if summary:
            out += [summary, ""]
        if dishes:
            out.append("**Local specialties to try:** " + ", ".join(dishes))
            out.append("")
        by_type: dict[str, list[dict]] = {}
        for p in places:
            by_type.setdefault(p.get("type") or "restaurant", []).append(p)
        # Lead with street food, then the rest in a stable order.
        for t in _PLACE_TYPES:
            group = by_type.get(t)
            if not group:
                continue
            out.append(f"### {_TYPE_HEADINGS.get(t, t.title())}")
            for p in group:
                tier = "$" * p["price_tier"] if isinstance(p.get("price_tier"), int) else ""
                hood = f" · {p['neighborhood']}" if p.get("neighborhood") else ""
                star = " ⭐" if p.get("local_specialty") else ""
                addr = (p.get("address") or "").strip()
                addr_part = f" · {addr}" if addr else ""
                get = f" · get: {p['what_to_get'].strip()}" \
                    if isinstance(p.get("what_to_get"), str) and p["what_to_get"].strip() else ""
                out.append(f"- **{p.get('name', '?')}**{star} ({tier}{hood}) — "
                           f"{p.get('why', '').strip()}{get}{addr_part} "
                           f"[map ↗]({self._map_url(p, place.city)})")
            out.append("")
        if warnings:
            out.append("> ⚠️ **Allergy check** — these mention ingredients you flagged:")
            for w in warnings:
                out.append(f"> - {w}")
        return "\n".join(out).rstrip()
