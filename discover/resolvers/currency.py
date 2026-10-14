"""Currency resolver — deterministic reference rates + calculator snapshot
(SPEC-3-discovery.md §4, D-65).

Country → currency via CLDR/Babel (offline). Rates from Frankfurter v2, which
blends reference rates from many central banks (50+ sources), for each trip base
currency. The rate snapshot is stored on the finding's `data` so the per-trip
calculator computes client-side from it — no live call on open, mirroring the
itinerary planner's static-rate converter.

v2's blended feed covers most world currencies; only a genuinely closed or
unpublished currency (e.g. Cuba's CUP) won't be quoted. That is not an error —
the finding is emitted flagged, naming the currency, with no rates (so the
calculator simply omits it).
"""
from __future__ import annotations

from discover.places import DerivedPlace
from discover.resolvers.base import Finding, ResolverContext, get_json, qs

# Provisional heuristic for D-70's `usd_useful` — replaced by a single flagged
# LLM judgement call in the LLM slice. Places where USD is official or the de-facto
# tourist/exchange currency.
_USD_FRIENDLY = {"US", "EC", "SV", "PA", "TC", "VG", "BQ", "KH", "CU", "VN"}


def _currency_for(country: str) -> tuple[str | None, str | None]:
    """(code, display name) for a country, offline via Babel; (None, None) if
    Babel can't map it."""
    try:
        from babel.numbers import get_currency_name, get_territory_currencies
        codes = get_territory_currencies(country)
        if not codes:
            return None, None
        code = codes[0]
        try:
            name = get_currency_name(code, locale="en")
        except Exception:
            name = code
        return code, name
    except Exception:
        return None, None


def render_currency_body(label: str, code: str, name: str, bases,
                         per_base: dict[str, float], as_of: str | None,
                         usd_useful: bool, *, source_note: str = "central-bank rates via Frankfurter") -> str:
    """Markdown body for a currency finding, shared by the resolver and the manual
    edit so the two never drift. `per_base` maps a base code → units of `code` per
    1 base (e.g. {"CAD": 0.58})."""
    lines = [f"**{label}** — official currency **{name} ({code})**.", ""]
    for base in bases:
        v = per_base.get(base)
        if v is not None:
            lines.append(f"- 1 {base} = {v:.4g} {code}")
    lines.append("")
    lines.append(f"Rates as of {as_of or 'today'} ({source_note}) — "
                 f"reference only, rates fluctuate. Open the calculator for "
                 f"quick conversions.")
    if not usd_useful:
        lines.append("")
        lines.append("_USD isn't the local/exchange currency here (provisional — "
                     "pending review); the calculator still offers it._")
    return "\n".join(lines)


class CurrencyResolver:
    topic = "currency"
    scope = "country"

    def __init__(self, base_url: str = "https://api.frankfurter.dev/v2"):
        self.base_url = base_url.rstrip("/")

    def _rate(self, base: str, symbol: str, timeout: int) -> tuple[float | None, str | None]:
        """1 `base` = X `symbol`, with the feed's own date. Same currency → 1.0.

        Frankfurter v2 renamed the latest-rates path (`/latest` → `/rates`) and the
        target-filter param (`symbols` → `quotes`), and returns a list of
        `{date, base, quote, rate}` rows. The v1 `{base, date, rates}` body is
        still accepted so a self-hosted v1 instance keeps working."""
        if base == symbol:
            return 1.0, None
        from discover import health
        data = get_json(f"{self.base_url}/rates?{qs(base=base, quotes=symbol)}",
                        timeout=timeout)
        # Admin alerts (D-197). A currency Frankfurter doesn't quote is an
        # answer, not a failure — only no answer at all counts.
        health.record(health.FRANKFURTER, health.DOWN if data is None else health.OK,
                      "Frankfurter didn't answer" if data is None else None)
        if isinstance(data, list):
            row = next((r for r in data if isinstance(r, dict)
                        and r.get("quote") == symbol), None)
            if not row or row.get("rate") is None:
                return None, None
            return float(row["rate"]), row.get("date")
        if not isinstance(data, dict):
            return None, None
        rate = (data.get("rates") or {}).get(symbol)
        return (float(rate) if rate is not None else None), data.get("date")

    def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None:
        country = place.country
        if not country:
            return None
        code, name = _currency_for(country)
        if not code:
            return Finding(
                topic=self.topic, scope=self.scope, place_key=country,
                resolver="currency.frankfurter", confidence="flagged",
                body_md=f"Couldn't determine the official currency for **{country}**.")

        rates: dict[str, float] = {}
        as_of = None
        for base in ctx.base_currencies:
            rate, date = self._rate(base, code, ctx.timeout)
            if rate is not None:
                rates[base] = rate
                as_of = as_of or date

        usd_useful = code == "USD" or country in _USD_FRIENDLY
        source = [{"title": "Central-bank rates via Frankfurter", "url": "https://frankfurter.dev"}]

        if not rates:
            return Finding(
                topic=self.topic, scope=self.scope, place_key=country,
                resolver="currency.frankfurter", confidence="flagged", sources=source,
                data={"currency": code, "rates": {}, "usd_useful": usd_useful},
                body_md=(f"**{place.label}** — official currency **{name} ({code})**.\n\n"
                         f"Frankfurter doesn't quote {code} (a closed or unpublished "
                         f"currency), so no reference rate is available here. Check a "
                         f"bank or a currency app before you go."))

        body_md = render_currency_body(place.label, code, name, ctx.base_currencies,
                                       rates, as_of, usd_useful)

        return Finding(
            topic=self.topic, scope=self.scope, place_key=country,
            resolver="currency.frankfurter", confidence="high", sources=source,
            ttl_seconds=86400,
            data={"currency": code, "currency_name": name, "date": as_of,
                  "bases": list(ctx.base_currencies),
                  "rates": {code: {f"per_{b}": rates[b] for b in rates}},
                  "usd_useful": usd_useful},
            body_md=body_md)
