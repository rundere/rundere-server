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
from app.trips.routes import templates

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


def costs_context(session: Session, trip, user, sub: str = "") -> dict:
    """Everything the Costs tab renders, for `user` (D-183). `sub` is the open
    sub-tab, "actual" or "plan" (YONDER-221)."""
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
                        "planned": _money(c.planned, cur) if Decimal(c.planned) else None,
                        "actual": _money(c.actual, cur)}
                       for c in plan.by_category
                       if Decimal(c.planned) or Decimal(c.actual)]
    lines = []
    if plan is not None:
        for b in plan.items:
            lines.append({
                "id": b.id, "title": b.title or _CATEGORY_LABELS.get(b.category, b.category),
                "category": _CATEGORY_LABELS.get(b.category, b.category),
                "amount": _money(b.amount, b.currency),
                "total": (_money(b.total, cur) if b.total and b.currency != cur else None),
                "actual": _money(b.actual, cur) if Decimal(b.actual or "0") else None,
                "for": ", ".join(n for n in b.traveler_names if n),
                "when": " – ".join(x for x in (b.start_date, b.end_date) if x),
                "draft": b.status == expenses_svc.DRAFT})
    can_edit = scope.can_edit_trip(session, user, trip)
    if sub not in ("actual", "plan"):
        # Open on whatever has something in it; Actual when both or neither.
        sub = "plan" if (lines and not rows) else "actual"
    return {"costs": {
        "sub": sub, "lines": lines,
        "drafts": _money(plan.drafts, cur) if plan and cur and Decimal(plan.drafts) else None,
        "spent_vs_plan": (_money(plan.actual, cur) if plan and cur else None),
        "currency": cur, "total": _money(ov.total, cur) if cur else None,
        "rows": rows, "balances": balances, "transfers": transfers,
        "unconverted": len(ov.unconverted),
        "planned": (_money(plan.planned, cur)
                    if plan and cur and Decimal(plan.planned) else None),
        "by_category": by_category,
        "can_edit": can_edit,
        "has_roster": bool(expenses_svc._roster(session, trip.id)),
        # One person on the trip: nobody to owe or settle with, so balances
        # would only repeat the total (owner, 2026-10-12).
        "shared": len(expenses_svc._roster(session, trip.id)) > 1,
        "currency_choices": _currency_choices(session, trip, user) if can_edit else None,
    }}


def _currency_choices(session: Session, trip, user) -> dict:
    """The picker (YONDER-213): the suggested codes first (destination, yours,
    the trip's, already used), then every ISO-4217 currency by code."""
    names = dict(expenses_svc.currency_options())
    first = expenses_svc.suggested_currencies(session, trip, user)
    return {"suggested": [(c, names.get(c, c)) for c in first],
            "rest": [(c, n) for c, n in names.items() if c not in set(first)]}


def _section(request: Request, session: Session, trip, sub: str = "") -> HTMLResponse:
    user = scope.user_of(request)
    return templates.TemplateResponse(
        request, "_costs_section.html",
        {"request": request, "trip": trip, **costs_context(session, trip, user, sub)})


def _done(request: Request, session: Session, trip, sub: str) -> HTMLResponse:
    """After a save from a dialog: close it and refresh the Costs section in
    place (YONDER-220). Not a redirect — a redirect to /trips/{id}#costs from
    that same page only changes the fragment, so the browser didn't reload and
    the dialog stayed open, inviting a second, third… thirty-eighth Save."""
    resp = _section(request, session, trip, sub)
    resp.body = resp.body + b'<div id="modal-root" hx-swap-oob="innerHTML"></div>'
    resp.headers["content-length"] = str(len(resp.body))
    resp.headers["HX-Retarget"] = "#costs-section"
    resp.headers["HX-Reswap"] = "outerHTML"
    return resp


def _visible_expense(session: Session, request: Request, trip, expense_id: str):
    """The expense if it's on this trip and the caller is on it or entered it
    (D-183); None otherwise — the caller 404s, the same as a missing one."""
    e = session.get(TripExpense, expense_id)
    if (e is None or e.trip_id != trip.id
            or not expenses_svc.can_see_expense(session, scope.user_of(request), e)):
        return None
    return e


def _form_view(session: Session, trip, user, e=None, form=None, from_plan=None) -> dict:
    """The add/edit form's values: from a submitted form (after an error), the
    expense being edited, or the defaults (me paying, everyone, today's trip
    currency)."""
    me = getattr(user, "traveler_id", None)
    roster = _roster(session, trip.id)
    ids = [r["id"] for r in roster]
    if form is not None:
        return form
    if e is None:
        # The person's own currency first (YONDER-213), then the trip's.
        out = {"title": "", "amount": "",
               "currency": expenses_svc.default_currency(session, trip, user) or "",
               "spent_on": "", "payer_id": me if me in ids else (ids[0] if ids else None),
               "split": "equal", "traveler_ids": ids, "shares": {},
               "category": "other", "rate": "", "notes": "",
               "activity_id": "", "budget_item_id": ""}
        if from_plan is not None:
            # "Log as spent" (D-181): start from the planned cost, linked to it,
            # split between the people it was planned for.
            people = expenses_svc.budget_people(session, [from_plan.id]).get(from_plan.id) or ids
            out.update(title=from_plan.title, amount=from_plan.amount,
                       currency=from_plan.currency, category=from_plan.category,
                       budget_item_id=from_plan.id, activity_id=from_plan.activity_id or "",
                       traveler_ids=[t for t in people if t in ids] or ids)
        return out
    shares = {s.traveler_id: s.amount for s in expenses_svc.shares_of(session, e.id)}
    return {"title": e.title, "amount": e.amount, "currency": e.currency,
            "spent_on": e.spent_on or "", "payer_id": e.payer_id, "split": e.split,
            "traveler_ids": list(shares), "shares": shares, "category": e.category,
            "rate": e.rate if e.rate_source == "entered" else "", "notes": e.notes or "",
            "activity_id": e.activity_id or "", "budget_item_id": e.budget_item_id or ""}


def _timeline_choices(session: Session, trip) -> list[tuple[str, str]]:
    """(id, "Mar 2 · Dinner at Ramiro") for the trip's timeline items, in order —
    what an actual can be linked to (YONDER-221)."""
    from app.models import TripActivity
    from app.trips import types as activity_types
    from app.trips.routes import fmt_date_short
    out = []
    for a in (session.query(TripActivity).filter(TripActivity.trip_id == trip.id)
              .order_by(TripActivity.starts_at).all()):
        t = activity_types.get_type(a.type)
        title = (t.title(a.values or {}) or t.label).strip()
        day = fmt_date_short((a.starts_at or "")[:10]) if a.starts_at else ""
        out.append((a.id, f"{day} · {title}" if day else title))
    return out


def _plan_choices(session: Session, trip, user) -> list[tuple[str, str]]:
    """(id, "Lodging · Hotel Alfama Azul — 600.00 EUR") for the planned costs the
    caller can see (D-183) — what an actual can say it pays for (D-181)."""
    out = []
    for b in expenses_svc.budget_of(session, trip.id):
        if not expenses_svc.can_see_budget(session, user, b):
            continue
        label = b.title or _CATEGORY_LABELS.get(b.category, b.category)
        out.append((b.id, f"{_CATEGORY_LABELS.get(b.category, b.category)} · {label} — "
                          f"{_money(b.amount, b.currency)}"))
    return out


def _modal(request: Request, session: Session, trip, *, e=None, form=None,
           error: str | None = None, from_plan=None) -> HTMLResponse:
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
        "form": _form_view(session, trip, user, e, form, from_plan),
        "roster": _roster(session, trip.id),
        "categories": [(c, _CATEGORY_LABELS.get(c, c)) for c in expenses_svc.CATEGORIES],
        "place": place, "comments": comments,
        "timeline": _timeline_choices(session, trip),
        "plan_items": _plan_choices(session, trip, user),
        "trip_currency": trip.expense_currency,
        "currency_choices": _currency_choices(session, trip, user),
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
            "notes": str(f.get("notes") or "").strip(),
            "activity_id": str(f.get("activity_id") or "").strip(),
            "budget_item_id": str(f.get("budget_item_id") or "").strip()}


def _changes(form: dict) -> dict:
    """The form as expense fields. Equal split sends who it's between; exact
    sends each person's amount (blank = not on it)."""
    out = {k: form[k] for k in ("title", "amount", "currency", "payer_id", "split",
                                "category", "notes")}
    # "" unlinks on an edit; on a new expense it's simply no link.
    out["activity_id"] = form.get("activity_id", "")
    out["budget_item_id"] = form.get("budget_item_id", "")
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
def new_expense(trip_id: str, request: Request, plan: str = "",
                session: Session = Depends(get_session)):
    """The add-expense dialog; `?plan=<id>` is "Log as spent" for a planned cost."""
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    b = _visible_budget(session, request, trip, plan) if plan else None
    if plan and b is None:
        return HTMLResponse("", status_code=404)
    return _modal(request, session, trip, from_plan=b)


@router.post("/{trip_id}/costs", response_class=HTMLResponse)
async def add_expense(trip_id: str, request: Request, session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)        # 403 for view-only
    if trip is None:
        return HTMLResponse("", status_code=404)
    form = await _read_form(request)
    try:
        changes = _changes(form)
        changes["activity_id"] = changes["activity_id"] or None
        changes["budget_item_id"] = changes["budget_item_id"] or None
        data = expenses_svc.ExpenseInput(**changes, source="web")
        expenses_svc.create(session, scope.user_of(request), trip, data)
    except ValueError as err:
        session.rollback()
        return _modal(request, session, trip, form=form, error=str(err))
    session.commit()
    return _done(request, session, trip, "actual")


# ------------------------------------------------------------------ planned costs (YONDER-221)
# The plan (D-178–D-184): one amount for the travellers it's for, never owed or
# settled. A person's own entries are kept; agents' arrive as drafts to keep.

def _visible_budget(session: Session, request: Request, trip, item_id: str):
    """The planned cost if it's on this trip and the caller is tagged on it or
    entered it (D-183); None otherwise — a 404, like a missing one."""
    from app.models import TripBudgetItem
    b = session.get(TripBudgetItem, item_id) if item_id else None
    if (b is None or b.trip_id != trip.id
            or not expenses_svc.can_see_budget(session, scope.user_of(request), b)):
        return None
    return b


def _budget_form(session: Session, trip, user, b=None, form=None) -> dict:
    if form is not None:
        return form
    me = getattr(user, "traveler_id", None)
    ids = [r["id"] for r in _roster(session, trip.id)]
    if b is None:
        return {"title": "", "amount": "", "category": "other", "start_date": "",
                "end_date": "", "notes": "", "activity_id": "",
                "currency": expenses_svc.default_currency(session, trip, user) or "",
                # Your own entry is for you by default (D-184).
                "traveler_ids": [me] if me in ids else ids[:1]}
    return {"title": b.title, "amount": b.amount, "currency": b.currency,
            "category": b.category, "start_date": b.start_date or "",
            "end_date": b.end_date or "", "notes": b.notes or "",
            "activity_id": b.activity_id or "",
            "traveler_ids": expenses_svc.budget_people(session, [b.id]).get(b.id, [])}


def _budget_modal(request: Request, session: Session, trip, *, b=None, form=None,
                  error: str | None = None) -> HTMLResponse:
    user = scope.user_of(request)
    return templates.TemplateResponse(request, "_budget_modal.html", {
        "request": request, "trip": trip, "item": b, "error": error,
        "form": _budget_form(session, trip, user, b, form),
        "roster": _roster(session, trip.id),
        "categories": [(c, _CATEGORY_LABELS.get(c, c)) for c in expenses_svc.CATEGORIES],
        "currency_choices": _currency_choices(session, trip, user),
        "timeline": _timeline_choices(session, trip),
        "can_edit": scope.can_edit_trip(session, user, trip),
        "draft": b is not None and b.status == expenses_svc.DRAFT,
    })


async def _read_budget_form(request: Request) -> dict:
    f = await request.form()
    return {"title": str(f.get("title") or "").strip(),
            "amount": str(f.get("amount") or "").strip(),
            "currency": str(f.get("currency") or "").strip().upper(),
            "category": str(f.get("category") or "other"),
            "start_date": str(f.get("start_date") or "").strip(),
            "end_date": str(f.get("end_date") or "").strip(),
            "notes": str(f.get("notes") or "").strip(),
            "activity_id": str(f.get("activity_id") or "").strip(),
            "traveler_ids": [int(v) for v in f.getlist("traveler_ids")
                             if str(v).strip().isdigit()]}


@router.get("/{trip_id}/costs/plan/new", response_class=HTMLResponse)
def new_budget(trip_id: str, request: Request, session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    if trip is None:
        return HTMLResponse("", status_code=404)
    return _budget_modal(request, session, trip)


@router.post("/{trip_id}/costs/plan", response_class=HTMLResponse)
async def add_budget(trip_id: str, request: Request, session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)        # 403 for view-only
    if trip is None:
        return HTMLResponse("", status_code=404)
    form = await _read_budget_form(request)
    try:
        if not form["traveler_ids"]:
            raise ValueError("pick who it's for")
        data = expenses_svc.BudgetInput(
            amount=form["amount"], currency=form["currency"], title=form["title"],
            category=form["category"], traveler_ids=form["traveler_ids"],
            start_date=form["start_date"] or None, end_date=form["end_date"] or None,
            activity_id=form["activity_id"] or None, notes=form["notes"] or None,
            source="web")
        expenses_svc.create_budget(session, scope.user_of(request), trip, data)
    except ValueError as err:
        session.rollback()
        return _budget_modal(request, session, trip, form=form, error=str(err))
    session.commit()
    return _done(request, session, trip, "plan")


@router.get("/{trip_id}/costs/plan/{item_id}/edit", response_class=HTMLResponse)
def edit_budget(trip_id: str, item_id: str, request: Request,
                session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    b = _visible_budget(session, request, trip, item_id) if trip else None
    if b is None:
        return HTMLResponse("", status_code=404)
    return _budget_modal(request, session, trip, b=b)


@router.post("/{trip_id}/costs/plan/{item_id}", response_class=HTMLResponse)
async def update_budget(trip_id: str, item_id: str, request: Request,
                        session: Session = Depends(get_session)):
    """Edit a planned cost; editing an agent's draft keeps it (D-180)."""
    trip = scope.trip_or_none(session, request, trip_id)
    b = _visible_budget(session, request, trip, item_id) if trip else None
    if b is None:
        return HTMLResponse("", status_code=404)
    form = await _read_budget_form(request)
    try:
        if not form["traveler_ids"]:
            raise ValueError("pick who it's for")
        expenses_svc.update_budget(session, scope.user_of(request), trip, b, {
            "title": form["title"], "amount": form["amount"], "currency": form["currency"],
            "category": form["category"], "traveler_ids": form["traveler_ids"],
            "start_date": form["start_date"] or None, "end_date": form["end_date"] or None,
            "notes": form["notes"], "activity_id": form["activity_id"]})
    except ValueError as err:
        session.rollback()
        return _budget_modal(request, session, trip, b=b, form=form, error=str(err))
    session.commit()
    return _done(request, session, trip, "plan")


@router.post("/{trip_id}/costs/plan/{item_id}/keep", response_class=HTMLResponse)
def keep_budget(trip_id: str, item_id: str, request: Request,
                session: Session = Depends(get_session)):
    """Keep an agent's draft as part of the plan (D-180)."""
    trip = scope.trip_or_none(session, request, trip_id)
    b = _visible_budget(session, request, trip, item_id) if trip else None
    if b is None:
        return HTMLResponse("", status_code=404)
    expenses_svc.update_budget(session, scope.user_of(request), trip, b,
                               {"status": expenses_svc.KEPT})
    session.commit()
    return _section(request, session, trip, "plan")


@router.post("/{trip_id}/costs/plan/{item_id}/delete", response_class=HTMLResponse)
def delete_budget(trip_id: str, item_id: str, request: Request,
                  session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    b = _visible_budget(session, request, trip, item_id) if trip else None
    if b is None:
        return HTMLResponse("", status_code=404)
    expenses_svc.delete_budget(session, b)
    session.commit()
    return _done(request, session, trip, "plan")


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
    return _done(request, session, trip, "actual")


@router.post("/{trip_id}/costs/{expense_id}/delete", response_class=HTMLResponse)
def delete_expense(trip_id: str, expense_id: str, request: Request,
                   session: Session = Depends(get_session)):
    trip = scope.trip_or_none(session, request, trip_id)
    e = _visible_expense(session, request, trip, expense_id) if trip else None
    if e is None:
        return HTMLResponse("", status_code=404)
    expenses_svc.delete(session, e)
    session.commit()
    return _done(request, session, trip, "actual")


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
