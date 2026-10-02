"""Cache-busting URLs for the app's own static files.

The shell CSS (theme.css, trips.css, tokens, fonts, htmx) is served by StaticFiles with
no version in the URL, so a browser or reverse proxy that cached an old copy keeps
serving it after an upgrade: new HTML against old CSS (an unstyled Home, no sidebar).
`asset("/review/static/theme.css")` appends a short content hash, so a changed file is
a new URL and a stale cache can't be reused. The hash is computed once per process."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

_APP = Path(__file__).parent
_DESIGN = _APP.parent / "design"
_DIRS = (("/review/static/", _APP / "review" / "static"),
         ("/trips/static/", _APP / "trips" / "static"),
         ("/brand/tokens/", _DESIGN / "tokens"),
         ("/brand/fonts/", _DESIGN / "fonts"))


@lru_cache(maxsize=None)
def asset(url: str) -> str:
    for prefix, root in _DIRS:
        if url.startswith(prefix):
            try:
                digest = hashlib.sha1((root / url[len(prefix):]).read_bytes()).hexdigest()[:10]
            except OSError:
                return url              # missing file: plain URL, never break the page
            return f"{url}?v={digest}"
    return url
