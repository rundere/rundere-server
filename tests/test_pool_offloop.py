"""YONDER-178: blocking DB work must never run on the event loop.

A client that fired and cancelled /api/v1 requests in a tight loop drained the
connection pool. The auth gate (and /mcp's bearer check) then waited for a
connection ON the event loop, which froze it for the whole checkout timeout
and also stopped finished requests from handing their connections back — the
server stopped answering, /health included, until restarted.
"""
import asyncio
import inspect

from fastapi.testclient import TestClient

import app.main as main_mod
import app.mcp_server as mcp_mod
from app.config import settings
from app.db import _engine_kwargs, engine
from app.main import app
from app.models import Base

Base.metadata.create_all(engine)
client = TestClient(app)


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class _Spy:
    """Wraps a sessionmaker and records whether it was opened on the loop."""

    def __init__(self, real):
        self.real, self.calls, self.on_loop = real, 0, 0

    def __call__(self, *a, **kw):
        self.calls += 1
        self.on_loop += _on_event_loop()
        return self.real(*a, **kw)


def test_auth_gate_resolves_user_off_the_event_loop(monkeypatch):
    spy = _Spy(main_mod.SessionLocal)
    monkeypatch.setattr(main_mod, "SessionLocal", spy)
    monkeypatch.setattr(settings, "auth_required", True)
    r = client.get("/api/v1/trips")
    assert r.status_code == 401
    assert spy.calls >= 1
    assert spy.on_loop == 0


def test_mcp_bearer_check_runs_off_the_event_loop(monkeypatch):
    spy = _Spy(mcp_mod.SessionLocal)
    monkeypatch.setattr(mcp_mod, "SessionLocal", spy)
    r = client.post("/mcp", headers={"Authorization": "Bearer not-a-real-token"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert spy.calls == 1
    assert spy.on_loop == 0


def test_health_is_async_and_db_free(monkeypatch):
    # Answers on the loop, not from the (possibly exhausted) worker threads.
    assert inspect.iscoroutinefunction(main_mod.health)

    def boom(*a, **kw):
        raise AssertionError("/health must not open a DB session")

    monkeypatch.setattr(main_mod, "SessionLocal", boom)
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_engine_pool_settings():
    assert _engine_kwargs("sqlite:///x.db") == {}
    kw = _engine_kwargs("postgresql+psycopg://u:p@db/rundere")
    assert kw["pool_pre_ping"] is True
    assert kw["pool_timeout"] == settings.db_pool_timeout_seconds
    assert kw["pool_size"] == settings.db_pool_size
    assert kw["max_overflow"] == settings.db_max_overflow


def test_sheds_load_past_the_inflight_limit(monkeypatch):
    # Pretend the limit is already reached: new work is refused at once with a
    # retry hint (no queueing), while /health keeps answering.
    monkeypatch.setattr(settings, "max_inflight_requests", 3)
    monkeypatch.setattr(main_mod, "_inflight", 3)
    r = client.get("/api/v1/trips")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"
    assert client.get("/health").status_code == 200
    # under the limit, requests go through (401 here: no token) and the
    # counter comes back down afterwards
    monkeypatch.setattr(main_mod, "_inflight", 0)
    assert client.get("/api/v1/trips").status_code == 401
    assert main_mod._inflight == 0


def test_load_shedding_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "max_inflight_requests", 0)
    monkeypatch.setattr(main_mod, "_inflight", 10_000)
    assert client.get("/api/v1/trips").status_code == 401
