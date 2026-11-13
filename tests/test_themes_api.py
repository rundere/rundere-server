"""Run·dere 5.0 travel themes, server half (SPEC-8 §15, D-237–D-240, YONDER-371).

The theme and scenery switch are per account and own-only; the theme id is
checked against the generated manifest, never a closed list; /themes serves
that manifest with the scene files' URLs.
"""
import json
from urllib.parse import urlsplit
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
    from app import authn, themes
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)
    MANIFEST = json.loads((ROOT / "design" / "themes" / "themes.json").read_text())

    def _mk_user(username):
        with SessionLocal() as s:
            existing = s.query(User).filter_by(username=username).one_or_none()
            if existing:
                s.delete(existing)
                s.commit()
            u = User(username=username, password_hash=authn.hash_password("pw-theme-test"),
                     auth_source="local")
            s.add(u)
            s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _auth(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _stored(user_id):
        with SessionLocal() as s:
            u = s.get(User, user_id)
            return u.ui_theme, u.scenery

    def test_new_account_defaults_to_base_with_scenery():
        u = _mk_user("theme-default")
        me = client.get("/api/v1/auth/me", headers=_auth(u)).json()
        assert me["ui_theme"] == "base" and me["resolved_theme"] == "base"
        assert me["scenery"] is True

    def test_set_own_theme_and_scenery():
        u = _mk_user("theme-setter")
        r = client.patch("/api/v1/auth/me", headers=_auth(u), json={"ui_theme": "desert"})
        assert r.status_code == 200, r.text
        assert r.json()["ui_theme"] == "desert" and r.json()["resolved_theme"] == "desert"
        assert r.json()["scenery"] is True                     # untouched
        r = client.patch("/api/v1/auth/me", headers=_auth(u), json={"scenery": False})
        assert r.status_code == 200 and r.json()["scenery"] is False
        assert r.json()["ui_theme"] == "desert"                # untouched
        assert _stored(u.id) == ("desert", False)
        me = client.get("/api/v1/auth/me", headers=_auth(u)).json()
        assert (me["ui_theme"], me["scenery"]) == ("desert", False)

    def test_every_manifest_theme_is_selectable():
        """Open-ended (D-240): whatever the manifest holds is accepted."""
        u = _mk_user("theme-all")
        for t in MANIFEST["themes"]:
            r = client.patch("/api/v1/auth/me", headers=_auth(u), json={"ui_theme": t["id"]})
            assert r.status_code == 200 and r.json()["resolved_theme"] == t["id"], t["id"]

    def test_unknown_theme_rejected():
        u = _mk_user("theme-unknown")
        client.patch("/api/v1/auth/me", headers=_auth(u), json={"ui_theme": "coast"})
        for bad in ("jungle", "random", "next-trip", "", "DESERT", "x" * 40):
            r = client.patch("/api/v1/auth/me", headers=_auth(u), json={"ui_theme": bad})
            assert r.status_code == 422, (bad, r.text)
        assert _stored(u.id)[0] == "coast"

    def test_removed_theme_resolves_to_base():
        """A stored id whose package is gone still loads, drawn as Base (D-240)."""
        u = _mk_user("theme-removed")
        with SessionLocal() as s:
            s.get(User, u.id).ui_theme = "retired-theme"
            s.commit()
        me = client.get("/api/v1/auth/me", headers=_auth(u)).json()
        assert me["ui_theme"] == "retired-theme" and me["resolved_theme"] == "base"
        assert themes.resolve(None) == "base"

    def test_theme_is_own_only():
        """Another user (an admin included) can't read or change someone's theme:
        the payload is the caller's own and PATCH takes no user id."""
        alice = _mk_user("theme-alice")
        bob = _mk_user("theme-bob")
        with SessionLocal() as s:
            s.get(User, bob.id).is_admin = True
            s.commit()
        client.patch("/api/v1/auth/me", headers=_auth(alice), json={"ui_theme": "forest"})
        bob_me = client.patch("/api/v1/auth/me", headers=_auth(bob),
                              json={"ui_theme": "metro", "scenery": False,
                                    "id": alice.id, "username": "theme-alice"})
        assert bob_me.status_code == 200 and bob_me.json()["id"] == bob.id
        assert _stored(alice.id) == ("forest", True)
        assert _stored(bob.id) == ("metro", False)
        assert client.get("/api/v1/auth/me", headers=_auth(bob)).json()["ui_theme"] == "metro"

    def test_appearance_requires_auth():
        assert client.patch("/api/v1/auth/me", json={"ui_theme": "desert"}).status_code == 401
        assert client.get("/api/v1/themes").status_code == 401

    def test_token_response_carries_theme():
        u = _mk_user("theme-login")
        client.patch("/api/v1/auth/me", headers=_auth(u), json={"ui_theme": "winter"})
        r = client.post("/api/v1/auth/login",
                        json={"username": "theme-login", "password": "pw-theme-test"})
        assert r.status_code == 200 and r.json()["user"]["resolved_theme"] == "winter"

    def test_themes_manifest_served():
        u = _mk_user("theme-manifest")
        r = client.get("/api/v1/themes", headers=_auth(u))
        assert r.status_code == 200, r.text
        body = r.json()
        assert [t["id"] for t in body["themes"]] == [t["id"] for t in MANIFEST["themes"]]
        by_id = {t["id"]: t for t in body["themes"]}
        assert by_id["base"]["scene"] is None and by_id["base"]["kind"] == "base"
        for t in MANIFEST["themes"]:
            if t["kind"] != "travel":
                continue
            out = by_id[t["id"]]
            assert out["version"] == t["version"] and out["tags"] == t["tags"]
            assert out["modes"]["dark"]["color"]["accent"] == t["modes"]["dark"]["color"]["accent"]
            for mode in ("light", "dark"):
                url = out["scene"][f"{mode}_url"]
                assert url.endswith(f"/brand/themes/{t['id']}/scene-{mode}.svg")
                svg = client.get(urlsplit(url).path)
                assert svg.status_code == 200 and "<svg" in svg.text

    def test_themes_use_public_base_url(monkeypatch):
        monkeypatch.setattr(settings, "public_base_url", "https://trips.example.test/")
        u = _mk_user("theme-baseurl")
        body = client.get("/api/v1/themes", headers=_auth(u)).json()
        desert = next(t for t in body["themes"] if t["id"] == "desert")
        assert desert["scene"]["light_url"] == \
            "https://trips.example.test/brand/themes/desert/scene-light.svg"

    def test_ids_come_from_manifest_not_a_list():
        assert themes.ids() == [t["id"] for t in MANIFEST["themes"]]
        assert "base" in themes.ids()
