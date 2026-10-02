"""D-161 / YONDER-202: the web UI's palette comes from design/tokens."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app.db import engine
    from app.main import app
    from app.models import Base

    Base.metadata.create_all(engine)
    client = TestClient(app)

    def test_tokens_css_is_served():
        r = client.get("/brand/tokens/tokens.css")
        assert r.status_code == 200 and "--rd-bg" in r.text and "--rd-accent" in r.text

    def test_every_base_template_links_tokens_before_the_app_css():
        for path in ("/", "/trips", "/login"):
            html = client.get(path, follow_redirects=True).text
            assert "/brand/tokens/tokens.css" in html, path
            css = [m for m in ("/review/static/theme.css", "/trips/static/trips.css") if m in html]
            assert css, path
            assert all(html.index("/brand/tokens/tokens.css") < html.index(m) for m in css), path

    def test_sqladmin_base_links_tokens_first():
        t = (ROOT / "app/review/admin_templates/sqladmin/base.html").read_text()
        assert t.index("/brand/tokens/tokens.css") < t.index("/review/static/theme.css")

    def test_app_palettes_alias_the_tokens_not_literals():
        theme = (ROOT / "app/review/static/theme.css").read_text()
        trips = (ROOT / "app/trips/static/trips.css").read_text()
        palette = re.findall(r"--(?:ground|surface|ink|ink-2|ink-3|line|line-strong|accent|ok|warn|crit)\s*:\s*([^;]+);", theme)
        assert palette and all("--rd-" in v for v in palette), palette
        base = re.findall(r"--color-(?:bg|surface|text|accent|divider)\s*:\s*([^;]+);", trips)
        assert base and all("--rd-" in v for v in base), base
        # no per-theme re-declaration is left: tokens.css owns dark/light
        assert theme.count("--ground:") == 1 and trips.count("--color-bg:") == 1
