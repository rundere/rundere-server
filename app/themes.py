"""Run·dere travel themes on the server (SPEC-8 §15, D-237–D-240).

A theme is a package under `design/themes/<id>/`; `scripts/build_tokens.py`
writes their manifest to `design/themes/themes.json`. The server never keeps
its own list of themes: the ids a person may choose are whatever the manifest
holds, so a new package needs no migration and no app release (D-240).

- `ids()`: the theme ids the manifest offers (always includes `base`).
- `resolve(ui_theme)`: the concrete theme to draw. In 5.0 that is the stored id,
  or `base` for an unknown or removed one (D-240).
- `manifest(base_url)`: the manifest for `GET /api/v1/themes`, with absolute
  URLs for each travel theme's fixed-colour scene files.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

BASE = "base"
THEMES_DIR = Path(__file__).resolve().parent.parent / "design" / "themes"
MANIFEST = THEMES_DIR / "themes.json"
# Where main.py serves design/themes/ (scene SVGs for the apps, D-239).
STATIC_PREFIX = "/brand/themes"


@lru_cache(maxsize=1)
def _load() -> dict:
    """The generated manifest, read once. A missing or unreadable manifest
    leaves only Base, so the server still runs (and everyone gets Base)."""
    try:
        data = json.loads(MANIFEST.read_text())
        themes = [t for t in data.get("themes", []) if isinstance(t, dict) and t.get("id")]
    except (OSError, ValueError):
        themes = []
    if not any(t["id"] == BASE for t in themes):
        themes.insert(0, {"id": BASE, "name": "Base", "kind": "base", "version": 1,
                          "description": "", "tags": [], "modes": {}})
    return {"version": 1, "themes": themes}


def ids() -> list[str]:
    return [t["id"] for t in _load()["themes"]]


def is_known(theme_id: str | None) -> bool:
    return bool(theme_id) and theme_id in ids()


def resolve(ui_theme: str | None) -> str:
    """D-240: the server turns `ui_theme` into the theme to draw. After 5.0 this
    is where `random` and `next-trip` would be resolved; for now it is the stored
    id, falling back to Base when the package is gone."""
    return ui_theme if is_known(ui_theme) else BASE


def manifest(base_url: str) -> dict:
    """The manifest with each travel theme's scene files as absolute URLs, so an
    app can fetch a theme newer than its build (D-240). Base has no scenery."""
    base_url = base_url.rstrip("/")
    out = []
    for t in _load()["themes"]:
        item = {k: t.get(k) for k in ("id", "name", "kind", "version", "description")}
        item["tags"] = list(t.get("tags") or [])
        item["modes"] = t.get("modes") or {}
        scene = t.get("scene")
        if scene:
            item["scene"] = {
                "title": scene.get("title", ""),
                "light_url": f"{base_url}{STATIC_PREFIX}/{scene['light']}",
                "dark_url": f"{base_url}{STATIC_PREFIX}/{scene['dark']}",
            }
        else:
            item["scene"] = None
        out.append(item)
    return {"version": _load()["version"], "themes": out}
