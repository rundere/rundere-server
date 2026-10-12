# SPEC-10 — Trip expenses and splitting costs (YONDER-172)

**Status: BUILDING (2026-10-11).** Decisions D-173–D-185 and D-188 (D-174 and D-179 revised 2026-10-11). Builds option B of
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
- **TripBudgetItem** (`trip_budget_items`): a planned cost — one estimated amount
  in its own currency (never multiplied), `draft` (an agent's suggestion) or
  `kept`, optionally for part of the trip (`start_date`–`end_date`), with
  optional city, timeline item and sources.
- **TripBudgetTraveler** (`trip_budget_travelers`): who a planned cost is for.
- **TripExpenseComment** (`trip_expense_comments`): the discussion on an actual.
- **TripExpense.budget_item_id**: the planned item an actual paid for.
- **Trip.expense_currency**: the currency balances and the plan are shown in.

## Decisions

- **D-173 — Expenses are built in and deliberately small.** Expenses, equal or
  exact splits, net balances, suggested settle-up payments and recorded
  settlements. Nothing else: no payment links, reminders, recurring expenses,
  multi-trip groups or bank/CSV/receipt imports (D-155). SplitPro stays the tool
  for anything heavier.
- **D-174 — The trip gates costs (revised 2026-10-11).** Reading costs needs
  view access to the trip and writing needs edit access — its owner and the
  travellers on its roster (`scope.can_edit_trip`); a view-only share can't
  write (D-122). Inside that gate, who sees which cost is D-183. Every write
  records `owner_id`.
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
- **D-179 — A planned cost is one amount (revised 2026-10-11).** Each planned
  cost is a single estimated amount for the travellers tagged on it (D-184) and
  is never multiplied — not by people, not by days. A room for two is one cost
  (the room's price), tagged with both; three days of lunches for one person is
  the three-day total. Its dates, when given, say when it applies; they don't
  change the amount. In the per-traveller view a cost is split evenly between
  the people tagged on it.
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

- **D-183 — Only the people on a cost see it.** An actual is visible to its
  payer and sharers, a planned cost to the travellers tagged on it, and either
  to whoever entered it. Not to the trip's owner, an admin, a view-only share or
  another traveller who isn't on it — like an itinerary item someone isn't
  tagged on. Balances, suggested payments and the plan are worked out from the
  costs each person can see: everything someone is on is visible to them, so
  their own balance is always complete. A cost they can't see 404s.
- **D-184 — Planned costs are for named travellers.** Every planned cost is
  tagged with the traveller(s) it's for; a person's own entry defaults to
  themselves, and an agent must name them (`traveler_ids` is required from
  research). Nothing is extrapolated to people a cost isn't for. The plan shows
  each traveller's budget beside their share of actual spending
  (`by_traveler`), each cost split evenly between the people tagged on it.
- **D-185 — The people on an actual can discuss it.** Comments sit on an actual
  expense and are visible exactly where it is (D-183); anyone who can see the
  expense can comment, and people delete only their own comments. Deleting the
  expense deletes its thread.
- **D-188 — Costs opens on Plan before the trip and on Actual from its first
  day (YONDER-223; owner, 2026-10-12).** The sub-tabs read Plan | Actual, the
  order the work happens in. With no tab asked for, a trip that hasn't started
  opens on Plan; from its start date on, including settling up afterwards, it
  opens on Actual. "Today" is the viewer's own date (D-187). A trip with no
  start date opens on Plan unless something has been spent. The iOS Costs view
  follows the same rule.

## Surfaces (first build)

- `/api/v1/trips/{id}/expenses` (actuals, balances and the `plan`; create),
  `…/expenses/{eid}` (edit, delete), `…/settlements` (record a payment),
  `…/expenses/currency`, `…/expenses/{eid}/comments` (comment; delete your own);
  `…/budget` (plan a cost), `…/budget/batch` (research drafts), `…/budget/{bid}`
  (edit/keep, delete).
- MCP: `list_expenses`, `add_expense`, `record_settlement`, `delete_expense`,
  `comment_on_expense`, `save_budget`, `delete_budget_item`; the
  `research_trip_budget` prompt.
- iOS: a Costs segment with Plan and Actual (list, balances, add/edit, settle up,
  keep drafts, log a planned item as spent) and the Log Expense shortcut.
- Web UI (YONDER-212): a Costs tab on the trip page (`app/trips/costs.py`) —
  balances, suggested payments with "Mark paid", the expense list, an add/edit
  form (equal or exact split), comments, the balance currency, and planned vs
  spent by category. Same rules and gates as the API (D-174, D-183).
- Location (YONDER-212): an actual records where it was logged —
  `place_lat`/`place_lon`/`place_label`/`place_accuracy_m`, named like the
  journal's. Only the iOS app captures it; the API takes it as optional and the
  web shows it (label or an OpenStreetMap link) without ever capturing or
  editing it. It's visible exactly where the expense is (D-183).

- Currency picker (YONDER-213): `ExpensesOut.suggested_currencies` is the one
  ordering for web, MCP and the app — the destination currencies from the
  caller's Discover findings, then their own base currency, the trip's balance
  currency, and those already used on this trip's expenses (nothing from other
  trips) — with every other current ISO-4217 code
  after. `default_currency` (the person's own, else the trip's, else the
  destination's) is what a new expense starts in. `/api/v1/auth/me` carries the
  person's `base_currency` (and `timezone`, D-187). Pickers show only the
  suggested currencies plus "Other currency…", which opens the full list for the
  rare unplanned one (a layover, a trip Discover hasn't run on) — owner,
  2026-10-12. The iOS screens move over in the app repo.

## Cleanup

Deleting a trip deletes its expenses, shares, comments, planned items and their
travellers (trip-only data). Deleting a timeline activity clears `activity_id` on expenses and planned
items that pointed at it.
