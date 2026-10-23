"""Remote MCP server inside Yonder at `/mcp` (YONDER-108, SPEC-4.2 §3.7).

MCP Streamable HTTP (stateless, JSON responses) for any MCP client — the
claude.ai connector and Claude apps, Claude Code, or other agents
(Perplexity/Perplexica, …). Every request needs a bearer token — an OAuth
access token (YONDER-107) or a personal API token — and a missing/invalid one
gets a 401 with `WWW-Authenticate: Bearer resource_metadata="…"`, which is how
claude.ai discovers Yonder's OAuth server. Cookies and proxy identity headers
are never accepted here.

Each tool runs AS THE SIGNED-IN USER and calls the same functions as
`/api/v1` in-process (app/api_v1.py → app/scope.py), so per-user visibility
and the admin sharing rules apply exactly as they do in the app.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from app import api_v1, authn, oauth, places as places_svc
from app.config import settings
from app.db import SessionLocal
from app.models import TripActivity, User

_caller_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "yonder_mcp_caller", default=None)
# Which agent is calling: the name of the credential it connected with (the
# OAuth app's registered name, or the personal API token's name).
_caller_agent: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "yonder_mcp_agent", default=None)

INSTRUCTIONS = """Run·dere (formerly Yonder) is the user's personal travel planner.
Tools act as the signed-in Run·dere user and see only what they can see in the app.
Start with list_trips, then get_trip for the full picture (timeline, travellers,
places, the user's Discover findings, their discovery profile and packing).
Each trip has `my_role`: owner, traveller or admin can change it; viewer means it
was shared with the user to view only, so don't try to change it (writes are
refused) — offer to research or summarise instead.
Timeline items take a `type` from list_activity_types and a `values` object with
that type's field keys. Research with the user's preferences: get_trip's
`discovery_profile` (also get_discovery_profile) gives dietary restrictions,
cuisines to avoid, allergies, budget, interests, their own notes and what they
hid as "not my thing", and how to apply them; if it's blank, ask them first.
Use save_research to keep research on the user's Discover tab — in your own
"research" section or updating a topic such as visa; it lands as a draft they
review, and locked findings are theirs to keep. For Food & Drink or Events &
Activities use save_city_guide (a city summary plus rows — for food, say what to
get); for a city's local phrases use save_phrases; for Safety & Transport use
save_safety. Items in get_trip's
`discovery_items` can go on the timeline with add_item_to_timeline. The
research_* prompts are step-by-step research kits for these. Dates are
ISO (YYYY-MM-DD, or YYYY-MM-DDTHH:MM local time for timeline items).
Saved places are Maps places the user kept before choosing a trip: capture_url
saves one from a link, save_places pushes a whole list (try dry_run first; it is
safe to repeat), list_saved_places shows them and file_saved_place puts one on a
trip, undated if the user hasn't decided when.
Costs come in two parts. Planned: the budget — research_trip_budget is the kit,
save_budget stores its estimates as drafts the user keeps. Actual: add_expense
records what was really spent (by hand, split between the trip's travellers by
id; pass budget_item_id when it fulfils a planned item), record_settlement records
a payment back; comment_on_expense discusses one. Only actuals count towards
balances and settle-up. Every cost is for the travellers tagged on it and only
they (and whoever entered it) see it — tag planned costs with traveler_ids and
never spread them to others. list_expenses shows both."""

mcp = MCPServer("Run·dere", instructions=INSTRUCTIONS, version=settings.app_version)


# --------------------------------------------------------------------------- plumbing
def _run(fn, /, **kwargs) -> Any:
    """Call an /api/v1 function as the MCP caller in a fresh DB session and
    return plain JSON. HTTP errors become tool errors the agent can read."""
    uid = _caller_id.get()
    if not uid:
        raise ToolError("not signed in")
    with SessionLocal() as session:
        user = session.get(User, uid)
        if user is None or not user.is_active:
            raise ToolError("this Run·dere account is not active")
        try:
            return jsonable_encoder(fn(session=session, user=user, **kwargs))
        except HTTPException as e:
            raise ToolError(f"{e.detail} (HTTP {e.status_code})") from None
        except ValueError as e:
            raise ToolError(str(e)) from None


TripId = Annotated[str, Field(description="Trip id (from list_trips)")]
Date = Annotated[str | None, Field(description="ISO date, YYYY-MM-DD")]
TravelerIds = Annotated[list[int] | None, Field(
    description="Who this is for: traveller ids from get_trip's `travelers`. "
                "[] = everyone on the trip. Omit to leave unchanged.")]


# --------------------------------------------------------------------------- trips
@mcp.tool()
def list_trips() -> dict[str, Any]:
    """List the trips the user can see: id, name, start/end dates, activity
    count. Newest first. Use an id with get_trip."""
    return {"trips": _run(api_v1.api_trips)}


@mcp.tool()
def get_trip(trip_id: TripId) -> dict[str, Any]:
    """Full snapshot of one trip: the trip with its notes, the timeline (each
    activity with its type, values, times and `traveler_ids` — empty means
    everyone), the travellers,
    places with lat/lon, the user's OWN Discover findings (weather, currency,
    visa, food, saved research…), the places/events/activities inside the Food
    and Events findings (`discovery_items`, each tied to its finding by
    `finding_id`), the packing list, and linked captures. `trip.my_role` says
    what the user may do here (viewer = read-only); each traveller has
    `has_account` (true = has a Run·dere login, false = a guest on the roster).
    A finding with an `attribution` (Open-Meteo weather) must be credited with
    that text and link wherever you show its data."""
    return _run(api_v1.api_trip_snapshot, trip_id=trip_id)


@mcp.tool()
def list_discovery_places(trip_id: TripId) -> dict[str, Any]:
    """The places Discovery researches on this trip for the user (Refine Discovery):
    each with its `key`, where it came from, `is_home` (never researched),
    `home_country` (local topics skipped) and `excluded` (the user chose to skip
    it). Check before researching; skip what the user doesn't want with
    set_discovery_place."""
    return _run(api_v1.api_discovery_places, trip_id=trip_id)


@mcp.tool()
def set_discovery_place(trip_id: TripId,
                        place_key: Annotated[str, Field(description="A place `key` from list_discovery_places, e.g. \"PT:Lisbon\"")],
                        excluded: Annotated[bool, Field(description="true = skip this place; false = research it again")],
                        ) -> dict[str, Any]:
    """Exclude a place from (or include it in) the user's Discovery on this trip.
    Idempotent; excluding drops that place's draft findings. Needs edit access."""
    return _run(api_v1.api_set_discovery_place, trip_id=trip_id, place_key=place_key,
                body=api_v1.DiscoveryPlaceIn(excluded=excluded))


@mcp.tool()
def update_trip(trip_id: TripId,
                name: Annotated[str | None, Field(description="New trip name")] = None,
                start_date: Date = None, end_date: Date = None,
                notes: Annotated[str | None, Field(description="Trip notes (replaces them)")] = None,
                ) -> dict[str, Any]:
    """Rename a trip or change its dates/notes. Only the fields you pass change.
    Needs edit access (owner or traveller on it)."""
    body = api_v1.TripPatch(name=name, start_date=start_date, end_date=end_date, notes=notes)
    return _run(api_v1.api_update_trip, trip_id=trip_id, body=body)


@mcp.tool()
def dashboard() -> dict[str, Any]:
    """The home overview: next/current trip, the next thing on the timeline (a
    departure, check-in or drop-off) and what's under way now, review-queue counts,
    capture activity."""
    return _run(api_v1.api_dashboard)


# --------------------------------------------------------------------------- timeline
@mcp.tool()
def list_activity_types() -> dict[str, Any]:
    """Timeline activity types (flight, train, drive, taxi, carRental, lodging, tour,
    event, dining, cruise, note) and the `values` field keys each takes (e.g.
    `when`, `endWhen`, `name`, `address`)."""
    return {"types": _run(lambda session, user: api_v1.api_activity_types(user=user))}


@mcp.tool()
def add_activity(trip_id: TripId,
                 type: Annotated[str, Field(description="Activity type key from list_activity_types")],
                 values: Annotated[dict[str, Any], Field(
                     description="The type's fields (see list_activity_types), e.g. for dining "
                                 "{\"name\": \"Noma\", \"when\": \"2026-09-06T19:00\", "
                                 "\"address\": \"…\"}")],
                 starts_at: Annotated[str | None, Field(
                     description="Sort time (ISO); defaults to values.when")] = None,
                 traveler_ids: TravelerIds = None) -> dict[str, Any]:
    """Add an item to a trip's timeline. Needs edit access to the trip."""
    body = api_v1.ActivityIn(type=type, values=values, starts_at=starts_at,
                             traveler_ids=traveler_ids)
    return _run(api_v1.api_add_activity, trip_id=trip_id, body=body)


@mcp.tool()
def update_activity(trip_id: TripId,
                    activity_id: Annotated[str, Field(description="Activity id from get_trip")],
                    type: Annotated[str | None, Field(description="New activity type")] = None,
                    values: Annotated[dict[str, Any] | None, Field(
                        description="Fields to change; MERGED into the current values. "
                                    "Set a key to null to clear it.")] = None,
                    starts_at: Annotated[str | None, Field(description="New sort time (ISO)")] = None,
                    traveler_ids: TravelerIds = None) -> dict[str, Any]:
    """Edit a timeline item. Only what you pass changes; `values` is merged."""
    def call(session, user):
        merged = None
        if values is not None:
            a = session.get(TripActivity, activity_id)
            current = dict(a.values or {}) if a is not None and a.trip_id == trip_id else {}
            merged = {k: v for k, v in {**current, **values}.items() if v is not None}
        body = api_v1.ActivityPatch(type=type, values=merged, starts_at=starts_at,
                                    traveler_ids=traveler_ids)
        return api_v1.api_update_activity(trip_id=trip_id, activity_id=activity_id,
                                          body=body, user=user, session=session)
    return _run(call)


@mcp.tool()
def delete_activity(trip_id: TripId,
                    activity_id: Annotated[str, Field(description="Activity id from get_trip")],
                    ) -> dict[str, Any]:
    """Remove an item from a trip's timeline. Needs edit access."""
    _run(api_v1.api_delete_activity, trip_id=trip_id, activity_id=activity_id)
    return {"ok": True, "deleted": activity_id}


# --------------------------------------------------------------------------- packing
@mcp.tool()
def generate_packing(trip_id: TripId) -> dict[str, Any]:
    """Generate (or regenerate) the user's OWN packing list for a trip from its
    timeline, weather and their packing profile. Returns the list."""
    return _run(api_v1.api_generate_packing, trip_id=trip_id)


@mcp.tool()
def set_packing_status(trip_id: TripId,
                       item_id: Annotated[str, Field(description="Packing item id")],
                       status: Literal["packed", "draft", "removed"]) -> dict[str, Any]:
    """Check a packing item off (packed), back on (draft), or drop it (removed).
    Only the user's own packing lines."""
    return _run(api_v1.api_update_packing_item, trip_id=trip_id, item_id=item_id,
                body=api_v1.PackingItemPatch(status=status))


@mcp.tool()
def add_packing_item(trip_id: TripId,
                     label: Annotated[str, Field(description="What to pack, e.g. \"Bear spray\"")],
                     category: Annotated[str, Field(
                         description="Category, e.g. clothing, toiletries, documents, "
                                     "electronics, activity")] = "activity",
                     quantity: Annotated[int, Field(ge=1, le=99)] = 1) -> dict[str, Any]:
    """Add a custom line to the user's own packing list for a trip."""
    return _run(api_v1.api_add_packing_item, trip_id=trip_id,
                body=api_v1.PackingItemIn(label=label, category=category, quantity=quantity))


# --------------------------------------------------------------------------- expenses
Amount = Annotated[str, Field(description="Decimal amount, e.g. \"42.50\"")]


@mcp.tool()
def list_expenses(trip_id: TripId) -> dict[str, Any]:
    """A trip's costs, in two parts. ACTUAL: the expenses and settle-up payments,
    each traveller's balance (net: + is owed, − owes) in the trip's expense
    currency, and the suggested payments that settle everyone up — only actuals
    count towards these. PLANNED (`plan`): the budget — planned items (one amount
    each for the travellers tagged on it), drafts from research not yet kept, and planned vs
    actual by category. Anything with no exchange rate is in `unconverted`."""
    return _run(api_v1.api_expenses, trip_id=trip_id)


@mcp.tool()
def add_expense(trip_id: TripId, amount: Amount,
                currency: Annotated[str, Field(description="ISO-4217 code, e.g. EUR")],
                payer_id: Annotated[int, Field(description="Traveller id (get_trip's "
                                                           "`travelers`) who paid")],
                title: Annotated[str, Field(description="What it was, e.g. \"Dinner at Noma\"")] = "",
                spent_on: Date = None,
                traveler_ids: Annotated[list[int] | None, Field(
                    description="Split equally between these travellers; omit or [] = "
                                "everyone on the trip")] = None,
                shares: Annotated[dict[int, str] | None, Field(
                    description="Exact split instead: {traveler_id: amount}, adding up to "
                                "`amount`")] = None,
                category: Literal["food", "lodging", "transport", "activities", "shopping",
                                  "fees", "other"] = "other",
                rate: Annotated[str | None, Field(
                    description="Trip-currency units per 1 unit of `currency`; omit to use "
                                "the trip's rate for that day")] = None,
                activity_id: Annotated[str | None, Field(
                    description="Timeline item it belongs to (get_trip)")] = None,
                budget_item_id: Annotated[str | None, Field(
                    description="The planned item this actually paid for "
                                "(list_expenses' plan)")] = None,
                notes: Annotated[str | None, Field(description="Notes")] = None,
                ) -> dict[str, Any]:
    """Record an expense on a trip, split equally or exactly between its
    travellers. Entered by hand only — never import bank, card or receipt data
    (D-155). Needs edit access to the trip."""
    body = api_v1.ExpenseIn(amount=amount, currency=currency, payer_id=payer_id, title=title,
                            spent_on=spent_on, split="exact" if shares else "equal",
                            traveler_ids=traveler_ids, shares=shares, category=category,
                            rate=rate, activity_id=activity_id,
                            budget_item_id=budget_item_id, notes=notes, source="mcp")
    return _run(api_v1.api_add_expense, trip_id=trip_id, body=body)


@mcp.tool()
def record_settlement(trip_id: TripId,
                      from_id: Annotated[int, Field(description="Traveller id who paid")],
                      to_id: Annotated[int, Field(description="Traveller id who was paid")],
                      amount: Amount,
                      currency: Annotated[str | None, Field(
                          description="Defaults to the trip's expense currency")] = None,
                      spent_on: Date = None) -> dict[str, Any]:
    """Record that one traveller paid another back (a settle-up payment)."""
    body = api_v1.SettlementIn(from_id=from_id, to_id=to_id, amount=amount,
                               currency=currency, spent_on=spent_on, source="mcp")
    return _run(api_v1.api_record_settlement, trip_id=trip_id, body=body)


class PlannedCost(BaseModel):
    """One planned cost from budget research."""
    title: str = Field(description="What it is, e.g. \"Tokyo hotel\" or \"Daily lunches\"")
    category: Literal["food", "lodging", "transport", "activities", "shopping", "fees",
                      "other"] = "other"
    amount: str = Field(description="The cost for the tagged travellers together, as a "
                                    "decimal string — ONE amount, never multiplied: a room "
                                    "for two is one cost; three days of lunches is the "
                                    "three-day total")
    currency: str = Field(description="ISO-4217 code it's priced in, e.g. JPY")
    traveler_ids: list[int] = Field(
        min_length=1, description="REQUIRED: who this cost is for — traveller ids from "
                                  "get_trip's `travelers`. It counts for them only; "
                                  "never add people it isn't for")
    start_date: str | None = Field(None, description="When it applies from (YYYY-MM-DD), "
                                                     "if not the whole trip")
    end_date: str | None = Field(None, description="When it applies to, if not the whole trip")
    place_key: str | None = Field(None, description="City key from get_trip's places, if it's "
                                                    "for one city")
    activity_id: str | None = Field(None, description="Timeline item it's for (get_trip)")
    notes: str | None = Field(None, description="Why this number: the range, what it assumes")
    sources: list[dict] | None = Field(None, description="[{title, url}] you priced it from")


@mcp.tool()
def save_budget(trip_id: TripId,
                items: Annotated[list[PlannedCost], Field(description="The planned costs")],
                replace: Annotated[bool, Field(
                    description="Drop your earlier drafts in these categories first (a "
                                "re-run). Kept items are never touched.")] = True,
                ) -> dict[str, Any]:
    """Save budget research to a trip's PLANNED costs. Each item is for the
    travellers you tag on it (`traveler_ids`) and is ONE amount for them — it's
    never multiplied by people or days, and never extrapolated to people it
    isn't for. Every item lands as a draft the
    person reviews and keeps; anything already kept (same category, title and
    travellers) is skipped. Only the tagged travellers and the user see it. Planned costs are only a budget: they never count towards
    balances or settle-up. Needs edit access. Use the research_trip_budget prompt."""
    body = api_v1.BudgetBatchIn(items=[api_v1.BudgetItemIn(**i.model_dump()) for i in items],
                                replace=replace)
    return _run(lambda session, user: api_v1.save_budget(session, user, trip_id, body,
                                                         source="mcp"))


@mcp.tool()
def delete_budget_item(trip_id: TripId,
                       item_id: Annotated[str, Field(description="Planned item id from "
                                                               "list_expenses' plan")],
                       ) -> dict[str, Any]:
    """Remove a planned cost (a draft you saved by mistake, say)."""
    _run(api_v1.api_delete_budget_item, trip_id=trip_id, item_id=item_id)
    return {"ok": True, "deleted": item_id}


@mcp.tool()
def comment_on_expense(trip_id: TripId,
                       expense_id: Annotated[str, Field(description="Expense id from "
                                                                    "list_expenses")],
                       body: Annotated[str, Field(description="The comment")],
                       ) -> dict[str, Any]:
    """Add a comment to an expense the user is on, for the people on it to discuss
    (a question about the split, a correction). Only post what the user asked you to."""
    return _run(api_v1.api_add_expense_comment, trip_id=trip_id, expense_id=expense_id,
                body=api_v1.ExpenseCommentIn(body=body))


@mcp.tool()
def delete_expense(trip_id: TripId,
                   expense_id: Annotated[str, Field(description="Expense id from list_expenses")],
                   ) -> dict[str, Any]:
    """Delete an expense or a recorded settlement. Needs edit access."""
    _run(api_v1.api_delete_expense, trip_id=trip_id, expense_id=expense_id)
    return {"ok": True, "deleted": expense_id}


# --------------------------------------------------------------------------- journal
@mcp.tool()
def create_journal_entry(body: Annotated[str, Field(description="The entry text (markdown ok)")],
                         entry_date: Annotated[str | None, Field(
                             description="Day the entry is about, YYYY-MM-DD (default today)")] = None,
                         trip_id: Annotated[str | None, Field(
                             description="Attach to this trip (optional)")] = None,
                         place_label: Annotated[str | None, Field(
                             description="Where, e.g. \"Lake Louise\"")] = None) -> dict[str, Any]:
    """Write a journal entry for the user. Journals are private to their author."""
    return _run(api_v1.api_journal_create, body=api_v1.JournalCreate(
        body=body, entry_date=entry_date, trip_id=trip_id, place_label=place_label,
        source="mcp"))


@mcp.tool()
def list_journal_entries(trip_id: Annotated[str | None, Field(
                             description="Only entries attached to this trip")] = None,
                         date_from: Date = None, date_to: Date = None) -> dict[str, Any]:
    """The user's own journal entries, newest day first, optionally filtered."""
    return {"entries": _run(api_v1.api_journal_list, trip_id=trip_id,
                            date_from=date_from, date_to=date_to)}


@mcp.tool()
def get_journal_entry(entry_id: Annotated[str, Field(description="Journal entry id")]) -> dict[str, Any]:
    """One of the user's journal entries."""
    return _run(api_v1.api_journal_get, entry_id=entry_id)


@mcp.tool()
def update_journal_entry(entry_id: Annotated[str, Field(description="Journal entry id")],
                         body: Annotated[str | None, Field(description="New text (replaces it)")] = None,
                         entry_date: Date = None,
                         place_label: Annotated[str | None, Field(description="Where")] = None,
                         ) -> dict[str, Any]:
    """Edit one of the user's journal entries. Only what you pass changes. To
    move it to another trip use attach_journal_entry."""
    return _run(api_v1.api_journal_update, entry_id=entry_id, body=api_v1.JournalPatch(
        body=body, entry_date=entry_date, place_label=place_label))


@mcp.tool()
def attach_journal_entry(entry_id: Annotated[str, Field(description="Journal entry id")],
                         trip_id: Annotated[str | None, Field(
                             description="Trip to attach it to; null detaches it")]) -> dict[str, Any]:
    """Attach a journal entry to a trip (or detach it with trip_id null)."""
    return _run(api_v1.api_journal_update, entry_id=entry_id,
                body=api_v1.JournalPatch(trip_id=trip_id, attach_trip=True))


# --------------------------------------------------------------------------- capture + review
@mcp.tool()
def capture_url(url: Annotated[str, Field(description="A booking/web page URL, a Google/Apple "
                                                      "Maps directions link, or a Google/Apple "
                                                      "Maps PLACE link")],
                travel_date: Annotated[str | None, Field(
                    description="YYYY-MM-DD for a maps directions link, if known. Without "
                                "one the route waits in the review queue with no date "
                                "yet (D-210)")] = None,
                travel_time: Annotated[str | None, Field(description="HH:MM (directions)")] = None,
                mode: Annotated[Literal["car", "transit", "walk"] | None, Field(
                    description="Directions travel mode")] = None,
                title: str | None = None, notes: str | None = None) -> dict[str, Any]:
    """Send a link into Run·dere. A booking page or directions link goes through
    the capture pipeline and lands in the user's review queue to approve (returns
    the batch id). A Google/Apple Maps PLACE link is instead kept as a saved
    place for the user to file to a trip and date later (returns the saved place,
    with a suggested trip); no travel_date is needed."""
    from extract import place_url
    if place_url.classify(url) == "place":
        def _save(session, user):
            place, outcome = places_svc.save(session, user, url=url, notes=notes,
                                             source="mcp", enrich=True)
            trips = api_v1.fileable_trips(session, user)
            suggested = (places_svc.suggest_trip(session, trips, place)
                         if place.status == places_svc.SAVED else None)
            out = api_v1._place_out(place, suggested, outcome)
            return {"saved_place": out.model_dump(), "outcome": outcome,
                    "next": "File it with file_saved_place, or the user does it from "
                            "Saved places (/places)."}
        return _run(_save)
    return _run(api_v1.api_capture, body=api_v1.CaptureIn(
        url=url, travel_date=travel_date, travel_time=travel_time, mode=mode,
        title=title, notes=notes))


# --------------------------------------------------------------------------- saved places
@mcp.tool()
def list_saved_places() -> dict[str, Any]:
    """The user's saved places: Google/Apple Maps places they kept before choosing a
    trip, newest first, each with a `suggested_trip_id` (a hint, never a decision).
    Filed and removed places are not listed."""
    def _list(session, user):
        trips = api_v1.fileable_trips(session, user)
        rows = [api_v1._place_out(p, places_svc.suggest_trip(session, trips, p))
                for p in places_svc.list_places(session, user)]
        return {"count": len(rows), "places": [r.model_dump() for r in rows]}
    return _run(_list)


@mcp.tool()
def save_places(places: Annotated[list[api_v1.SavedPlaceIn], Field(
                    description="Up to 200 places. Each needs a `url` (a full Google/Apple Maps "
                                "place link) or a `name` and/or `lat`+`lon`; add `address`, "
                                "`category` (an OSM tag like amenity=restaurant, or a MapKit "
                                "category), `notes` and `source_url` when you have them. For a "
                                "Google Takeout list, map each entry's title, note, address, "
                                "coordinates and Maps URL to these fields.")],
                dry_run: Annotated[bool, Field(
                    description="Report what each place would do and write nothing. Use it "
                                "first to review a list before pushing it.")] = False,
                enrich: Annotated[bool, Field(
                    description="Look up address and category via the server's geocoder for "
                                "places missing them. At most 25 places per call.")] = False,
                revive: Annotated[bool, Field(
                    description="Re-add places the user removed earlier. Never re-adds a "
                                "place that was already filed to a trip.")] = False,
                allow_near_duplicates: Annotated[bool, Field(
                    description="Also add a place with the same name within 150 m of one "
                                "already saved.")] = False) -> dict[str, Any]:
    """Save a list of places for the user, for example a Google Maps list you have
    read from a Takeout export, after choosing the ones worth keeping. Safe to run
    twice: each place is saved at most once, a place already saved, filed to a trip
    or removed is skipped and reported, and one bad entry never stops the rest.
    Returns `counts` and one `results` row per place, in order, with an `outcome`:
    created, revived, duplicate, near_duplicate, already_filed, removed_earlier or
    error (with a reason)."""
    return _run(lambda session, user: places_svc.save_many(
        session, user, [p.model_dump() for p in places], dry_run=dry_run, enrich=enrich,
        revive=revive, allow_near_duplicates=allow_near_duplicates, source="mcp"))


@mcp.tool()
def update_saved_place(place_id: Annotated[str, Field(description="Saved place id (from list_saved_places)")],
                       name: str | None = None, address: str | None = None,
                       notes: str | None = None,
                       activity_type: Annotated[str | None, Field(
                           description="A type from list_activity_types, e.g. dining, tour, "
                                       "lodging, event")] = None) -> dict[str, Any]:
    """Rename or edit a saved place before it is filed. Only the fields you pass change."""
    return _run(lambda session, user: api_v1._place_out(places_svc.update_place(
        session, user, place_id, name=name, address=address, notes=notes,
        activity_type=activity_type)).model_dump())


@mcp.tool()
def file_saved_place(place_id: Annotated[str, Field(description="Saved place id")],
                     trip_id: TripId,
                     starts_at: Annotated[str | None, Field(
                         description="YYYY-MM-DD or YYYY-MM-DDTHH:MM local. Omit to leave it "
                                     "undated, to be scheduled later")] = None,
                     activity_type: str | None = None, notes: str | None = None,
                     traveler_ids: TravelerIds = None) -> dict[str, Any]:
    """File a saved place onto a trip's timeline as an activity (the user's own trips or
    ones they can edit). Filing a place that was already filed is refused (409), so a
    retry never adds it twice."""
    return _run(lambda session, user: api_v1.file_place(
        session, user, place_id, trip_id, api_v1.SavedPlaceFileIn(
            starts_at=starts_at, activity_type=activity_type, notes=notes,
            traveler_ids=traveler_ids)))


@mcp.tool()
def remove_saved_place(place_id: Annotated[str, Field(description="Saved place id")]) -> dict[str, Any]:
    """Remove a saved place. Re-sending a list that still contains it will not bring it
    back unless `revive` is set."""
    def _rm(session, user):
        places_svc.delete_place(session, user, place_id)
        return {"ok": True, "place_id": place_id}
    return _run(_rm)


@mcp.tool()
def list_review(bucket: Literal["all", "attention", "waiting", "ready", "staged", "logged"] = "all",
                ) -> dict[str, Any]:
    """The user's capture review queue with counts per bucket, and the items in
    `bucket`."""
    return _run(api_v1.api_review, bucket=bucket)


@mcp.tool()
def get_review_item(item_id: Annotated[str, Field(description="Review item id")]) -> dict[str, Any]:
    """One capture in the review queue."""
    return _run(api_v1.api_review_item, item_id=item_id)


@mcp.tool()
def assign_review_item_to_trip(item_id: Annotated[str, Field(description="Review item id")],
                               trip_id: TripId) -> dict[str, Any]:
    """File a capture into a trip: the user's own trips or trips they travel on
    (not someone else's trip just because it's visible)."""
    return _run(lambda session, user: api_v1.assign_capture_to_trip(
        session, user, item_id, trip_id))


# --------------------------------------------------------------------------- Discover
@mcp.tool()
def save_research(trip_id: TripId,
                  title: Annotated[str, Field(description="Short heading, e.g. \"Day hikes near Banff\"")],
                  body_md: Annotated[str, Field(
                      description="The finding in markdown: **bold**, ### headings, - bullets, "
                                  "[text](https://…) links")],
                  topic: Annotated[Literal["research", "food", "events", "safety", "visa",
                                           "plugs", "packing"], Field(
                      description="Which Discover topic. `research` (default) is your own "
                                  "section; the others update the same finding Run·dere's "
                                  "local Discover run produces for that place")] = "research",
                  place_key: Annotated[str | None, Field(
                      description="Which place: a country code (\"CA\") or city key "
                                  "(\"CA:Banff\") — reuse the place_key of the matching "
                                  "finding in get_trip's `discovery`. Required except for "
                                  "topic=research, where omitting it means the whole trip")] = None,
                  sources: Annotated[list[dict[str, str]] | None, Field(
                      description="Sources as [{\"title\": \"…\", \"url\": \"https://…\"}]")] = None,
                  ) -> dict[str, Any]:
    """Save research onto the USER'S OWN Discover tab for a trip, as a draft
    they review. Saving again for the same topic + place SUPERSEDES the current
    version (the old one is kept as history), so write the complete, up-to-date
    picture each time — e.g. a refresh days before the trip with newly announced
    events. Run·dere's local Discover run won't overwrite what you save; it's
    shown as written by the app you connected as. A finding the user LOCKED
    (see `locked` in get_trip) is refused — tell them instead.
    For restaurants/bars or events/activities as rows, use save_city_guide;
    for local phrases, use save_phrases; for Safety & Transport, save_safety.
    When you write about weather (e.g. in research or packing), give what it
    feels like, the humidity and the altitude — never air temperature alone (D-189)."""
    body = api_v1.FindingIn(topic=topic, title=title, body_md=body_md, place_key=place_key,
                            sources=[api_v1.SourceIn(**s) for s in (sources or [])
                                     if isinstance(s, dict) and s.get("url")])
    agent = _caller_agent.get()
    return _run(lambda session, user: api_v1.save_research_finding(
        session, user, trip_id, body, agent=agent))


_PLACE_KEY = Field(description="The city key, e.g. \"JP:Kyoto\" — reuse the place_key "
                               "of the city's findings in get_trip's `discovery`")


@mcp.tool()
def get_discovery_profile() -> dict[str, Any]:
    """The user's discovery profile: dietary restrictions, cuisines to avoid,
    allergies, budget ceiling, drink and activity interests, mobility,
    nationality and base currency — each with a label — plus `notes` (their
    likes, dislikes and past favourites in their own words), `not_my_thing`
    (places/events they hid as not their thing, across trips) and `how_to_use`.
    Read it before researching food, drink or things to do and follow it; if
    `blank` is true, ask them what they like first. (get_trip includes it too.)"""
    return _run(lambda session, user: api_v1.my_discovery_profile(session, user))


@mcp.tool()
def save_city_guide(trip_id: TripId,
                    topic: Annotated[Literal["food", "events"], Field(
                        description="`food` = Food & Drink (restaurants, cafés, bars, pubs, "
                                    "markets…); `events` = Events & Activities")],
                    place_key: Annotated[str, _PLACE_KEY],
                    summary_md: Annotated[str, Field(
                        description="The city write-up shown above the tables: what the "
                                    "city is like for this, and your recommendation. A few "
                                    "short paragraphs of markdown")],
                    items: Annotated[list[api_v1.ItemIn], Field(
                        description="The rows. Food: kind is restaurant|cafe|bar|pub|brewery|"
                                    "distillery|winery|market|bakery|dessert|street_food; set "
                                    "what_to_get (the dish or drink to order) whenever you "
                                    "know it. Events: kind `event` (has a date — starts_on "
                                    "YYYY-MM-DD[THH:MM], venue, address) or `activity` "
                                    "(anytime — address, duration, booking). Give each a "
                                    "source_url or sources")],
                    title: Annotated[str | None, Field(
                        description="Heading; defaults to \"<City> — food & drink\" / "
                                    "\"… — events & activities\"")] = None,
                    replace_items: Annotated[bool, Field(
                        description="True (default): these rows are the complete list for "
                                    "the city — rows you leave out are dropped unless the "
                                    "person hid, edited or scheduled them. False: add/update "
                                    "only, e.g. a follow-up batch")] = True,
                    sources: Annotated[list[dict[str, str]] | None, Field(
                        description="Sources for the write-up as "
                                    "[{\"title\": \"…\", \"url\": \"https://…\"}]")] = None,
                    ) -> dict[str, Any]:
    """Save a city's Food & Drink or Events & Activities onto the USER'S OWN
    Discover tab: a summary + recommendation, and the places/events as rows
    they can sort, hide and add to their timeline. Lands as a draft they
    review; supersedes the previous version for that city (kept as history)
    and the local Discover run won't overwrite it. Rows are matched by name, so
    re-saving updates them in place. The saved rows come back in get_trip's
    `discovery_items`."""
    from discover.items import city_of
    label = {"food": "food & drink", "events": "events & activities"}[topic]
    body = api_v1.FindingIn(
        topic=topic, place_key=place_key, body_md=summary_md, items=items,
        replace_items=replace_items,
        title=title or f"{city_of(place_key)} — {label}",
        sources=[api_v1.SourceIn(**s) for s in (sources or [])
                 if isinstance(s, dict) and s.get("url")])
    agent = _caller_agent.get()
    return _run(lambda session, user: api_v1.save_research_finding(
        session, user, trip_id, body, agent=agent))


@mcp.tool()
def save_phrases(trip_id: TripId,
                 place_key: Annotated[str, _PLACE_KEY],
                 register_summary: Annotated[str, Field(
                     description="How formal or casual locals are, what to call people, "
                                 "what to avoid — one short paragraph")],
                 greetings: Annotated[list[api_v1.PhraseIn], Field(
                     description="How locals actually greet: phrase, meaning, "
                                 "pronunciation (respelled for English speakers), and a "
                                 "note on when/with whom to use it")] = [],
                 expressions: Annotated[list[api_v1.PhraseIn], Field(
                     description="Useful local expressions beyond greetings "
                                 "(thanks, no thanks, the bill, local slang)")] = [],
                 etiquette: Annotated[list[str], Field(
                     description="Short etiquette notes, one per item")] = [],
                 sources: Annotated[list[dict[str, str]] | None, Field(
                     description="Sources as [{\"title\": \"…\", \"url\": \"https://…\"}]")] = None,
                 ) -> dict[str, Any]:
    """Save Local phrases & register for a city onto the USER'S OWN Discover
    tab (under Language), as a draft they review. Only this layer is yours to
    write — the national language and the phrasebook stay with Run·dere. A
    finding the user LOCKED is refused; tell them instead."""
    from discover.items import city_of
    body = api_v1.FindingIn(
        topic="language_local", place_key=place_key, title=city_of(place_key),
        phrases=api_v1.PhrasesIn(register_summary=register_summary, greetings=greetings,
                                 expressions=expressions, etiquette=etiquette),
        sources=[api_v1.SourceIn(**s) for s in (sources or [])
                 if isinstance(s, dict) and s.get("url")])
    agent = _caller_agent.get()
    return _run(lambda session, user: api_v1.save_research_finding(
        session, user, trip_id, body, agent=agent))


@mcp.tool()
def save_safety(trip_id: TripId,
                place_key: Annotated[str, _PLACE_KEY],
                safety: Annotated[api_v1.SafetyIn | None, Field(
                    description="summary (brief, factual, non-alarmist), advisories "
                                "[{text, source_url}] — every advisory needs its source; "
                                "two or more different sites avoid the 'verify' "
                                "banner — and scams (one per item)")] = None,
                transport: Annotated[api_v1.TransportIn | None, Field(
                    description="summary (getting around), public_transit, "
                                "from_airport, ride_hailing (apps that work here), "
                                "apps (to install), tips")] = None,
                notes_md: Annotated[str, Field(
                    description="Optional extra markdown shown after the subsections")] = "",
                sources: Annotated[list[dict[str, str]] | None, Field(
                    description="Other sources as [{\"title\": \"…\", \"url\": \"https://…\"}]")] = None,
                ) -> dict[str, Any]:
    """Save a city's Safety & Transport onto the USER'S OWN Discover tab, as
    headed subsections (Summary, Advisories, Scams; Getting around, Public
    transit, From the airport, Ride-hailing, Apps, Tips). Lands as a draft they
    review and supersedes the previous version (kept as history)."""
    from discover.items import city_of
    body = api_v1.FindingIn(
        topic="safety", place_key=place_key, title=city_of(place_key), body_md=notes_md,
        safety_transport=api_v1.SafetyTransportIn(safety=safety, transport=transport),
        sources=[api_v1.SourceIn(**s) for s in (sources or [])
                 if isinstance(s, dict) and s.get("url")])
    agent = _caller_agent.get()
    return _run(lambda session, user: api_v1.save_research_finding(
        session, user, trip_id, body, agent=agent))


@mcp.tool()
def add_item_to_timeline(trip_id: TripId,
                         item_id: Annotated[str, Field(
                             description="A Discover item's id from get_trip's "
                                         "`discovery_items`")],
                         starts_at: Annotated[str | None, Field(
                             description="When, as YYYY-MM-DDTHH:MM local time. Omit for an "
                                         "event to use its own date (09:00)")] = None,
                         traveler_ids: Annotated[list[int] | None, Field(
                             description="Who it's for (traveller ids from get_trip); "
                                         "omit for everyone")] = None,
                         ) -> dict[str, Any]:
    """Put a Discover restaurant, event or activity on the trip's timeline,
    filled in from the item (name, address, map link, website, what to get) —
    a Restaurant, Event or Tour / activity. The item then shows as on the
    timeline. Refused if it already is."""
    body = api_v1.ItemToTimelineIn(starts_at=starts_at, traveler_ids=traveler_ids)
    return _run(lambda session, user: api_v1.add_item_to_timeline(
        session, user, trip_id, item_id, body))


# --------------------------------------------------------------------------- research kits (prompts)
def _register_kit_prompts() -> None:
    """Each research kit (research_kits/<slug>/SKILL.md) becomes a prompt,
    `research_<slug>`, taking the trip id and optionally one city."""
    from app import research_kits

    for kit in research_kits.all_kits():
        def make(k):
            def prompt(trip_id: TripId,
                       place_key: Annotated[str | None, Field(
                           description="One city key (e.g. \"JP:Kyoto\"); omit for "
                                       "every city in the trip")] = None) -> str:
                return research_kits.prompt_text(k, trip_id, place_key)
            return prompt
        mcp.prompt(name="research_" + kit.slug.replace("-", "_"),
                   title=f"Research {kit.title}", description=kit.description)(make(kit))


_register_kit_prompts()


# --------------------------------------------------------------------------- HTTP endpoint
_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _new_session_manager():
    """A fresh Streamable HTTP manager (one per app lifespan — a manager can
    only run once). Stateless + JSON: every POST is self-contained, so nothing
    breaks across restarts or replicas, and bearer auth runs on every call."""
    mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                            json_response=True, transport_security=_SECURITY)
    return mcp.session_manager


@contextlib.asynccontextmanager
async def lifespan():
    manager = _new_session_manager()
    async with manager.run():
        yield


def _unauthorized(request: Request, invalid: bool) -> tuple[int, list, bytes]:
    meta = f"{oauth.base_url(request)}/.well-known/oauth-protected-resource"
    challenge = f'Bearer resource_metadata="{meta}", scope="{oauth.DEFAULT_SCOPE}"'
    if invalid:
        challenge += ', error="invalid_token"'
    body = json.dumps({"error": "invalid_token" if invalid else "unauthorized",
                       "error_description": "a Run·dere bearer token is required"}).encode()
    headers = [(b"content-type", b"application/json"), (b"www-authenticate", challenge.encode()),
               (b"cache-control", b"no-store")]
    return 401, headers, body


def _bearer_caller(token: str, request: Request) -> tuple[Any, Any]:
    """(user id, agent label) for a bearer token, or (None, None). Sync."""
    with SessionLocal() as session:
        user = authn.user_from_bearer(session, token, request)
        uid = user.id if user is not None else None
        agent = oauth.credential_label(session, token) if uid else None
    return uid, agent


class MCPEndpoint:
    """ASGI app for `/mcp`: bearer auth → caller context → the MCP SDK."""

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        authz = request.headers.get("authorization", "")
        token = authz[7:].strip() if authz[:7].lower() == "bearer " else ""
        uid = agent = None
        if token:
            # Off the event loop (YONDER-178): blocking DB I/O here would stall
            # every request while the pool is drained.
            uid, agent = await run_in_threadpool(_bearer_caller, token, request)
        if uid is None:
            status, headers, body = _unauthorized(request, invalid=bool(token))
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return
        reset = _caller_id.set(uid)
        reset_agent = _caller_agent.set(agent)
        try:
            try:
                manager = mcp.session_manager
            except RuntimeError:
                manager = None
            if manager is None or getattr(manager, "_task_group", None) is None:
                await send({"type": "http.response.start", "status": 503,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"MCP not started"})
                return
            await manager.handle_request(scope, receive, send)
        finally:
            _caller_agent.reset(reset_agent)
            _caller_id.reset(reset)


endpoint = MCPEndpoint()
