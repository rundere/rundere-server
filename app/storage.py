"""Start-up check that the server can write where it keeps data (YONDER-293).

Since 4.12.8 the app runs unprivileged, often as the host's own uid
(`user: "1000:1000"`). Files an older image made as root stay root's, and the
first write to them failed at request time: a SQLite database read-only to the
app answered every sign-in with a 500. Now the server checks before it serves
and, if anything is in the way, refuses to start with each path, its owner and
what to run.

Checked, when they exist (a missing folder is created later by the code that
uses it):
- a SQLite database file and its folder (SQLite writes a journal beside it);
- every FOLDER under the artifacts, documents and tile-cache folders: new files
  land in them, content-addressed subfolders included;
- every file in the tile cache, where an expired tile is rewritten in place.
Stored artifacts and documents are written once and only read after that, so
their files aren't checked one by one.
"""
from __future__ import annotations

import os
from pathlib import Path


class NotWritable(RuntimeError):
    """The server can't write where it keeps data. The message lists the paths."""


def _sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///"):]
    if not raw or raw.startswith(":memory:"):
        return None
    return Path(raw.split("?", 1)[0])


def _folders(root: Path):
    yield root
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            yield Path(dirpath) / d


def problems() -> list[Path]:
    """Every existing data path this process can't write, in a stable order."""
    from app.config import settings
    bad: list[Path] = []

    def check(p: Path) -> None:
        if p.exists() and not os.access(p, os.W_OK):
            bad.append(p)

    db = _sqlite_path(settings.database_url)
    if db is not None:
        check(db)
        check(db.parent if str(db.parent) else Path("."))
    tiles = Path(settings.map_tile_cache_dir or Path(settings.artifacts_dir) / "tile-cache")
    roots = [Path(settings.artifacts_dir), tiles]
    if (settings.documents_dir or "").strip():
        roots.append(Path(settings.documents_dir.strip()))
    for root in roots:
        if root.is_dir():
            for folder in _folders(root):
                check(folder)
    if tiles.is_dir():
        for f in sorted(tiles.iterdir()):
            if f.is_file():
                check(f)
    seen: set[Path] = set()
    return [p for p in bad if not (p in seen or seen.add(p))]


def _owner(p: Path) -> str:
    try:
        st = p.stat()
        return f"{st.st_uid}:{st.st_gid}"
    except OSError:
        return "?"


def check_startup() -> None:
    """Raise NotWritable, naming every path and the fix, if any data path
    can't be written by this process."""
    bad = problems()
    if not bad:
        return
    me = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "this user"
    shown = bad[:20]
    lines = [f"  {p}  (owned by {_owner(p)})" for p in shown]
    if len(bad) > len(shown):
        lines.append(f"  … and {len(bad) - len(shown)} more")
    folders = sorted({str(p if p.is_dir() else p.parent) for p in bad})[:5]
    raise NotWritable(
        f"Run·dere runs as {me} but can't write {len(bad)} data path(s) "
        "(as the app sees them; in a container, the host folders mounted there):\n"
        + "\n".join(lines)
        + "\nFiles left by an older image that ran as root are the usual cause. "
          f"Give them to {me}, e.g. on the host: sudo chown -R {me} "
        + " ".join(folders))


if __name__ == "__main__":                  # the image runs this before migrations
    import sys
    try:
        check_startup()
    except NotWritable as err:
        print(f"rundere: {err}", file=sys.stderr)
        sys.exit(1)
