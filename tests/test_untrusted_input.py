"""Security review, area 5: untrusted input (YONDER-293).

The currency calculator puts rates into a <script>, and a currency code was
free text; the bookmarklet's hand-off tab captured whatever any site posted to
it without asking. Auth off here (the single-user world), as in the suite.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, DiscoveryFinding, Trip

    Base.metadata.create_all(engine)
    client = TestClient(app)

    _EVIL = "</SCRIPT><IMG SRC=X ONERROR=&#X61;LERT(1)>"

    def test_calculator_json_cannot_close_its_script_tag():
        with SessionLocal() as s:
            t = Trip(name="Untrusted calc", source="manual")
            s.add(t)
            s.flush()
            f = DiscoveryFinding(trip_id=t.id, user_id=None, topic="currency", scope="country",
                                 place_key="JP", body_md="**JP**", data={
                                     "currency": "JPY", "date": "2031-01-01",
                                     "rates": {"JPY": {"per_CAD": 110.0, f"per_{_EVIL}": 2.0}}})
            s.add(f)
            s.commit()
            tid, fid = t.id, f.id
        r = client.get(f"/trips/{tid}/finding/{fid}/calculator")
        assert r.status_code == 200
        script = r.text[r.text.index("var perPivot"):]
        script = script[:script.index("</script>")]
        assert "</SCRIPT" not in script and "<IMG" not in script   # escaped as <…

    def test_currency_edit_keeps_iso_codes_only():
        with SessionLocal() as s:
            t = Trip(name="Untrusted edit", source="manual")
            s.add(t)
            s.flush()
            f = DiscoveryFinding(trip_id=t.id, user_id=None, topic="currency", scope="country",
                                 place_key="JP", body_md="**JP**",
                                 data={"currency": "JPY", "rates": {}})
            s.add(f)
            s.commit()
            tid, fid = t.id, f.id
        client.post(f"/trips/{tid}/finding/{fid}/edit",
                    data={"cur_base": ["cad", _EVIL, "ZZZ"], "cur_rate": ["110", "2", "3"]})
        with SessionLocal() as s:
            data = s.get(DiscoveryFinding, fid).data
        assert data["bases"] == ["CAD"]
        assert set(data["rates"]["JPY"]) == {"per_CAD"}

    def test_handoff_waits_for_a_confirm_click():
        """Any site can open the hand-off tab and post to it; nothing is captured
        until the person confirms, in a tab no site can frame."""
        page = client.get("/capture/handoff").text
        assert 'id="hand-go"' in page and "e.origin" in page
        handler = page[page.index("addEventListener('message'"):page.index("hand-go').addEventListener")]
        assert "htmx.trigger" not in handler                 # no auto-submit on message

    def test_currency_codes_must_be_real_iso_codes():
        """Every picker offers Babel's ISO 4217 list, and the one server-side
        validator holds expenses, budget, rates and Discover edits to it."""
        import pytest
        from app.expenses import currency_code
        assert currency_code(" eur ") == "EUR"
        for bad in ("ZZZ", "EURO", "", None, "</SCRIPT>"):
            with pytest.raises(ValueError):
                currency_code(bad)
