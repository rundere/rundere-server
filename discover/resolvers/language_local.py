"""Language L3 — the regional register overlay (SPEC-3.2 §11.3, step 10).

The deterministic LanguageResolver ships L1 (WHICH official languages, from CLDR)
and L2 (the STABLE SKELETON — a vetted phrasebook of standard forms). This is the
missing third layer:

  L3 — the REGIONAL REGISTER: how people here actually greet and speak, which
       standard-language phrasebooks can't carry. Switzerland's official language
       is German, and L2 gives you "Guten Tag" — but you greet with "Grüezi". That
       gap (language ≠ register) is what L3 fills.

L3 is search+LLM-backed and therefore FLAGGED confidence: dialect, slang, and
local greetings are soft, source-derived knowledge, not the vetted skeleton. It
runs in the LLM tier behind the same Resolver interface as food/events/safety,
city-scoped so the register is pinned to the actual place (Zürich German ≠ Bern
German), and layered ON TOP of the country-level L1/L2 finding — its own topic
(`language_local`) so a re-run never collides with the deterministic resolver.

Grounding (D-66/D-90): every greeting/expression carries the exact source_url of
the search result it came from; anything the model invents a URL for is dropped.
"""
from __future__ import annotations

import logging

from discover.grounding import ground_items, normalize_url
from discover.places import DerivedPlace
from discover.resolvers.base import Finding, ResolverContext
from discover.search import searxng_search
from discover.synth import DEFAULT_MODEL, synthesize_json

log = logging.getLogger("yonder.discovery.llm")


def _official_languages(country: str | None) -> list[str]:
    """Distinct base language names for a country — the standard register L3 is
    contrasted against ('the standard is German; here they say ...'). Best-effort;
    an empty list just means the prompt omits the contrast hint."""
    if not country:
        return []
    try:
        from babel import Locale
        from babel.languages import get_official_languages
        codes = get_official_languages(country) or \
            get_official_languages(country, de_facto=True)
    except Exception:
        return []
    seen: set[str] = set()
    names: list[str] = []
    for c in codes:
        base = c.split("_")[0].split("-")[0]
        if base in seen:
            continue
        seen.add(base)
        try:
            names.append(Locale.parse(base).get_display_name("en") or base)
        except Exception:
            names.append(base)
    return names


_SCHEMA = {
    "type": "object",
    "properties": {
        "register_summary": {"type": "string"},
        "greetings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string"},
                    "meaning": {"type": "string"},
                    "pronunciation": {"type": ["string", "null"]},
                    "note": {"type": ["string", "null"]},
                    "source_url": {"type": "string"},
                },
                "required": ["phrase", "meaning", "source_url"],
            },
        },
        "expressions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string"},
                    "meaning": {"type": "string"},
                    "source_url": {"type": "string"},
                },
                "required": ["phrase", "meaning", "source_url"],
            },
        },
        "etiquette": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["register_summary", "greetings", "expressions", "etiquette"],
}

_SYSTEM = (
    "You are a local-language guide focused on REGIONAL REGISTER — how people in "
    "this specific place actually greet and speak day to day, which a standard "
    "phrasebook misses. Think 'Grüezi' in Zürich vs textbook 'Guten Tag'. Use ONLY "
    "the search results provided — never invent a phrase or a URL, and copy each "
    "source_url exactly from a result. Prioritise: (1) the real local GREETING(S) a "
    "visitor should use, with pronunciation and a short note on when/whom; (2) a few "
    "genuinely useful everyday LOCAL EXPRESSIONS or slang; (3) brief etiquette on "
    "how greetings work here (formality, cheek-kisses, tone). Keep it to the "
    "DISTINCTIVELY LOCAL — do not repeat generic standard-language phrases. If the "
    "results only describe the standard language with no regional colour, return "
    "empty lists rather than padding with textbook forms.")


class LanguageLocalResolver:
    topic = "language_local"
    scope = "city"

    def __init__(self, *, searxng_url: str | None, ollama_url: str | None,
                 model: str = DEFAULT_MODEL, timeout: int = 600,
                 max_snippets: int = 20):
        self.searxng_url = searxng_url
        self.ollama_url = ollama_url
        self.model = model
        self.timeout = timeout
        self.max_snippets = max_snippets

    # -- query construction -------------------------------------------------
    def _queries(self, city: str, country: str | None) -> list[str]:
        c = f"{city} {country}".strip() if country else city
        return [
            f"local greetings {city}",
            f"how do locals greet each other {c}",
            f"{city} dialect phrases",
            f"local slang {c}",
            f"language etiquette {city}",
        ]

    def _fallback_queries(self, place: DerivedPlace, langs: list[str]) -> list[str]:
        """Broader queries tried when the register-specific pass grounds nothing —
        lean on the base language name(s) and generic phrasing so a city whose
        exact-name searches came up dry (Tangier's Darija, say) still yields
        something instead of the register card vanishing."""
        label = place.label
        qs = [f"common local phrases {label}", f"how do people greet in {label}",
              f"useful everyday phrases {label}"]
        for name in (langs or [])[:2]:
            qs.append(f"{name} greetings and everyday phrases")
            qs.append(f"how to say hello and thank you in {name}")
        return qs

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

    def _prompt(self, place: DerivedPlace, langs: list[str], results) -> str:
        lines = [f"City: {place.label}"]
        if langs:
            lines.append(f"Standard/official language(s) here: {', '.join(langs)}. "
                         "Tell me how the LOCAL register differs from these.")
        lines += ["", "Search results (your ONLY source of truth):"]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] {r.title} — {r.url}")
            if r.snippet:
                lines.append(f"    {r.snippet}")
        lines += ["", "Return JSON with register_summary, greetings[], "
                  "expressions[], etiquette[]. Copy each greeting's and "
                  "expression's source_url exactly from the list above. Only "
                  "include distinctively local content; empty lists are fine."]
        return "\n".join(lines)

    def _extract(self, place: DerivedPlace, langs: list[str], queries: list[str]):
        """One pass: search → synthesize → ground. Returns (greetings, expressions,
        summary, etiquette, results), or None when the search came back empty."""
        results = self._search(queries)
        if not results:
            return None
        out = synthesize_json(self._prompt(place, langs, results), _SCHEMA,
                              base_url=self.ollama_url, system=_SYSTEM,
                              model=self.model, timeout=self.timeout)
        if not out:
            log.warning("language_local: no usable LLM output for %s (see the synth: line above "
                        "for the cause)", place.label)
            return [], [], "", [], results
        allowed = [r.url for r in results]
        greetings = ground_items(out.get("greetings") or [], allowed).kept
        expressions = ground_items(out.get("expressions") or [], allowed).kept
        summary = (out.get("register_summary") or "").strip()
        etiquette = [e for e in (out.get("etiquette") or [])
                     if isinstance(e, str) and e.strip()]
        return greetings, expressions, summary, etiquette, results

    # -- resolve ------------------------------------------------------------
    def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None:
        if not self.searxng_url or not self.ollama_url:
            return None                          # degrade: LLM tier not configured
        if not place.city:
            return None                          # city-scope only
        langs = _official_languages(place.country)
        primary = self._extract(place, langs,
                                self._queries(place.city, place.country))
        greetings, expressions, summary, etiquette, results = \
            primary or ([], [], "", [], [])
        # A register overlay with neither a greeting nor a summary adds nothing —
        # retry with broader, language-name-led queries before giving up.
        if not greetings and not summary:
            fb = self._extract(place, langs, self._fallback_queries(place, langs))
            if fb is not None:
                f_g, f_e, f_s, f_et, f_r = fb
                if f_g or f_s:
                    log.info("language_local: primary empty, fallback surfaced %s",
                             place.label)
                    greetings, expressions, summary, etiquette, results = \
                        f_g, f_e, f_s, f_et, f_r
        if not results:
            log.warning("language_local: no SearXNG results for %s (primary+fallback)"
                        " — is SearXNG reachable at %s?", place.label, self.searxng_url)
            return None
        if not greetings and not summary:
            log.info("language_local: nothing grounded for %s after fallback (%d "
                     "results)", place.label, len(results))
            return None
        log.info("language_local: %s → %d greetings, %d expressions",
                 place.label, len(greetings), len(expressions))

        title_by_url = {normalize_url(r.url): r.title for r in results}
        sources = self._sources(greetings + expressions, title_by_url)
        return Finding(
            topic=self.topic, scope=self.scope, place_key=place.city_key,
            resolver="language_local.searxng+ollama", confidence="flagged",
            ttl_seconds=7_776_000,               # 90 days — register is slow-changing
            sources=sources,
            data={"register_summary": summary, "greetings": greetings,
                  "expressions": expressions, "etiquette": etiquette},
            body_md=self._render(place, summary, greetings, expressions, etiquette))

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

    def _render(self, place: DerivedPlace, summary: str, greetings: list[dict],
                expressions: list[dict], etiquette: list[str]) -> str:
        return render_phrases(place.label, summary, greetings, expressions, etiquette)


def render_phrases(label: str, summary: str, greetings: list[dict],
                   expressions: list[dict], etiquette: list[str]) -> str:
    """The Local phrases & register write-up — shared by this resolver and by
    phrases an AI agent saves over MCP/API (same data shape either way)."""
    out = [f"**{label}** — local phrases & register", ""]
    out.append("> ⚠️ **Local colour, not a phrasebook** — colloquial forms from "
               "the web; the vetted standard phrases are under Language.")
    out.append("")
    if summary:
        out.append(summary)
        out.append("")
    if greetings:
        out.append("### How locals greet")
        for g in greetings:
            pron = f" · {g['pronunciation']}" if g.get("pronunciation") else ""
            note = f" — {g['note'].strip()}" if g.get("note") else ""
            out.append(f"- **{g.get('phrase', '?')}**{pron} "
                       f"({g.get('meaning', '').strip()}){note}")
        out.append("")
    if expressions:
        out.append("### Useful local expressions")
        for e in expressions:
            out.append(f"- **{e.get('phrase', '?')}** — {e.get('meaning', '').strip()}")
        out.append("")
    if etiquette:
        out.append("### Etiquette")
        for e in etiquette:
            out.append(f"- {e}")
    return "\n".join(out).rstrip()
