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

# Canonical phrase set + English labels — the order rendered for every language.
PHRASE_KEYS: list[tuple[str, str]] = [
    ("hello", "Hello"), ("goodbye", "Goodbye"), ("please", "Please"),
    ("thanks", "Thank you"), ("yes", "Yes"), ("no", "No"),
    ("excuse", "Excuse me"), ("sorry", "Sorry"),
    ("speak_english", "Do you speak English?"),
    ("dont_understand", "I don't understand"), ("how_much", "How much is it?"),
    ("toilet", "Where is the toilet?"), ("help", "Help!"), ("cheers", "Cheers!"),
]

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


def supported_languages(phrasebook: dict | None = None) -> list[tuple[str, str]]:
    """(code, display name) for every language with a phrasebook — the options
    offered by the manual language picker (Edit on a language finding). Reads the
    injected (DB-backed) phrasebook when given, else the JSON seed."""
    book = phrasebook if phrasebook is not None else _PHRASES
    codes = [k for k in book if not k.startswith("_")]
    return sorted(((c, _lang_name(c)) for c in codes), key=lambda cn: cn[1])


def _render_one_language(code: str, name: str, book_src: dict, sections: list,
                         data_langs: list, *, widely: bool = False) -> bool:
    """Append one language's phrasebook (or its 'not yet' note) to `sections`;
    record it in `data_langs`. Returns whether it contributed a phrasebook."""
    entry_rec = {"code": code, "name": name, "has_phrasebook": False,
                 "widely_spoken": widely}
    if code == "en":
        data_langs.append(entry_rec)
        sections += [f"**{name}** — spoken here, no phrasebook needed.", ""]
        return False
    book = book_src.get(code)
    entry_rec["has_phrasebook"] = bool(book)
    data_langs.append(entry_rec)
    sections.append(f"**{name}**")
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
                        phrasebook: dict | None = None) -> tuple[str, dict, str]:
    """Render the phrasebook body for a language-code list — shared by the auto
    resolver (CLDR-detected codes) and the manual picker (Edit). `also_codes` are
    languages that aren't *de jure* official but are widely spoken (French in
    Morocco, Swiss German in Switzerland), rendered under a separate heading. Reads
    the injected (DB-backed) phrasebook when given, else the JSON seed. Returns
    (body_md, data, confidence)."""
    book_src = phrasebook if phrasebook is not None else _PHRASES
    names = [(c, _lang_name(c)) for c in codes]
    if not names:
        return ("_No languages selected._", {"languages": []}, "flagged")

    qual = "official " if official else ""
    header = (f"**{label}** — {qual}language{'s' if len(names) > 1 else ''}: "
              f"{', '.join(n for _, n in names)}.")
    sections = [header, ""]
    data_langs: list[dict] = []
    any_book = False
    for code, name in names:
        any_book = _render_one_language(code, name, book_src, sections,
                                        data_langs, widely=False) or any_book

    also = [(c, _lang_name(c)) for c in (also_codes or []) if c not in codes]
    if also:
        sections.append(f"**Also widely spoken:** {', '.join(n for _, n in also)} "
                        "(not an official language, but common day to day).")
        sections.append("")
        for code, name in also:
            any_book = _render_one_language(code, name, book_src, sections,
                                            data_langs, widely=True) or any_book

    sections.append("_Everyday phrases are standard forms; local greetings and "
                    "slang vary by region (added later by the research tier)._")
    return ("\n".join(sections).strip(), {"languages": data_langs},
            "high" if any_book else "medium")


class LanguageResolver:
    topic = "language"
    scope = "country"

    def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None:
        country = place.country
        if not country:
            return None
        try:
            from babel.languages import get_official_languages
            de_jure = get_official_languages(country)
            de_facto = get_official_languages(country, de_facto=True)
            if not de_jure:
                # Countries with no *de jure* official language (the US, e.g.)
                # return () — the de facto set IS the official set for them.
                de_jure = de_facto
        except Exception:
            de_jure = de_facto = ()

        # Distinct base languages, official order preserved.
        langs = _distinct_base(de_jure)
        # De-facto languages that aren't de jure official — widely spoken but not
        # official (French in Morocco, Swiss German in Switzerland). Surfaced under
        # "Also widely spoken" so they're not lost (the earlier de-jure-only rule
        # hid French from every Morocco trip).
        widely = [c for c in _distinct_base(de_facto) if c not in langs]

        if not langs:
            return Finding(
                topic=self.topic, scope=self.scope, place_key=country,
                resolver="language.static", confidence="flagged", sources=_SOURCE,
                body_md=f"Couldn't determine the official language(s) for "
                        f"**{country}**. Use the **Languages** button to pick one "
                        f"or more.")

        body, data, confidence = build_language_body(
            place.label, langs, official=True, also_codes=widely,
            phrasebook=ctx.phrasebook)
        return Finding(
            topic=self.topic, scope=self.scope, place_key=country,
            resolver="language.static", confidence=confidence, sources=_SOURCE,
            data=data, body_md=body)
