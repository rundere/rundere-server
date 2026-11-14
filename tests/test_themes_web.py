"""Run·dere 5.0 travel themes on the web (SPEC-8 §15, D-237–D-240, YONDER-372).

Pages render the person's resolved theme server-side as data-travel, draw the
scenery floor last when it's on, and Account › Appearance sets both, for the
caller only. Signed-out pages stay Base and the CSP doesn't change.
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from app import authn, oauth
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    MANIFEST = json.loads((ROOT / "design" / "themes" / "themes.json").read_text())
    TRAVEL = [t["id"] for t in MANIFEST["themes"] if t["kind"] == "travel"]
    PW = "pw-theme-web-test"

    @pytest.fixture(autouse=True)
    def _sign_in_on(monkeypatch):
        # The suite starts with sign-in off (conftest); pages only know who is
        # looking, and so which theme to draw, when it's on.
        monkeypatch.setattr(settings, "auth_required", True)

    def _mk_user(username, ui_theme="base", scenery=True):
        with SessionLocal() as s:
            existing = s.query(User).filter_by(username=username).one_or_none()
            if existing:
                s.delete(existing)
                s.commit()
            u = User(username=username, password_hash=authn.hash_password(PW),
                     auth_source="local", ui_theme=ui_theme, scenery=scenery,
                     email=f"{username}@example.test")
            s.add(u)
            s.commit()
            return u.id

    def _web(username):
        c = TestClient(app)
        r = c.post("/login", data={"username": username, "password": PW, "next": "/"},
                   follow_redirects=False)
        assert r.status_code == 303, r.text
        return c

    def _stored(user_id):
        with SessionLocal() as s:
            u = s.get(User, user_id)
            return u.ui_theme, u.scenery

    def _travel_attr(html):
        m = re.search(r'<html[^>]*data-travel="([^"]*)"', html)
        return m.group(1) if m else None

    def test_base_account_renders_base_without_scenery():
        _mk_user("tw-base")
        html = _web("tw-base").get("/account").text
        assert _travel_attr(html) == "base"
        assert 'class="rd-floor"' not in html and "has-scenery" not in html

    def test_travel_theme_renders_server_side_with_floor_last():
        _mk_user("tw-desert", ui_theme="desert")
        c = _web("tw-desert")
        for path in ("/account", "/trips"):
            html = c.get(path).text
            assert _travel_attr(html) == "desert", path
            assert "has-scenery" in html
            floor = html.index('class="rd-floor"')
            # The floor is the page's last element: after the shell, before </body>.
            assert html.index("</div>", html.index('id="modal-root"')) < floor
            assert html[floor:].count("<svg") == 1
            assert re.search(r"</svg>\s*</div>\s*</body>", html[floor:]), path
            assert 'aria-hidden="true"' in html[floor:floor + 200]
            assert "var(--rd-scene-" in html[floor:]          # token-coloured, both modes

    def test_scenery_off_hides_the_floor_but_keeps_the_theme():
        _mk_user("tw-noscene", ui_theme="forest", scenery=False)
        html = _web("tw-noscene").get("/account").text
        assert _travel_attr(html) == "forest"
        assert 'class="rd-floor"' not in html and "has-scenery" not in html

    def test_removed_theme_renders_base():
        _mk_user("tw-gone", ui_theme="retired-theme")
        html = _web("tw-gone").get("/account").text
        assert _travel_attr(html) == "base" and 'class="rd-floor"' not in html

    def test_theme_applies_with_sign_in_optional(monkeypatch):
        """TX_AUTH_REQUIRED off: a signed-in person still gets their theme."""
        _mk_user("tw-optional", ui_theme="coast")
        c = _web("tw-optional")
        monkeypatch.setattr(settings, "auth_required", False)
        assert _travel_attr(c.get("/account").text) == "coast"

    def test_signed_out_pages_stay_base():
        html = TestClient(app).get("/login").text
        assert "data-travel" not in html or _travel_attr(html) == "base"
        assert 'class="rd-floor"' not in html

    def test_picker_lists_every_manifest_theme():
        _mk_user("tw-picker", ui_theme="metro")
        html = _web("tw-picker").get("/account").text
        assert 'id="appearance"' in html
        for t in MANIFEST["themes"]:
            assert f'name="ui_theme" value="{t["id"]}"' in html, t["id"]
        assert re.search(r'value="metro" checked', html)
        assert re.search(r'name="scenery" value="1" checked', html)

    def test_set_theme_and_scenery_from_account():
        uid = _mk_user("tw-setter")
        c = _web("tw-setter")
        r = c.post("/account/appearance", data={"ui_theme": "winter", "scenery": "1"},
                   follow_redirects=False)
        assert r.status_code == 303 and "msg=" in r.headers["location"]
        assert _stored(uid) == ("winter", True)
        assert _travel_attr(c.get("/account").text) == "winter"
        # An unticked box sends nothing: scenery off.
        c.post("/account/appearance", data={"ui_theme": "winter"}, follow_redirects=False)
        assert _stored(uid) == ("winter", False)

    def test_every_travel_theme_is_selectable_and_draws_its_floor():
        _mk_user("tw-all")
        c = _web("tw-all")
        for tid in TRAVEL:
            c.post("/account/appearance", data={"ui_theme": tid, "scenery": "1"},
                   follow_redirects=False)
            html = c.get("/account").text
            assert _travel_attr(html) == tid and 'class="rd-floor"' in html, tid

    def test_unknown_theme_rejected():
        uid = _mk_user("tw-unknown", ui_theme="coast")
        c = _web("tw-unknown")
        for bad in ("jungle", "random", "", "../coast"):
            r = c.post("/account/appearance", data={"ui_theme": bad, "scenery": "1"},
                       follow_redirects=False)
            assert r.status_code == 303 and "err=" in r.headers["location"], bad
        assert _stored(uid) == ("coast", True)

    def test_appearance_is_own_only():
        """Setting yours never touches anyone else's, an admin's included."""
        alice = _mk_user("tw-alice", ui_theme="forest")
        bob = _mk_user("tw-bob")
        with SessionLocal() as s:
            s.get(User, bob).is_admin = True
            s.commit()
        c = _web("tw-bob")
        c.post("/account/appearance", data={"ui_theme": "metro", "user_id": alice,
                                            "username": "tw-alice"}, follow_redirects=False)
        assert _stored(alice) == ("forest", True)
        assert _stored(bob) == ("metro", False)
        assert _travel_attr(_web("tw-alice").get("/account").text) == "forest"

    def test_api_tokens_cannot_change_appearance():
        uid = _mk_user("tw-pat", ui_theme="coast")
        with SessionLocal() as s:
            pat = oauth.create_pat(s, s.get(User, uid), "laptop")[1]
        r = TestClient(app).post("/account/appearance", data={"ui_theme": "metro"},
                                 headers={"Authorization": f"Bearer {pat}"},
                                 follow_redirects=False)
        assert r.status_code == 403
        assert _stored(uid) == ("coast", True)

    def test_csp_unchanged_by_a_travel_theme():
        _mk_user("tw-csp-base")
        _mk_user("tw-csp-travel", ui_theme="desert")
        base = _web("tw-csp-base").get("/account").headers.get("content-security-policy")
        travel = _web("tw-csp-travel").get("/account").headers.get("content-security-policy")
        assert base and base == travel
        assert "script-src 'self'" in travel and "unsafe-eval" not in travel

    def test_floor_keeps_the_scene_shape():
        """A fixed-height floor sliced the top off the art on wide windows; the
        floor's aspect ratio must match every scene's viewBox (D-239)."""
        css = (ROOT / "app" / "review" / "static" / "theme.css").read_text()
        m = re.search(r"\.rd-floor\{[^}]*aspect-ratio:(\d+)/(\d+)", css)
        assert m, "the floor has no aspect-ratio"
        for tid in TRAVEL:
            svg = (ROOT / "design" / "themes" / tid / "scene.svg").read_text()
            w, h = map(float, re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg).groups())
            assert w / h == int(m.group(1)) / int(m.group(2)), tid
