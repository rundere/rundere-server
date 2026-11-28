"""What this server can do, for apps to ask before they offer it (D-244).

`GET /api/v1/auth/methods` returns `capabilities`: a list of short, stable,
lowercase names. An app gates a feature on a name instead of guessing from a
404 or from the version number, so a build keeps working across server releases.

Rules for the list:
- A name is added when a feature ships and is never renamed or reused. The
  list only grows (an app may have been built against any earlier name).
- A name is present only when the feature works *on this server*: data export
  and account deletion need sign-in on and aren't on a demo server, so they are
  absent there even though the code exists.
- Unknown names are ignored by apps. Adding one is never a breaking change.
"""
from __future__ import annotations

from app.config import settings


def current() -> list[str]:
    from app import account_deletion, demo
    names = [
        "trip_pdf_flags",      # timeline.pdf honours times, addresses, maps, details (D-233)
        "release_notes",       # GET /release-notes and /release-notes/seen (D-234)
        "places_suggest",      # GET /places/suggest (SPEC-11)
        "places_lists",        # /places/lists (SPEC-11)
        "themes",              # GET /themes, ui_theme and scenery on /auth/me (D-237..D-240)
        "theme_scene_png",     # each theme's scene also published as PNG (D-245)
        "trip_create",         # POST /trips: the caller owns it and is on its roster (5.0.3)
        "trip_roster",         # GET/POST/DELETE /trips/{id}/travelers (5.0.3)
        "trip_preferences",    # trip_preferences on GET /trips/{id}: the roster's combined constraints (D-251)
        "trip_packing_controls",  # PUT /trips/{id}/packing/bags and /templates (YONDER-411)
    ]
    if account_deletion.available():
        names.append("account_delete")     # the web Delete page exists (D-242)
    if bool(settings.auth_required) and not demo.enabled():
        names.append("data_export")        # the web Export page exists (D-241)
    return names
