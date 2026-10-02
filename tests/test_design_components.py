"""YONDER-202 step 3: components follow the design system."""
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

APP = ROOT / "app"
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-⛿✀-➿⭐]️?")
# typographic marks (check, star, arrow) are text, not emoji icons
TYPOGRAPHIC = set("✓✔★➤✗✕")


def test_icons_are_outline_svg_and_unknown_names_fail():
    from app.icons import icon
    svg = str(icon("trips", 22))
    assert svg.startswith("<svg") and 'stroke-width="1.75"' in svg and 'fill="none"' in svg
    assert 'width="22"' in svg and "currentColor" in svg
    try:
        icon("nope")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass


def test_no_emoji_icons_in_templates():
    bad = []
    for f in list(APP.rglob("*.html")):
        for i, line in enumerate(f.read_text(encoding="utf8").splitlines(), 1):
            for m in EMOJI.findall(line):
                if m[0] not in TYPOGRAPHIC:
                    bad.append(f"{f.relative_to(ROOT)}:{i} {m}")
    assert not bad, bad


def test_blueprint_corner_crosses_are_gone():
    for f in APP.rglob("*.html"):
        assert 'class="corner' not in f.read_text(encoding="utf8"), f
    assert ".corner" not in (APP / "trips/static/trips.css").read_text()


def test_controls_are_44_with_a_32_small_size_and_a_danger_style():
    css = (APP / "review/static/theme.css").read_text()
    assert "min-height:var(--rd-control-md)" in css and "min-height:var(--rd-control-sm)" in css
    assert ".btn.btn-danger" in css and "color-mix(in srgb, var(--rd-danger) 50%, transparent)" in css


def test_account_page_has_no_competing_primaries():
    assert "act primary" not in (APP / "review/templates/account.html").read_text()


def test_admin_tables_take_the_palette():
    css = (APP / "review/static/admin-theme.css").read_text()
    assert ".table thead th" in css and "var(--surface-sunk)" in css and ".form-check-input" in css


if DEPS:
    from app.db import engine
    from app.main import app
    from app.models import Base

    Base.metadata.create_all(engine)
    client = TestClient(app)

    def test_home_renders_icons_not_emoji():
        html = client.get("/").text
        assert html.count('class="rd-ico"') >= 5
        assert not any(ch in html for ch in "📥🗂🧭📓⚙")

    def test_trip_delete_uses_the_destructive_style(monkeypatch):
        from app.db import SessionLocal
        from app.models import Trip
        with SessionLocal() as s:
            t = Trip(name="Design danger", source="manual", start_date="2099-01-01", end_date="2099-01-02")
            s.add(t); s.commit(); tid = t.id
        try:
            assert 'class="btn btn-danger"' in client.get(f"/trips/{tid}").text
        finally:
            with SessionLocal() as s:
                s.query(Trip).filter(Trip.id == tid).delete(); s.commit()
