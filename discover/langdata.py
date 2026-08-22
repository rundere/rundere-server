"""Editable language data (Yonder 3.6) — the phrasebook + region→language map.

Mirrors planning/catalog.py exactly: the JSON files under discover/data are the
shipped DEFAULTS that seed the DB at startup; at runtime the resolvers read from
the DB (owner-editable in the admin), falling back to the JSON when no session is
given (tests) or the tables are empty (pre-seed).

The phrasebook is unified: vetted rows (the L2 skeleton) and generated rows (LLM
fallbacks for languages the vetted set lacks) live in one table, distinguished by
`source`. `phrasebook()` returns every enabled phrase for rendering;
`generated_languages()` names the languages whose phrases are generated-only, so
the regional resolver can flag them and skip regenerating what's already stored.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent / "data"


def _load(name: str) -> dict:
    with open(_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _phrases_doc() -> dict:
    return _load("phrases.json")


@lru_cache(maxsize=1)
def _regional_doc() -> dict:
    return _load("regional_languages.json")


# ---------------------------------------------------------------- phrasebook

def seed_phrasebook() -> dict:
    """The shipped default phrasebook (from phrases.json), as
    `{lang: {key: {t, p}}}` — the seed and the no-session fallback."""
    return {k: v for k, v in _phrases_doc().items() if not k.startswith("_")}


def phrasebook(session=None) -> dict:
    """The active phrasebook as `{lang: {key: {t, p}}}` — every enabled phrase.
    From the DB when a session is given, else the JSON seed (tests / pre-seed)."""
    if session is None:
        return seed_phrasebook()
    from app.models import PhrasebookPhrase
    rows = session.query(PhrasebookPhrase).filter(
        PhrasebookPhrase.enabled.is_(True)).all()
    if not rows:
        return seed_phrasebook()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r.lang_code, {})[r.phrase_key] = {
            "t": r.text, "p": r.pronunciation or ""}
    return out


def generated_languages(session=None) -> frozenset[str]:
    """Languages whose stored phrases are generated-only (no vetted row) — the
    regional resolver flags these and won't regenerate them. Empty without a
    session (the JSON seed is all vetted)."""
    if session is None:
        return frozenset()
    from app.models import PhrasebookPhrase
    rows = session.query(PhrasebookPhrase.lang_code, PhrasebookPhrase.source).filter(
        PhrasebookPhrase.enabled.is_(True)).all()
    has_vetted, seen = set(), set()
    for code, source in rows:
        seen.add(code)
        if source == "vetted":
            has_vetted.add(code)
    return frozenset(seen - has_vetted)


def seed_phrasebook_rows(session) -> None:
    """Insert any phrase missing from the DB (JSON = defaults, DB = truth) — same
    idempotent contract as seed_catalog. Owner edits/deletions are never
    overwritten; only genuinely-new (lang, key) pairs are added, as `vetted`."""
    from app.models import PhrasebookPhrase
    have = {r.id for r in session.query(PhrasebookPhrase.id).all()}
    for lang, book in seed_phrasebook().items():
        for key, entry in (book or {}).items():
            pid = f"{lang}:{key}"
            if pid in have or not isinstance(entry, dict):
                continue
            session.add(PhrasebookPhrase(
                id=pid, lang_code=lang, phrase_key=key,
                text=entry.get("t", ""), pronunciation=entry.get("p", ""),
                source="vetted"))
    session.commit()


def persist_generated(session, lang_code: str, book: dict) -> None:
    """Store a generated phrasebook as `generated` rows so it's reusable and
    promotable. Never overwrites an existing row (a vetted phrase or an owner's
    edit wins); only fills genuinely-missing (lang, key) pairs."""
    from app.models import PhrasebookPhrase
    if not book:
        return
    have = {r.phrase_key for r in session.query(PhrasebookPhrase.phrase_key)
            .filter(PhrasebookPhrase.lang_code == lang_code).all()}
    added = False
    for key, entry in book.items():
        if key in have or not isinstance(entry, dict) or not entry.get("t"):
            continue
        session.add(PhrasebookPhrase(
            id=f"{lang_code}:{key}", lang_code=lang_code, phrase_key=key,
            text=entry.get("t", ""), pronunciation=entry.get("p", ""),
            source="generated"))
        added = True
    if added:
        session.commit()


def persist_finding_generated(session, finding_data: dict | None) -> None:
    """Persist any `generated_phrasebooks` a resolver put on its finding — called
    by the discovery loop after each finding so a generated regional phrasebook
    becomes reusable + promotable. Best-effort; a DB hiccup never fails the run."""
    books = (finding_data or {}).get("generated_phrasebooks") or {}
    for lang, book in books.items():
        try:
            persist_generated(session, lang, book)
        except Exception:                         # pragma: no cover - defensive
            session.rollback()


# ------------------------------------------------------------ regional map

def seed_regional() -> dict:
    """The shipped default region→language map (from regional_languages.json), as
    `{country: {region_lower: [lang_code]}}`."""
    return {k: v for k, v in _regional_doc().items() if not k.startswith("_")}


def regional_map(session=None) -> dict:
    """The active region→language map as `{country: {region_lower: [lang]}}`.
    From the DB when a session is given, else the JSON seed. Region names are
    lower-cased here so a lookup on the geocoded region name is case-insensitive."""
    if session is None:
        return {c: {r.lower(): list(langs) for r, langs in regions.items()}
                for c, regions in seed_regional().items()}
    from app.models import RegionalLanguage
    rows = session.query(RegionalLanguage).filter(
        RegionalLanguage.enabled.is_(True)).all()
    if not rows:
        return regional_map(None)
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r.country.upper(), {}).setdefault(
            r.region.strip().lower(), []).append(r.lang_code)
    return out


def seed_regional_rows(session) -> None:
    """Insert any region→language mapping missing from the DB (idempotent)."""
    from app.models import RegionalLanguage
    have = {r.id for r in session.query(RegionalLanguage.id).all()}
    for country, regions in seed_regional().items():
        for region, langs in (regions or {}).items():
            for lang in (langs or []):
                rid = f"{country.upper()}:{region.strip().lower()}:{lang}"
                if rid in have:
                    continue
                session.add(RegionalLanguage(
                    id=rid, country=country.upper(),
                    region=region.strip().lower(), lang_code=lang))
    session.commit()
