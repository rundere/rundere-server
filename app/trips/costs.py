"""The trip page's Costs tab — the web side of trip expenses (YONDER-212,
SPEC-10 follow-up).

Server-rendered on top of the same rules as /api/v1 and MCP: every number comes
from `app.expenses` through `api_v1.expenses_overview`, so the web never works a
balance out on its own. The gates are the API's:

- the trip gate (D-174): `scope.trip_or_none` — view to read, edit to write
  (a view-only share gets 403 on every POST);
- only the people on a cost see it (D-183): an expense the caller can't see is a
  404 exactly like a missing one, for reads and writes alike.

Location (YONDER-212) is captured by the iOS app only. The web shows a stored one
(its label, or a map link) and never captures or edits it.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import expenses as expenses_svc, scope
from app.db import get_session
from app.models import TripExpense
from app.trips.routes import _redirect, templates

router = APIRouter(prefix="/trips")

_CATEGORY_LABELS = {"food": "Food & drink", "lodging": "Lodging", "transport": "Transport",
                    "activities": "Activities", "shopping": "Shopping", "fees": "Fees",
                    "other": "Other"}


def _osm_link(lat, lon) -> str | None:
    # OpenStreetMap, not Google: Run·dere's maps stay off Google (geocoding policy).
    if lat is None or lon is None:
        return None
    return (f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}"
            f"#map=17/{lat:.5f}/{lon:.5f}")


def _money(amount: str | None, currency: str | None) -> str:
    """"1234.5", "EUR" -> "1,234.50 EUR" at the currency's minor unit."""
    if amount in (None, ""):
        return "—"
    try:
        d = Decimal(str(amount))
    except InvalidOperation:
        return f"{amount} {currency or ''}".strip()
    q = expenses_svc.quantum(currency or "")
    places = max(0, -q.as_tuple().exponent)
    return f"{d:,.{places}f} {currency or ''}".strip()


def _roster(session: Session, trip_id: str) -> list[dict]:
    ids = expenses_svc._roster(session, trip_id)
    nm = expenses_svc.names(session, ids)
    return [{"id": t, "name": nm.get(t, f"#{t}")} for t in ids]


def costs_context(session: Session, trip, user) -> dict:
    """Everything the Costs tab renders, for `user` (D-183)."""
    from app.api_v1 import expenses_overview
    ov = expenses_overview(session, trip, user)
    me = getattr(user, "traveler_id", None)
    cur = ov.currency
    rows = []
    for e in ov.expenses:
        rows.append({
            "id": e.id, "kind": e.kind, "title": e.title or (
                "Settle-up" if e.kind == expenses_svc.SETTLEMENT else "Expense"),
            "amount": _money(e.amount, e.currency),
            "converted": (_money(e.amount_in_trip_currency, cur)
                          if e.amount_in_trip_currency and e.currency != cur else None),
            "unconverted": e.rate is None,
            "spent_on": e.spent_on, "payer": e.payer_name,
            "for": ", ".join(s.name for s in e.shares),
            "category": _CATEGORY_LABELS.get(e.category, e.category),
            "comments": len(e.comments),
            "place_label": e.place_label,
            "place_link": _osm_link(e.place_lat, e.place_lon),
        })
    rows.sort(key=lambda r: (r["spent_on"] or ""), reverse=True)
    balances = [{"name": b.name, "paid": _money(b.paid, cur), "share": _money(b.share, cur),
                 "net": _money(b.net.lstrip("-"), cur),
                 "sign": (-1 if b.net.startswith("-") else (0 if Decimal(b.net) == 0 else 1)),
                 "me": b.traveler_id == me}
                for b in ov.balances]
    transfers = [{"from_id": t.from_id, "from": t.from_name, "to_id": t.to_id, "to": t.to_name,
                  "amount": t.amount, "display": _money(t.amount, cur)}
                 for t in ov.transfers]
    plan = ov.plan
    by_category = []
    if plan is not None:
        by_category = [{"label": _CATEGORY_LABELS.get(c.category, c.category),
                        "planned": _money(c.planned, cur), "actual": _money(c.actual, cur)}
                       for c in plan.by_category
                       if Decimal(c.planned) or Decimal(c.actual)]
    can_edit = scope.can_edit_trip(session, user, trip)
    return {"costs": {
        "currency": cur, "total": _money(ov.total, cur) if cur else None,
        "rows": rows, "balances": balances, "transfers": transfers,
        "unconverted": len(ov.unconverted),
        "planned": (_money(plan.planned, cur)
                    if plan and cur and Decimal(plan.planned) else None),
        "by_category": by_category,
        "can_edit": can_edit,
        "has_roster": bool(expenses_svc._roster(session, trip.id)),
    }}


def _section(request: Request, session: Session, trip) -> HTMLResponse:
    user = scope.user_of(request)
    return templates.TemplateResponse(
        request, "_costs_section.html",
        {"request": request, "trip": trip, **costs_context(session, trip, user)})


def _visible_expense(session: Session, request: Request, trip, expense_id: str):
    """The expense if it's on this trip and the caller is on it or entered it
    (D-183); None otherwise — the caller 404s, the same as a missing one."""
    e = session.get(TripExpense, expense_id)
    if (e is None or e.trip_id != trip.id
            or not expenses_svc.can_see_expense(session, scope.user_of(request), e)):
        return None
    return e


def _form_view(session: Session, trip, user, e=None, form=None) -> dict:
    """The add/edit form's values: from a submitted form (after an error), the
    expense being edited, or the defaults (me paying, everyone, today's trip
    currency)."""
    me = getattr(user, "traveler_id", None)
    roster = _roster(session, trip.id)
    ids = [r["id"] for r in roster]
    if form is not None:
        return form
    if e is None:
        return {"title": "", "amount": "", "currency": trip.expense_currency or "",
                "spent_on": "", "payer_id": me if me in ids else (ids[0] if ids else None),
                "split": "equal", "traveler_ids": ids, "shares": {},
                "category": "other", "rate": "", "notes": ""}
    shares = {s.traveler_id: s.amount for s in expenses_svc.shares_of(session, e.id)}
    return {"title": e.title, "amount": e.amount, "currency": e.currency,
            "spent_on": e.spent_on or "", "payer_id": e.payer_id, "split": e.split,
            "traveler_ids": list(shares), "shares": shares, "category": e.category,
            "rate": e.rate if e.rate_source == "entered" else "", "notes": e.notes or ""}


def _modal(request: Request, session: Session, trip, *, e=None, form=None,
           error: str | None = None) -> HTMLResponse:
    user = scope.user_of(request)
    place = None
    comments = []
    if e is not None:
        if e.place_lat is not None:
            place = {"label": e.place_label, "link": _osm_link(e.place_lat, e.place_lon),
                     "accuracy": e.place_accuracy_m}
        from app.api_v1 import _one_expense_out
        comments = _one_expense_out(session, trip, e, user).comments
    return templates.TemplateResponse(request, "_expense_modal.html", {
        "request": request, "trip": trip, "expense": e, "error": error,
        "form": _form_view(session, trip, user, e, form),
        "roster": _roster(session, trip.id),
        "categories": [(c, _CATEGORY_LABELS.get(c, c)) for c in expenses_svc.CATEGORIES],
        "place": place, "comments": comments,
        "can_edit": scope.can_edit_trip(session, user, trip),
        "settlement": e is not None and e.kind == expenses_svc.SETTLEMENT,
    })


async def _read_form(request: Request) -> dict:
    """The submitted add/edit form, normalised to ExpenseInput field names."""
    f = await request.form()
    ids = [int(v) for v in f.getlist("traveler_ids") if str(v).strip().isdigit()]
    shares = {}
    for key, val in f.multi_items():
        if key.startswith("share_") and str(val).strip():
            try:
                shares[int(key[6:])] = str(val).strip()
            except ValueError:
                continue
    payer = str(f.get("payer_id") or "").strip()
    return {"title": str(f.get("title") or "").strip(),
            "amount": str(f.get("amount") or "").strip(),
            "currency": str(f.get("currency") or "").strip().upper(),
            "spent_on": str(f.get("spent_on") or "").strip(),
            "payer_id": int(payer) if payer.isdigit() else None,
            "split": "exact" if f.get("split") == "exact" else "equal",
            "traveler_ids": ids, "shares": shares,
            "category": str(f.get("category") or "other"),
            "rate": str(f.get("rate") or "").strip(),
            "notes": str(f.get("notes") or "").strip()}


def _changes(form: dict) -> dict:
    """The form as expense fields. Equal split sends who it's between; exact
    sends each person's amount (blank = not on it)."""
    out = {k: form[k] for k in ("title", "amount", "currency", "payer_id", "split",
                                "category", "notes")}
    out["spent_on"] = form["spent_on"] or None
    out["rate"] = form["rate"] or None
    if form["split"] == "exact":
        out["shares"] = form["shares"]
        out["traveler_ids"] = None
    else:
        if not form["traveler_ids"]:
            raise ValueError("pick at least one person it's for")
        out["traveler_ids"] = form["traveler_ids"]
        out["shares"] = None
    if out["payer_id"] is None:
        raise ValueError("pick who paid")
    return out


# ------------------------------------------------------------------ routes

@router.get("/{trip_id}/costs", response_class=HTMLResponse)
def costs_section(trip_id: str, request: Request, session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    return _section(request, session, trip)


@router.get("/{trip_id}/costs/new", response_class=HTMLResponse)
def new_expense(trip_id: str, request: Request, session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    return _modal(request, session, trip)


@router.post("/{trip_id}/costs", response_class=HTMLResponse)
async def add_expense(trip_id: str, request: Request, session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)        # 403 for view-only
    if trip is None:
        return HTMLResponse("", status_code=404)
    form = await _read_form(request)
    try:
        data = expenses_svc.ExpenseInput(**_changes(form), source="web")
        expenses_svc.create(session, scope.user_of(request), trip, data)
    except ValueError as err:
        session.rollback()
        return _modal(request, session, trip, form=form, error=str(err))
    session.commit()
    return _redirect(f"/trips/{trip.id}#costs")


# Fixed paths before /costs/{expense_id}, which would otherwise swallow them.
@router.post("/{trip_id}/costs/settle", response_class=HTMLResponse)
async def settle(trip_id: str, request: Request, session: Session = Depends(get_session)):
    """Record a suggested payment as made (D-177): `from` paid `to` back."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    f = await request.form()
    try:
        expenses_svc.record_settlement(
            session, scope.user_of(request), trip, from_id=int(f.get("from_id") or 0),
            to_id=int(f.get("to_id") or 0), amount=str(f.get("amount") or ""),
            source="web")
    except ValueError as err:
        session.rollback()
        return HTMLResponse(f"<p class='bp-error'>{_esc(str(err))}</p>", status_code=400)
    session.commit()
    return _section(request, session, trip)


@router.post("/{trip_id}/costs/currency", response_class=HTMLResponse)
async def set_currency(trip_id: str, request: Request, session: Session = Depends(get_session)):
    """Change the currency balances are shown in (D-176)."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    cur = str((await request.form()).get("currency") or "")
    try:
        expenses_svc.set_trip_currency(session, scope.user_of(request), trip, cur)
    except ValueError as err:
        session.rollback()
        return HTMLResponse(f"<p class='bp-error'>{_esc(str(err))}</p>", status_code=400)
    session.commit()
    return _section(request, session, trip)


@router.get("/{trip_id}/costs/{expense_id}/edit", response_class=HTMLResponse)
def edit_expense(trip_id: str, expense_id: str, request: Request,
                 session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    e = _visible_expense(session, request, trip, expense_id) if trip else None
    if e is None:
        return HTMLResponse("", status_code=404)
    return _modal(request, session, trip, e=e)


@router.post("/{trip_id}/costs/{expense_id}", response_class=HTMLResponse)
async def update_expense(trip_id: str, expense_id: str, request: Request,
                         session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    e = _visible_expense(session, request, trip, expense_id) if trip else None
    if e is None:
        return HTMLResponse("", status_code=404)
    form = await _read_form(request)
    try:
        changes = _changes(form)
        if e.kind == expenses_svc.SETTLEMENT:
            # A settlement's people are fixed: only its amount, date and note move.
            changes = {k: changes[k] for k in ("amount", "spent_on", "notes", "rate")}
        # The web never sends a location: an edit keeps the stored one (YONDER-212).
        expenses_svc.update(session, scope.user_of(request), trip, e, changes)
    except ValueError as err:
        session.rollback()
        return _modal(request, session, trip, e=e, form=form, error=str(err))
    session.commit()
    return _redirect(f"/trips/{trip.id}#costs")


@router.post("/{trip_id}/costs/{expense_id}/delete", response_class=HTMLResponse)
def delete_expense(trip_id: str, expense_id: str, request: Request,
                   session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    e = _visible_expense(session, request, trip, expense_id) if trip else None
    if e is None:
        return HTMLResponse("", status_code=404)
    expenses_svc.delete(session, e)
    session.commit()
    return _redirect(f"/trips/{trip.id}#costs")


@router.post("/{trip_id}/costs/{expense_id}/comment", response_class=HTMLResponse)
async def comment_expense(trip_id: str, expense_id: str, request: Request,
                          session: Session = Depends(get_session)):
    """Comment on an actual (D-185) — anyone who can see it."""
    trip = scope.trip_or_none(session, request, trip_id)
    e = _visible_expense(session, request, trip, expense_id) if trip else None
    if e is None:
        return HTMLResponse("", status_code=404)
    body = str((await request.form()).get("body") or "")
    try:
        expenses_svc.add_comment(session, scope.user_of(request), e, body)
    except ValueError as err:
        session.rollback()
        return _modal(request, session, trip, e=e, error=str(err))
    session.commit()
    return _modal(request, session, trip, e=e)


def _esc(s: str) -> str:
    from markupsafe import escape
    return str(escape(s))
