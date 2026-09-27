"""synthesize_json failure diagnostics: every None comes with a cause-specific log."""
import logging
import urllib.error

import pytest

import discover.synth as synth


def _call():
    return synth.synthesize_json("prompt", {"type": "object"},
                                 base_url="http://ollama:11434", model="m")


def _reply(content, **extra):
    return lambda *a, **k: {"message": {"content": content, **extra},
                            "done_reason": "stop", "eval_count": 9}


def test_returns_parsed_object(monkeypatch):
    monkeypatch.setattr(synth, "_post_chat", _reply('{"a": 1}'))
    assert _call() == {"a": 1}


@pytest.mark.parametrize("fake, needle", [
    (_reply("", thinking="x" * 50), "empty content"),
    (_reply("{not json"), "invalid JSON"),
    (_reply("[1, 2]"), "not an object"),
])
def test_bad_content_logs_cause(monkeypatch, caplog, fake, needle):
    monkeypatch.setattr(synth, "_post_chat", fake)
    with caplog.at_level(logging.WARNING, logger="yonder.discovery.llm"):
        assert _call() is None
    assert needle in caplog.text


def test_empty_content_reports_thinking_and_tokens(monkeypatch, caplog):
    monkeypatch.setattr(synth, "_post_chat", _reply("", thinking="x" * 50))
    with caplog.at_level(logging.WARNING, logger="yonder.discovery.llm"):
        _call()
    assert "thinking_chars=50" in caplog.text and "out_tokens=9" in caplog.text


@pytest.mark.parametrize("exc, needle", [
    (urllib.error.URLError(ConnectionRefusedError("refused")), "unreachable"),
    (urllib.error.URLError(TimeoutError()), "timed out"),
    (TimeoutError(), "timed out"),
])
def test_transport_errors_log_cause(monkeypatch, caplog, exc, needle):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(synth, "_post_chat", boom)
    with caplog.at_level(logging.WARNING, logger="yonder.discovery.llm"):
        assert _call() is None
    assert needle in caplog.text
