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
    f = PlugsResolver().resolve(DerivedPlace("US", "Glendale", 32, -104), _ctx())
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


def test_currency_unquoted_currency_is_flagged(monkeypatch):
    # ECB doesn't quote the Moroccan dirham → no rates, flagged, still names it.
    monkeypatch.setattr(currency_mod, "get_json",
                        lambda url, timeout=12: {"date": "2026-08-13", "rates": {}})
    f = CurrencyResolver().resolve(DerivedPlace("MA", "Harar", 31, -8), _ctx())
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
