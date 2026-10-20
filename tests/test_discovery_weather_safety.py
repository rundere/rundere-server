"""Weather summary line (YONDER-167) and Safety & Transport subsections
(YONDER-168), Run·dere 4.5."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from discover.weather_summary import summarize  # noqa: E402

try:
    from fastapi.testclient import TestClient
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, DiscoveryFinding, Trip
    Base.metadata.create_all(engine)
    DEPS = True
except ImportError:
    DEPS = False


def _w(hi, lo, rain=0, days=5, mode="forecast"):
    return {"high_c": hi, "low_c": lo, "rain_days": rain, "days": days, "mode": mode}


def test_warm_trip_with_cool_nights():
    s = summarize([("Addis Ababa", _w(31, 18, 1, 6)), ("Gondar", _w(27, 12, 1, 5)),
                   ("Lalibela", _w(24, 14, 0, 3))])
    assert s["line"] == ("Warm and mostly dry — highs 24–31 °C across 3 cities; cool nights "
                         "down to 12 °C in Gondar; rain on 2 of 14 days.")
    assert s["hints"] == ["a warm layer for the evenings"] and not s["seasonal"]


def test_cold_wet_single_city_is_seasonal():
    s = summarize([("Reykjavik", _w(8, 2, 5, 7, "seasonal"))])
    assert s["line"] == "Cold and wet — highs around 8 °C; rain on 5 of 7 days."
    assert s["hints"] == ["warm clothes", "a rain layer or umbrella"] and s["seasonal"]


def test_variable_trip_names_the_extremes():
    s = summarize([("Dubai", _w(38, 28, 0, 4)), ("Innsbruck", _w(12, -1, 3, 4))])
    assert s["band"] == "Variable"
    assert "warmest in Dubai, coolest in Innsbruck" in s["line"]
    assert "freezing nights, down to -1 °C in Innsbruck" in s["line"]
    assert "sun protection and light clothing" in s["hints"] and "warm clothes" in s["hints"]


def test_high_dry_city_hints_d189():
    """YONDER-211: feels-like, dryness and altitude reach the line and the hints."""
    d = {**_w(18, 3, 0, 4), "feels_low_c": -1, "humidity_band": "very dry",
         "elevation_m": 1620, "high_altitude": True}
    s = summarize([("Prescott", d)])
    assert "feels near -1 °C at dawn" in s["line"]
    assert "very dry air" in s["line"] and "high altitude in Prescott (~1,620 m)" in s["line"]
    assert "warm clothes" in s["hints"]
    assert "lip balm, moisturiser and extra water" in s["hints"]
    assert "sunscreen, and take day 1 easy at altitude" in s["hints"]


def test_feels_like_alone_turns_on_the_warm_layer():
    """Air lows of 14 °C wouldn't ask for a layer; feeling like 9 °C does."""
    s = summarize([("Lima", {**_w(22, 14, 0, 3), "feels_low_c": 9,
                             "humidity_band": "humid"})])
    assert "a warm layer for the evenings" in s["hints"]
    assert "breathable clothes" in s["hints"] and "humid" in s["line"]


def test_no_usable_data():
    assert summarize([]) is None
    assert summarize([("X", {"mode": "forecast"})]) is None


if DEPS:
    SAFETY = {
        "safety": {"summary": "Generally safe; watch for pickpockets.",
                   "advisories": [{"text": "Avoid unlicensed guides",
                                   "source_url": "https://ex.com/a"}],
                   "scams": ["Fake closed-shop detours"], "corroborated": False},
        "transport": {"summary": "Walkable medina; petit taxis elsewhere.",
                      "public_transit": "Buses via Alsa; pay cash.",
                      "ride_hailing": ["Careem", "inDrive"], "apps": ["Alsa"],
                      "from_airport": "Bus 19 or a fixed-price taxi.", "tips": [],
                      "source_urls": []},
    }

    def test_page_shows_weather_line_and_safety_subsections():
        with SessionLocal() as s:
            t = Trip(name="WS trip", start_date="2026-10-10", end_date="2026-10-20")
            s.add(t)
            s.flush()
            s.add_all([
                DiscoveryFinding(trip_id=t.id, topic="weather", scope="city",
                                 place_key="ET:Harar", status="draft", body_md="w",
                                 data=_w(31, 18, 1, 6)),
                DiscoveryFinding(trip_id=t.id, topic="safety", scope="city",
                                 place_key="ET:Harar", status="draft",
                                 body_md="RAW SAFETY BODY", data=SAFETY),
                DiscoveryFinding(trip_id=t.id, topic="safety", scope="city",
                                 place_key="ET:Gondar", status="draft", resolver="agent",
                                 body_md="**Gondar**\n\nAgent safety notes", data={"agent": "Claude"}),
            ])
            s.commit()
            tid = t.id
        try:
            page = TestClient(app).get(f"/trips/{tid}").text
            assert "Hot and mostly dry — highs around 31 °C; rain on 1 of 6 days." in page
            assert "Pack: sun protection and light clothing" in page and "umbrella" not in page
            for h in ("Advisories", "Scams to know", "Public transit",
                      "Ride-hailing that works here", "From the airport"):
                assert h in page
            assert "Careem" in page and "RAW SAFETY BODY" not in page
            assert "single source" in page
            assert "Agent safety notes" in page        # markdown finding still renders
        finally:
            with SessionLocal() as s:
                s.query(DiscoveryFinding).filter_by(trip_id=tid).delete()
                s.query(Trip).filter_by(id=tid).delete()
                s.commit()
