"""Research kits (4.5, YONDER-170): reusable agent instructions that research
part of a trip and save it to Discover over MCP. Each kit is a skill folder —
research_kits/<slug>/SKILL.md with a `name`/`description` header — served as
an MCP prompt, over the API, and as a zip to add to agents that take skills."""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

KITS_DIR = Path(__file__).parent.parent / "research_kits"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


@dataclass(frozen=True)
class Kit:
    slug: str                 # folder name, e.g. "food-and-drink"
    name: str                 # skill name from the header
    description: str
    body: str                 # the instructions, header stripped
    text: str                 # the whole SKILL.md

    @property
    def title(self) -> str:
        m = re.search(r"^# .*?—\s*(.+)$", self.body, re.M)
        return m.group(1).strip() if m else self.slug.replace("-", " ").capitalize()


def _parse(slug: str, text: str) -> Kit:
    meta: dict[str, str] = {}
    body = text
    m = _FRONT.match(text)
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep:
                meta[k.strip()] = v.strip()
    return Kit(slug=slug, name=meta.get("name") or slug,
               description=meta.get("description", ""), body=body.lstrip(), text=text)


@lru_cache(maxsize=1)
def all_kits() -> tuple[Kit, ...]:
    if not KITS_DIR.is_dir():
        return ()
    out = []
    for d in sorted(KITS_DIR.iterdir()):
        f = d / "SKILL.md"
        if d.is_dir() and _SLUG.match(d.name) and f.is_file():
            out.append(_parse(d.name, f.read_text(encoding="utf-8")))
    return tuple(out)


def get(slug: str) -> Kit | None:
    return next((k for k in all_kits() if k.slug == slug), None)


def zip_bytes(kit: Kit) -> bytes:
    """The kit as a skill zip: <name>/SKILL.md plus any other files in its folder."""
    buf = io.BytesIO()
    folder = KITS_DIR / kit.slug
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(folder.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                z.write(f, f"{kit.name}/{f.relative_to(folder).as_posix()}")
    return buf.getvalue()


def prompt_text(kit: Kit, trip_id: str, place_key: str | None = None) -> str:
    """The kit as a ready-to-send prompt for one trip (and optionally one city)."""
    scope = (f"Only research the city `{place_key}`." if place_key
             else "Research every city in the trip's `places`.")
    return f"Run·dere trip id: `{trip_id}`. {scope}\n\n{kit.body}"
