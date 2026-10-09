# SPEC-10 — Trip expenses and splitting costs (YONDER-172)

**Status: BUILDING (2026-10-09).** Decisions D-173–D-182. Builds option B of
YONDER-172 (built in, not a SplitPro integration) inside the D-155 posture.

## Intent

A light way for travellers to record what a trip costs and who owes whom —
**not** a SplitPro/Splitwise replacement. A traveller logs an expense against a
trip, picks who paid and who it's for, and the trip shows net balances and the
fewest payments that settle them. Someone can record "Ana paid Ben back"; that's
as far as settling goes. No payment links, reminders, groups outside a trip,
recurring bills, imports or receipt OCR (D-155).

Costs come in **two parts** (D-178): the **plan** — what the trip should cost,
researched ahead by an agent with the trip-budget kit or entered by hand — and
the **actuals** — what was really spent, logged on the go. Only actuals settle
people up and make the trip's actual cost.

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
- **TripBudgetItem** (`trip_budget_items`): a planned cost — an estimate in its
  own currency on a `basis` (trip / person / day / person_day), `draft` (an
  agent's suggestion) or `kept`, with optional city, timeline item and sources.
- **TripExpense.budget_item_id**: the planned item an actual paid for.
- **Trip.expense_currency**: the currency balances and the plan are shown in.

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

- **D-178 — Planned and actual are separate.** The plan is a budget: it is never
  split, never owed and never settled. Balances, suggested payments and the
  trip's actual cost come from actual expenses only.
- **D-179 — A planned cost is an estimate on a basis.** Whole trip, per person,
  per day or per person per day, multiplied by the roster size and the trip's
  days when it's shown, so adding a traveller or a day updates the plan. Without
  dates, a per-day estimate counts once and the plan says the days are unknown.
- **D-180 — Budget research is a research kit and lands as drafts.** The
  trip-budget kit (`research_trip_budget`) prices the trip from real sources and
  saves with `save_budget`. Every agent item is a draft; a re-run replaces the
  agent's own drafts in the categories it re-researches; what the person kept
  (or edited, which keeps it) is never overwritten — the same contract as
  Discover's locked findings.
- **D-181 — Actuals can name the plan they fulfil.** Logging an actual against a
  planned item links them, so the plan shows spent-so-far per item and planned vs
  actual per category. Deleting either side keeps the other, unlinked.
- **D-182 — Log an expense on the go.** An App Shortcut (Siri, Shortcuts, Action
  button, Home Screen quick action, `rundere://expense/new`) opens a quick-entry
  sheet on the trip under way today (else the next one), with the person as payer
  and an even split, like the journal's quick entry. Amount and title can come
  from the shortcut. Entering stays by hand (D-155).

## Surfaces (first build)

- `/api/v1/trips/{id}/expenses` (actuals, balances and the `plan`; create),
  `…/expenses/{eid}` (edit, delete), `…/settlements` (record a payment),
  `…/expenses/currency`; `…/budget` (plan a cost), `…/budget/batch` (research
  drafts), `…/budget/{bid}` (edit/keep, delete).
- MCP: `list_expenses`, `add_expense`, `record_settlement`, `delete_expense`,
  `save_budget`, `delete_budget_item`; the `research_trip_budget` prompt.
- iOS: a Costs segment with Plan and Actual (list, balances, add/edit, settle up,
  keep drafts, log a planned item as spent) and the Log Expense shortcut.
- Web UI: follow-up item.

## Cleanup

Deleting a trip deletes its expenses, shares and planned items (trip-only
data). Deleting a timeline activity clears `activity_id` on expenses and planned
items that pointed at it.
