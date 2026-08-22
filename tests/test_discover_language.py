"""Language resolver tests (SPEC-3-discovery.md §5) — offline: Babel (L1) +
the vetted phrasebook file (L2).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from discover.places import DerivedPlace
from discover.resolvers.language import (LanguageResolver, build_language_body,
                                         supported_languages)
from discover.resolvers.base import ResolverContext

_R = LanguageResolver()
_CTX = ResolverContext()


def test_multilingual_country_lists_all_official_languages():
    f = _R.resolve(DerivedPlace("CH", "Geneva", 46.2, 6.1), _CTX)
    assert f.topic == "language" and f.scope == "country" and f.place_key == "CH"
    assert f.confidence == "high"
    # L1: all three official languages named.
    assert "German" in f.body_md and "French" in f.body_md and "Italian" in f.body_md
    # L2: a vetted phrase from each.
    assert "Danke" in f.body_md and "Bonjour" in f.body_md and "Ciao" in f.body_md
    codes = {l["code"] for l in f.data["languages"]}
    assert codes == {"de", "fr", "it"}
    assert all(l["has_phrasebook"] for l in f.data["languages"])


def test_japanese_includes_script_and_romaji():
    f = _R.resolve(DerivedPlace("JP", "Tokyo", 35.6, 139.7), _CTX)
    assert "こんにちは" in f.body_md and "konnichiwa" in f.body_md


def test_language_without_phrasebook_is_noted_not_guessed():
    # Morocco → Arabic + Tamazight; neither is in the vetted dataset.
    f = _R.resolve(DerivedPlace("MA", "Harar", 31.6, -8.0), _CTX)
    assert "Arabic" in f.body_md
    assert "isn't in Yonder's dataset" in f.body_md
    assert f.confidence == "medium"          # no phrasebook available
    assert all(not l["has_phrasebook"] for l in f.data["languages"])


def test_english_country_needs_no_phrasebook():
    f = _R.resolve(DerivedPlace("CA", "Vancouver", 49.2, -123.1), _CTX)
    assert "no phrasebook needed" in f.body_md
    # French (the other CA official language) still gets its phrases.
    assert "Bonjour" in f.body_md


def test_defacto_official_language_resolves_not_flagged():
    # The US has no *de jure* official language — get_official_languages('US')
    # returns () — but English is de facto official. It must resolve to English,
    # not flag as "couldn't determine".
    f = _R.resolve(DerivedPlace("US", "Phoenix", 35.1, -106.6), _CTX)
    assert f.confidence != "flagged"
    assert "no phrasebook needed" in f.body_md          # English, spoken here
    assert "Couldn't determine" not in f.body_md


def test_unknown_country_is_flagged():
    f = _R.resolve(DerivedPlace("ZZ", None, None, None), _CTX)
    assert f.confidence == "flagged"


def test_placeless_returns_none():
    assert _R.resolve(DerivedPlace(None, None, None, None), _CTX) is None


# ------------------------------------------------------------------ picker

def test_supported_languages_are_the_phrasebook_set():
    codes = {c for c, _ in supported_languages()}
    assert {"fr", "es", "de", "it", "pt", "nl", "ja"} <= codes
    assert "en" not in codes                      # English carries no phrasebook


def test_build_language_body_from_manual_picks():
    # The Morocco fix: pick French manually and get the French phrasebook.
    body, data, conf = build_language_body("Harar, MA", ["fr"], official=False)
    assert "Bonjour" in body and "official" not in body.split("\n")[0]
    assert conf == "high"
    assert [l["code"] for l in data["languages"]] == ["fr"]


def test_build_language_body_empty_is_flagged():
    body, data, conf = build_language_body("X", [], official=False)
    assert conf == "flagged" and data["languages"] == []
