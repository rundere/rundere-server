"""One line for the whole trip's weather (Run·dere 4.5, YONDER-167).

Built from each city's weather finding (average high/low, rain days, how many
days, forecast vs seasonal) with fixed rules — no model, so it can't invent
anything. Example:

  "Warm and mostly dry — highs 24–31 °C across 3 cities; cool nights down to
   12 °C in Gondar; rain on 2 of 18 days."

plus short packing hints ("a warm layer for the evenings").

D-189 (YONDER-211): when the findings carry them, feels-like, humidity and
altitude shape the line and the hints too ("feels near 3 °C at dawn", "very
dry", "high altitude in Prescott") — older findings without them read as
before.
"""
from __future__ import annotations

_BANDS = [(30, "Hot"), (24, "Warm"), (17, "Mild"), (10, "Cool"), (-100, "Cold")]


def _band(temp: float) -> str:
    return next(label for floor, label in _BANDS if temp >= floor)


def summarize(cities: list[tuple[str, dict]]) -> dict | None:
    """`cities` = [(label, weather finding data), …]. None when there's no usable
    data. Returns {"line", "hints", "band", "seasonal"}."""
    rows = [(label, d) for label, d in cities
            if isinstance(d, dict) and isinstance(d.get("high_c"), (int, float))
            and isinstance(d.get("low_c"), (int, float))]
    if not rows:
        return None
    highs = [d["high_c"] for _, d in rows]
    lows = [d["low_c"] for _, d in rows]
    hi_min, hi_max = min(highs), max(highs)
    lo_min = min(lows)
    coldest = next(label for label, d in rows if d["low_c"] == lo_min)
    avg_high = sum(highs) / len(highs)
    days = sum(int(d.get("days") or 0) for _, d in rows)
    rain = sum(int(d.get("rain_days") or 0) for _, d in rows)
    share = rain / days if days else 0

    variable = hi_max - hi_min >= 10
    band = "Variable" if variable else _band(avg_high)
    wet = ("wet" if share >= 0.4 else "some rain" if share >= 0.2 else "mostly dry")
    head = f"{band} and {wet}" if band != "Variable" else f"Variable, {wet}"

    temps = (f"highs {hi_min}–{hi_max} °C" if hi_min != hi_max else f"highs around {hi_min} °C")
    temps += f" across {len(rows)} cities" if len(rows) > 1 else ""
    parts = [temps]
    if variable:
        warmest = next(label for label, d in rows if d["high_c"] == hi_max)
        coolest = next(label for label, d in rows if d["high_c"] == hi_min)
        parts.append(f"warmest in {warmest}, coolest in {coolest}")
    where = f" in {coldest}" if len(rows) > 1 else ""
    if lo_min <= 0:
        parts.append(f"freezing nights, down to {lo_min} °C{where}")
    elif lo_min <= 12 and avg_high >= 20:
        parts.append(f"cool nights down to {lo_min} °C{where}")
    # What it FEELS like at dawn, when that's noticeably colder than the air.
    felt = [(label, d["feels_low_c"]) for label, d in rows
            if isinstance(d.get("feels_low_c"), (int, float))]
    feel_min = min((f for _, f in felt), default=None)
    if feel_min is not None and feel_min <= lo_min - 2:
        at = next(label for label, f in felt if f == feel_min)
        parts.append(f"feels near {feel_min} °C at dawn"
                     + (f" in {at}" if len(rows) > 1 else ""))
    dry = [label for label, d in rows if d.get("humidity_band") in ("very dry", "dry")]
    humid = [label for label, d in rows if d.get("humidity_band") in ("humid", "very humid")]
    if dry:
        very = all(d.get("humidity_band") == "very dry" for label, d in rows if label in dry)
        parts.append(("very dry air" if very else "dry air")
                     + (f" in {', '.join(dry)}" if len(rows) > 1 else ""))
    if humid:
        parts.append("humid" + (f" in {', '.join(humid)}" if len(rows) > 1 else ""))
    high = [(label, d.get("elevation_m")) for label, d in rows if d.get("high_altitude")]
    if high:
        parts.append("high altitude in " + ", ".join(
            f"{label} (~{m:,} m)" if isinstance(m, (int, float)) else label
            for label, m in high))
    if days:
        parts.append(f"rain on {rain} of {days} days" if rain else "no rain expected")
    line = f"{head} — " + "; ".join(parts) + "."

    hints = []
    if avg_high >= 28 or hi_max >= 32:
        hints.append("sun protection and light clothing")
    cold = min(lo_min, feel_min) if feel_min is not None else lo_min
    if cold <= 0 or avg_high < 17:
        hints.append("warm clothes")
    elif cold <= 12:
        hints.append("a warm layer for the evenings")
    if share >= 0.2:
        hints.append("a rain layer or umbrella")
    if dry:
        hints.append("lip balm, moisturiser and extra water")
    if humid:
        hints.append("breathable clothes")
    if high:
        hints.append("sunscreen, and take day 1 easy at altitude")
    # All from last year's weather for the same dates → "typical", not a forecast.
    seasonal = all(str(d.get("mode") or "") == "seasonal" for _, d in rows)
    return {"line": line, "hints": hints, "band": band, "seasonal": seasonal}
