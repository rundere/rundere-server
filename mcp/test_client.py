"""Unit tests for the Yonder MCP client (Phase 4) — no network, no MCP runtime.

Run:  pytest mcp/test_client.py   (or via the yonder image; stdlib only)
"""
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client import YonderClient, YonderError  # noqa: E402


class _Resp:
    def __init__(self, data):
        self._d = json.dumps(data).encode()

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_login_then_bearer_get(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((req.get_method(), req.full_url, req.headers.get("Authorization")))
        if req.full_url.endswith("/api/v1/auth/login"):
            return _Resp({"access": "tok123"})
        return _Resp([{"id": "t1", "name": "Trip"}])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    c = YonderClient("http://x", username="me", password="pw")
    assert c.list_trips()[0]["name"] == "Trip"
    assert calls[0][0] == "POST" and calls[0][1].endswith("/api/v1/auth/login")
    assert calls[1][2] == "Bearer tok123"        # GET carried the bearer


def test_pretoken_skips_login(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _Resp({"ok": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    c = YonderClient("http://x", token="mint-abc")
    c.dashboard()
    assert not any("/auth/login" in u for u in seen)   # no login when a token is supplied


def test_401_triggers_relogin_and_retry(monkeypatch):
    state = {"first": True, "logins": 0}

    def fake_urlopen(req, timeout=None):
        if req.full_url.endswith("/api/v1/auth/login"):
            state["logins"] += 1
            return _Resp({"access": f"fresh{state['logins']}"})
        if state["first"]:
            state["first"] = False
            raise urllib.error.HTTPError(req.full_url, 401, "expired", {},
                                         io.BytesIO(b'{"detail":"x"}'))
        assert req.headers.get("Authorization") == "Bearer fresh1"
        return _Resp({"ok": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    c = YonderClient("http://x", username="me", password="pw", token="stale")
    assert c.dashboard() == {"ok": True}
    assert state["logins"] == 1                  # re-logged in exactly once


def test_post_capture_shapes_body(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        if req.full_url.endswith("/api/v1/auth/login"):
            return _Resp({"access": "t"})
        captured["method"] = req.get_method()
        captured["path"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp({"batch_id": "b1", "status": "received"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    c = YonderClient("http://x", username="me", password="pw")
    out = c.capture_url("https://maps.example/dir", travel_date="2026-09-05", title="Drive")
    assert out["batch_id"] == "b1"
    assert captured["method"] == "POST" and captured["path"].endswith("/api/v1/capture")
    assert captured["body"]["url"] == "https://maps.example/dir"
    assert captured["body"]["travel_date"] == "2026-09-05"


def test_patch_drops_unset_fields_but_keeps_empty_lists(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["method"] = req.get_method()
        captured["path"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp({"id": "a1"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    c = YonderClient("http://x", token="ypat_x")
    c.update_activity("t1", "a1", values=None, traveler_ids=[])
    assert captured["method"] == "PATCH"
    assert captured["path"].endswith("/api/v1/trips/t1/activities/a1")
    assert captured["body"] == {"traveler_ids": []}


def test_save_research_posts_finding(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["path"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp({"id": "f1", "topic": "research"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    c = YonderClient("http://x", token="ypat_x")
    assert c.save_research("t1", "Hikes", "- a")["topic"] == "research"
    assert captured["path"].endswith("/api/v1/trips/t1/findings")
    assert captured["body"]["title"] == "Hikes" and captured["body"]["sources"] == []
