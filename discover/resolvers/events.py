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


def _fmt_date(iso: str | None) -> str:
    """'2026-10-02' -> 'October 2, 2026', or '' if unparseable."""
    if not iso:
        return ""
    try:
        y, m, d = iso.split("-")
        return f"{_MONTHS[int(m) - 1]} {int(d)}, {int(y)}"
    except (ValueError, IndexError):
        return ""


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
                    "kind": {"type": "string", "enum": ["event", "activity"]},
                    "venue": {"type": ["string", "null"]},
                    "address": {"type": ["string", "null"]},
                },
                "required": ["name", "why", "source_url"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["impacts", "notable"],
}

_SYSTEM = (
    "You are a local events & activities guide. From the search results, EXTRACT "
    "the actual named events, festivals, holidays, and attractions — do NOT list a "
    "website, a calendar, a 'list of holidays' page, a vacation package, or a "
    "school calendar as an item. If a result is a holiday calendar, extract ONLY "
    "the specific holidays that fall INSIDE the traveller's exact date window — a "
    "holiday outside those dates (even the day after they leave) must be omitted. "
    "DE-DUPLICATE: the same event from several sources is ONE item; never repeat a "
    "name. Copy each item's exact source_url from the list. Return two groups: (1) "
    "impacts — public/religious holidays, closures, or strikes that fall inside the "
    "window, named with the date; (2) notable — specific, named events (with dates) "
    "and concrete named attractions worth the trip, most notable first. ALWAYS "
    "include the city's marquee, signature festivals and major events that occur "
    "during the window — a famous annual festival happening while they're there is "
    "exactly the point; never omit a real, well-known one out of caution. Do NOT "
    "invent a vague 'local festival' to pad the list. When a notable item matches a "
    "stated interest, set matched_interest to it, else null. Skip aggregators and ads. "
    "Set each notable item's kind: `event` when it happens on specific dates, "
    "`activity` when you can do it any day (an attraction, a tour, a hike); give "
    "its venue and address when the results do. Also write `summary`: two or three "
    "sentences on what's on in this city during the window and what you'd prioritise.")


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
        # Events/festivals FIRST — they're the primary value and were being
        # starved by the holiday queries under the old greedy fill. A dedicated
        # "festivals" query targets marquee annual events (Balloon Fiesta, etc.).
        qs = [
            f"top things to do {city}",
            f"events {city} {when}".strip(),
            f"festivals {city} {when}".strip(),
            f"holidays {city} {when}".strip(),
            f"{c} public holidays {when}".strip(),
            f"religious observances {c} {when}".strip(),
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

    def _fallback_queries(self, city: str, when: str) -> list[str]:
        """Broader, tourism-shaped queries tried ONLY when the primary pass grounds
        nothing — a smaller town may have no dated 'events' but still real
        attractions worth surfacing (Glendale Caverns, a plaza, a historic site).
        Cast wider so the city gets a finding instead of silently dropping out."""
        return [f"{city} attractions", f"best things to do in {city}",
                f"{city} tourism", f"visit {city} points of interest",
                f"museums and landmarks {city}", f"day trips from {city}"]

    def _search(self, queries: list[str]):
        """Round-robin the queries into the snippet budget so EVERY query type is
        represented — one hit from each query, then the second from each, etc. The
        old greedy fill let the first queries monopolise the 28-snippet cap, which
        starved the events/festivals queries and dropped signature events like the
        Balloon Fiesta before they ever reached Ollama."""
        per_query = [searxng_search(q, base_url=self.searxng_url, timeout=self.timeout)
                     for q in queries]
        seen: set[str] = set()
        results = []
        depth = 0
        while len(results) < self.max_snippets:
            advanced = False
            for lst in per_query:
                if depth >= len(lst):
                    continue
                advanced = True
                key = normalize_url(lst[depth].url)
                if key and key not in seen:
                    seen.add(key)
                    results.append(lst[depth])
                    if len(results) >= self.max_snippets:
                        break
            if not advanced:
                break
            depth += 1
        return results

    def _prompt(self, place: DerivedPlace, start: str, end: str,
                interests: list[str], results) -> str:
        lines = [f"City: {place.label}"]
        if start and end:
            lines.append(f"Travel window: {start} to {end} (inclusive). This is the "
                         "ONLY window that matters.")
        labels = [_interest_label(i) for i in interests]
        lines.append("Traveller interests: "
                     + (", ".join(labels) if labels else "none stated"))
        lines += ["", "Search results (your ONLY source of truth):"]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] {r.title} — {r.url}")
            if r.snippet:
                lines.append(f"    {r.snippet}")
        window = (f" that fall inside {start}–{end}" if start and end else "")
        lines += ["", "Return JSON with impacts[] and notable[]. impacts: ONLY "
                  f"public/religious holidays, closures, or strikes{window} — omit "
                  "any dated outside the window (a holiday the day after they leave "
                  "does NOT count). notable: specific named events (include the date) "
                  "and concrete named venues/attractions worth the trip — ALWAYS "
                  "include the marquee/signature festivals and major events running "
                  "during the window (do not omit a famous one out of caution). Do "
                  "NOT pad with vague, undated, or invented 'local festival' entries. "
                  "Never list a calendar/listing website as an item. De-duplicate. "
                  "Copy every source_url exactly from above."]
        return "\n".join(lines)

    def _extract(self, place: DerivedPlace, start: str, end: str,
                 interests: list[str], queries: list[str]):
        """One pass: search → synthesize → ground. Returns (impacts, notable,
        results, summary) with results kept for source attribution, or None when
        the search itself came back empty."""
        results = self._search(queries)
        if not results:
            return None
        out = synthesize_json(self._prompt(place, start, end, interests, results),
                              _SCHEMA, base_url=self.ollama_url, system=_SYSTEM,
                              model=self.model, timeout=self.timeout)
        if not out:
            log.warning("events: no usable LLM output for %s (see the synth: line above "
                        "for the cause)", place.label)
            return [], [], results, ""
        allowed = [r.url for r in results]
        impacts = _dedupe(ground_items(out.get("impacts") or [], allowed).kept)
        notable = _dedupe(ground_items(out.get("notable") or [], allowed).kept)
        summary = out.get("summary").strip() if isinstance(out.get("summary"), str) else ""
        return impacts, notable, results, summary

    # -- resolve ------------------------------------------------------------
    def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None:
        if not self.searxng_url or not self.ollama_url:
            return None
        if not place.city:
            return None
        profile = ctx.discovery_profile or {}
        interests = ((profile.get("activity") or {}).get("interests")) or []
        month, year = _month_year(ctx.start_date)
        when = f"{month} {year}" if month and year else ""    # broad, for SEARCH
        start, end = _fmt_date(ctx.start_date), _fmt_date(ctx.end_date)

        primary = self._extract(
            place, start, end, interests,
            self._queries(place.city, place.country, when, interests))
        impacts, notable, results, summary = primary or ([], [], [], "")
        if not (impacts or notable):
            # Nothing grounded on the events-shaped pass — retry with broader,
            # tourism-shaped queries before giving up, so a smaller town still
            # surfaces its attractions instead of vanishing.
            fb = self._extract(place, start, end, interests,
                               self._fallback_queries(place.city, when))
            if fb is not None:
                f_impacts, f_notable, f_results, f_summary = fb
                if f_impacts or f_notable:
                    log.info("events: primary empty, fallback surfaced %s",
                             place.label)
                    impacts, notable, results, summary = (f_impacts, f_notable,
                                                          f_results, f_summary)
        if not results:
            log.warning("events: no SearXNG results for %s (primary+fallback) — is "
                        "SearXNG reachable at %s?", place.label, self.searxng_url)
            return None
        if not (impacts or notable):
            log.info("events: nothing grounded for %s after fallback (%d results)",
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
            data={"impacts": impacts, "notable": notable, "summary": summary},
            body_md=self._render(place, impacts, notable, summary))

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
                notable: list[dict], summary: str = "") -> str:
        out = [f"**{place.label}** — events & activities", ""]
        if summary:
            out += [summary, ""]
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
