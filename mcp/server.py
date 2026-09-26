"""Yonder MCP server — stdio edition (LAN / Claude Code / Claude Desktop).

The PRIMARY way to use Yonder from Claude is the remote MCP endpoint built into
the app at `https://<yonder-host>/mcp` (YONDER-108): add it as a claude.ai
connector (OAuth sign-in), or to Claude Code with a personal API token. This
script is the alternative for hosts that can reach Yonder's `/api/v1` but not
`/mcp`: the same tools, as a thin client of the same endpoints, so it inherits
all auth + per-user scoping.

Run:  YONDER_URL=https://yonder.home.example YONDER_TOKEN=ypat_… python mcp/server.py

Config (env):
  YONDER_URL       base URL of the Yonder app (default http://localhost:8000)
  YONDER_TOKEN     a personal API token (Account → Personal API tokens)  ← preferred
  YONDER_USER      alternatively, a username   ┐ the MCP is scoped to THIS user
  YONDER_PASSWORD  and that user's password    ┘

Needs the MCP SDK (2.x) on the host running it:  pip install -r mcp/requirements.txt
"""
from __future__ import annotations

import os
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from client import YonderClient        # same directory (added to sys.path[0])

mcp = MCPServer("Yonder")
_client = YonderClient(
    os.environ.get("YONDER_URL", "http://localhost:8000"),
    username=os.environ.get("YONDER_USER"),
    password=os.environ.get("YONDER_PASSWORD"),
    token=os.environ.get("YONDER_TOKEN") or None,
)


# --------------------------------------------------------------------------- trips
@mcp.tool()
def list_trips() -> dict[str, Any]:
    """List the trips the user can see (id, name, dates, activity_count)."""
    return {"trips": _client.list_trips()}


@mcp.tool()
def get_trip(trip_id: str) -> dict[str, Any]:
    """Full snapshot of one trip: timeline (with traveler_ids; empty = everyone),
    travellers, places with lat/lon, the user's own Discover findings and
    packing, and linked captures."""
    return _client.get_trip(trip_id)


@mcp.tool()
def update_trip(trip_id: str, name: str | None = None, start_date: str | None = None,
                end_date: str | None = None, notes: str | None = None) -> dict[str, Any]:
    """Rename a trip or change its dates (YYYY-MM-DD) / notes."""
    return _client.update_trip(trip_id, name=name, start_date=start_date,
                               end_date=end_date, notes=notes)


@mcp.tool()
def dashboard() -> dict[str, Any]:
    """The home overview: next/current trip, review-queue counts, capture activity."""
    return _client.dashboard()


# --------------------------------------------------------------------------- timeline
@mcp.tool()
def list_activity_types() -> dict[str, Any]:
    """Timeline activity types and the `values` field keys each takes."""
    return {"types": _client.activity_types()}


@mcp.tool()
def add_activity(trip_id: str, type: str, values: dict[str, Any],
                 starts_at: str | None = None,
                 traveler_ids: list[int] | None = None) -> dict[str, Any]:
    """Add a timeline item. `type` from list_activity_types; `values` its fields
    (e.g. {"name": "Noma", "when": "2026-09-06T19:00"}); `traveler_ids` from
    get_trip's travelers ([] = everyone)."""
    return _client.add_activity(trip_id, type, values, starts_at=starts_at,
                                traveler_ids=traveler_ids)


@mcp.tool()
def update_activity(trip_id: str, activity_id: str, type: str | None = None,
                    values: dict[str, Any] | None = None, starts_at: str | None = None,
                    traveler_ids: list[int] | None = None) -> dict[str, Any]:
    """Edit a timeline item. `values` REPLACES all fields (pass the full set from
    get_trip); `traveler_ids` [] = everyone, omit = unchanged."""
    return _client.update_activity(trip_id, activity_id, type=type, values=values,
                                   starts_at=starts_at, traveler_ids=traveler_ids)


@mcp.tool()
def delete_activity(trip_id: str, activity_id: str) -> dict[str, Any]:
    """Remove a timeline item."""
    _client.delete_activity(trip_id, activity_id)
    return {"ok": True, "deleted": activity_id}


# --------------------------------------------------------------------------- packing
@mcp.tool()
def generate_packing(trip_id: str) -> dict[str, Any]:
    """Generate (or re-generate) the user's own packing list for a trip."""
    return _client.generate_packing(trip_id)


@mcp.tool()
def set_packing_status(trip_id: str, item_id: str,
                       status: Literal["packed", "draft", "removed"]) -> dict[str, Any]:
    """Check a packing item off (packed), back on (draft), or drop it (removed)."""
    return _client.set_packing_status(trip_id, item_id, status)


@mcp.tool()
def add_packing_item(trip_id: str, label: str, category: str = "activity",
                     quantity: int = 1) -> dict[str, Any]:
    """Add a custom line to the user's own packing list."""
    return _client.add_packing_item(trip_id, label, category=category, quantity=quantity)


# --------------------------------------------------------------------------- journal
@mcp.tool()
def create_journal_entry(body: str, entry_date: str | None = None,
                         trip_id: str | None = None,
                         place_label: str | None = None) -> dict[str, Any]:
    """Write a (private) journal entry; optionally attached to a trip."""
    return _client.journal_create(body=body, entry_date=entry_date, trip_id=trip_id,
                                  place_label=place_label, source="mcp")


@mcp.tool()
def list_journal_entries(trip_id: str | None = None, date_from: str | None = None,
                         date_to: str | None = None) -> dict[str, Any]:
    """The user's own journal entries, newest day first."""
    return {"entries": _client.journal_list(trip_id=trip_id, date_from=date_from,
                                            date_to=date_to)}


@mcp.tool()
def get_journal_entry(entry_id: str) -> dict[str, Any]:
    """One journal entry."""
    return _client.journal_get(entry_id)


@mcp.tool()
def update_journal_entry(entry_id: str, body: str | None = None,
                         entry_date: str | None = None,
                         place_label: str | None = None) -> dict[str, Any]:
    """Edit a journal entry (only the fields given)."""
    return _client.journal_update(entry_id, body=body, entry_date=entry_date,
                                  place_label=place_label)


@mcp.tool()
def attach_journal_entry(entry_id: str, trip_id: str | None) -> dict[str, Any]:
    """Attach a journal entry to a trip, or detach it (trip_id null)."""
    return _client.journal_attach(entry_id, trip_id)


# --------------------------------------------------------------------------- capture + review
@mcp.tool()
def capture_url(url: str, travel_date: str | None = None, travel_time: str | None = None,
                mode: Literal["car", "transit", "walk"] | None = None,
                title: str | None = None, notes: str | None = None) -> dict[str, Any]:
    """Share a booking page or a maps directions link (needs travel_date) into
    the user's review queue. Returns the batch id."""
    return _client.capture_url(url, travel_date=travel_date, title=title, notes=notes,
                               travel_time=travel_time, mode=mode)


@mcp.tool()
def list_review(bucket: Literal["all", "attention", "waiting", "ready", "staged",
                                "logged"] = "all") -> dict[str, Any]:
    """The user's capture review queue with counts per bucket."""
    return _client.review(bucket)


@mcp.tool()
def get_review_item(item_id: str) -> dict[str, Any]:
    """One capture in the review queue."""
    return _client.review_item(item_id)


@mcp.tool()
def assign_review_item_to_trip(item_id: str, trip_id: str) -> dict[str, Any]:
    """File a capture into one of the user's own/open trips or a trip they travel on."""
    return _client.assign_trip(item_id, trip_id)


# --------------------------------------------------------------------------- Discover
@mcp.tool()
def save_research(trip_id: str, title: str, body_md: str, place_key: str | None = None,
                  sources: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Save research onto the user's own Discover tab for a trip, as a
    "Research (Claude)" draft. Same trip + place supersedes the current version
    (the old one is kept as history). `sources`: [{"title": "…", "url": "https://…"}]."""
    return _client.save_research(trip_id, title, body_md, place_key=place_key,
                                 sources=sources)


if __name__ == "__main__":
    mcp.run()
