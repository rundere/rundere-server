"""Run·dere 4.8 — trip expenses, balances and settle-up (SPEC-10, YONDER-172).

The one place the expense rules live, so /api/v1 and MCP never diverge:

- **Small on purpose (D-173).** Expenses with an equal or exact split, net
  balances, suggested payments and recorded settlements. No imports (D-155).
- **Only the people on a cost see it (D-183).** The trip gate still applies
  (`scope.require_trip`: view to read, edit to write), and inside it a cost is
  visible only to the travellers tagged on it — payer and sharers for an actual,
  the tagged travellers for a planned item — and to whoever entered it. Each
  viewer's balances and plan are worked out from what they can see.
- **Roster people only (D-175).** Payer and sharers are TravelerIdentity ids on
  the trip's roster when the expense is saved. Shares are always stored, an equal
  split divided to the currency's minor unit with the leftover going to the first
  travellers in id order, so they add up exactly.
- **Own currency + the rate used (D-176, D-195).** Balances are in the trip's
  expense currency. A blank rate comes from the TRIP's dated rate table
  (app/rates.py) for the cost's day — the same number for everyone on the trip;
  unknown stays NULL and the expense is listed as unconverted instead of guessed.
  A cost keeps the rate it was converted with: it re-derives only when its
  currency, day or rate changes, or when it had no rate yet.
- **Settlements are payments (D-177)** and net with expenses; suggested payments
  are a greedy largest-debtor → largest-creditor match.
- **Planned vs actual (D-178–D-181, D-184).** Planned items (TripBudgetItem) are
  the budget: one amount each for the travellers tagged on it — nothing is
  multiplied by people or days (D-179) — never owed or settled. Only actual expenses feed balances and the trip's actual
  cost. Agents' planned items land as drafts the person keeps (D-180); an actual
  can name the planned item it fulfils, for plan vs actual by category (D-181).

- **Comments (D-185).** The people on an actual can discuss it; comments are
  visible wherever the expense is.

Bad input raises ValueError (the API maps it to 400).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app import scope
from app import rates as rates_svc
from app.models import (DiscoveryFinding, TravelerIdentity, Trip, TripBudgetItem,
                        TripBudgetTraveler, TripExpense, TripExpenseComment,
                        TripExpenseShare, TripRate)
from app.trips import who

EXPENSE, SETTLEMENT = "expense", "settlement"
EQUAL, EXACT = "equal", "exact"
CATEGORIES = ("food", "lodging", "transport", "activities", "shopping", "fees", "other")
# ISO-4217 minor units that aren't 2. Everything else rounds to cents.
_ZERO_DECIMAL = {"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF",
                 "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF"}
_THREE_DECIMAL = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
_MAX_AMOUNT = Decimal("1000000000")
_RATE_Q = Decimal("0.0000000001")       # rates are kept to 10 places (fits String(32))


def quantum(currency: str) -> Decimal:
    c = (currency or "").upper()
    if c in _ZERO_DECIMAL:
        return Decimal("1")
    if c in _THREE_DECIMAL:
        return Decimal("0.001")
    return Decimal("0.01")


def money(value, currency: str) -> Decimal:
    """`value` as a positive amount rounded to `currency`'s minor unit."""
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"not an amount: {value!r}") from None
    if not d.is_finite() or d <= 0 or d > _MAX_AMOUNT:
        raise ValueError("an amount must be more than 0")
    q = d.quantize(quantum(currency), rounding=ROUND_HALF_UP)
    if q <= 0:
        raise ValueError("an amount must be more than 0")
    return q


@lru_cache(maxsize=1)
def _iso_codes() -> frozenset[str]:
    from babel.numbers import list_currencies
    return frozenset(list_currencies())


def currency_code(value: str | None) -> str:
    """A real ISO 4217 code, or ValueError. Every picker offers codes from
    Babel's list, so the server holds every write to it too: a hand-made request
    can't store a code no picker would offer (YONDER-293)."""
    c = (value or "").strip().upper()
    if c not in _iso_codes():
        raise ValueError(f"not a currency code: {value!r} (use ISO-4217, e.g. EUR)")
    return c


def _rate(value) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"not a rate: {value!r}") from None
    if not d.is_finite() or d <= 0 or d > _MAX_AMOUNT:
        raise ValueError("a rate must be more than 0")
    d = d.quantize(_RATE_Q, rounding=ROUND_HALF_UP)
    if d <= 0:
        raise ValueError("a rate must be more than 0")
    return d


def _fmt(d: Decimal | None) -> str | None:
    return None if d is None else format(d.normalize(), "f")


# --------------------------------------------------------------------------- rates
def discover_rate(session: Session, trip_id: str, user_id: str | None,
                  frm: str, to: str) -> Decimal | None:
    """Units of `to` per 1 `frm` from the caller's stored currency findings on this
    trip (D-65 snapshots). None when it can't tell."""
    return discover_quote(session, trip_id, user_id, frm, to)[0]


def discover_quote(session: Session, trip_id: str, user_id: str | None,
                   frm: str, to: str) -> tuple[Decimal | None, str | None]:
    """discover_rate plus the snapshot's date: pivots through a base both are
    quoted against — the same table the trip's currency calculator uses. Since
    D-195 only a FALLBACK for the trip's rate table (app/rates.py)."""
    if frm == to:
        return Decimal(1), None
    q = session.query(DiscoveryFinding).filter(DiscoveryFinding.trip_id == trip_id,
                                               DiscoveryFinding.topic == "currency")
    if user_id is not None:
        q = q.filter(DiscoveryFinding.user_id == user_id)
    # per_base[base][code] = units of code per 1 base.
    per_base: dict[str, dict[str, Decimal]] = {}
    dated: dict[str, str | None] = {}
    for f in q.all():
        for code, per in ((f.data or {}).get("rates") or {}).items():
            for key, v in (per or {}).items():
                try:
                    val = Decimal(str(v))
                except (InvalidOperation, ValueError):
                    continue
                if val > 0:
                    base = key.replace("per_", "")
                    per_base.setdefault(base, {base: Decimal(1)})[str(code).upper()] = val
                    dated.setdefault(base, (f.data or {}).get("date"))
    for base, table in per_base.items():
        if frm in table and to in table:
            return ((table[to] / table[frm]).quantize(_RATE_Q, rounding=ROUND_HALF_UP),
                    dated.get(base))
    return None, None


# --------------------------------------------------------------------------- currency picker
def suggested_currencies(session: Session, trip: Trip, user) -> list[str]:
    """The currencies an expense on this trip most likely uses, best first
    (YONDER-213): the destination's — every currency the caller's Discover
    findings name for THIS trip, so a multi-country trip lists them all — then
    the caller's own base currency, the trip's balance currency, and whatever
    the caller's visible expenses on THIS trip already use. Nothing comes from
    other trips: Morocco last year never puts MAD on the Brazil trip. One ordering for web, MCP and
    the app; a picker lists every other ISO-4217 code after these."""
    out: list[str] = []

    def add(code) -> None:
        c = (code or "").strip().upper()
        if len(c) == 3 and c.isalpha() and c not in out:
            out.append(c)
    q = session.query(DiscoveryFinding).filter(DiscoveryFinding.trip_id == trip.id,
                                               DiscoveryFinding.topic == "currency")
    uid = getattr(user, "id", None)
    if uid is not None:
        q = q.filter(DiscoveryFinding.user_id == uid)
    for f in q.order_by(DiscoveryFinding.place_key).all():
        add((f.data or {}).get("currency"))
    add(own_currency(session, user))
    add(trip.expense_currency)
    for e in visible_expenses(session, trip, user)[0]:
        add(e.currency)
    return out


def own_currency(session: Session, user) -> str | None:
    """The caller's base currency from their travel profile."""
    tid = getattr(user, "traveler_id", None)
    t = session.get(TravelerIdentity, tid) if tid is not None else None
    return (t.base_currency or None) if t else None


def default_currency(session: Session, trip: Trip, user) -> str | None:
    """What a new expense starts in (YONDER-213): the person's own currency,
    else the trip's balance currency, else the destination's."""
    return (own_currency(session, user) or trip.expense_currency
            or next(iter(suggested_currencies(session, trip, user)), None))


@lru_cache(maxsize=1)
def currency_options() -> tuple[tuple[str, str], ...]:
    """(code, "EUR · Euro") for every currency some country uses today, by code —
    Babel's full list also carries retired ones (pesetas, francs) nobody spends."""
    from datetime import date

    from babel import Locale
    from babel.numbers import get_currency_name, get_territory_currencies
    current: set[str] = set()
    for terr in Locale.parse("en").territories:
        if len(terr) == 2 and terr.isalpha():
            try:
                current.update(get_territory_currencies(terr, date.today(), tender=True))
            except Exception:
                continue
    out = []
    for c in sorted(current):
        try:
            out.append((c, f"{c} · {get_currency_name(c, locale='en')}"))
        except Exception:
            out.append((c, c))
    return tuple(out)


# --------------------------------------------------------------------------- shares
def _roster(session: Session, trip_id: str) -> list[int]:
    return sorted(who.roster_ids(session, trip_id))


def _on_roster(tid, roster: list[int], what: str) -> int:
    try:
        t = int(tid)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: not a traveller id: {tid!r}") from None
    if t not in roster:
        raise ValueError(f"{what}: traveller {t} isn't on this trip")
    return t


def equal_shares(total: Decimal, traveler_ids: list[int], currency: str) -> dict[int, Decimal]:
    """Split `total` equally; leftover minor units go to the first ids (D-175)."""
    ids = sorted(set(traveler_ids))
    q = quantum(currency)
    units = int(total / q)
    base, extra = divmod(units, len(ids))
    return {tid: (base + (1 if i < extra else 0)) * q for i, tid in enumerate(ids)}


def build_shares(session: Session, trip_id: str, total: Decimal, currency: str,
                 split: str, traveler_ids: list | None,
                 exact: dict | None) -> dict[int, Decimal]:
    roster = _roster(session, trip_id)
    if split == EXACT:
        if not exact:
            raise ValueError("an exact split needs each person's share")
        shares: dict[int, Decimal] = {}
        for tid, amt in exact.items():
            t = _on_roster(tid, roster, "share")
            try:
                zero = str(amt).strip() == "" or Decimal(str(amt).strip()) == 0
            except (InvalidOperation, ValueError):
                zero = False                            # money() reports it
            if not zero:
                shares[t] = shares.get(t, Decimal(0)) + money(amt, currency)
        if not shares:
            raise ValueError("an exact split needs at least one share above 0")
        if sum(shares.values()) != total:
            raise ValueError(f"the shares add up to {_fmt(sum(shares.values()))}, "
                             f"not {_fmt(total)} {currency}")
        return shares
    if split != EQUAL:
        raise ValueError("split must be 'equal' or 'exact'")
    ids = [_on_roster(t, roster, "split") for t in (traveler_ids or [])] or roster
    return equal_shares(total, ids, currency)


# --------------------------------------------------------------------------- writes
@dataclass
class ExpenseInput:
    amount: object
    currency: str
    payer_id: int
    title: str = ""
    spent_on: str | None = None
    split: str = EQUAL
    traveler_ids: list | None = None       # equal split; None/[] = everyone on the trip
    shares: dict | None = None             # exact split: {traveler_id: amount}
    category: str = "other"
    rate: object = None                    # trip currency per 1 unit; blank = look it up
    activity_id: str | None = None
    budget_item_id: str | None = None      # D-181: the planned item this fulfils
    notes: str | None = None
    source: str = "api"
    # Where it was logged (YONDER-212): the iOS app sends it, nothing else does.
    place_lat: float | None = None
    place_lon: float | None = None
    place_label: str | None = None
    place_accuracy_m: float | None = None


PLACE_FIELDS = ("place_lat", "place_lon", "place_label", "place_accuracy_m")


def _check_place(lat, lon, label, accuracy) -> dict:
    """The location columns from the caller's values: both coordinates or
    neither, in range; a label and accuracy only travel with coordinates."""
    if lat is None and lon is None:
        if label or accuracy is not None:
            raise ValueError("a place needs place_lat and place_lon")
        return dict.fromkeys(PLACE_FIELDS)
    if lat is None or lon is None:
        raise ValueError("send both place_lat and place_lon, or neither")
    try:
        lat, lon = float(lat), float(lon)
        acc = None if accuracy is None else float(accuracy)
    except (TypeError, ValueError):
        raise ValueError("place_lat, place_lon and place_accuracy_m are numbers") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("place_lat/place_lon are out of range")
    if acc is not None and not 0 <= acc < 1e7:
        raise ValueError("place_accuracy_m must be 0 or more")
    return {"place_lat": lat, "place_lon": lon,
            "place_label": (label or "").strip()[:256] or None, "place_accuracy_m": acc}


def _check_budget_item(session: Session, trip_id: str, item_id: str | None) -> str | None:
    if not item_id:
        return None
    b = session.get(TripBudgetItem, item_id)
    if b is None or b.trip_id != trip_id:
        raise ValueError("that planned item isn't on this trip")
    return b.id


def _check_activity(session: Session, trip_id: str, activity_id: str | None) -> str | None:
    from app.models import TripActivity
    if not activity_id:
        return None
    a = session.get(TripActivity, activity_id)
    if a is None or a.trip_id != trip_id:
        raise ValueError("that timeline item isn't on this trip")
    return a.id


def _apply_rate(session: Session, trip: Trip, e, user_id: str | None, entered,
                look: rates_svc.Lookup | None = None) -> None:
    """Set a cost's rate (an expense or a planned item): same currency → 1; an
    entered rate as given; otherwise the trip's rate table for the cost's day
    (D-195) — or NULL, unconverted."""
    tc = trip.expense_currency
    given = _rate(entered)
    e.rate_id = None
    if tc is None or e.currency == tc:
        e.rate, e.rate_source, e.rate_date = "1", "same", None
    elif given is not None:
        e.rate, e.rate_source = _fmt(given), "entered"
        e.rate_date = rates_svc.day_for(e, trip)
    else:
        look = look or rates_svc.Lookup(session, trip, user_id)
        row = look.find(e.currency, tc, rates_svc.day_for(e, trip))
        if row is None:
            e.rate, e.rate_source, e.rate_date = None, None, None
        else:
            e.rate, e.rate_source = row.rate, row.source
            e.rate_date, e.rate_id = row.as_of or row.rate_date, row.id


def _rerate(e, changes: dict, before: tuple) -> bool:
    """D-195: re-derive a cost's rate only when something it depends on really
    changed — an explicit rate, a different currency or day — or it had none.
    Sending the same currency back (the app does on every edit) changes nothing."""
    if changes.get("rate") is not None:
        return True
    return e.rate is None or (e.currency, rates_svc.day_key(e)) != before


def _check_date(d: str | None) -> str | None:
    from datetime import date
    if not d:
        return None
    try:
        date.fromisoformat(d[:10])
    except ValueError:
        raise ValueError(f"not a date: {d!r} (use YYYY-MM-DD)") from None
    return d[:10]


def _write_shares(session: Session, expense_id: str, shares: dict[int, Decimal]) -> None:
    session.query(TripExpenseShare).filter(
        TripExpenseShare.expense_id == expense_id).delete(synchronize_session=False)
    for tid, amt in sorted(shares.items()):
        session.add(TripExpenseShare(expense_id=expense_id, traveler_id=tid, amount=_fmt(amt)))
    session.flush()                     # sessions run with autoflush off (app/db.py)


def create(session: Session, user, trip: Trip, data: ExpenseInput,
           kind: str = EXPENSE) -> TripExpense:
    """Record an expense (or, with kind=settlement, a payment). Caller commits."""
    cur = currency_code(data.currency)
    total = money(data.amount, cur)
    roster = _roster(session, trip.id)
    if not roster:
        raise ValueError("add the trip's travellers first — expenses are split between them")
    payer = _on_roster(data.payer_id, roster, "payer")
    category = (data.category or "other").lower()
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
    shares = build_shares(session, trip.id, total, cur, data.split, data.traveler_ids,
                          data.shares)
    if trip.expense_currency is None:
        trip.expense_currency = cur                     # D-176: first expense sets it
    uid = getattr(user, "id", None)
    e = TripExpense(trip_id=trip.id, owner_id=uid, kind=kind,
                    title=(data.title or "").strip()[:200], amount=_fmt(total), currency=cur,
                    spent_on=_check_date(data.spent_on), payer_id=payer, split=data.split,
                    category=category,
                    activity_id=_check_activity(session, trip.id, data.activity_id),
                    budget_item_id=_check_budget_item(session, trip.id, data.budget_item_id),
                    notes=(data.notes or None), source=data.source or "api",
                    **_check_place(data.place_lat, data.place_lon, data.place_label,
                                   data.place_accuracy_m))
    _apply_rate(session, trip, e, uid, data.rate)
    session.add(e)
    session.flush()
    _write_shares(session, e.id, shares)
    return e


def record_settlement(session: Session, user, trip: Trip, *, from_id: int, to_id: int,
                      amount, currency: str | None = None, spent_on: str | None = None,
                      rate=None, notes: str | None = None, source: str = "api") -> TripExpense:
    """`from_id` paid `to_id` back (D-177). Defaults to the trip's currency."""
    if int(from_id) == int(to_id):
        raise ValueError("a settlement is between two different travellers")
    cur = currency or trip.expense_currency
    if not cur:
        raise ValueError("this trip has no expenses to settle yet")
    data = ExpenseInput(amount=amount, currency=cur, payer_id=from_id, title="Settle-up",
                        spent_on=spent_on, split=EXACT, shares={to_id: amount},
                        category="other", rate=rate, notes=notes, source=source)
    return create(session, user, trip, data, kind=SETTLEMENT)


def update(session: Session, user, trip: Trip, e: TripExpense, changes: dict) -> TripExpense:
    """Apply the fields in `changes` (ExpenseInput field names). Changing the
    amount, currency or split rebuilds the shares. Caller commits."""
    before = (e.currency, rates_svc.day_key(e))
    for key in ("title", "notes"):
        if key in changes and changes[key] is not None:
            setattr(e, key, (changes[key] or "").strip()[:200] if key == "title"
                    else (changes[key] or None))
    if changes.get("spent_on") is not None:
        e.spent_on = _check_date(changes["spent_on"])
    if changes.get("category") is not None:
        c = changes["category"].lower()
        if c not in CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
        e.category = c
    if changes.get("activity_id") is not None:          # "" unlinks
        e.activity_id = _check_activity(session, trip.id, changes["activity_id"])
    if changes.get("budget_item_id") is not None:       # "" unlinks
        e.budget_item_id = _check_budget_item(session, trip.id, changes["budget_item_id"])
    if any(k in changes for k in PLACE_FIELDS):
        # A location is replaced as a whole (all nulls clears it); the app never
        # re-captures on edit, so this is only an explicit correction.
        merged = {k: changes.get(k, getattr(e, k)) for k in PLACE_FIELDS}
        for k, v in _check_place(*(merged[k] for k in PLACE_FIELDS)).items():
            setattr(e, k, v)
    roster = _roster(session, trip.id)
    if changes.get("payer_id") is not None:
        e.payer_id = _on_roster(changes["payer_id"], roster, "payer")
    reshare = any(changes.get(k) is not None for k in
                  ("amount", "currency", "split", "traveler_ids", "shares"))
    if changes.get("currency") is not None:
        e.currency = currency_code(changes["currency"])
    if changes.get("amount") is not None or changes.get("currency") is not None:
        e.amount = _fmt(money(changes.get("amount") or e.amount, e.currency))
    if reshare:
        split = changes.get("split") or e.split
        current = {sh.traveler_id: sh.amount for sh in shares_of(session, e.id)}
        ids, exact = None, None
        if e.kind == SETTLEMENT:
            split, exact = EXACT, {tid: e.amount for tid in current}   # follows the amount
        elif split == EXACT:
            exact = changes.get("shares") if changes.get("shares") is not None else current
        else:
            ids = changes.get("traveler_ids")
            if ids is None and e.split == EQUAL:
                ids = sorted(current)                   # keep who it's split between
        shares = build_shares(session, trip.id, Decimal(e.amount), e.currency, split,
                              ids, exact)
        e.split = split
        _write_shares(session, e.id, shares)
    if _rerate(e, changes, before):
        _apply_rate(session, trip, e, getattr(user, "id", None), changes.get("rate"))
    session.flush()
    return e


def delete(session: Session, e: TripExpense) -> None:
    session.query(TripExpenseShare).filter(
        TripExpenseShare.expense_id == e.id).delete(synchronize_session=False)
    session.query(TripExpenseComment).filter(
        TripExpenseComment.expense_id == e.id).delete(synchronize_session=False)
    session.delete(e)


def set_trip_currency(session: Session, user, trip: Trip, currency: str) -> None:
    """Change the balance currency (D-176, D-195): every cost is converted again
    from the trip's rate table at its own day (same currency becomes 1); entered
    rates are cleared (they were against the old currency) and must be given
    again. The same currency again changes nothing."""
    code = currency_code(currency)
    if code == trip.expense_currency:
        return
    trip.expense_currency = code
    look = rates_svc.Lookup(session, trip, getattr(user, "id", None))
    for e in [*expenses_of(session, trip.id), *budget_of(session, trip.id)]:
        _apply_rate(session, trip, e, look.user_id, None, look)


def apply_to_unconverted(session: Session, trip: Trip, user, currency: str) -> list[str]:
    """After a rate refresh (YONDER-242): convert the caller's VISIBLE costs in
    `currency` that have no rate yet (D-183 — nobody's hidden costs are
    touched). Costs that already have a rate keep it. Returns their ids."""
    look = rates_svc.Lookup(session, trip, getattr(user, "id", None))
    done = []
    expenses, _ = visible_expenses(session, trip, user)
    budget = [b for b in budget_of(session, trip.id) if can_see_budget(session, user, b)]
    for e in [*expenses, *budget]:
        if e.currency == currency and e.rate is None:
            _apply_rate(session, trip, e, look.user_id, None, look)
            if e.rate is not None:
                done.append(e.id)
    session.flush()
    return done


def unlink_activity(session: Session, activity_id: str) -> None:
    """A deleted timeline item leaves its expenses and planned items in place,
    unlinked (rule 3)."""
    for model in (TripExpense, TripBudgetItem):
        session.query(model).filter(model.activity_id == activity_id).update(
            {"activity_id": None}, synchronize_session=False)


def purge_trip(session: Session, trip_id: str) -> None:
    ids = [i for (i,) in session.query(TripExpense.id).filter(TripExpense.trip_id == trip_id)]
    if ids:
        session.query(TripExpenseShare).filter(
            TripExpenseShare.expense_id.in_(ids)).delete(synchronize_session=False)
    session.query(TripExpense).filter(TripExpense.trip_id == trip_id).delete(
        synchronize_session=False)
    bids = [i for (i,) in session.query(TripBudgetItem.id)
            .filter(TripBudgetItem.trip_id == trip_id)]
    if bids:
        session.query(TripBudgetTraveler).filter(
            TripBudgetTraveler.budget_item_id.in_(bids)).delete(synchronize_session=False)
    session.query(TripBudgetItem).filter(TripBudgetItem.trip_id == trip_id).delete(
        synchronize_session=False)
    session.query(TripExpenseComment).filter(TripExpenseComment.trip_id == trip_id).delete(
        synchronize_session=False)
    session.query(TripRate).filter(TripRate.trip_id == trip_id).delete(
        synchronize_session=False)


# --------------------------------------------------------------------------- visibility (D-183)
def can_see(user, owner_id: str | None, people) -> bool:
    """A cost is visible to the travellers on it and to whoever entered it.
    Being the trip's owner or an admin doesn't widen that; auth-off sees all."""
    if scope.unrestricted(user):
        return True
    if owner_id is not None and owner_id == getattr(user, "id", None):
        return True
    tid = getattr(user, "traveler_id", None)
    return tid is not None and tid in set(people)


def expense_people(e: TripExpense, shares) -> set[int]:
    return {e.payer_id, *(s.traveler_id for s in shares)}


def can_see_expense(session: Session, user, e: TripExpense) -> bool:
    return can_see(user, e.owner_id, expense_people(e, shares_of(session, e.id)))


def visible_expenses(session: Session, trip: Trip, user) -> tuple[list[TripExpense], dict]:
    """(the expenses this person can see, their shares by expense id)."""
    rows = expenses_of(session, trip.id)
    smap = share_map(session, [e.id for e in rows])
    seen = [e for e in rows if can_see(user, e.owner_id, expense_people(e, smap.get(e.id, [])))]
    return seen, smap


# --------------------------------------------------------------------------- comments (D-185)
MAX_COMMENT = 4000


def comments_of(session: Session, expense_ids) -> dict[str, list[TripExpenseComment]]:
    ids = list(expense_ids)
    out: dict[str, list[TripExpenseComment]] = {}
    if ids:
        for c in (session.query(TripExpenseComment)
                  .filter(TripExpenseComment.expense_id.in_(ids))
                  .order_by(TripExpenseComment.created_at).all()):
            out.setdefault(c.expense_id, []).append(c)
    return out


def add_comment(session: Session, user, e: TripExpense, body: str) -> TripExpenseComment:
    """Comment on an expense the caller can see (the API checks). Caller commits."""
    text = (body or "").strip()
    if not text:
        raise ValueError("a comment needs some text")
    if len(text) > MAX_COMMENT:
        raise ValueError(f"a comment is at most {MAX_COMMENT} characters")
    c = TripExpenseComment(expense_id=e.id, trip_id=e.trip_id,
                           author_id=getattr(user, "id", None), body=text)
    session.add(c)
    session.flush()
    return c


def can_delete_comment(user, c: TripExpenseComment) -> bool:
    return scope.unrestricted(user) or (c.author_id is not None
                                        and c.author_id == getattr(user, "id", None))


# --------------------------------------------------------------------------- reads
def expenses_of(session: Session, trip_id: str) -> list[TripExpense]:
    return (session.query(TripExpense).filter(TripExpense.trip_id == trip_id)
            .order_by(TripExpense.spent_on.desc(), TripExpense.created_at.desc()).all())


def shares_of(session: Session, expense_id: str) -> list[TripExpenseShare]:
    return (session.query(TripExpenseShare)
            .filter(TripExpenseShare.expense_id == expense_id)
            .order_by(TripExpenseShare.traveler_id).all())


def share_map(session: Session, expense_ids: list[str]) -> dict[str, list[TripExpenseShare]]:
    out: dict[str, list[TripExpenseShare]] = {}
    if expense_ids:
        for s in (session.query(TripExpenseShare)
                  .filter(TripExpenseShare.expense_id.in_(expense_ids))
                  .order_by(TripExpenseShare.traveler_id).all()):
            out.setdefault(s.expense_id, []).append(s)
    return out


def names(session: Session, ids) -> dict[int, str]:
    ids = list(set(ids))
    if not ids:
        return {}
    return {t.id: t.display_name for t in
            session.query(TravelerIdentity).filter(TravelerIdentity.id.in_(ids)).all()}


@dataclass
class Summary:
    currency: str | None
    paid: dict[int, Decimal] = field(default_factory=dict)      # expenses paid for others
    share: dict[int, Decimal] = field(default_factory=dict)     # their part of expenses
    net: dict[int, Decimal] = field(default_factory=dict)       # + is owed, − owes
    transfers: list[tuple[int, int, Decimal]] = field(default_factory=list)
    unconverted: list[str] = field(default_factory=list)
    total: Decimal = Decimal(0)                                 # trip spend, settlements excluded


def summarize(session: Session, trip: Trip, user=None) -> Summary:
    """Balances in the trip's expense currency (D-176, D-177), from the expenses
    `user` can see (D-183). Every expense someone is on is visible to them, so
    their own balance is always complete; other people's are as far as they can
    see."""
    cur = trip.expense_currency
    s = Summary(currency=cur)
    rows, smap = visible_expenses(session, trip, user)
    me = getattr(user, "traveler_id", None)
    roster = _roster(session, trip.id)
    for tid in (roster if scope.unrestricted(user) else [t for t in roster if t == me]):
        s.net.setdefault(tid, Decimal(0))
    for e in rows:
        if e.rate is None:
            s.unconverted.append(e.id)
            continue
        rate = Decimal(e.rate)
        amt = Decimal(e.amount) * rate
        s.net[e.payer_id] = s.net.get(e.payer_id, Decimal(0)) + amt
        if e.kind == EXPENSE:
            s.total += amt
            s.paid[e.payer_id] = s.paid.get(e.payer_id, Decimal(0)) + amt
        for sh in smap.get(e.id, []):
            part = Decimal(sh.amount) * rate
            s.net[sh.traveler_id] = s.net.get(sh.traveler_id, Decimal(0)) - part
            if e.kind == EXPENSE:
                s.share[sh.traveler_id] = s.share.get(sh.traveler_id, Decimal(0)) + part
    q = quantum(cur or "")
    rnd = {k: v.quantize(q, rounding=ROUND_HALF_UP) for k, v in s.net.items()}
    s.net = rnd
    s.paid = {k: v.quantize(q, rounding=ROUND_HALF_UP) for k, v in s.paid.items()}
    s.share = {k: v.quantize(q, rounding=ROUND_HALF_UP) for k, v in s.share.items()}
    s.total = s.total.quantize(q, rounding=ROUND_HALF_UP)
    s.transfers = suggest_transfers(rnd, q)
    return s


def suggest_transfers(net: dict[int, Decimal], q: Decimal) -> list[tuple[int, int, Decimal]]:
    """Greedy largest debtor → largest creditor (D-177): at most n−1 payments.
    Ties break on traveller id so the answer is stable."""
    debtors = sorted(((-v, k) for k, v in net.items() if v < 0), key=lambda x: (-x[0], x[1]))
    creditors = sorted(((v, k) for k, v in net.items() if v > 0), key=lambda x: (-x[0], x[1]))
    out: list[tuple[int, int, Decimal]] = []
    i = j = 0
    debt = [list(d) for d in debtors]
    cred = [list(c) for c in creditors]
    while i < len(debt) and j < len(cred):
        pay = min(debt[i][0], cred[j][0])
        if pay >= q:
            out.append((debt[i][1], cred[j][1], pay))
        debt[i][0] -= pay
        cred[j][0] -= pay
        if debt[i][0] < q:
            i += 1
        if cred[j][0] < q:
            j += 1
    return out


# --------------------------------------------------------------------------- budget (planned)
DRAFT, KEPT = "draft", "kept"
MAX_BUDGET_BATCH = 100


def trip_days(trip: Trip) -> int | None:
    """Days the trip spans, both ends included; None without both dates."""
    from datetime import date
    try:
        a, b = date.fromisoformat(trip.start_date or ""), date.fromisoformat(trip.end_date or "")
    except ValueError:
        return None
    return (b - a).days + 1 if b >= a else None


@dataclass
class BudgetInput:
    amount: object
    currency: str
    title: str = ""
    category: str = "other"
    traveler_ids: list | None = None       # who it's for (D-184); required from agents
    start_date: str | None = None          # when it applies, if not the whole trip
    end_date: str | None = None
    rate: object = None
    place_key: str | None = None
    activity_id: str | None = None
    notes: str | None = None
    sources: list | None = None            # [{title, url}]
    source: str = "api"


def _check_category(category: str | None) -> str:
    c = (category or "other").lower()
    if c not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
    return c


def _clean_sources(sources) -> list | None:
    out = []
    for s in sources or []:
        if isinstance(s, dict) and str(s.get("url") or "").startswith(("http://", "https://")):
            out.append({"title": str(s.get("title") or "")[:200], "url": str(s["url"])[:2000]})
    return out or None


def _budget_people(session: Session, trip: Trip, user, ids, *, default_me: bool) -> list[int]:
    """The travellers a planned cost is for: roster ids only, at least one. A
    person's own entry defaults to themselves; an agent must say (D-184)."""
    roster = _roster(session, trip.id)
    picked = sorted({_on_roster(t, roster, "traveller") for t in (ids or [])})
    if not picked and default_me:
        me = getattr(user, "traveler_id", None)
        if me in roster:
            picked = [me]
    if not picked:
        raise ValueError("tag the traveller(s) this cost is for (traveler_ids from the trip)")
    return picked


def _set_budget_people(session: Session, item_id: str, ids: list[int]) -> None:
    session.query(TripBudgetTraveler).filter(
        TripBudgetTraveler.budget_item_id == item_id).delete(synchronize_session=False)
    for t in ids:
        session.add(TripBudgetTraveler(budget_item_id=item_id, traveler_id=t))
    session.flush()


def budget_people(session: Session, item_ids) -> dict[str, list[int]]:
    ids = list(item_ids)
    out: dict[str, list[int]] = {}
    if ids:
        for r in (session.query(TripBudgetTraveler)
                  .filter(TripBudgetTraveler.budget_item_id.in_(ids)).all()):
            out.setdefault(r.budget_item_id, []).append(r.traveler_id)
    for v in out.values():
        v.sort()
    return out


def can_see_budget(session: Session, user, b: TripBudgetItem) -> bool:
    return can_see(user, b.owner_id, budget_people(session, [b.id]).get(b.id, []))


def _check_span(start: str | None, end: str | None) -> tuple[str | None, str | None]:
    a, b = _check_date(start), _check_date(end)
    if a and b and b < a:
        raise ValueError("the end date is before the start date")
    return a, b


def create_budget(session: Session, user, trip: Trip, data: BudgetInput,
                  status: str = KEPT) -> TripBudgetItem:
    """Add a planned cost for the travellers it's tagged with. People's own
    entries are kept (and default to themselves); agents' are drafts that must
    name their travellers (D-180, D-184). Caller commits."""
    cur = currency_code(data.currency)
    people = _budget_people(session, trip, user, data.traveler_ids, default_me=status == KEPT)
    start, end = _check_span(data.start_date, data.end_date)
    b = TripBudgetItem(trip_id=trip.id, owner_id=getattr(user, "id", None),
                       title=(data.title or "").strip()[:200],
                       category=_check_category(data.category),
                       amount=_fmt(money(data.amount, cur)), currency=cur,
                       status=status,
                       start_date=start, end_date=end,
                       place_key=(data.place_key or None),
                       activity_id=_check_activity(session, trip.id, data.activity_id),
                       notes=(data.notes or None), sources=_clean_sources(data.sources),
                       source=data.source or "api")
    if trip.expense_currency is None:
        trip.expense_currency = cur
    _apply_rate(session, trip, b, getattr(user, "id", None), data.rate)
    session.add(b)
    session.flush()
    _set_budget_people(session, b.id, people)
    return b


def update_budget(session: Session, user, trip: Trip, b: TripBudgetItem,
                  changes: dict) -> TripBudgetItem:
    """Edit a planned item; a person's edit keeps it (a draft becomes theirs)."""
    before = (b.currency, rates_svc.day_key(b))
    if changes.get("title") is not None:
        b.title = (changes["title"] or "").strip()[:200]
    if changes.get("category") is not None:
        b.category = _check_category(changes["category"])
    if changes.get("currency") is not None:
        b.currency = currency_code(changes["currency"])
    if changes.get("amount") is not None or changes.get("currency") is not None:
        b.amount = _fmt(money(changes.get("amount") or b.amount, b.currency))
    if changes.get("traveler_ids") is not None:
        _set_budget_people(session, b.id, _budget_people(
            session, trip, user, changes["traveler_ids"], default_me=False))
    if "start_date" in changes or "end_date" in changes:
        b.start_date, b.end_date = _check_span(
            changes.get("start_date", b.start_date) or None,
            changes.get("end_date", b.end_date) or None)
    if changes.get("notes") is not None:
        b.notes = changes["notes"] or None
    if changes.get("place_key") is not None:
        b.place_key = changes["place_key"] or None
    if changes.get("activity_id") is not None:
        b.activity_id = _check_activity(session, trip.id, changes["activity_id"])
    if changes.get("status") is not None:
        if changes["status"] not in (DRAFT, KEPT):
            raise ValueError("status must be 'draft' or 'kept'")
        b.status = changes["status"]
    elif changes:
        b.status = KEPT
    if _rerate(b, changes, before):
        _apply_rate(session, trip, b, getattr(user, "id", None), changes.get("rate"))
    session.flush()
    return b


def delete_budget(session: Session, b: TripBudgetItem) -> None:
    """Remove a planned item; actuals that pointed at it stay, unlinked."""
    session.query(TripExpense).filter(TripExpense.budget_item_id == b.id).update(
        {"budget_item_id": None}, synchronize_session=False)
    session.query(TripBudgetTraveler).filter(
        TripBudgetTraveler.budget_item_id == b.id).delete(synchronize_session=False)
    session.delete(b)


def save_budget_batch(session: Session, user, trip: Trip, items: list[BudgetInput],
                      *, replace: bool = True) -> dict:
    """An agent's budget research (D-180): every item lands as a draft and must
    name the traveller(s) it's for — nothing is extrapolated to anyone else
    (D-184). With `replace`, the caller's own earlier DRAFTS in the categories
    it's re-researching go first, so a re-run doesn't pile up; kept items are
    never touched, and an item matching a kept one (same category, title and
    travellers) is skipped. Returns the outcome per item. Caller commits."""
    if len(items) > MAX_BUDGET_BATCH:
        raise ValueError(f"at most {MAX_BUDGET_BATCH} planned items per call")
    existing = budget_of(session, trip.id)
    tags = budget_people(session, [b.id for b in existing])
    kept = {(b.category, b.title.strip().lower(), tuple(tags.get(b.id, [])))
            for b in existing if b.status == KEPT}
    if replace:
        cats = {(i.category or "other").lower() for i in items}
        uid = getattr(user, "id", None)
        for b in existing:
            if b.status == DRAFT and b.category in cats and b.owner_id == uid:
                delete_budget(session, b)
        session.flush()
    results = []
    for i, data in enumerate(items):
        try:
            people = tuple(sorted({int(t) for t in (data.traveler_ids or [])}))
        except (TypeError, ValueError):
            people = ()
        key = ((data.category or "other").lower(), (data.title or "").strip().lower(), people)
        if key in kept:
            results.append({"index": i, "outcome": "kept_already"})
            continue
        try:
            b = create_budget(session, user, trip, data, status=DRAFT)
            results.append({"index": i, "outcome": "drafted", "id": b.id})
        except ValueError as err:
            results.append({"index": i, "outcome": "error", "error": str(err)})
    return {"results": results,
            "counts": {o: sum(1 for r in results if r["outcome"] == o)
                       for o in sorted({r["outcome"] for r in results})}}


def budget_of(session: Session, trip_id: str) -> list[TripBudgetItem]:
    return (session.query(TripBudgetItem).filter(TripBudgetItem.trip_id == trip_id)
            .order_by(TripBudgetItem.category, TripBudgetItem.created_at).all())


@dataclass
class BudgetLine:
    item: TripBudgetItem
    traveler_ids: list[int]          # who it's for
    total: Decimal | None            # in the trip currency; None = no rate


@dataclass
class Plan:
    currency: str | None
    people: int                      # on the roster (for reference; items count their own)
    days: int | None
    lines: list[BudgetLine]
    planned: Decimal                 # kept items
    drafts: Decimal                  # agents' suggestions not yet kept
    actual: Decimal                  # actual expenses, settlements excluded
    by_category: list[tuple[str, Decimal, Decimal, Decimal]]   # (category, planned, drafts, actual)
    by_traveler: list[tuple[int, Decimal, Decimal]]            # (traveller, planned, actual)
    unconverted: list[str]


def _split_evenly(total: Decimal, ids: list[int], q: Decimal) -> dict[int, Decimal]:
    if not ids:
        return {}
    units = int((total / q).to_integral_value(rounding=ROUND_HALF_UP))
    base, extra = divmod(units, len(ids))
    return {t: (base + (1 if i < extra else 0)) * q for i, t in enumerate(sorted(ids))}


def plan(session: Session, trip: Trip, user=None) -> Plan:
    """The budget next to what was actually spent, in the trip currency (D-181),
    from what `user` can see (D-183). A planned cost is one amount (D-179); per
    traveller (D-184) it's split evenly between the people tagged on it. Actuals
    per traveller are their shares of expenses."""
    cur = trip.expense_currency
    q = quantum(cur or "")
    items = budget_of(session, trip.id)
    tags = budget_people(session, [b.id for b in items])
    lines, unconverted = [], []
    planned = drafts = Decimal(0)
    cats: dict[str, list[Decimal]] = {}
    per: dict[int, list[Decimal]] = {}
    for b in items:
        people = tags.get(b.id, [])
        if not can_see(user, b.owner_id, people):
            continue
        total = None
        if b.rate is None:
            unconverted.append(b.id)
        else:
            total = (Decimal(b.amount) * Decimal(b.rate)).quantize(q, rounding=ROUND_HALF_UP)
            row = cats.setdefault(b.category, [Decimal(0)] * 3)
            if b.status == KEPT:
                planned += total
                row[0] += total
                for t, v in _split_evenly(total, people, q).items():
                    per.setdefault(t, [Decimal(0), Decimal(0)])[0] += v
            else:
                drafts += total
                row[1] += total
        lines.append(BudgetLine(item=b, traveler_ids=people, total=total))
    actual = Decimal(0)
    rows, smap = visible_expenses(session, trip, user)
    for e in rows:
        if e.kind == EXPENSE and e.rate is not None:
            rate = Decimal(e.rate)
            amt = (Decimal(e.amount) * rate).quantize(q, rounding=ROUND_HALF_UP)
            actual += amt
            cats.setdefault(e.category, [Decimal(0)] * 3)[2] += amt
            for sh in smap.get(e.id, []):
                per.setdefault(sh.traveler_id, [Decimal(0), Decimal(0)])[1] += (
                    Decimal(sh.amount) * rate).quantize(q, rounding=ROUND_HALF_UP)
    order = {c: i for i, c in enumerate(CATEGORIES)}
    by_cat = [(c, *v) for c, v in sorted(cats.items(), key=lambda kv: order.get(kv[0], 99))]
    by_tr = [(t, v[0], v[1]) for t, v in sorted(per.items())]
    return Plan(currency=cur, people=len(_roster(session, trip.id)), days=trip_days(trip),
                lines=lines, planned=planned, drafts=drafts, actual=actual,
                by_category=by_cat, by_traveler=by_tr, unconverted=unconverted)
