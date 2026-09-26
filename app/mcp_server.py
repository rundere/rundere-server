"""Remote MCP server inside Yonder at `/mcp` (YONDER-108, SPEC-4.2 §3.7).

MCP Streamable HTTP (stateless, JSON responses) for the claude.ai connector,
the Claude apps, and Claude Code. Every request needs a bearer token — an OAuth
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
from pydantic import Field
from starlette.requests import Request

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from app import api_v1, authn, oauth
from app.config import settings
from app.db import SessionLocal
from app.models import TripActivity, User

_caller_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "yonder_mcp_caller", default=None)

INSTRUCTIONS = """Yonder is the user's personal travel planner. Tools act as the
signed-in Yonder user and see only what they can see in the app.
Start with list_trips, then get_trip for the full picture (timeline, travellers,
places, the user's Discover findings and packing). Timeline items take a `type`
from list_activity_types and a `values` object with that type's field keys.
Use save_research to keep your research on the user's Discover tab for a trip —
it lands as a draft they review. Dates are ISO (YYYY-MM-DD, or
YYYY-MM-DDTHH:MM local time for timeline items)."""

mcp = MCPServer("Yonder", instructions=INSTRUCTIONS, version=settings.app_version)


# --------------------------------------------------------------------------- plumbing
def _run(fn, /, **kwargs) -> Any:
    """Call an /api/v1 function as the MCP caller in a fresh DB session and
    return plain JSON. HTTP errors become tool errors Claude can read."""
    uid = _caller_id.get()
    if not uid:
        raise ToolError("not signed in")
    with SessionLocal() as session:
        user = session.get(User, uid)
        if user is None or not user.is_active:
            raise ToolError("this Yonder account is not active")
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
    """Full snapshot of one trip: the timeline (each activity with its type,
    values, times and `traveler_ids` — empty means everyone), the travellers,
    places with lat/lon, the user's OWN Discover findings (weather, currency,
    visa, food, saved research…) and packing list, and linked captures."""
    return _run(api_v1.api_trip_snapshot, trip_id=trip_id)


@mcp.tool()
def update_trip(trip_id: TripId,
                name: Annotated[str | None, Field(description="New trip name")] = None,
                start_date: Date = None, end_date: Date = None,
                notes: Annotated[str | None, Field(description="Trip notes (replaces them)")] = None,
                ) -> dict[str, Any]:
    """Rename a trip or change its dates/notes. Only the fields you pass change.
    Needs edit access (owner, traveller on it, or open trip)."""
    body = api_v1.TripPatch(name=name, start_date=start_date, end_date=end_date, notes=notes)
    return _run(api_v1.api_update_trip, trip_id=trip_id, body=body)


@mcp.tool()
def dashboard() -> dict[str, Any]:
    """The home overview: next/current trip, review-queue counts, capture activity."""
    return _run(api_v1.api_dashboard)


# --------------------------------------------------------------------------- timeline
@mcp.tool()
def list_activity_types() -> dict[str, Any]:
    """Timeline activity types (flight, train, drive, carRental, lodging, tour,
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
def capture_url(url: Annotated[str, Field(description="A booking/web page URL or a Google/Apple "
                                                      "Maps directions link")],
                travel_date: Annotated[str | None, Field(
                    description="YYYY-MM-DD — required for a maps directions link")] = None,
                travel_time: Annotated[str | None, Field(description="HH:MM (directions)")] = None,
                mode: Annotated[Literal["car", "transit", "walk"] | None, Field(
                    description="Directions travel mode")] = None,
                title: str | None = None, notes: str | None = None) -> dict[str, Any]:
    """Send a link into Yonder's capture pipeline. It is extracted and lands in
    the user's review queue to approve. Returns the batch id."""
    return _run(api_v1.api_capture, body=api_v1.CaptureIn(
        url=url, travel_date=travel_date, travel_time=travel_time, mode=mode,
        title=title, notes=notes))


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
    """File a capture into a trip: the user's own trips, open trips, or trips
    they travel on (not someone else's trip just because it's visible)."""
    return _run(lambda session, user: api_v1.assign_capture_to_trip(
        session, user, item_id, trip_id))


# --------------------------------------------------------------------------- Discover
@mcp.tool()
def save_research(trip_id: TripId,
                  title: Annotated[str, Field(description="Short heading, e.g. \"Day hikes near Banff\"")],
                  body_md: Annotated[str, Field(
                      description="The research in markdown: **bold**, ### headings, - bullets, "
                                  "[text](https://…) links")],
                  place_key: Annotated[str | None, Field(
                      description="Which place it's about: omit for the whole trip, or a "
                                  "place key from get_trip's `places` (e.g. \"CA:Banff\")")] = None,
                  sources: Annotated[list[dict[str, str]] | None, Field(
                      description="Sources as [{\"title\": \"…\", \"url\": \"https://…\"}]")] = None,
                  ) -> dict[str, Any]:
    """Save research onto the USER'S OWN Discover tab for a trip, as a
    "Research (Claude)" draft they can review and edit. Saving again for the
    same trip + place replaces that draft — include everything worth keeping.
    Once they've reviewed it (approved/edited), use a different place_key."""
    body = api_v1.FindingIn(title=title, body_md=body_md, place_key=place_key,
                            sources=[api_v1.SourceIn(**s) for s in (sources or [])
                                     if isinstance(s, dict) and s.get("url")])
    return _run(lambda session, user: api_v1.save_research_finding(session, user, trip_id, body))


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
                       "error_description": "a Yonder bearer token is required"}).encode()
    headers = [(b"content-type", b"application/json"), (b"www-authenticate", challenge.encode()),
               (b"cache-control", b"no-store")]
    return 401, headers, body


class MCPEndpoint:
    """ASGI app for `/mcp`: bearer auth → caller context → the MCP SDK."""

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        authz = request.headers.get("authorization", "")
        token = authz[7:].strip() if authz[:7].lower() == "bearer " else ""
        uid = None
        if token:
            with SessionLocal() as session:
                user = authn.user_from_bearer(session, token, request)
                uid = user.id if user is not None else None
        if uid is None:
            status, headers, body = _unauthorized(request, invalid=bool(token))
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return
        reset = _caller_id.set(uid)
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
            _caller_id.reset(reset)


endpoint = MCPEndpoint()
