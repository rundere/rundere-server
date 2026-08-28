"""Yonder MCP server (Phase 4, Workstream E) — exposes Yonder to agents over the
same /api/v1 surface, so it inherits all auth + per-user scoping.

Run:  YONDER_URL=https://yonder.home.example YONDER_USER=me YONDER_PASSWORD=… \
      python mcp/server.py            # stdio transport (Claude Desktop / Code)

Config (env):
  YONDER_URL       base URL of the Yonder app (default http://localhost:8000)
  YONDER_USER      Yonder username to act as   ┐ the MCP is scoped to THIS user
  YONDER_PASSWORD  that user's password        ┘ (see the API's per-user scoping)
  YONDER_TOKEN     alternatively, a pre-issued access token (skips login)

Needs the MCP SDK on the host running it:  pip install -r mcp/requirements.txt
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from client import YonderClient        # same directory (added to sys.path[0])

mcp = FastMCP("Yonder")
_client = YonderClient(
    os.environ.get("YONDER_URL", "http://localhost:8000"),
    username=os.environ.get("YONDER_USER"),
    password=os.environ.get("YONDER_PASSWORD"),
    token=os.environ.get("YONDER_TOKEN") or None,
)


@mcp.tool()
def list_trips() -> list:
    """List the trips visible to the configured Yonder user (id, name, dates,
    activity_count). Use an id with get_trip for the full details."""
    return _client.list_trips()


@mcp.tool()
def get_trip(trip_id: str) -> dict:
    """Full snapshot of one trip: timeline activities (+values), travellers,
    places with lat/lon, discovery findings (weather/currency/visa/food/…), the
    user's own packing plan, and linked captures."""
    return _client.get_trip(trip_id)


@mcp.tool()
def dashboard() -> dict:
    """The home overview: the next/current trip, review-queue counts, and capture
    activity for the configured user."""
    return _client.dashboard()


@mcp.tool()
def review(bucket: str = "all") -> dict:
    """The user's capture review queue. bucket ∈ attention|waiting|ready|staged|
    logged|all."""
    return _client.review(bucket)


@mcp.tool()
def capture_url(url: str, travel_date: str | None = None,
                title: str | None = None, notes: str | None = None) -> dict:
    """Share a link into Yonder's capture pipeline (a booking webpage, or a
    Google Maps directions link — which needs travel_date). It lands in the
    review queue for the user to approve. Returns the batch id."""
    return _client.capture_url(url, travel_date=travel_date, title=title, notes=notes)


@mcp.tool()
def add_activity(trip_id: str, type: str, values: dict,
                 starts_at: str | None = None) -> dict:
    """Add a timeline item (a location, event, dining, lodging, flight, note, …)
    to a trip the user can edit. `type` is the activity type; `values` its
    fields (e.g. {"title": "...", "when": "2026-09-06T19:00"}); `starts_at`
    defaults to values.when."""
    return _client.add_activity(trip_id, type, values, starts_at=starts_at)


@mcp.tool()
def generate_packing(trip_id: str) -> dict:
    """Generate (or re-generate) the user's own packing plan for a trip and
    return it. Packing is per-user — this is the caller's list."""
    return _client.generate_packing(trip_id)


@mcp.tool()
def set_packing_status(trip_id: str, item_id: str, status: str) -> dict:
    """Check a packing item off (or back on): status ∈ packed|draft|removed.
    Only the caller's own packing lines."""
    return _client.set_packing_status(trip_id, item_id, status)


if __name__ == "__main__":
    mcp.run()
