"""Static files are linked with a content hash and revalidate, so an upgrade can't leave a
browser on old CSS beside new HTML (the unstyled Home / missing sidebar)."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_asset_url_carries_a_content_hash_that_changes_with_the_file(tmp_path, monkeypatch):
    from app import assets
    d = tmp_path / "static"; d.mkdir()
    (d / "x.css").write_text("a{}")
    monkeypatch.setattr(assets, "_DIRS", (("/t/", d),))
    assets.asset.cache_clear()
    first = assets.asset("/t/x.css")
    assert re.fullmatch(r"/t/x\.css\?v=[0-9a-f]{10}", first)
    (d / "x.css").write_text("a{color:red}")
    assets.asset.cache_clear()
    assert assets.asset("/t/x.css") != first
    assert assets.asset("/t/missing.css") == "/t/missing.css"      # never breaks the page
    assert assets.asset("/other/x.css") == "/other/x.css"


def test_pages_link_hashed_css_and_static_revalidates():
    from fastapi.testclient import TestClient

    from app.db import engine
    from app.main import app
    from app.models import Base
    Base.metadata.create_all(engine)
    c = TestClient(app)
    html = c.get("/").text
    for name in ("/review/static/theme.css", "/brand/tokens/tokens.css"):
        assert re.search(re.escape(name) + r"\?v=[0-9a-f]{10}", html), name
    r = c.get("/review/static/theme.css")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_trip_page_width_does_not_depend_on_the_active_tab():
    css = (ROOT / "app/trips/static/trips.css").read_text()
    assert 'data-tab="discovery"]) { max-width' not in css
    assert ".bp-wrap:has(.yn-tabs) { max-width: 1200px; }" in css
