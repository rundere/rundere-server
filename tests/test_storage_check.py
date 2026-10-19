"""Start-up refuses to serve when a data path can't be written (YONDER-293).

The demo hit this: 4.12.9 run as uid 1000 on a demo.db an older root image had
made, so every sign-in failed with a 500. Read-only paths are simulated with
chmod; as root everything is writable anyway, so those tests skip.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import storage  # noqa: E402
from app.config import settings  # noqa: E402

needs_non_root = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                                    reason="root can write everything")


@pytest.fixture()
def layout(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "artifacts" / "ab").mkdir(parents=True)
    (data / "artifacts" / "tile-cache").mkdir()
    (data / "artifacts" / "tile-cache" / "t1.tile").write_bytes(b"x")
    (data / "docs" / "user-1").mkdir(parents=True)
    db = data / "demo.db"
    db.write_bytes(b"")
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db}")
    monkeypatch.setattr(settings, "artifacts_dir", str(data / "artifacts"))
    monkeypatch.setattr(settings, "map_tile_cache_dir", "")
    monkeypatch.setattr(settings, "documents_dir", str(data / "docs"))
    yield data
    for p in data.rglob("*"):                      # let tmp_path clean up
        os.chmod(p, 0o700 if p.is_dir() else 0o600)


def test_all_writable_starts(layout):
    assert storage.problems() == []
    storage.check_startup()


@needs_non_root
def test_read_only_database_refuses_to_start(layout):
    os.chmod(layout / "demo.db", 0o444)
    with pytest.raises(storage.NotWritable) as err:
        storage.check_startup()
    msg = str(err.value)
    assert "demo.db" in msg and "chown -R" in msg and f"{os.getuid()}:" in msg


@needs_non_root
def test_read_only_subfolder_and_tile_are_found(layout):
    os.chmod(layout / "artifacts" / "ab", 0o555)
    os.chmod(layout / "artifacts" / "tile-cache" / "t1.tile", 0o444)
    os.chmod(layout / "docs" / "user-1", 0o555)
    bad = {p.name for p in storage.problems()}
    assert bad == {"ab", "t1.tile", "user-1"}


def test_missing_folders_and_other_databases_are_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://u:p@db/x")
    monkeypatch.setattr(settings, "artifacts_dir", str(tmp_path / "not-yet"))
    monkeypatch.setattr(settings, "map_tile_cache_dir", "")
    monkeypatch.setattr(settings, "documents_dir", "")
    assert storage.problems() == []
