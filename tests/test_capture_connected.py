"""YONDER-257 — the Capture page shows what's connected: signed-in Run·dere
apps on the iPhone/iPad card and guide, AI assistants and API tokens on the
AI-assistants card and guide, each person's own only; Disconnect from a guide
returns to it. iPhone and iPad is no longer "Coming soon" (YONDER-54 shipped)."""
import sys
import uuid
from datetime import datetime, timedelta, timezone
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
    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import (Base, OAuthClient, OAuthGrant, PersonalApiToken, RefreshToken,
                            User)

    Base.metadata.create_all(engine)
    client = TestClient(app)
    NOW = datetime.now(timezone.utc)

    @pytest.fixture(autouse=True)
    def env(monkeypatch):
        monkeypatch.setattr(settings, "auth_required", True)
        yield
        with SessionLocal() as s:
            ids = [u.id for u in s.query(User).filter(User.username.like("cc-%"))]
            for m in (OAuthGrant, PersonalApiToken, RefreshToken):
                s.query(m).filter(m.user_id.in_(ids)).delete(synchronize_session=False)
            s.query(OAuthClient).filter(OAuthClient.id.like("cc-%")).delete(
                synchronize_session=False)
            s.query(User).filter(User.id.in_(ids)).delete(synchronize_session=False)
            s.commit()

    def _user(name):
        with SessionLocal() as s:
            u = User(username=name, auth_source="local")
            s.add(u)
            s.commit()
            s.refresh(u)
            s.expunge(u)
            return u

    def _h(u):
        return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    def _client(cid, name, first_party=False):
        with SessionLocal() as s:
            if s.get(OAuthClient, cid) is None:
                s.add(OAuthClient(id=cid, name=name, redirect_uris=["https://x.example/cb"],
                                  source="builtin" if first_party else "dcr",
                                  first_party=first_party))
                s.commit()

    def _grant(u, cid, used=None):
        with SessionLocal() as s:
            g = OAuthGrant(client_id=cid, user_id=u.id, token_version=u.token_version,
                           expires_at=NOW + timedelta(days=30), last_used_at=used)
            s.add(g)
            s.commit()
            return g.id

    def _setup():
        me, other = _user(f"cc-me-{uuid.uuid4().hex[:5]}"), _user(f"cc-other-{uuid.uuid4().hex[:5]}")
        _client("cc-ios", "Run·dere for iOS", first_party=True)
        _client("cc-claude", "Claude")
        ios = _grant(me, "cc-ios", used=NOW - timedelta(hours=3))
        claude = _grant(me, "cc-claude", used=NOW - timedelta(minutes=10))
        theirs = _grant(other, "cc-claude")
        with SessionLocal() as s:
            s.add(RefreshToken(user_id=me.id, token_hash=uuid.uuid4().hex,
                               expires_at=NOW + timedelta(days=30),
                               user_agent="Rundere/0.8 (iPad; iPadOS 18) CFNetwork"))
            s.add(RefreshToken(user_id=me.id, token_hash=uuid.uuid4().hex,      # rotated away
                               expires_at=NOW + timedelta(days=30), revoked_at=NOW))
            s.add(PersonalApiToken(user_id=me.id, name="Claude Code", token_hash=uuid.uuid4().hex,
                                   token_version=me.token_version))
            s.commit()
        return me, other, ios, claude, theirs

    def test_cards_show_what_is_connected_and_ios_is_available():
        me, other, *_ = _setup()
        page = client.get("/capture", headers=_h(me)).text
        assert "Coming soon" not in page
        assert "Connected · 2 devices" in page                  # app grant + password session
        assert "Run·dere for iOS, Run·dere on iPad (password sign-in) · last used just now" in page
        assert "Connected" in page and "Claude, API token “Claude Code” · last used 10 minutes ago" in page
        assert "Connected ·" not in client.get("/capture", headers=_h(other)).text.split(
            "AI assistants")[0].split("iPhone and iPad")[1]

    def test_guides_list_connections_with_disconnect_back_to_the_guide():
        me, other, ios, claude, theirs = _setup()
        guide = client.get("/capture/setup?tab=ios", headers=_h(me)).text
        assert "Not available yet" not in guide and "Share → Run·dere" in guide
        assert "Your signed-in devices" in guide and "Run·dere for iOS" in guide
        assert "Run·dere on iPad (password sign-in)" in guide
        assert f'/account/connected-apps/{ios}/revoke' in guide
        mcp = client.get("/capture/setup?tab=mcp", headers=_h(me)).text
        assert "Connected to your account" in mcp and "Claude" in mcp
        assert "API token “Claude Code”" in mcp and "/account#api-tokens" in mcp
        assert f'/account/connected-apps/{theirs}/revoke' not in mcp      # theirs, not mine
        r = client.post(f"/account/connected-apps/{claude}/revoke", headers=_h(me),
                        data={"next": "/capture/setup?tab=mcp"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/capture/setup?tab=mcp&msg=Disconnected"
        with SessionLocal() as s:
            assert s.get(OAuthGrant, claude).revoked_at is not None
        assert "Disconnected" in client.get(r.headers["location"], headers=_h(me)).text
        # Someone else's grant, or an outside address: refused / back to Account.
        r = client.post(f"/account/connected-apps/{theirs}/revoke", headers=_h(me),
                        data={"next": "https://evil.example/"}, follow_redirects=False)
        assert r.headers["location"].startswith("/account?err=")
        with SessionLocal() as s:
            assert s.get(OAuthGrant, theirs).revoked_at is None

    def test_nothing_connected():
        lone = _user(f"cc-lone-{uuid.uuid4().hex[:5]}")
        assert "No Run·dere app is signed in" in client.get("/capture/setup?tab=ios",
                                                            headers=_h(lone)).text
        assert "No assistant or tool is connected" in client.get("/capture/setup?tab=mcp",
                                                                 headers=_h(lone)).text
