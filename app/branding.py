"""Product name, in one place (Run·dere 4.4, formerly Yonder — YONDER-155).

User-facing Python text reads BRAND. Templates spell "Run·dere" out: there are
a dozen Jinja environments, and a missed global would render an empty name.

BRAND_HISTORY is the continuity line for the 4.4.x release; drop it from the
pages that show it in the next minor release.

Internal identifiers stay as they are — the TX_ env prefix, cookie names, token
prefixes, the `yonder.*` target keys, the X-Yonder-* headers, logger names — so
existing deployments, tokens and installed bookmarklets keep working.
"""
from __future__ import annotations

BRAND = "Run·dere"
BRAND_HISTORY = "Run·dere (formerly Yonder)"
