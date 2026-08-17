"""Weather resolver — deterministic Open-Meteo (SPEC-3-discovery.md §3).

City-scope, keyed off the derived place's coordinates. Within Open-Meteo's ~16-day
forecast horizon it uses the real forecast; for a trip further out (the common
case — big trips aren't booked a day ahead) it falls back to the archive API for
the SAME calendar dates a year earlier, clearly labelled as a seasonal guide, not
a forecast. A past trip reports the actual recorded weather.
"""
from __future__ import annotations

from datetime import date, timedelta

from discover.places import DerivedPlace
from discover.resolvers.base import Finding, ResolverContext, get_json, qs

_SOURCE = [{"title": "Open-Meteo", "url": "https://open-meteo.com"}]
_DAILY = "temperature_2m_max,temperature_2m_min,precipitation_sum"
# Open-Meteo's forecast endpoint accepts end_date up to (today + 15) inclusive —
# confirmed live (a +16 request 400s: "out of allowed range ... today+15"). This
# must stay <= that true ceiling or every straddling-trip request 400s outright
# (Open-Meteo rejects the whole multi-day range, not just the excess day).
_FORECAST_HORIZON_DAYS = 15


def _parse(d: str | None) -> date | None:
    try:
        return date.fromisoformat((d or "")[:10])
    except ValueError:
        return None


def _prior_year(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:                      # Feb 29 → Feb 28
        return d.replace(year=d.year - 1, day=28)


def _mean(xs: list) -> float | None:
    vals = [x for x in xs if isinstance(x, (int, float))]
    return sum(vals) / len(vals) if vals else None


class WeatherResolver:
    topic = "weather"
    scope = "city"

    def __init__(self, forecast_url: str = "https://api.open-meteo.com/v1",
                 archive_url: str = "https://archive-api.open-meteo.com/v1"):
        # Full endpoints — the two APIs live at different paths (/forecast vs
        # /archive) as well as different hosts.
        self.forecast_ep = forecast_url.rstrip("/") + "/forecast"
        self.archive_ep = archive_url.rstrip("/") + "/archive"

    def _fetch(self, endpoint: str, lat: float, lon: float, start: date, end: date,
               timeout: int) -> dict | None:
        url = endpoint + "?" + qs(
            latitude=lat, longitude=lon, daily=_DAILY, timezone="auto",
            start_date=start.isoformat(), end_date=end.isoformat())
        return get_json(url, timeout=timeout)

    def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None:
        if place.lat is None or place.lon is None:
            return None
        key = place.city_key or place.country
        start, end = _parse(ctx.start_date), _parse(ctx.end_date)
        if not (start and end):
            return Finding(
                topic=self.topic, scope=self.scope, place_key=key,
                resolver="weather.openmeteo", confidence="flagged", sources=_SOURCE,
                body_md=f"Add trip start/end dates to get a weather outlook for "
                        f"**{place.label}**.")

        today = _parse(ctx.today) or date.today()
        horizon = today + timedelta(days=_FORECAST_HORIZON_DAYS)

        # Each segment is (mode, q_start, q_end, endpoint). A trip that STRADDLES
        # the horizon (starts within it, ends beyond) gets two: the forecastable
        # front, plus a seasonal (prior-year archive) estimate for the tail that
        # falls outside Open-Meteo's forecast range — previously that tail was
        # silently dropped (D-past bug: the whole trip's dates were shown in the
        # body even though only the front segment was ever queried).
        segments: list[tuple[str, date, date, str]] = []
        if end < today:
            segments = [("past", start, end, self.archive_ep)]
        elif start > horizon:
            segments = [("seasonal", _prior_year(start), _prior_year(end), self.archive_ep)]
        else:
            segments = [("forecast", max(start, today), min(end, horizon), self.forecast_ep)]
            if end > horizon:
                tail_start = horizon + timedelta(days=1)
                segments.append(("seasonal", _prior_year(tail_start), _prior_year(end),
                                 self.archive_ep))

        highs: list = []
        lows: list = []
        precip: list = []
        hit: list[str] = []
        forecast_through = None
        for mode, q_start, q_end, ep in segments:
            data = self._fetch(ep, place.lat, place.lon, q_start, q_end, ctx.timeout)
            daily = (data or {}).get("daily") or {}
            h = daily.get("temperature_2m_max") or []
            l = daily.get("temperature_2m_min") or []
            if not h or not l:
                continue
            highs += h
            lows += l
            precip += daily.get("precipitation_sum") or []
            hit.append(mode)
            if mode == "forecast":
                forecast_through = q_end

        hi, lo = _mean(highs), _mean(lows)
        if hi is None or lo is None:
            return Finding(
                topic=self.topic, scope=self.scope, place_key=key,
                resolver="weather.openmeteo", confidence="flagged", sources=_SOURCE,
                body_md=f"No weather data came back for **{place.label}** "
                        f"({ctx.start_date}–{ctx.end_date}).")

        rain_days = sum(1 for p in precip if isinstance(p, (int, float)) and p >= 1.0)
        n = len(highs)
        window = f"{ctx.start_date} – {ctx.end_date}"

        if hit == ["forecast"]:
            head = f"**{place.label}** — forecast for {window}"
            tail = "Source: Open-Meteo (forecast)."
            conf = "high"
        elif hit == ["past"]:
            head = f"**{place.label}** — recorded weather for {window}"
            tail = "Source: Open-Meteo (archive)."
            conf = "high"
        elif hit == ["seasonal"]:
            head = f"**{place.label}** — typical for {window}"
            tail = (f"This is last year's actual weather for the same dates — a "
                    f"seasonal guide, not a forecast. Source: Open-Meteo (archive).")
            conf = "medium"
        else:                                       # forecast + seasonal tail
            head = f"**{place.label}** — {window}"
            tail = (f"Real forecast through {forecast_through.isoformat()}; the rest of "
                    f"the trip is beyond Open-Meteo's forecast range, so those days use "
                    f"last year's actual weather instead — a seasonal guide, not a "
                    f"forecast. Source: Open-Meteo (forecast + archive).")
            conf = "medium"

        mode_out = "+".join(hit)
        body = (f"{head}: highs ~{round(hi)}°C, lows ~{round(lo)}°C, "
                f"{rain_days} day(s) with rain over {n} days.\n\n{tail}")

        return Finding(
            topic=self.topic, scope=self.scope, place_key=key,
            resolver="weather.openmeteo", confidence=conf, sources=_SOURCE,
            ttl_seconds=(6 * 3600 if hit == ["forecast"] else 30 * 86400),
            data={"mode": mode_out, "high_c": round(hi), "low_c": round(lo),
                  "rain_days": rain_days, "days": n},
            body_md=body)
