"""Tiny stdlib HTTP helpers for the discovery tier (SPEC-3-discovery.md §3).

A LEAF module: it imports nothing from `discover.resolvers` or `discover.search`,
so both of those can import it freely without the import cycle that arises when
`discover.search` (imported first) pulls in `discover.resolvers.base`, whose
package __init__ eagerly loads the resolvers that import `discover.search`.

`get_json` / `qs` used to live in `discover.resolvers.base`; they're re-exported
from there for backwards compatibility.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


def get_json(url: str, timeout: int = 12, retries: int = 1) -> dict | None:
    """GET a JSON body, or None on any failure — a resolver that can't reach its
    source degrades to a flagged/absent finding, never a 500.

    One retry by default: the public rate feeds (Frankfurter behind Cloudflare)
    flap intermittently, and a lone transient shouldn't silently drop a base
    currency or a whole topic."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Yonder-Discovery"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            if attempt >= retries:
                return None
    return None


def qs(**params) -> str:
    """Encode query params, dropping None — keeps resolver URL-building terse."""
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
