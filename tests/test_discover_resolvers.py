"""Deterministic resolver + runner tests (SPEC-3-discovery.md §3/§4/§6).

Offline: plugs is pure static; currency/weather have their HTTP helper stubbed.
The runner is exercised against a real DB session with a fake airport resolver.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from discover.places import DerivedPlace
from discover.resolvers import (CurrencyResolver, PlugsResolver, WeatherResolver,
                                build_deterministic)
from discover.resolvers.base import ResolverContext
import discover.resolvers.currency as currency_mod
import discover.resolvers.weather as weather_mod


def _ctx(**kw):
    base = dict(start_date="2026-09-01", end_date="2026-09-10", today="2026-08-14")
    base.update(kw)
    return ResolverContext(**base)


# ------------------------------------------------------------------ plugs

def test_plugs_known_country_warns_on_adapter_and_voltage():
    f = PlugsResolver().resolve(DerivedPlace("CH", "Geneva", 46.2, 6.1), _ctx())
    assert f.topic == "plugs" and f.scope == "country" and f.place_key == "CH"
    assert f.confidence == "high"
    assert "Type J" in f.body_md and "230V" in f.body_md
    assert "adapter" in f.body_md.lower()        # A/B don't fit J
    assert "Voltage" in f.body_md                 # 230 vs home 120


def test_plugs_us_no_adapter_no_voltage_warning():
    f = PlugsResolver().resolve(DerivedPlace("US", "Glendale", 33, -112), _ctx())
    assert "adapter" not in f.body_md.lower()
    assert "Voltage" not in f.body_md


def test_plugs_unknown_country_is_flagged():
    f = PlugsResolver().resolve(DerivedPlace("ZZ", None, None, None), _ctx())
    assert f.confidence == "flagged" and "isn't in Yonder" in f.body_md


# ------------------------------------------------------------------ currency

def test_currency_builds_rate_snapshot(monkeypatch):
    def fake_get_json(url, timeout=12):
        base = "CAD" if "base=CAD" in url else "USD"
        rate = 0.58 if base == "CAD" else 0.72
        return {"base": base, "date": "2026-08-13", "rates": {"CHF": rate}}
    monkeypatch.setattr(currency_mod, "get_json", fake_get_json)

    f = CurrencyResolver().resolve(DerivedPlace("CH", "Geneva", 46, 6), _ctx())
    assert f.confidence == "high" and f.place_key == "CH"
    assert f.data["currency"] == "CHF"
    assert f.data["rates"]["CHF"] == {"per_CAD": 0.58, "per_USD": 0.72}
    assert "1 CAD = 0.58 CHF" in f.body_md
    assert f.ttl_seconds == 86400


def test_currency_parses_frankfurter_v2_list_body(monkeypatch):
    # Live v2 shape: a list of {date, base, quote, rate} rows (was a crash:
    # AttributeError: 'list' object has no attribute 'get').
    def fake_get_json(url, timeout=12):
        base = "CAD" if "base=CAD" in url else "USD"
        rate = 0.58 if base == "CAD" else 0.72
        return [{"date": "2026-09-27", "base": base, "quote": "CHF", "rate": rate}]
    monkeypatch.setattr(currency_mod, "get_json", fake_get_json)

    f = CurrencyResolver().resolve(DerivedPlace("CH", "Geneva", 46, 6), _ctx())
    assert f.confidence == "high"
    assert f.data["rates"]["CHF"] == {"per_CAD": 0.58, "per_USD": 0.72}
    assert f.data["date"] == "2026-09-27"


def test_currency_v2_empty_list_is_flagged(monkeypatch):
    monkeypatch.setattr(currency_mod, "get_json", lambda url, timeout=12: [])
    f = CurrencyResolver().resolve(DerivedPlace("MA", "Tangier", 35, -6), _ctx())
    assert f.confidence == "flagged" and not f.data["rates"]


def test_currency_unquoted_currency_is_flagged(monkeypatch):
    # Frankfurter returns no rate for this currency → no rates, flagged, still names it.
    monkeypatch.setattr(currency_mod, "get_json",
                        lambda url, timeout=12: {"date": "2026-08-13", "rates": {}})
    f = CurrencyResolver().resolve(DerivedPlace("MA", "Tangier", 35, -6), _ctx())
    assert f.confidence == "flagged"
    assert "MAD" in f.body_md and not f.data["rates"]


# ------------------------------------------------------------------ weather

def _daily(highs, lows, precip):
    return {"daily": {"temperature_2m_max": highs, "temperature_2m_min": lows,
                      "precipitation_sum": precip}}


def test_weather_seasonal_when_beyond_forecast_horizon(monkeypatch):
    captured = {}
    def fake_get_json(url, timeout=12):
        captured["url"] = url
        return _daily([22, 24, 20], [12, 13, 11], [0, 3.0, 0.2])
    monkeypatch.setattr(weather_mod, "get_json", fake_get_json)

    # Trip 2026-09 vs today 2026-08-14 → beyond the 16-day horizon → archive, prior year.
    f = WeatherResolver().resolve(DerivedPlace("CH", "Geneva", 46.2, 6.1), _ctx())
    assert f.confidence == "medium"                 # seasonal guide
    assert "typical" in f.body_md.lower()
    assert "archive" in captured["url"]             # hit the archive endpoint
    assert "2025-09-01" in captured["url"]          # shifted a year back
    assert "highs ~22°C" in f.body_md               # mean(22,24,20)=22
    assert "1 day(s) with rain" in f.body_md         # only the 3.0mm day


def test_weather_straddles_horizon_blends_forecast_and_seasonal(monkeypatch):
    # today 2026-08-14, horizon = today+15 = 2026-08-29. Trip starts inside the
    # horizon (08-20) but ends well past it (09-10) — the front segment must be
    # a real forecast, the tail a prior-year seasonal estimate, combined into
    # ONE finding (findings are one-per-place) rather than the tail silently
    # vanishing.
    def fake_get_json(url, timeout=12):
        if "archive" in url:
            return _daily([18, 20], [8, 9], [0, 0])
        return _daily([30, 32], [20, 22], [5.0, 0])
    monkeypatch.setattr(weather_mod, "get_json", fake_get_json)

    f = WeatherResolver().resolve(DerivedPlace("CH", "Geneva", 46.2, 6.1),
                                  _ctx(start_date="2026-08-20", end_date="2026-09-10",
                                       today="2026-08-14"))
    assert f.confidence == "medium"
    assert "Real forecast through 2026-08-29" in f.body_md
    assert "seasonal" in f.body_md.lower()
    assert f.data["mode"] == "forecast+seasonal"
    # mean across BOTH segments' daily series, not just one
    assert f.data["high_c"] == round((30 + 32 + 18 + 20) / 4)
    assert f.data["rain_days"] == 1


def test_weather_forecast_within_horizon(monkeypatch):
    monkeypatch.setattr(weather_mod, "get_json",
                        lambda url, timeout=12: _daily([25], [15], [0]))
    f = WeatherResolver().resolve(DerivedPlace("CH", "Geneva", 46.2, 6.1),
                                  _ctx(start_date="2026-08-20", end_date="2026-08-22",
                                       today="2026-08-14"))
    assert f.confidence == "high" and "forecast" in f.body_md.lower()


def test_weather_no_dates_is_flagged():
    f = WeatherResolver().resolve(DerivedPlace("CH", "Geneva", 46.2, 6.1),
                                  _ctx(start_date=None, end_date=None))
    assert f.confidence == "flagged"


def test_weather_skips_placeless_city():
    assert WeatherResolver().resolve(DerivedPlace("CH", "Geneva", None, None),
                                     _ctx()) is None


def test_build_deterministic_wires_all_topics():
    topics = {r.topic for r in build_deterministic()}
    assert topics == {"plugs", "currency", "weather", "language"}


# ---------------------------------------------------------------- D-189, YONDER-211

def _prescott_daily():
    """A stubbed Open-Meteo answer for a high, dry city (Prescott-like)."""
    return {"elevation": 1620.0, "daily": {
        "temperature_2m_max": [18, 18], "temperature_2m_min": [3, 3],
        "precipitation_sum": [0, 0],
        "apparent_temperature_max": [16, 16], "apparent_temperature_min": [-1, -1],
        "relative_humidity_2m_mean": [20, 20],
        "relative_humidity_2m_min": [10, 10], "relative_humidity_2m_max": [35, 35]}}


def test_weather_high_dry_city_carries_feels_like_humidity_and_altitude(monkeypatch):
    captured = {}
    def fake_get_json(url, timeout=12):
        captured["url"] = url
        return _prescott_daily()
    monkeypatch.setattr(weather_mod, "get_json", fake_get_json)
    f = WeatherResolver().resolve(DerivedPlace("US", "Prescott", 34.54, -112.47),
                                  _ctx(start_date="2026-08-20", end_date="2026-08-21",
                                       today="2026-08-14"))
    for var in ("apparent_temperature_max", "relative_humidity_2m_mean"):
        assert var in captured["url"]                       # asked for in the same call
    d = f.data
    assert (d["feels_high_c"], d["feels_low_c"]) == (16, -1)
    assert (d["humidity_pct"], d["humidity_band"]) == (20, "very dry")
    assert d["elevation_m"] == 1620 and d["high_altitude"] is True
    assert d["swing_c"] == 15
    assert "Feels like ~16°C by day and ~-1°C at dawn." in f.body_md
    assert "very dry" in f.body_md and "high altitude" in f.body_md
    assert "Big day/night swing" in f.body_md


def test_weather_archive_modes_carry_the_new_fields_too(monkeypatch):
    """Seasonal (prior-year archive) carries them as well as forecast does."""
    monkeypatch.setattr(weather_mod, "get_json", lambda url, timeout=12: _prescott_daily())
    f = WeatherResolver().resolve(DerivedPlace("US", "Prescott", 34.54, -112.47), _ctx())
    assert f.data["mode"] == "seasonal" and f.data["humidity_band"] == "very dry"


def test_weather_without_the_new_fields_reads_as_before(monkeypatch):
    monkeypatch.setattr(weather_mod, "get_json",
                        lambda url, timeout=12: _daily([25], [15], [0]))
    f = WeatherResolver().resolve(DerivedPlace("CH", "Geneva", 46.2, 6.1),
                                  _ctx(start_date="2026-08-20", end_date="2026-08-22",
                                       today="2026-08-14"))
    assert set(f.data) == {"mode", "high_c", "low_c", "rain_days", "days"}
    assert "Feels like" not in f.body_md and "Humidity" not in f.body_md
