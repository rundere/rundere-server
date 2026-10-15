"""Get the app, and who to ask: the public About page and the sign-in page
(YONDER-278, SPEC-8 D-202; the add-server link is YONDER-260).

One "Add to the Run·dere app" link serves every phone: both the iOS app and the
Android app (YONDER-282) handle `rundere://add-server`. Only the install links
differ by platform. Every link here comes from the operator's settings; anything
that isn't https:// (or, for the contact, an email address) is ignored, so a
typo can't put a javascript: or plain-http link on a public page.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from app.config import settings

_EMAIL = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+$")
DEMO_NAME = "Run·dere demo"


def origin(request) -> str:
    """scheme://host[:port] this page was served on. Not TX_PUBLIC_BASE_URL:
    sign-in differs per host (YONDER-164), so the app must get the host the
    person is actually on."""
    u = request.base_url
    return f"{u.scheme}://{u.netloc}"


def _https(url: str) -> str:
    url = (url or "").strip()
    return url if url.lower().startswith("https://") and " " not in url else ""


def add_server_link(request) -> str:
    """rundere://add-server?url=<origin>[&name=…]. Never anything else: no
    username, token or key."""
    from app import demo
    link = "rundere://add-server?url=" + quote(origin(request), safe="")
    if demo.enabled():
        link += "&name=" + quote(DEMO_NAME, safe="")
    return link


def get_app(request) -> dict:
    """What the "Get the app" rows show on this host."""
    store = _https(settings.ios_app_store_url)
    return {
        "add_server": add_server_link(request),
        # The App Store replaces TestFlight once the app is public.
        "ios_store": store,
        "ios_testflight": "" if store else _https(settings.ios_testflight_url),
        "android": _https(settings.android_app_url),
    }


def server_contact() -> dict | None:
    """{"href", "label"} for "Questions about this server", or None."""
    c = (settings.server_contact or "").strip()
    if _EMAIL.match(c):
        return {"href": "mailto:" + c, "label": c}
    url = _https(c)
    return {"href": url, "label": url.removeprefix("https://").rstrip("/")} if url else None
