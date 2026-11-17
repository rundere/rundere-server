"""Run·dere — what changed in each version, shown once after an upgrade (SPEC-8 §14, D-234).

The notes are hand-written in `app/release_notes.md`, which ships in the image, so
nothing is fetched at run time (D-146). This module reads that file and decides
what a person is shown:

- `unseen(user)`: every version newer than the person's `seen_version`, up to the
  running one, newest first, at most `MAX_SHOWN`. An account with no
  `seen_version` (from before 5.0) gets only the running version.
- "For admins" items only for admins; a version with nothing left for the
  person is dropped, and if nothing is left at all the dialog doesn't show.
- `mark_seen(user)` records the running version, from the web dialog or the apps
  (`POST /api/v1/release-notes/seen`), so nobody is shown the same notes twice.

Items are short Markdown strings; `render` turns **bold**, `code` and
[links](https://…) into HTML and escapes everything else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup, escape

from app.config import settings

PATH = Path(__file__).with_name("release_notes.md")
MAX_SHOWN = 5                  # versions in the dialog; the rest are on the About page

_HEAD = re.compile(r"^##\s+v?(\d+\.\d+\.\d+)(?:\s+[—-]\s+(\d{4}-\d{2}-\d{2}))?\s*$")
_AUDIENCE = {"for everyone": "everyone", "for admins": "admins"}


@dataclass
class Version:
    version: str
    date: str | None = None
    everyone: list[str] = field(default_factory=list)
    admins: list[str] = field(default_factory=list)

    def to_dict(self, *, admin: bool) -> dict:
        return {"version": self.version, "date": self.date, "everyone": list(self.everyone),
                "admins": list(self.admins) if admin else []}


def vkey(version: str | None) -> tuple[int, ...]:
    """'4.12.10' -> (4, 12, 10), so 4.12.10 sorts after 4.12.9. Junk sorts first."""
    try:
        return tuple(int(p) for p in (version or "").strip().lstrip("v").split("."))
    except ValueError:
        return ()


def parse(text: str) -> list[Version]:
    """Every version in the notes file, newest first. HTML comments are skipped,
    and so is anything outside a `## X.Y.Z` section or an audience heading."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    out: list[Version] = []
    cur: Version | None = None
    audience = None
    for line in text.splitlines():
        m = _HEAD.match(line)
        if m:
            cur = Version(m.group(1), m.group(2))
            out.append(cur)
            audience = None
            continue
        if line.startswith("### ") and cur is not None:
            audience = _AUDIENCE.get(line[4:].strip().lower())
            continue
        if line.startswith("- ") and cur is not None and audience:
            getattr(cur, audience).append(line[2:].strip())
    return sorted(out, key=lambda v: vkey(v.version), reverse=True)


@lru_cache(maxsize=4)
def _cached(mtime: float) -> tuple[Version, ...]:
    return tuple(parse(PATH.read_text(encoding="utf-8")))


def versions() -> list[Version]:
    """The notes file, parsed once per change of the file."""
    try:
        return list(_cached(PATH.stat().st_mtime))
    except OSError:                          # a broken image never takes a page down
        return []


def _is_admin(user) -> bool:
    return bool(getattr(user, "is_admin", False))


def released(running: str | None = None) -> list[Version]:
    """Versions up to the running one: a section for a version not out yet stays
    hidden until the server runs it."""
    top = vkey(running or settings.app_version)
    return [v for v in versions() if vkey(v.version) <= top]


def for_person(vs: list[Version], user) -> list[dict]:
    """The versions as this person sees them: admin items only for admins, and a
    version with nothing for them left out."""
    admin = _is_admin(user)
    out = [v.to_dict(admin=admin) for v in vs]
    return [d for d in out if d["everyone"] or d["admins"]]


def unseen(user, running: str | None = None) -> list[dict]:
    """What the dialog shows this person: newest first, at most MAX_SHOWN."""
    if user is None:
        return []
    running = running or settings.app_version
    seen = getattr(user, "seen_version", None)
    if seen and vkey(seen) >= vkey(running):
        return []
    floor = vkey(seen) if seen else None
    vs = [v for v in released(running)
          if (vkey(v.version) > floor if floor is not None else v.version == running)]
    return for_person(vs, user)[:MAX_SHOWN]


def mark_seen(session, user, running: str | None = None) -> None:
    """Record that this person has seen everything up to the running version."""
    from app.models import User
    row = session.get(User, user.id)
    if row is not None:
        row.seen_version = running or settings.app_version
        session.commit()


_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https://[^\s)]+)\)")


def render(item: str) -> Markup:
    """One note item as HTML: **bold**, `code` and [links](https://…) only, the
    rest escaped. Links open in a new tab."""
    html = str(escape(item))
    html = _CODE.sub(r"<code>\1</code>", html)
    html = _BOLD.sub(r"<strong>\1</strong>", html)
    html = _LINK.sub(r'<a href="\2" target="_blank" rel="noopener">\1</a>', html)
    return Markup(html)


def dialog_notes(request) -> list[dict]:
    """For the app header: the notes to show on this full page load, or []. Never
    on a demo server (D-198) or with nobody signed in; never raises."""
    try:
        from app import demo
        if demo.enabled():
            return []
        user = getattr(getattr(request, "state", None), "user", None)
        notes = unseen(user)
        seen = getattr(user, "seen_version", None)
        if user is not None and not notes and (seen is None or vkey(seen) < vkey(settings.app_version)):
            # Nothing in these versions for this person (admin-only notes for
            # someone who isn't): record them as seen without a dialog.
            from app.db import SessionLocal
            with SessionLocal() as session:
                mark_seen(session, user)
            user.seen_version = settings.app_version
        return notes
    except Exception:                        # pragma: no cover - the page matters more
        return []
