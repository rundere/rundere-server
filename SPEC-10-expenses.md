# SPEC-10 — Trip expenses and splitting costs (YONDER-172)

**Status: BUILDING (2026-10-09).** Decisions D-173–D-177. Builds option B of
YONDER-172 (built in, not a SplitPro integration) inside the D-155 posture.

## Intent

A light way for travellers to record what a trip costs and who owes whom —
**not** a SplitPro/Splitwise replacement. A traveller logs an expense against a
trip, picks who paid and who it's for, and the trip shows net balances and the
fewest payments that settle them. Someone can record "Ana paid Ben back"; that's
as far as settling goes. No payment links, reminders, groups outside a trip,
recurring bills, imports or receipt OCR (D-155).

## Model

- **TripExpense** (`trip_expenses`): one row per expense or settlement on a trip.
  `kind` is `expense` or `settlement`. Amount is a decimal string in the
  expense's own `currency`; `rate` converts it into the trip's expense currency.
  `payer_id` and every share are traveller ids from the trip's roster
  (TravelerIdentity, so off-platform guests can pay and owe). `activity_id`
  optionally links a timeline item (soft link). `owner_id` records who entered it.
- **TripExpenseShare** (`trip_expense_shares`): who an expense is for and their
  part of it, in the expense's currency. Always stored explicitly, even for an
  equal split, so later roster changes never rewrite history.
- **Trip.expense_currency**: the currency balances are shown in.

## Decisions

- **D-173 — Expenses are built in and deliberately small.** Expenses, equal or
  exact splits, net balances, suggested settle-up payments and recorded
  settlements. Nothing else: no payment links, reminders, recurring expenses,
  multi-trip groups or bank/CSV/receipt imports (D-155). SplitPro stays the tool
  for anything heavier.
- **D-174 — Expenses follow the trip's sharing, not the journal's.** Splitting
  costs is about other people, so anyone who can view the trip sees its expenses
  and balances (`scope.can_view_trip`), and anyone who can edit the trip — its
  owner and the travellers on its roster — can add, change or delete them
  (`scope.can_edit_trip`); a view-only share can't (D-122). Every write records
  `owner_id`.
- **D-175 — Payers and sharers are the trip's travellers.** Ids must be on the
  roster when the expense is saved. An equal split with no one chosen means
  everyone on the roster at that moment, divided to the currency's minor unit
  with the leftover cents going to the first travellers in id order, so the
  shares always add up exactly. Exact splits must add up to the amount. Removing
  someone from the roster later keeps their past expenses and balance.
- **D-176 — Each expense keeps its own currency and the rate it was converted
  at.** Balances are in the trip's expense currency (set explicitly, else the
  first expense's currency). The rate is entered by the person, or filled from
  their stored Discover currency rates for the trip when they leave it blank; no
  live call is made (D-146). An expense with no known rate is listed as
  unconverted and left out of the balances until a rate is given. Changing the
  trip currency re-fills rates that came from Discover and clears entered ones.
- **D-177 — Settling up is a recorded payment.** "Ana paid Ben 40" is a
  settlement row: Ana is the payer, Ben the only share. Balances net expenses and
  settlements together. Suggested payments are the greedy largest-debtor →
  largest-creditor matching, so at most n−1 payments for n people.

## Surfaces (first build)

- `/api/v1/trips/{id}/expenses` (list with balances; create), `…/expenses/{eid}`
  (edit, delete), `…/settlements` (record a payment), `…/expenses/currency`.
- MCP: `list_expenses`, `add_expense`, `record_settlement`, `delete_expense`.
- iOS: an Expenses section on a trip (list, balances, add/edit, settle up).
- Web UI: follow-up item.

## Cleanup

Deleting a trip deletes its expenses and shares (trip-only data). Deleting a
timeline activity clears `activity_id` on expenses that pointed at it.
