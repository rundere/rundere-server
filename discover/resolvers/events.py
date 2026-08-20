"""Events & activities resolver (SPEC-3.2 §3.2) — the three-tier model (D-98).

Same pipeline as the food resolver, but the output is split into three tiers:

  Tier 1 — IMPACTS   events that affect ANY traveller regardless of interests
                     (religious observances, national holidays, strikes, seasonal
                     closures). Searched unconditionally, rendered as warnings.
  Tier 2 — SUGGESTED events/activities matching the traveller's checked activity
                     interests (the curation lens). The main recommendation list.
  Tier 3 — OTHER     results that turned up but match no checked interest —
                     available, shown under a heading, not highlighted.

Scope: city. Confidence: medium (date-bounded, source-grounded). Short TTL — this
is the most date-sensitive resolver, so a re-run closer to the trip yields fresher
results.
"""
from __future__ import annotations

import logging

from discover.grounding import ground_items, normalize_url
from discover.places import DerivedPlace
from discover.resolvers.base import Finding, ResolverContext
from discover.search import searxng_search
from discover.synth import DEFAULT_MODEL, synthesize_json

log = logging.getLogger("yonder.discovery.llm")

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]

# Activity-interest key -> the phrase used in a SearXNG query / a display label.
_ACTIVITY_PHRASES = {
    "markets": "local markets", "hidden_gems": "hidden gems", "museums": "museums",
    "outdoor": "outdoor activities", "historical": "historical sites",
    "nightlife": "nightlife", "sports": "sports events", "shopping": "shopping",
    "wellness": "wellness spa", "cooking_classes": "cooking classes",
    "live_music": "live music", "architecture": "architecture",
}


def _month_year(start_date: str | None) -> tuple[str | None, int | None]:
    if not start_date:
        return None, None
    try:
        y, m, _ = start_date.split("-")
        return _MONTHS[int(m) - 1], int(y)
    except (ValueError, IndexError):
        return None, None


def _interest_label(key: str) -> str:
    return _ACTIVITY_PHRASES.get(key, key.replace("_", " ")).title()


def _dedupe(items: list[dict]) -> list[dict]:
    """Drop items with a duplicate (case/space-insensitive) name — the model is
    told not to repeat, but several calendar sources describe the same holiday, so
    belt-and-suspenders."""
    seen, out = set(), []
    for it in items:
        key = " ".join((it.get("name") or "").lower().split())
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out


_ITEM = {
    "name": {"type": "string"},
    "date_range": {"type": ["string", "null"]},
    "source_url": {"type": "string"},
}
_SCHEMA = {
    "type": "object",
    "properties": {
        "impacts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {**_ITEM, "effect": {"type": "string"}},
                "required": ["name", "effect", "source_url"],
            },
        },
        "notable": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    **_ITEM,
                    "matched_interest": {"type": ["string", "null"]},
                    "why": {"type": "string"},
                },
                "required": ["name", "why", "source_url"],
            },
        },
    },
    "required": ["impacts", "notable"],
}

_SYSTEM = (
    "You are a local events & activities guide. From the search results, EXTRACT "
    "the actual named events, festivals, holidays, and attractions — do NOT list a "
    "website, a calendar, a 'list of holidays' page, a vacation package, or a "
    "school calendar as an item. If a result is a holiday calendar, extract the "
    "specific holidays it names that fall during the travel dates. DE-DUPLICATE: "
    "the same event from several sources is ONE item; never repeat a name. Copy "
    "each item's exact source_url from the list. Return two groups: (1) impacts — "
    "specific dated public/religious holidays, closures, or strikes during the "
    "travel window that affect any traveller (name the actual holiday and its date, "
    "e.g. 'Columbus Day — Oct 12'), not the calendar website; (2) notable — "
    "specific, named events and attractions worth a traveller's time (a festival, a "
    "landmark, a market, a venue), most notable first. When a notable item matches "
    "one of the traveller's stated interests, set matched_interest to it, else "
    "null. Skip generic 'things to do' aggregators and ads.")


class EventsResolver:
    topic = "events"
    scope = "city"

    def __init__(self, *, searxng_url: str | None, ollama_url: str | None,
                 model: str = DEFAULT_MODEL, timeout: int = 600,
                 max_snippets: int = 28):
        self.searxng_url = searxng_url
        self.ollama_url = ollama_url
        self.model = model
        self.timeout = timeout
        self.max_snippets = max_snippets

    # -- query construction -------------------------------------------------
    def _queries(self, city: str, country: str | None, when: str,
                 interests: list[str]) -> list[str]:
        c = country or ""
        qs = [
            f"holidays {city} {when}".strip(),
            f"{c} public holidays {when}".strip(),
            f"religious observances {c} {when}".strip(),
            f"top things to do {city}",
            f"events {city} {when}".strip(),
        ]
        for interest in interests:
            phrase = _ACTIVITY_PHRASES.get(interest)
            if phrase:
                qs.append(f"{phrase} {city} {when}".strip())
                qs.append(f"{phrase} near {city}")
        # de-dup identical queries while preserving order
        seen, out = set(), []
        for q in qs:
            if q and q not in seen:
                seen.add(q)
                out.append(q)
        return out

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

    def _prompt(self, place: DerivedPlace, when: str, interests: list[str],
                results) -> str:
        lines = [f"City: {place.label}"]
        if when:
            lines.append(f"Travel dates: around {when}")
        labels = [_interest_label(i) for i in interests]
        lines.append("Traveller interests: "
                     + (", ".join(labels) if labels else "none stated"))
        lines += ["", "Search results (your ONLY source of truth):"]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] {r.title} — {r.url}")
            if r.snippet:
                lines.append(f"    {r.snippet}")
        lines += ["", "Return JSON with impacts[] and notable[]. Extract concrete "
                  "named events/holidays/attractions — never list a calendar or "
                  "listing website. De-duplicate. Set matched_interest on a notable "
                  "item only when it matches a stated interest, else null. Copy every "
                  "source_url exactly from the list above."]
        return "\n".join(lines)

    # -- resolve ------------------------------------------------------------
    def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None:
        if not self.searxng_url or not self.ollama_url:
            return None
        if not place.city:
            return None
        profile = ctx.discovery_profile or {}
        interests = ((profile.get("activity") or {}).get("interests")) or []
        month, year = _month_year(ctx.start_date)
        when = f"{month} {year}" if month and year else ""
        results = self._search(self._queries(place.city, place.country, when, interests))
        if not results:
            log.warning("events: no SearXNG results for %s — is SearXNG reachable at "
                        "%s with JSON format enabled?", place.label, self.searxng_url)
            return None
        out = synthesize_json(self._prompt(place, when, interests, results), _SCHEMA,
                              base_url=self.ollama_url, system=_SYSTEM,
                              model=self.model, timeout=self.timeout)
        if not out:
            log.warning("events: Ollama returned nothing for %s — is Ollama reachable "
                        "at %s and is %s pulled?", place.label, self.ollama_url, self.model)
            return None
        allowed = [r.url for r in results]
        impacts = _dedupe(ground_items(out.get("impacts") or [], allowed).kept)
        notable = _dedupe(ground_items(out.get("notable") or [], allowed).kept)
        if not (impacts or notable):
            log.info("events: results proposed but none grounded for %s "
                     "(model cited URLs not in the %d search results)",
                     place.label, len(results))
            return None
        log.info("events: %s → %d impacts, %d notable", place.label,
                 len(impacts), len(notable))
        title_by_url = {normalize_url(r.url): r.title for r in results}
        sources = self._sources([*impacts, *notable], title_by_url)
        return Finding(
            topic=self.topic, scope=self.scope, place_key=place.city_key,
            resolver="events.searxng+ollama", confidence="medium",
            ttl_seconds=604_800,                 # 7 days — most date-sensitive
            sources=sources,
            data={"impacts": impacts, "notable": notable},
            body_md=self._render(place, impacts, notable))

    @staticmethod
    def _sources(items: list[dict], title_by_url: dict) -> list[dict]:
        seen, out = set(), []
        for it in items:
            key = normalize_url(it.get("source_url") or "")
            if key and key not in seen:
                seen.add(key)
                out.append({"title": title_by_url.get(key, it.get("source_url")),
                            "url": it.get("source_url")})
        return out

    def _render(self, place: DerivedPlace, impacts: list[dict],
                notable: list[dict]) -> str:
        out = [f"**{place.label}** — events & activities", ""]
        if impacts:
            out.append("### Heads-up while you're there")
            for im in impacts:
                dr = f" ({im['date_range']})" if im.get("date_range") else ""
                out.append(f"> ⚠️ **{im.get('name', '?')}**{dr} — "
                           f"{im.get('effect', '').strip()}")
            out.append("")
        if notable:
            # Group only by REAL matched interests; everything else (incl. the
            # no-interest case) falls under one neutral "Notable" heading.
            valid = {k for k, _ in _ACTIVITY_PHRASES.items()}
            by_interest: dict[str, list[dict]] = {}
            general: list[dict] = []
            for n in notable:
                mi = n.get("matched_interest")
                (by_interest.setdefault(mi, []) if mi in valid else general).append(n)
            for interest, items in by_interest.items():
                out.append(f"### {_interest_label(interest)}")
                for n in items:
                    out.append(self._line(n))
                out.append("")
            if general:
                out.append("### Notable events & activities")
                for n in general:
                    out.append(self._line(n))
                out.append("")
        return "\n".join(out).rstrip()

    @staticmethod
    def _line(item: dict) -> str:
        dr = f" ({item['date_range']})" if item.get("date_range") else ""
        return f"- **{item.get('name', '?')}**{dr} — {item.get('why', '').strip()}"
