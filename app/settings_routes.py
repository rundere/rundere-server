"""Settings hub (4.1): one place for everything that isn't daily use.

The top bar keeps only the daily surfaces (Home, Review, Capture, Trips,
Journal); every profile and administration page is an entry here instead of
its own header link. Administration entries show only to admins, or to everyone when
auth is off (the single-user world, same rule as app/scope.py).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import scope
from app.config import settings

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))

# The menu itself (entries, names, who sees which group) lives in ONE place,
# review/templates/_settings_menu.html, shared with the Settings sidebar that
# every settings page shows, so the two can never drift apart.


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", {
        "me": getattr(request.state, "user", None)})


@router.get("/settings/integrations", response_class=HTMLResponse)
def integrations_page(request: Request):
    """Optional integrations (SPEC-8 D-146): Run·dere is the first-party system and
    works with neither connected. Today AirTrail / AdventureLog are server-wide and
    admin-only (D-156); per-user connections (YONDER-192) will be set up here.
    Booleans only — never a URL or credential."""
    user = scope.user_of(request)
    return templates.TemplateResponse(request, "integrations.html", {
        "me": user,
        "can_use": scope.external_writes_allowed(user),
        "have_adventurelog": bool(settings.adventurelog_api_key),
        "have_airtrail": bool(settings.airtrail_token)})
