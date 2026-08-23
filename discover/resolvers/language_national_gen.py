"""National-language phrasebook generator (Yonder 3.7).

The deterministic LanguageResolver (build_deterministic) produces the national
language finding fast: vetted phrases where the skeleton has them, an honest
"not in Yonder's dataset yet" note where it doesn't (Central Atlas Tamazight in
Morocco, and any national language not yet curated).

This LLM-tier resolver closes that gap. For a country's national languages that
lack a vetted phrasebook, it GENERATES one via the shared grounded generator
(discover.phrasegen), then re-renders the WHOLE national finding — vetted +
freshly-generated (flagged) + notes — and returns it under the SAME
(topic="language", place_key=country) key. Running in the LLM tier, it lands
after the deterministic draft and upserts over it: the user sees the national
finding appear instantly (vetted) and then upgrade as the gaps fill in.

Generated books are handed to the job (finding.data["generated_phrasebooks"]) to
persist as `generated` PhrasebookPhrase rows — so a re-run reuses them (the
deterministic resolver then flags them straight from the DB) and the owner can
promote them to vetted. Endpoints unset, or nothing left to generate → None, so
the fast deterministic finding stands untouched.
"""
from __future__ import annotations

import logging

from discover.phrasegen import generate_phrasebook
from discover.places import DerivedPlace
from discover.resolvers.base import Finding, ResolverContext
from discover.resolvers.language import (_SOURCE, _lang_name, build_language_body,
                                        national_language_codes)
from discover.synth import DEFAULT_MODEL

log = logging.getLogger("yonder.discovery.llm")


class NationalPhrasebookResolver:
    topic = "language"
    scope = "country"

    def __init__(self, *, searxng_url: str | None, ollama_url: str | None,
                 model: str = DEFAULT_MODEL, timeout: int = 600,
                 max_snippets: int = 12):
        self.searxng_url = searxng_url
        self.ollama_url = ollama_url
        self.model = model
        self.timeout = timeout
        self.max_snippets = max_snippets

    def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None:
        if not self.searxng_url or not self.ollama_url:
            return None                          # LLM tier not configured
        country = place.country
        if not country:
            return None
        official, widely = national_language_codes(country)
        if not official:
            return None
        book_src = dict(ctx.phrasebook if ctx.phrasebook is not None else {})
        already_gen = set(ctx.generated_languages or ())

        # Which shown languages still have NO phrasebook at all (not vetted, not
        # previously generated)? Those are the gaps worth generating. English never
        # needs one.
        gaps = [c for c in (*official, *widely)
                if c != "en" and c not in book_src]
        if not gaps:
            return None                          # deterministic finding is complete

        fresh: dict[str, dict] = {}
        for code in gaps:
            gen = generate_phrasebook(
                _lang_name(code), searxng_url=self.searxng_url,
                ollama_url=self.ollama_url, model=self.model, timeout=self.timeout,
                max_snippets=self.max_snippets)
            if gen:
                book, _results = gen
                book_src[code] = book
                fresh[code] = book
        if not fresh:
            return None                          # generation found nothing new

        generated_codes = already_gen | set(fresh)
        body, data, confidence = build_language_body(
            place.label, official, official=True, also_codes=widely,
            phrasebook=book_src, generated_codes=generated_codes)
        data["generated_phrasebooks"] = fresh    # the job persists these
        log.info("language(national-gen): %s generated %s", country, list(fresh))
        return Finding(
            topic=self.topic, scope=self.scope, place_key=country,
            resolver="language.national_gen", confidence=confidence,
            sources=_SOURCE, data=data, body_md=body)
