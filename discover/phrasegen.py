"""Shared LLM phrasebook generation (Yonder 3.7).

The grounded, FLAGGED phrasebook generator used by BOTH language resolvers for the
long tail the vetted set doesn't cover:

  * regional languages (RegionalLanguageResolver) — Kannada in Karnataka, …
  * national languages (NationalPhrasebookResolver) — Central Atlas Tamazight in
    Morocco, and any national language not yet in phrases.json.

Pipeline: SearXNG (register/phrase-shaped queries) → Ollama (schema-constrained,
temp 0) → keep only the phrase keys the model was confident about. Generated
content is always rendered under a flag and is promotable to vetted in the admin.

A leaf-ish module: imports the canonical phrase set from discover.phrases_meta
(not discover.resolvers.language) so it never pulls the resolvers package in at
module load — the same cycle that bit discover.search.
"""
from __future__ import annotations

from discover.grounding import normalize_url
from discover.phrases_meta import PHRASE_KEYS, PHRASE_KEYSET
from discover.search import searxng_search
from discover.synth import DEFAULT_MODEL, synthesize_json

_GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "phrases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "enum": sorted(PHRASE_KEYSET)},
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


def _queries(lang_name: str) -> list[str]:
    return [
        f"{lang_name} basic phrases with pronunciation for travellers",
        f"how to say hello thank you please in {lang_name}",
        f"{lang_name} phrasebook english",
    ]


def _search(lang_name: str, *, searxng_url: str, timeout: int, max_snippets: int):
    seen, results = set(), []
    for q in _queries(lang_name):
        for r in searxng_search(q, base_url=searxng_url, timeout=timeout):
            key = normalize_url(r.url)
            if key and key not in seen:
                seen.add(key)
                results.append(r)
                if len(results) >= max_snippets:
                    return results
    return results


def _prompt(lang_name: str, results) -> str:
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


def generate_phrasebook(lang_name: str, *, searxng_url: str | None,
                        ollama_url: str | None, model: str = DEFAULT_MODEL,
                        timeout: int = 600, max_snippets: int = 12):
    """Generate a phrasebook for one language. Returns (book, results) — book is
    {key: {t, p}}, results are the SearXNG hits it was built from (for source
    attribution) — or None. Best-effort: any failure (endpoints unset, no search
    results, empty synthesis) degrades to None, so the caller falls back to a note.
    """
    if not searxng_url or not ollama_url:
        return None
    results = _search(lang_name, searxng_url=searxng_url, timeout=timeout,
                      max_snippets=max_snippets)
    if not results:
        return None
    out = synthesize_json(_prompt(lang_name, results), _GEN_SCHEMA,
                          base_url=ollama_url, system=_GEN_SYSTEM,
                          model=model, timeout=timeout)
    if not out:
        return None
    book: dict[str, dict] = {}
    for item in (out.get("phrases") or []):
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        text = (item.get("text") or "").strip()
        if key in PHRASE_KEYSET and text and key not in book:
            book[key] = {"t": text, "p": (item.get("pronunciation") or "").strip()}
    if not book:
        return None
    return book, results
