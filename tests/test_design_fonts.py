"""YONDER-202 step 2: brand fonts are self-hosted and wired into the shared CSS."""
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

    def test_fonts_css_and_files_are_served():
        css = client.get("/brand/fonts/fonts.css")
        assert css.status_code == 200
        for fam in ("Outfit", "Figtree", "JetBrains Mono"):
            assert f'font-family:"{fam}"' in css.text
        # every url() in fonts.css resolves to a real woff2
        for name in set(re.findall(r"url\(([^)]+\.woff2)\)", css.text)):
            r = client.get(f"/brand/fonts/{name}")
            assert r.status_code == 200 and r.content[:4] == b"wOF2", name

    def test_pages_link_fonts_after_tokens_and_never_a_cdn():
        for path in ("/", "/trips", "/login"):
            html = client.get(path, follow_redirects=True).text
            assert html.index("/brand/tokens/tokens.css") < html.index("/brand/fonts/fonts.css"), path
            assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html, path

    def test_app_css_uses_the_brand_stacks():
        theme = (ROOT / "app/review/static/theme.css").read_text()
        trips = (ROOT / "app/trips/static/trips.css").read_text()
        assert "--ui:var(--rd-font-body)" in theme and "--mono:var(--rd-font-mono)" in theme
        assert "--font-body: var(--rd-font-body)" in trips
        # Outfit (heading font) is for headings of 20px and up only
        for m in re.finditer(r"([^{}]+)\{([^}]*var\(--font-heading\)[^}]*)\}", trips):
            sizes = re.findall(r"font-size:\s*([\d.]+)(px|rem)", m.group(2))
            for n, unit in sizes:
                px = float(n) * (16 if unit == "rem" else 1)
                assert px >= 20, (m.group(1).strip(), px)

    def test_dockerfile_copies_the_fonts():
        assert "COPY design/fonts/ design/fonts/" in (ROOT / "Dockerfile").read_text()
