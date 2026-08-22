"""Editable language data (Yonder 3.6) — the phrasebook + regional map as DB
data seeded from JSON, with the generated→vetted promotion path.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import SessionLocal, engine
from app.models import Base, PhrasebookPhrase, RegionalLanguage
from discover import langdata

Base.metadata.create_all(engine)


def _clean(s):
    s.query(PhrasebookPhrase).delete()
    s.query(RegionalLanguage).delete()
    s.commit()


# ------------------------------------------------------------- phrasebook

def test_seed_then_read_from_db():
    with SessionLocal() as s:
        _clean(s)
        langdata.seed_phrasebook_rows(s)
        book = langdata.phrasebook(s)
        # Hindi was seeded from phrases.json and is now readable from the DB.
        assert "hi" in book and book["hi"]["hello"]["t"] == "नमस्ते"
        # every seeded row is vetted → nothing generated-only
        assert langdata.generated_languages(s) == frozenset()


def test_seed_is_idempotent_and_preserves_edits():
    with SessionLocal() as s:
        _clean(s)
        langdata.seed_phrasebook_rows(s)
        row = s.get(PhrasebookPhrase, "hi:hello")
        row.text = "नमस्ते-EDITED"
        s.commit()
        langdata.seed_phrasebook_rows(s)             # re-seed must not clobber
        assert s.get(PhrasebookPhrase, "hi:hello").text == "नमस्ते-EDITED"


def test_persist_generated_then_promote():
    with SessionLocal() as s:
        _clean(s)
        langdata.seed_phrasebook_rows(s)
        # A generated Kannada book lands as `generated` rows.
        langdata.persist_generated(s, "kn", {
            "hello": {"t": "ನಮಸ್ಕಾರ", "p": "namaskara"},
            "thanks": {"t": "ಧನ್ಯವಾದ", "p": "dhanyavada"}})
        assert "kn" in langdata.generated_languages(s)   # generated-only
        assert langdata.phrasebook(s)["kn"]["hello"]["t"] == "ನಮಸ್ಕಾರ"
        # Promote to vetted (what the admin does): flip source.
        for r in s.query(PhrasebookPhrase).filter_by(lang_code="kn").all():
            r.source = "vetted"
        s.commit()
        assert "kn" not in langdata.generated_languages(s)


def test_persist_generated_never_overwrites_existing():
    with SessionLocal() as s:
        _clean(s)
        langdata.seed_phrasebook_rows(s)             # hi is vetted
        langdata.persist_generated(s, "hi", {"hello": {"t": "WRONG", "p": "x"}})
        # the vetted Hindi hello must survive
        assert s.get(PhrasebookPhrase, "hi:hello").text == "नमस्ते"


def test_persist_finding_generated_reads_the_finding_blob():
    with SessionLocal() as s:
        _clean(s)
        data = {"generated_phrasebooks": {"kn": {"hello": {"t": "ನಮಸ್ಕಾರ", "p": "n"}}}}
        langdata.persist_finding_generated(s, data)
        assert s.get(PhrasebookPhrase, "kn:hello") is not None


# ------------------------------------------------------------- regional map

def test_regional_seed_and_read():
    with SessionLocal() as s:
        _clean(s)
        langdata.seed_regional_rows(s)
        rmap = langdata.regional_map(s)
        assert "kn" in rmap["IN"]["karnataka"]
        assert "ca" in rmap["ES"]["catalonia"]


def test_regional_read_is_case_insensitive_on_region():
    with SessionLocal() as s:
        _clean(s)
        s.add(RegionalLanguage(id="IN:karnataka:kn", country="IN",
                               region="Karnataka", lang_code="kn"))
        s.commit()
        # stored "Karnataka" is lower-cased on read so the resolver's
        # place.region.lower() lookup matches.
        assert langdata.regional_map(s)["IN"]["karnataka"] == ["kn"]


def test_fallbacks_without_session():
    # No session → the JSON seed, so tests and pre-seed installs still work.
    assert "hi" in langdata.phrasebook(None)
    assert "kn" in langdata.regional_map(None)["IN"]["karnataka"]
    assert langdata.generated_languages(None) == frozenset()
