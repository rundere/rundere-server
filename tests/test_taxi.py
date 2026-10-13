"""Taxi is its own ground mode and timeline activity (owner, 2026-10-12; D-193).
Uber, Lyft and the like are taxis; the older "rideshare" mode files as one too.
In review, the kind of ground booking is a dropdown the reviewer can correct."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.committing import build_yonder_activity  # noqa: E402
from app.trips.types import get_type, map_query  # noqa: E402
from resolve.keys import keys_for  # noqa: E402

TAXI = {"item_type": "ground_transport", "booking_status": "confirmed",
        "booking_reference": "UBR-123", "provider": {"name": "Uber"},
        "ground_transport": {
            "mode": "taxi",
            "departure": {"place": {"raw": "Residence Inn Phoenix North",
                                    "address": "4331 Desert Willow Way, Phoenix, NM"},
                          "time": {"local": "2026-10-11T03:15", "precision": "datetime"}},
            "arrival": {"place": {"raw": "Phoenix International Sunport", "iata": "PHX"},
                        "time": {"local": "2026-10-11T03:26", "precision": "datetime"}}}}


def _with_mode(mode):
    p = json.loads(json.dumps(TAXI))
    p["ground_transport"]["mode"] = mode
    return p


def test_taxi_is_a_timeline_activity_type():
    t = get_type("taxi")
    assert t.key == "taxi" and t.label == "Taxi"
    assert [f["key"] for f in t.fields][:5] == ["when", "endWhen", "company", "from", "to"]
    assert t.title({"company": "Uber"}) == "Taxi · Uber"
    assert map_query("taxi", {"from": "A", "to": "B"}) == "A → B"


def test_a_taxi_logs_as_a_taxi_and_so_does_rideshare():
    for mode in ("taxi", "rideshare"):
        atype, values = build_yonder_activity(_with_mode(mode))
        assert atype == "taxi", mode
        assert values["company"] == "Uber"
        assert "4331 Desert Willow Way, Phoenix, NM" in values["from"]
        assert values["when"] == "2026-10-11T03:15" and values["confirmation"] == "UBR-123"
    assert build_yonder_activity(_with_mode("car"))[0] == "drive"   # a drive is still a drive


def test_a_booked_taxi_keys_on_its_reference():
    assert keys_for(TAXI).primary.startswith("taxi:")


def test_both_schemas_accept_taxi():
    for f in ("travel-extraction.schema.json", "travel-item.schema.json"):
        text = (ROOT / "schemas" / f).read_text()
        enums = [v["enum"] for v in _walk(json.loads(text))
                 if isinstance(v, dict) and "rideshare" in (v.get("enum") or [])]
        assert enums and all("taxi" in e for e in enums), f


def _walk(node):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, TravelItem
    Base.metadata.create_all(engine)
    client = TestClient(app)

    def _ground_item(mode="car"):
        with SessionLocal() as s:
            it = TravelItem(item_type="ground_transport", schema_version="1.4.0",
                            source={"type": "email", "ref": "m-taxi", "capture_context": {}},
                            extraction={"payload": _with_mode(mode)},
                            review={"required": False},
                            status={"extract": "ok", "resolve": "ok"})
            s.add(it); s.commit()
            return it.id

    def test_kind_is_a_dropdown_and_taxi_can_be_picked():
        item_id = _ground_item("car")
        html = client.get(f"/review/item/{item_id}").text
        assert 'name="f:/ground_transport/mode"' in html and "<select" in html
        assert '>Taxi (Uber, Lyft…)</option>' in html
        assert 'value="rideshare"' not in html               # only on items that have it
        client.post(f"/review/item/{item_id}/save", data={"f:/ground_transport/mode": "taxi"})
        with SessionLocal() as s:
            it = s.get(TravelItem, item_id)
            assert it.extraction["payload"]["ground_transport"]["mode"] == "taxi"

    def test_an_old_rideshare_item_keeps_its_option():
        item_id = _ground_item("rideshare")
        html = client.get(f"/review/item/{item_id}").text
        assert 'value="rideshare" selected' in html
