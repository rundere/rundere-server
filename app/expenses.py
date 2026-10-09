"""Run·dere 4.8 — trip expenses, balances and settle-up (SPEC-10, YONDER-172).

The one place the expense rules live, so /api/v1 and MCP never diverge:

- **Small on purpose (D-173).** Expenses with an equal or exact split, net
  balances, suggested payments and recorded settlements. No imports (D-155).
- **Trip sharing, not journal sharing (D-174).** Readers need `can_view_trip`,
  writers `can_edit_trip`; callers resolve the trip with `scope.require_trip`.
- **Roster people only (D-175).** Payer and sharers are TravelerIdentity ids on
  the trip's roster when the expense is saved. Shares are always stored, an equal
  split divided to the currency's minor unit with the leftover going to the first
  travellers in id order, so they add up exactly.
- **Own currency + the rate used (D-176).** Balances are in the trip's expense
  currency. A blank rate is filled from the caller's stored Discover currency
  rates for the trip (no live call, D-146); unknown stays NULL and the expense is
  listed as unconverted instead of guessed.
- **Settlements are payments (D-177)** and net with expenses; suggested payments
  are a greedy largest-debtor → largest-creditor match.
- **Planned vs actual (D-178–D-181).** Planned items (TripBudgetItem) are the
  budget: estimates on a basis, expanded by the roster and the trip's days, never
  split or settled. Only actual expenses feed balances and the trip's actual
  cost. Agents' planned items land as drafts the person keeps (D-180); an actual
  can name the planned item it fulfils, for plan vs actual by category (D-181).

Bad input raises ValueError (the API maps it to 400).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models import (DiscoveryFinding, TravelerIdentity, Trip, TripBudgetItem,
                        TripExpense, TripExpenseShare)
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


def currency_code(value: str | None) -> str:
    c = (value or "").strip().upper()
    if len(c) != 3 or not c.isalpha():
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
    trip (D-65 snapshots), pivoting through a base both are quoted against — the
    same table the trip's currency calculator uses. None when it can't tell."""
    if frm == to:
        return Decimal(1)
    q = session.query(DiscoveryFinding).filter(DiscoveryFinding.trip_id == trip_id,
                                               DiscoveryFinding.topic == "currency")
    if user_id is not None:
        q = q.filter(DiscoveryFinding.user_id == user_id)
    # per_base[base][code] = units of code per 1 base.
    per_base: dict[str, dict[str, Decimal]] = {}
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
    for table in per_base.values():
        if frm in table and to in table:
            return (table[to] / table[frm]).quantize(_RATE_Q, rounding=ROUND_HALF_UP)
    return None


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


def _apply_rate(session: Session, trip: Trip, e: TripExpense, user_id: str | None,
                entered) -> None:
    tc = trip.expense_currency
    given = _rate(entered)
    if tc is None or e.currency == tc:
        e.rate, e.rate_source = "1", "same"
    elif given is not None:
        e.rate, e.rate_source = _fmt(given), "entered"
    else:
        found = discover_rate(session, trip.id, user_id, e.currency, tc)
        e.rate, e.rate_source = (_fmt(found), "discover") if found is not None else (None, None)


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
                    notes=(data.notes or None), source=data.source or "api")
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
    if changes.get("rate") is not None or changes.get("currency") is not None:
        _apply_rate(session, trip, e, getattr(user, "id", None), changes.get("rate"))
    session.flush()
    return e


def delete(session: Session, e: TripExpense) -> None:
    session.query(TripExpenseShare).filter(
        TripExpenseShare.expense_id == e.id).delete(synchronize_session=False)
    session.delete(e)


def set_trip_currency(session: Session, user, trip: Trip, currency: str) -> None:
    """Change the balance currency (D-176): rates are re-derived — same currency
    becomes 1, Discover rates are looked up again, entered rates are cleared
    (they were against the old currency) and must be given again."""
    trip.expense_currency = currency_code(currency)
    uid = getattr(user, "id", None)
    for e in [*expenses_of(session, trip.id), *budget_of(session, trip.id)]:
        _apply_rate(session, trip, e, uid, None)


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
    session.query(TripBudgetItem).filter(TripBudgetItem.trip_id == trip_id).delete(
        synchronize_session=False)


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


def summarize(session: Session, trip: Trip) -> Summary:
    """Balances in the trip's expense currency (D-176, D-177)."""
    cur = trip.expense_currency
    s = Summary(currency=cur)
    rows = expenses_of(session, trip.id)
    smap = share_map(session, [e.id for e in rows])
    for tid in _roster(session, trip.id):
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
BASES = ("trip", "person", "day", "person_day")
MAX_BUDGET_BATCH = 100


def trip_days(trip: Trip) -> int | None:
    """Days the trip spans, both ends included; None without both dates."""
    from datetime import date
    try:
        a, b = date.fromisoformat(trip.start_date or ""), date.fromisoformat(trip.end_date or "")
    except ValueError:
        return None
    return (b - a).days + 1 if b >= a else None


def multiplier(basis: str, people: int, days: int | None) -> int:
    """How many times an estimate counts (D-179). Unknown days count once; the
    API flags those items so the person can add the trip's dates."""
    p, d = max(people, 1), days or 1
    return {"trip": 1, "person": p, "day": d, "person_day": p * d}.get(basis, 1)


@dataclass
class BudgetInput:
    amount: object
    currency: str
    title: str = ""
    category: str = "other"
    basis: str = "trip"
    rate: object = None
    place_key: str | None = None
    activity_id: str | None = None
    notes: str | None = None
    sources: list | None = None            # [{title, url}]
    source: str = "api"


def _check_basis(basis: str | None) -> str:
    b = (basis or "trip").lower()
    if b not in BASES:
        raise ValueError(f"basis must be one of {', '.join(BASES)}")
    return b


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


def create_budget(session: Session, user, trip: Trip, data: BudgetInput,
                  status: str = KEPT) -> TripBudgetItem:
    """Add a planned cost. People's own entries are kept; agents' are drafts
    (D-180). Caller commits."""
    cur = currency_code(data.currency)
    b = TripBudgetItem(trip_id=trip.id, owner_id=getattr(user, "id", None),
                       title=(data.title or "").strip()[:200],
                       category=_check_category(data.category),
                       amount=_fmt(money(data.amount, cur)), currency=cur,
                       basis=_check_basis(data.basis), status=status,
                       place_key=(data.place_key or None),
                       activity_id=_check_activity(session, trip.id, data.activity_id),
                       notes=(data.notes or None), sources=_clean_sources(data.sources),
                       source=data.source or "api")
    if trip.expense_currency is None:
        trip.expense_currency = cur
    _apply_rate(session, trip, b, getattr(user, "id", None), data.rate)
    session.add(b)
    session.flush()
    return b


def update_budget(session: Session, user, trip: Trip, b: TripBudgetItem,
                  changes: dict) -> TripBudgetItem:
    """Edit a planned item; a person's edit keeps it (a draft becomes theirs)."""
    if changes.get("title") is not None:
        b.title = (changes["title"] or "").strip()[:200]
    if changes.get("category") is not None:
        b.category = _check_category(changes["category"])
    if changes.get("basis") is not None:
        b.basis = _check_basis(changes["basis"])
    if changes.get("currency") is not None:
        b.currency = currency_code(changes["currency"])
    if changes.get("amount") is not None or changes.get("currency") is not None:
        b.amount = _fmt(money(changes.get("amount") or b.amount, b.currency))
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
    if changes.get("rate") is not None or changes.get("currency") is not None:
        _apply_rate(session, trip, b, getattr(user, "id", None), changes.get("rate"))
    session.flush()
    return b


def delete_budget(session: Session, b: TripBudgetItem) -> None:
    """Remove a planned item; actuals that pointed at it stay, unlinked."""
    session.query(TripExpense).filter(TripExpense.budget_item_id == b.id).update(
        {"budget_item_id": None}, synchronize_session=False)
    session.delete(b)


def save_budget_batch(session: Session, user, trip: Trip, items: list[BudgetInput],
                      *, replace: bool = True) -> dict:
    """An agent's budget research (D-180): every item lands as a draft. With
    `replace`, the agent's earlier DRAFTS in the categories it's re-researching
    go first, so a re-run doesn't pile up; kept items are never touched, and an
    item matching a kept one (same category and title) is skipped. Returns the
    outcome per item. Caller commits."""
    if len(items) > MAX_BUDGET_BATCH:
        raise ValueError(f"at most {MAX_BUDGET_BATCH} planned items per call")
    kept = {(b.category, b.title.strip().lower()) for b in budget_of(session, trip.id)
            if b.status == KEPT}
    if replace:
        cats = {(i.category or "other").lower() for i in items}
        for b in budget_of(session, trip.id):
            if b.status == DRAFT and b.category in cats:
                delete_budget(session, b)
        session.flush()
    results = []
    for i, data in enumerate(items):
        key = ((data.category or "other").lower(), (data.title or "").strip().lower())
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
    times: int                       # how many times the estimate counts
    total: Decimal | None            # in the trip currency; None = no rate


@dataclass
class Plan:
    currency: str | None
    people: int
    days: int | None
    lines: list[BudgetLine]
    planned: Decimal                 # kept items
    drafts: Decimal                  # agents' suggestions not yet kept
    actual: Decimal                  # actual expenses, settlements excluded
    by_category: list[tuple[str, Decimal, Decimal, Decimal]]   # (category, planned, drafts, actual)
    unconverted: list[str]


def plan(session: Session, trip: Trip) -> Plan:
    """The budget next to what was actually spent, in the trip currency (D-181)."""
    cur = trip.expense_currency
    q = quantum(cur or "")
    people, days = len(_roster(session, trip.id)), trip_days(trip)
    lines, unconverted = [], []
    planned = drafts = Decimal(0)
    cats: dict[str, list[Decimal]] = {}
    for b in budget_of(session, trip.id):
        times = multiplier(b.basis, people, days)
        total = None
        if b.rate is None:
            unconverted.append(b.id)
        else:
            total = (Decimal(b.amount) * times * Decimal(b.rate)).quantize(q, rounding=ROUND_HALF_UP)
            row = cats.setdefault(b.category, [Decimal(0)] * 3)
            if b.status == KEPT:
                planned += total
                row[0] += total
            else:
                drafts += total
                row[1] += total
        lines.append(BudgetLine(item=b, times=times, total=total))
    actual = Decimal(0)
    for e in expenses_of(session, trip.id):
        if e.kind == EXPENSE and e.rate is not None:
            amt = (Decimal(e.amount) * Decimal(e.rate)).quantize(q, rounding=ROUND_HALF_UP)
            actual += amt
            cats.setdefault(e.category, [Decimal(0)] * 3)[2] += amt
    order = {c: i for i, c in enumerate(CATEGORIES)}
    by_cat = [(c, *v) for c, v in sorted(cats.items(), key=lambda kv: order.get(kv[0], 99))]
    return Plan(currency=cur, people=people, days=days, lines=lines, planned=planned,
                drafts=drafts, actual=actual, by_category=by_cat, unconverted=unconverted)
