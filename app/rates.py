"""Run·dere — one dated exchange-rate table per trip (YONDER-237, SPEC-10 D-195).

The owner's rule: the trip is the shared context. Rates move fast; they must be
consistent for the stretch of time that matters and never reach back to change
old trip data. So:

- **One table per trip, not per person.** `trip_rates` holds units of the trip's
  balance currency per 1 unit of another currency, for a DATE. Everyone on the
  trip converting a cost from the same day gets the same number.
- **A cost's date picks the rate.** An expense uses its `spent_on` day; a
  planned item its start date, else the trip's start; a day in the future (or
  no day) means today, the latest rate there is.
- **Fetched on demand, one currency and one day at a time,** from Frankfurter
  (it serves historical dates), and stored. Supersedes D-176's "no live call":
  the call is optional — no `TX_FRANKFURTER_URL`, or the feed down, falls back
  to the nearest rate already on the trip's table, then to a Discover currency
  snapshot (the trip owner's first, so it's still one number per trip), and
  otherwise the cost stays unconverted. Never a 500 (D-146).
- **Frozen once the trip has ended (D-157):** no fetches; the table and entered
  rates still work.
- **No silent re-rating.** Costs copy the rate they used (`rate`, `rate_date`,
  `rate_id`); refreshing a row on the table changes only the table and the
  costs that had no rate yet. See expenses.update for when a cost re-derives.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models import Trip, TripRate

_RATE_Q = Decimal("0.0000000001")        # same precision as app/expenses.py
FETCH_TIMEOUT = 6                         # seconds; the person is waiting on a save
MAX_FETCHES = 25                          # per request (a balance-currency change)


class RatesFrozen(ValueError):
    """The trip has ended: rates are no longer fetched (D-157)."""


def _fmt(d: Decimal) -> str:
    return format(d.quantize(_RATE_Q, rounding=ROUND_HALF_UP).normalize(), "f")


def today() -> str:
    return date.today().isoformat()


def day_for(cost, trip: Trip) -> str:
    """The day whose rate a cost uses: an expense's `spent_on`, a planned item's
    start (else the trip's), clamped to today — there are no future rates."""
    d = (getattr(cost, "spent_on", None) or getattr(cost, "start_date", None)
         or (trip.start_date if hasattr(cost, "start_date") else None) or "")[:10]
    t = today()
    return d if d and d <= t else t


def day_key(cost) -> str | None:
    """The stored date a cost's rate depends on — its rate re-derives only when
    this changes (D-195)."""
    return getattr(cost, "spent_on", None) or getattr(cost, "start_date", None)


def frozen(trip: Trip) -> bool:
    from discover.runner import trip_has_ended
    return trip_has_ended(trip)


def fetch(frm: str, to: str, day: str) -> tuple[Decimal | None, str | None]:
    """1 `frm` = X `to` on `day` from Frankfurter v2, with the date it actually
    published. (None, None) when unconfigured, unreachable or not quoted. The
    one live call here; tests replace it."""
    from app.config import settings
    from discover.http import get_json, qs
    base_url = (settings.frankfurter_url or "").rstrip("/")
    if not base_url:
        return None, None
    data = get_json(f"{base_url}/rates?{qs(base=frm, quotes=to, date=day)}",
                    timeout=FETCH_TIMEOUT)
    if isinstance(data, list):           # v2: [{date, base, quote, rate}]
        row = next((r for r in data if isinstance(r, dict) and r.get("quote") == to), None)
        val, as_of = (row or {}).get("rate"), (row or {}).get("date")
    elif isinstance(data, dict):         # a self-hosted v1: {date, rates: {to: x}}
        val, as_of = (data.get("rates") or {}).get(to), data.get("date")
    else:
        return None, None
    try:
        d = Decimal(str(val)) if val is not None else None
    except (InvalidOperation, ValueError):
        return None, None
    if d is None or not d.is_finite() or d <= 0:
        return None, None
    return d, (str(as_of)[:10] if as_of else day)


class Lookup:
    """Rate lookups for one request, with a cap on the live calls so re-rating
    a whole trip stays quick."""

    def __init__(self, session: Session, trip: Trip, user_id: str | None):
        self.session, self.trip, self.user_id = session, trip, user_id
        self.fetches = 0
        self.can_fetch = not frozen(trip)

    def _row(self, frm: str, to: str, day: str) -> TripRate | None:
        return (self.session.query(TripRate)
                .filter(TripRate.trip_id == self.trip.id, TripRate.currency == frm,
                        TripRate.quote == to, TripRate.rate_date == day).one_or_none())

    def store(self, frm: str, to: str, day: str, rate: Decimal, as_of: str | None,
              source: str) -> TripRate:
        """Add or replace the table's row for that day (a refresh replaces it;
        costs keep the rate they copied)."""
        row = self._row(frm, to, day)
        if row is None:
            row = TripRate(trip_id=self.trip.id, currency=frm, quote=to, rate_date=day)
            self.session.add(row)
        row.rate, row.as_of, row.source = _fmt(rate), as_of, source
        row.fetched_by = self.user_id
        from datetime import datetime, timezone
        row.fetched_at = datetime.now(timezone.utc)
        self.session.flush()            # sessions run with autoflush off (app/db.py)
        return row

    def fetch_into_table(self, frm: str, to: str, day: str) -> TripRate | None:
        if not self.can_fetch or self.fetches >= MAX_FETCHES:
            return None
        self.fetches += 1
        rate, as_of = fetch(frm, to, day)
        return self.store(frm, to, day, rate, as_of, "frankfurter") if rate else None

    def _nearest(self, frm: str, to: str, day: str) -> TripRate | None:
        rows = (self.session.query(TripRate)
                .filter(TripRate.trip_id == self.trip.id, TripRate.currency == frm,
                        TripRate.quote == to).all())
        if not rows:
            return None

        def gap(r):
            try:
                return (abs((date.fromisoformat(r.rate_date) - date.fromisoformat(day)).days),
                        r.rate_date > day)      # a tie goes to the earlier day
            except ValueError:
                return (10 ** 6, True)
        return min(rows, key=gap)

    def _from_discover(self, frm: str, to: str, day: str) -> TripRate | None:
        """A Discover currency snapshot, put on the table so it's one number for
        everyone from now on: the trip owner's findings first, then the caller's."""
        from app.expenses import discover_quote
        for uid in dict.fromkeys([self.trip.owner_id, self.user_id]):
            if uid is None:
                continue
            rate, as_of = discover_quote(self.session, self.trip.id, uid, frm, to)
            if rate is not None:
                return self.store(frm, to, (as_of or day)[:10], rate, as_of, "discover")
        return None

    def find(self, frm: str, to: str, day: str) -> TripRate | None:
        """The trip's rate for `frm`→`to` on `day`: the table's row for that
        day, else a fetch for that day, else the nearest day on the table, else
        a Discover snapshot. None = unconverted."""
        return (self._row(frm, to, day) or self.fetch_into_table(frm, to, day)
                or self._nearest(frm, to, day) or self._from_discover(frm, to, day))


def refresh(session: Session, trip: Trip, user_id: str | None, currency: str,
            day: str | None = None) -> TripRate:
    """Re-fetch ONE currency's rate for one day (default today) onto the trip's
    table (the app's "fetch this rate"). Costs that already have a rate keep it.
    Raises RatesFrozen on an ended trip, ValueError when there's no rate to get."""
    if trip.expense_currency is None:
        raise ValueError("this trip has no costs yet, so there's no currency to convert to")
    if currency == trip.expense_currency:
        raise ValueError(f"{currency} is the trip's own currency — its rate is always 1")
    if frozen(trip):
        raise RatesFrozen("this trip has ended, so rates are no longer fetched — "
                          "enter the rate by hand instead")
    day = (day or today())[:10]
    try:
        date.fromisoformat(day)
    except ValueError:
        raise ValueError(f"not a date: {day!r} (use YYYY-MM-DD)") from None
    day = min(day, today())
    look = Lookup(session, trip, user_id)
    row = look.fetch_into_table(currency, trip.expense_currency, day)
    if row is None:
        raise ValueError(f"no exchange rate is available for {currency} right now "
                         "(the rate service is unreachable or doesn't quote it)")
    return row
