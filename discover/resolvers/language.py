"""Language resolver — layered, deterministic tiers (SPEC-3-discovery.md §5).

Language is the one topic where "static per country" is wrong on two axes:
country ≠ language (CH = de/fr/it) and language ≠ register (CH-German greets
"Grüezi", not "Guten Tag"). This resolver ships the two DETERMINISTIC layers:

  L1 — WHICH languages: the country's official languages from CLDR via Babel,
       offline. Region refinement is out of MVP scope (D-69): a multilingual
       country simply lists all its official languages.
  L2 — the STABLE SKELETON: ~14 vetted phrases per language from
       discover/data/phrases.json. Curated, never generated — this layer must be
       right (a wrong transliteration you then say to a stranger is the failure
       mode), so a language without a phrasebook gets a note, not a guess.

L3 — the regional register ("Grüezi") — is search+LLM-backed and flagged, and
lands with the LLM tier, layered on top of this skeleton.
"""
from __future__ import annotations

import json
from pathlib import Path

from discover.places import DerivedPlace
from discover.resolvers.base import Finding, ResolverContext

_DATA = Path(__file__).resolve().parent.parent / "data" / "phrases.json"

# Canonical phrase set — defined in the leaf discover.phrases_meta (so the shared
# generator can import it without the resolvers-package cycle); re-exported here.
from discover.phrases_meta import PHRASE_KEYS  # noqa: E402

_SOURCE = [{"title": "Official languages: CLDR/Babel · phrases: Wikivoyage (CC BY-SA), vetted"}]


def _load() -> dict:
    with open(_DATA, encoding="utf-8") as fh:
        return {k: v for k, v in json.load(fh).items() if not k.startswith("_")}


# The JSON default — the fallback when no DB-backed phrasebook is injected (tests
# / pre-seed). At runtime the resolvers receive the editable phrasebook via the
# ResolverContext / an explicit arg (discover/langdata), so this is only the seed.
_PHRASES = _load()


def _base_lang(code: str) -> str:
    """The base ISO 639-1 code — 'de_CH'/'gsw' region/script stripped to 'de'."""
    return code.split("_")[0].split("-")[0]


def _distinct_base(codes) -> list[str]:
    """Base language codes, de-duplicated, original order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for c in codes:
        base = _base_lang(c)
        if base not in seen:
            seen.add(base)
            out.append(base)
    return out


def _lang_name(code: str) -> str:
    try:
        from babel import Locale
        return Locale.parse(code).get_display_name("en") or code
    except Exception:
        return code


def official_language_codes(country: str) -> list[str]:
    """Distinct base ISO-639-1 codes officially spoken in `country`, de-facto
    fallback and all (shared by the national resolver and the regional one, which
    subtracts these so it never re-lists a national language)."""
    try:
        from babel.languages import get_official_languages
        codes = get_official_languages(country) or \
            get_official_languages(country, de_facto=True)
    except Exception:
        codes = ()
    out: list[str] = []
    seen: set[str] = set()
    for c in codes:
        base = _base_lang(c)
        if base not in seen:
            seen.add(base)
            out.append(base)
    return out


def national_language_codes(country: str) -> tuple[list[str], list[str]]:
    """(official, widely_spoken) base language codes for a country: de-jure
    officials, plus the de-facto-only extras (French in Morocco, Swiss German in
    Switzerland). Shared by the deterministic national resolver and the national
    phrasebook generator so both agree on which languages a country has."""
    try:
        from babel.languages import get_official_languages
        de_jure = get_official_languages(country)
        de_facto = get_official_languages(country, de_facto=True)
        if not de_jure:                  # US etc. — de facto IS the official set
            de_jure = de_facto
    except Exception:
        de_jure = de_facto = ()
    official = _distinct_base(de_jure)
    widely = [c for c in _distinct_base(de_facto) if c not in official]
    return official, widely


def supported_languages(phrasebook: dict | None = None) -> list[tuple[str, str]]:
    """(code, display name) for every language with a phrasebook — the options
    offered by the manual language picker (Edit on a language finding). Reads the
    injected (DB-backed) phrasebook when given, else the JSON seed."""
    book = phrasebook if phrasebook is not None else _PHRASES
    codes = [k for k in book if not k.startswith("_")]
    return sorted(((c, _lang_name(c)) for c in codes), key=lambda cn: cn[1])


def _render_one_language(code: str, name: str, book_src: dict, sections: list,
                         data_langs: list, *, widely: bool = False,
                         generated: bool = False) -> bool:
    """Append one language's phrasebook (or its 'not yet' note) to `sections`;
    record it in `data_langs`. A GENERATED language is tagged inline as machine-
    generated. Returns whether it contributed a phrasebook."""
    entry_rec = {"code": code, "name": name, "has_phrasebook": False,
                 "widely_spoken": widely, "generated": generated}
    if code == "en":
        data_langs.append(entry_rec)
        sections += [f"**{name}** — spoken here, no phrasebook needed.", ""]
        return False
    book = book_src.get(code)
    entry_rec["has_phrasebook"] = bool(book)
    data_langs.append(entry_rec)
    tag = " _(generated — verify)_" if (book and generated) else ""
    sections.append(f"**{name}**{tag}")
    if book:
        for key, plabel in PHRASE_KEYS:
            e = book.get(key)
            if e:
                pron = f" · {e['p']}" if e.get("p") else ""
                sections.append(f"- {plabel} — **{e['t']}**{pron}")
    else:
        sections.append(f"- A vetted phrasebook for {name} isn't in Yonder's "
                        f"dataset yet.")
    sections.append("")
    return bool(book)


def build_language_body(label: str, codes: list[str], *, official: bool = True,
                        also_codes: list[str] | None = None,
                        phrasebook: dict | None = None,
                        generated_codes=frozenset()) -> tuple[str, dict, str]:
    """Render the phrasebook body for a language-code list — shared by the auto
    resolver (CLDR-detected codes) and the manual picker (Edit). `also_codes` are
    languages that aren't *de jure* official but are widely spoken (French in
    Morocco, Swiss German in Switzerland), rendered under a separate heading.
    `generated_codes` are languages whose phrases are LLM-generated (not vetted) —
    tagged inline and forcing `flagged` confidence + a banner. Reads the injected
    (DB-backed) phrasebook when given, else the JSON seed. Returns
    (body_md, data, confidence)."""
    book_src = phrasebook if phrasebook is not None else _PHRASES
    gen = frozenset(generated_codes or ())
    names = [(c, _lang_name(c)) for c in codes]
    if not names:
        return ("_No languages selected._", {"languages": []}, "flagged")

    qual = "official " if official else ""
    header = (f"**{label}** — {qual}language{'s' if len(names) > 1 else ''}: "
              f"{', '.join(n for _, n in names)}.")
    sections = [header, ""]
    if any(c in gen for c in codes) or any(c in gen for c in (also_codes or [])):
        sections.append("> ⚠️ **Some phrasebooks below are machine-generated** from "
                        "web sources, not vetted — double-check before you say them.")
        sections.append("")
    data_langs: list[dict] = []
    any_book = False
    for code, name in names:
        any_book = _render_one_language(code, name, book_src, sections, data_langs,
                                        widely=False, generated=code in gen) or any_book

    also = [(c, _lang_name(c)) for c in (also_codes or []) if c not in codes]
    if also:
        sections.append(f"**Also widely spoken:** {', '.join(n for _, n in also)} "
                        "(not an official language, but common day to day).")
        sections.append("")
        for code, name in also:
            any_book = _render_one_language(code, name, book_src, sections, data_langs,
                                            widely=True, generated=code in gen) or any_book

    sections.append("_Everyday phrases are standard forms; local greetings and "
                    "slang vary by region (added later by the research tier)._")
    shown = {d["code"] for d in data_langs}
    confidence = ("flagged" if (gen & shown) else "high" if any_book else "medium")
    return ("\n".join(sections).strip(), {"languages": data_langs}, confidence)


class LanguageResolver:
    topic = "language"
    scope = "country"

    def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None:
        country = place.country
        if not country:
            return None
        langs, widely = national_language_codes(country)
        if not langs:
            return Finding(
                topic=self.topic, scope=self.scope, place_key=country,
                resolver="language.static", confidence="flagged", sources=_SOURCE,
                body_md=f"Couldn't determine the official language(s) for "
                        f"**{country}**. Use the **Languages** button to pick one "
                        f"or more.")

        # Prior-generated languages (persisted by the national generator) live in
        # ctx.phrasebook too — flag them so this deterministic finding never shows
        # generated phrases as if they were vetted.
        body, data, confidence = build_language_body(
            place.label, langs, official=True, also_codes=widely,
            phrasebook=ctx.phrasebook,
            generated_codes=frozenset(ctx.generated_languages or ()))
        return Finding(
            topic=self.topic, scope=self.scope, place_key=country,
            resolver="language.static", confidence=confidence, sources=_SOURCE,
            data=data, body_md=body)
