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

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "review" / "templates"))

# (title, href, one-line description, needs a signed-in user)
_YOU = [
    ("Account", "/account", "Change your password and the email reset links go to.", True),
    ("Travel profile", "/trips/settings/travel-profile",
     "Nationality, base currency, names on tickets.", True),
    ("Home profiles", "/review/home",
     "Where you've lived, so trips from home aren't logged as visits.", False),
    ("Packing profile", "/trips/settings/packing-profile",
     "What you always bring and how you pack.", False),
    ("Discovery profile", "/trips/settings/discovery-profile",
     "Food, activity and pace preferences for trip research.", False),
]

_ADMIN = [
    ("Users", "/users", "Accounts, roles, password resets, email delivery."),
    ("Travellers", "/admin/traveler-identity/list", "The roster accounts link to."),
    ("Rules", "/admin/rule-setting/list", "Which review rules enforce, audit or are off."),
    ("Source rules", "/admin/source-rule/list", "How captured emails are routed."),
    ("API keys", "/admin/api-key/list", "Capture clients (Apps Script, add-ons)."),
    ("Packing catalog", "/admin/packing-catalog-item/list", "Items packing plans draw from."),
    ("Bag inventory", "/admin/bag/list", "The bags items can be packed into."),
    ("Phrasebook", "/admin/phrasebook-phrase/list", "Phrases for the language findings."),
    ("Regional languages", "/admin/regional-language/list",
     "Languages spoken by region, for discovery."),
]


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user = getattr(request.state, "user", None)
    signed_in = user is not None
    show_admin = user is None or bool(getattr(user, "is_admin", False))
    you = [{"title": t, "href": h, "desc": d} for t, h, d, needs_user in _YOU
           if signed_in or not needs_user]
    admin = ([{"title": t, "href": h, "desc": d} for t, h, d in _ADMIN
              if signed_in or h != "/users"] if show_admin else [])
    return templates.TemplateResponse(request, "settings.html", {
        "you": you, "admin": admin, "me": user})
