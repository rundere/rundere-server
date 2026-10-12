"""Owner feedback 2026-10-12: two settings screens that didn't follow the theme.

- YONDER-216: Travel profile uses the same profile-card layout as Discovery and
  Packing, not the bare review-style form.
- YONDER-217: SQLAdmin edit forms put Tabler's fieldset on the palette, so the
  labels are readable in the dark theme."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False


def test_admin_theme_maps_tablers_surfaces():
    css = (ROOT / "app" / "review" / "static" / "admin-theme.css").read_text()
    assert "--tblr-bg-surface-secondary:var(--surface-sunk)" in css
    assert ".form-fieldset{" in css and ".col-form-label{color:var(--ink)}" in css.replace(
        ".form-label,.col-form-label", ".col-form-label")


if DEPS:
    from app.main import app
    client = TestClient(app)

    def test_travel_profile_uses_the_profile_card_layout():
        html = client.get("/trips/settings/travel-profile").text
        if "isn't linked to a traveller" in html:
            assert "card blueprint" in html
            return
        assert "yn-profile-card" in html and 'class="btn btn-primary"' in html
        assert 'class="ops"' not in html                 # the old review-style shell
