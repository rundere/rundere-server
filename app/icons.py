"""Outline icons for the web UI (design/README §5: 24 grid, 1.75 stroke, round
caps and joins, outline only) — used instead of emoji, which the design system
forbids as icons. `icon("name")` returns inline SVG that takes `currentColor`;
templates get it as a Jinja global via `install(templates)`."""
from __future__ import annotations

from markupsafe import Markup

_PATHS = {
    # Capture: tray receiving a downward arrow
    "capture": '<path d="M12 3v10M8 9l4 4 4-4"/><path d="M4 14v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>',
    # Review: tray with a check
    "review": '<path d="M4 13l2-8h12l2 8"/><path d="M4 13v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5h-5l-1 2h-4l-1-2z"/>',
    "trips": '<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>',
    "journal": '<path d="M6 4h11a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H6z"/><path d="M6 4v16M9.5 8.5h5M9.5 12h5"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8"/>',
    "send": '<path d="M12 16V4M8 8l4-4 4 4"/><path d="M4 14v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>',
    "pin": '<path d="M12 21s-6-5.2-6-10a6 6 0 0 1 12 0c0 4.8-6 10-6 10z"/><circle cx="12" cy="11" r="2"/>',
    "lock": '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    "alert": '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17v.01"/>',
}


def icon(name: str, size: int = 20) -> Markup:
    body = _PATHS[name]
    return Markup(
        f'<svg class="rd-ico" viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
        f'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{body}</svg>')


def install(templates) -> None:
    templates.env.globals["icon"] = icon
