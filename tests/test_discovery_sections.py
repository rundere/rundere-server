"""Discovery sections (4.5, YONDER-166): topics grouped into a rail of seven
sections, Practical holding plugs/currency/visa, and the currency calculator
in the trip header."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from discover import sections  # noqa: E402

try:
    from fastapi.testclient import TestClient
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, DiscoveryFinding, Trip
    Base.metadata.create_all(engine)
    DEPS = True
except ImportError:
    DEPS = False


def test_every_section_is_returned_with_counts():
    built = sections.build({
        "plugs": [{"status": "approved"}],
        "currency": [{"status": "draft"}],
        "language_local": [{"status": "draft", "external": True}],
        "language": [{"status": "draft"}],
    })
    assert [s["key"] for s in built] == ["weather", "food", "events", "language",
                                         "safety", "research", "practical"]
    by = {s["key"]: s for s in built}
    assert by["practical"]["count"] == 2
    assert [g["topic"] for g in by["practical"]["groups"]] == ["plugs", "currency"]
    assert by["language"]["from_agent"] == 1
    assert [g["label"] for g in by["language"]["groups"]] == ["National language",
                                                             "Local phrases & register"]
    assert by["food"]["count"] == 0 and by["food"]["groups"] == []
    assert sections.section_of("visa") == "practical"
    assert sections.section_of("language_regional") == "language"


if DEPS:
    def _trip_with(findings):
        with SessionLocal() as s:
            t = Trip(name="Sections trip", start_date="2026-10-10", end_date="2026-10-20")
            s.add(t)
            s.flush()
            for topic, key, data in findings:
                s.add(DiscoveryFinding(trip_id=t.id, topic=topic, scope="country",
                                       place_key=key, status="draft", body_md=f"{topic} body",
                                       data=data))
            s.commit()
            return t.id

    def _cleanup(tid):
        with SessionLocal() as s:
            s.query(DiscoveryFinding).filter_by(trip_id=tid).delete()
            s.query(Trip).filter_by(id=tid).delete()
            s.commit()

    RATES = {"currency": "MAD", "date": "2026-09-27", "rates": {"MAD": {"per_CAD": 7.3}}}

    def test_trip_page_has_rail_practical_and_currency_button():
        tid = _trip_with([("plugs", "MA", None), ("currency", "MA", RATES),
                          ("weather", "MA:Gondar", None)])
        try:
            page = TestClient(app).get(f"/trips/{tid}").text
            assert 'class="yn-disc-rail"' in page and page.count('aria-labelledby="dtab-') == 7
            assert "Plugs &amp; voltage" in page and "Practical" in page
            assert "rates from 2026-09-27" in page                   # compact card
            assert "Currency · MAD" in page                          # header button
            assert f"/trips/{tid}/finding/" in page and "/calculator" in page
        finally:
            _cleanup(tid)

    def test_several_currencies_become_a_menu_and_none_hides_it():
        tid = _trip_with([("currency", "MA", RATES),
                          ("currency", "ES", {**RATES, "currency": "EUR"})])
        try:
            page = TestClient(app).get(f"/trips/{tid}").text
            assert 'class="yn-menu"' in page and "MAD · MA" in page and "EUR · ES" in page
        finally:
            _cleanup(tid)
        tid = _trip_with([("currency", "CU", {"currency": "CUP", "rates": {}})])
        try:
            page = TestClient(app).get(f"/trips/{tid}").text
            assert "Currency ·" not in page and 'class="yn-menu"' not in page
            assert "no reference rate" in page
        finally:
            _cleanup(tid)
