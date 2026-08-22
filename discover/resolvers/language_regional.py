"""Regional-language resolver (SPEC-3.2 §5, region refinement — D-69 lifted).

The national LanguageResolver is country-scoped: a trip to India lists Hindi +
English, the *national* official languages. But you're in Bengaluru, where the
language of daily life is **Kannada** — a scheduled language of Karnataka, not a
national official one, so it never surfaces. Same for Catalan in Barcelona,
Cantonese in Guangzhou, Welsh in Cardiff.

This city-scoped resolver closes that gap. It reads the geocoded region name off
the DerivedPlace (Photon already returns state/province), looks up the region's
languages in the curated `data/regional_languages.json` map, **subtracts the
national set** (so it never re-lists Hindi), and emits a per-city finding for
what's left. Each regional language's phrasebook comes from, in order:

  1. the VETTED phrases.json skeleton, if present (rendered exactly like L2), or
  2. a GENERATED, source-grounded, FLAGGED phrasebook (SearXNG → Ollama, the L3
     pipeline) for the long tail the vetted set doesn't cover (Kannada today), or
  3. a plain "no phrasebook yet" note when neither is available.

A generated phrasebook is honest about its provenance: the finding is `flagged`,
carries a "machine-generated — verify" banner, and links the sources it was built
from. That is the deliberate trade-off for covering languages a human hasn't
vetted — the L2 skeleton stays vetted-only; generated content is never silently
mixed in as if it were.

Lives in the LLM tier (build_llm) because generation needs the SearXNG/Ollama
endpoints; with those unset it still emits vetted phrases + notes, never a 500.
"""
from __future__ import annotations

import json
import logging

from discover import langdata
from discover.grounding import normalize_url
from discover.places import DerivedPlace
from discover.resolvers.base import Finding, ResolverContext
from discover.resolvers.language import (PHRASE_KEYS, _lang_name,
                                        official_language_codes)
from discover.search import searxng_search
from discover.synth import DEFAULT_MODEL, synthesize_json

log = logging.getLogger("yonder.discovery.llm")

_KEY_LABELS = dict(PHRASE_KEYS)
_KEYSET = {k for k, _ in PHRASE_KEYS}


_GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "phrases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "enum": sorted(_KEYSET)},
                    "text": {"type": "string"},
                    "pronunciation": {"type": "string"},
                },
                "required": ["key", "text"],
            },
        },
    },
    "required": ["phrases"],
}

_GEN_SYSTEM = (
    "You build a short traveller phrasebook for one language, using the search "
    "results as reference. For each requested phrase key, give the phrase in the "
    "language's OWN native script and a simple romanised pronunciation. Only "
    "include a phrase you can determine with confidence from the sources — OMIT "
    "any you are unsure of rather than guessing. Never invent a phrase. A short, "
    "correct list beats a long, doubtful one.")


class RegionalLanguageResolver:
    topic = "language_regional"
    scope = "city"

    def __init__(self, *, searxng_url: str | None, ollama_url: str | None,
                 model: str = DEFAULT_MODEL, timeout: int = 600,
                 max_snippets: int = 12):
        self.searxng_url = searxng_url
        self.ollama_url = ollama_url
        self.model = model
        self.timeout = timeout
        self.max_snippets = max_snippets

    # -- which languages ----------------------------------------------------
    def _regional_codes(self, place: DerivedPlace, rmap: dict) -> list[str]:
        """Regional language codes for the place, national languages removed."""
        country = (place.country or "").upper()
        region = (place.region or "").strip().lower()
        if not country or not region:
            return []
        codes = (rmap.get(country) or {}).get(region) or []
        national = set(official_language_codes(country))
        out, seen = [], set()
        for c in codes:
            if c not in national and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    # -- generation (the L3 pipeline, for un-vetted regional languages) -----
    def _search(self, lang_name: str):
        queries = [
            f"{lang_name} basic phrases with pronunciation for travellers",
            f"how to say hello thank you please in {lang_name}",
            f"{lang_name} phrasebook english",
        ]
        seen, results = set(), []
        for q in queries:
            for r in searxng_search(q, base_url=self.searxng_url, timeout=self.timeout):
                key = normalize_url(r.url)
                if key and key not in seen:
                    seen.add(key)
                    results.append(r)
                    if len(results) >= self.max_snippets:
                        return results
        return results

    def _gen_prompt(self, lang_name: str, results) -> str:
        wanted = ", ".join(f"{k} ({lbl})" for k, lbl in PHRASE_KEYS)
        lines = [f"Language: {lang_name}", "",
                 f"Phrase keys to fill (omit any you're unsure of): {wanted}", "",
                 "Search results (reference):"]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] {r.title} — {r.url}")
            if r.snippet:
                lines.append(f"    {r.snippet}")
        lines += ["", "Return JSON: phrases[] of {key, text (native script), "
                  "pronunciation}. Native script in text, romanisation in "
                  "pronunciation. Only phrases you're confident of."]
        return "\n".join(lines)

    def _generate(self, lang_name: str, ctx: ResolverContext):
        """Return (book, results) — book is {key: {t, p}} — or None. Best-effort;
        any failure degrades to None (the language falls back to a note)."""
        if not self.searxng_url or not self.ollama_url:
            return None
        results = self._search(lang_name)
        if not results:
            return None
        out = synthesize_json(self._gen_prompt(lang_name, results), _GEN_SCHEMA,
                              base_url=self.ollama_url, system=_GEN_SYSTEM,
                              model=self.model, timeout=self.timeout)
        if not out:
            return None
        book: dict[str, dict] = {}
        for item in (out.get("phrases") or []):
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            text = (item.get("text") or "").strip()
            if key in _KEYSET and text and key not in book:
                book[key] = {"t": text, "p": (item.get("pronunciation") or "").strip()}
        if not book:
            return None
        return book, results

    # -- resolve ------------------------------------------------------------
    def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None:
        rmap = ctx.regional_map if ctx.regional_map is not None \
            else langdata.regional_map(None)
        codes = self._regional_codes(place, rmap)
        if not codes:
            return None                          # no regional language beyond national

        book_src = ctx.phrasebook if ctx.phrasebook is not None \
            else langdata.seed_phrasebook()
        gen_langs = ctx.generated_languages or frozenset()

        entries: list[dict] = []                 # {code, name, book, generated}
        sources: list[dict] = []
        seen_src: set[str] = set()
        fresh_generated: dict[str, dict] = {}    # lang -> book, for the job to persist
        any_generated = any_missing = False
        for code in codes:
            name = _lang_name(code)
            stored = book_src.get(code)
            if stored and code not in gen_langs:
                entries.append({"code": code, "name": name, "book": stored,
                                "generated": False})
                continue
            if stored and code in gen_langs:
                # A previously-generated book already in the DB — reuse it (still
                # flagged), never regenerate what's stored and possibly owner-edited.
                entries.append({"code": code, "name": name, "book": stored,
                                "generated": True})
                any_generated = True
                continue
            gen = self._generate(name, ctx)
            if gen:
                book, results = gen
                entries.append({"code": code, "name": name, "book": book,
                                "generated": True})
                any_generated = True
                fresh_generated[code] = book
                for r in results:
                    k = normalize_url(r.url)
                    if k and k not in seen_src:
                        seen_src.add(k)
                        sources.append({"title": r.title, "url": r.url})
            else:
                entries.append({"code": code, "name": name, "book": None,
                                "generated": False})
                any_missing = True

        confidence = "flagged" if (any_generated or any_missing) else "high"
        data = {"languages": [{"code": e["code"], "name": e["name"],
                               "has_phrasebook": bool(e["book"]),
                               "generated": e["generated"]} for e in entries],
                # Consumed by the discovery job to persist newly-generated books as
                # `generated` PhrasebookPhrase rows (reusable + promotable).
                "generated_phrasebooks": fresh_generated}
        log.info("language_regional: %s → %s (generated=%s)", place.label,
                 [e["code"] for e in entries], any_generated)
        return Finding(
            topic=self.topic, scope=self.scope, place_key=place.city_key,
            resolver="language_regional", confidence=confidence, sources=sources,
            ttl_seconds=7_776_000, data=data,
            body_md=self._render(place, entries, any_generated))

    # -- render -------------------------------------------------------------
    @staticmethod
    def _render(place: DerivedPlace, entries: list[dict], any_generated: bool) -> str:
        region = place.region or "this region"
        names = ", ".join(e["name"] for e in entries)
        out = [f"**{place.label}** — regional language"
               f"{'s' if len(entries) > 1 else ''}: {names}.",
               "",
               f"Spoken in {region}, alongside the national language(s) under "
               "Language.", ""]
        if any_generated:
            out.append("> ⚠️ **Machine-generated phrasebook** — the flagged "
                       "language(s) below were built from web sources, not vetted. "
                       "Double-check before you say them.")
            out.append("")
        for e in entries:
            tag = " _(generated — verify)_" if e["generated"] else ""
            out.append(f"**{e['name']}**{tag}")
            book = e["book"]
            if book:
                for key, label in PHRASE_KEYS:
                    entry = book.get(key)
                    if entry and entry.get("t"):
                        pron = f" · {entry['p']}" if entry.get("p") else ""
                        out.append(f"- {label} — **{entry['t']}**{pron}")
            else:
                out.append(f"- A phrasebook for {e['name']} isn't available yet.")
            out.append("")
        return "\n".join(out).rstrip()
