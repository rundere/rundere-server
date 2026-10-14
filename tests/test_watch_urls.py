"""YONDER-126 (D-197): production watches other Run·dere servers' /health (the demo)
through TX_WATCH_URLS and alerts like any other service."""
import io
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.notify import monitor  # noqa: E402


class _Resp(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(json.dumps(body).encode()); self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_up_down_and_garbage(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=10: _Resp({"status": "ok", "version": "4.12.0"}))
    assert monitor.check_watched("https://demo.rundere.app/health") == (True, "up (version 4.12.0)")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=10: _Resp({"status": "no"}))
    assert monitor.check_watched("https://demo.rundere.app/health")[0] is False

    def boom(req, timeout=10):
        raise OSError("refused")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    ok, why = monitor.check_watched("https://demo.rundere.app/health")
    assert not ok and why == "didn't answer (OSError)"            # no URL or detail leaked


def test_each_watched_url_is_reported_under_servers(monkeypatch):
    seen = []
    monkeypatch.setattr(settings, "watch_urls", " https://demo.rundere.app/health , ")
    monkeypatch.setattr(monitor, "check_watched", lambda url: (False, "didn't answer (OSError)"))
    for name in ("check_documents", "check_antivirus", "check_llm", "check_inbox"):
        monkeypatch.setattr(monitor, name, (lambda *a, **k: None))
    monkeypatch.setattr(monitor, "_due", lambda *a, **k: False)
    monkeypatch.setattr(monitor.notify, "report",
                        lambda key, ok, **kw: seen.append((key, ok, kw["category"], kw["label"])))
    monitor.run_once(session_factory=None)
    assert ("watch:demo.rundere.app", False, "servers", "Run·dere at demo.rundere.app") in seen
