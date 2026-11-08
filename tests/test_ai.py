"""AI on/off and providers (YONDER-354, SPEC-12 D-228).

The endpoint each feature may use (app/ai.py), and the two wire formats
(ai/client.py), with the HTTP layer faked: no test reaches a model.
"""
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai import client                                    # noqa: E402
from ai.client import Endpoint, chat_json                # noqa: E402
from app import ai                                       # noqa: E402
from app.config import Settings                          # noqa: E402

KEY = "sk-test-not-a-real-key-0123456789"


@pytest.fixture
def cfg(monkeypatch):
    """A fresh Settings for app.ai to read, built from keyword overrides."""
    def make(**kw):
        s = Settings(**kw)
        monkeypatch.setattr(ai, "settings", s)
        return s
    return make


# --------------------------------------------------------------- on and off
def test_off_by_default(cfg):
    cfg()
    assert ai.configured() is None
    assert ai.endpoint(ai.EXTRACTION) is None and ai.endpoint(ai.DISCOVER) is None
    assert ai.extractor() is None
    assert ai.config_problems() == []


def test_a_local_openai_endpoint_serves_both_features(cfg):
    cfg(ai_enabled=True, ai_provider="openai", ai_url="http://litellm.test:4000/v1",
        ai_model_chat="qwen3:14b", ai_api_key=KEY)
    for f in ai.FEATURES:
        llm = ai.endpoint(f)
        assert llm.provider == "openai" and llm.model == "qwen3:14b" and not llm.cloud
    assert ai.extractor().llm.api_key == KEY


def test_incomplete_config_is_off_and_says_why(cfg):
    cfg(ai_enabled=True, ai_provider="openai", ai_url="", ai_model_chat="")
    assert ai.configured() is None
    problems = " ".join(ai.config_problems())
    assert "TX_AI_URL is empty" in problems and "TX_AI_MODEL_CHAT is empty" in problems


def test_unknown_provider_is_off(cfg):
    cfg(ai_enabled=True, ai_provider="anthropic", ai_url="https://x.test", ai_model_chat="m")
    assert ai.configured() is None
    assert any("ollama or openai" in p for p in ai.config_problems())


# --------------------------------------------------------------- D-228: cloud
def test_cloud_serves_discover_but_not_extraction(cfg):
    cfg(ai_enabled=True, ai_provider="openai", ai_url="https://api.example.test/v1",
        ai_model_chat="m", ai_api_key=KEY, ai_location="cloud")
    assert ai.endpoint(ai.DISCOVER).cloud is True
    assert ai.endpoint(ai.EXTRACTION) is None            # travel emails stay local
    assert ai.extractor() is None
    assert any("TX_AI_CLOUD_EXTRACTION" in p for p in ai.config_problems())


def test_cloud_extraction_needs_its_own_switch(cfg):
    cfg(ai_enabled=True, ai_provider="openai", ai_url="https://api.example.test/v1",
        ai_model_chat="m", ai_location="cloud", ai_cloud_extraction=True)
    assert ai.endpoint(ai.EXTRACTION) is not None


def test_cloud_must_be_https(cfg):
    cfg(ai_enabled=True, ai_provider="openai", ai_url="http://api.example.test/v1",
        ai_model_chat="m", ai_location="cloud")
    assert ai.configured() is None
    assert any("must use https" in p for p in ai.config_problems())


def test_unknown_feature_is_a_bug():
    with pytest.raises(ValueError):
        ai.endpoint("vision")


# --------------------------------------------------------------- 4.x settings
def test_old_ollama_settings_still_work_for_one_release(cfg):
    cfg(ai_enabled=True, ollama_url="http://ollama.test:11434", ollama_model="qwen3:14b")
    llm = ai.endpoint(ai.EXTRACTION)
    assert (llm.provider, llm.url, llm.model) == ("ollama", "http://ollama.test:11434", "qwen3:14b")
    assert any("deprecated" in p for p in ai.config_problems())


def test_the_built_in_ollama_default_doesnt_count_as_configured(cfg):
    cfg(ai_enabled=True)                                 # ollama_url only has its default
    assert ai.configured() is None


def test_old_settings_alone_dont_turn_ai_on(cfg):
    cfg(ollama_url="http://ollama.test:11434")           # the upgrade note says set TX_AI_ENABLED
    assert ai.configured() is None


def test_new_settings_win_over_old(cfg):
    cfg(ai_enabled=True, ollama_url="http://ollama.test:11434", ai_provider="openai",
        ai_url="http://litellm.test:4000/v1", ai_model_chat="m")
    assert ai.configured().provider == "openai"
    assert not any("deprecated" in p for p in ai.config_problems())


# --------------------------------------------------------------- demo (D-198)
def test_a_demo_server_never_calls_ai(cfg, monkeypatch):
    cfg(ai_enabled=True, ai_provider="ollama", ai_url="http://ollama.test:11434",
        ai_model_chat="m")
    from app import demo
    monkeypatch.setattr(demo, "enabled", lambda: True)
    assert ai.configured() is None and ai.extractor() is None


# --------------------------------------------------------------- the key
def test_the_key_is_never_shown(cfg):
    cfg(ai_enabled=True, ai_provider="openai", ai_url="http://litellm.test:4000/v1",
        ai_model_chat="m", ai_api_key=KEY)
    llm = ai.configured()
    assert KEY not in repr(llm) and KEY not in llm.where
    assert KEY not in json.dumps(ai.describe())


def test_credentials_in_the_address_are_not_shown(cfg):
    cfg(ai_enabled=True, ai_provider="openai", ai_url="http://me:secret@litellm.test:4000/v1",
        ai_model_chat="m")
    assert ai.describe()["url"] == "http://litellm.test:4000/v1"


def test_the_key_can_come_from_a_file(tmp_path):
    f = tmp_path / "ai_key"
    f.write_text(KEY + "\n")
    assert Settings(ai_api_key_file=str(f)).ai_api_key == KEY


# --------------------------------------------------------------- the wire
class _Fake:
    """Stands in for urllib.request.urlopen: records the request, replies."""
    def __init__(self, reply):
        self.reply, self.requests = reply, []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        body = io.BytesIO(json.dumps(self.reply).encode())
        body.status = 200
        body.__enter__ = lambda *a: body
        body.__exit__ = lambda *a: False
        return body


def _openai_reply(content):
    # The shape LiteLLM returns (trimmed).
    return {"id": "chatcmpl-1", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}}


def test_openai_request_and_reply(monkeypatch):
    fake = _Fake(_openai_reply('{"a": 1}'))
    monkeypatch.setattr(client.urllib.request, "urlopen", fake)
    llm = Endpoint(provider="openai", url="http://litellm.test:4000/v1/", model="m", api_key=KEY)
    reply = chat_json(llm, [{"role": "user", "content": "hi"}], {"type": "object"})
    req = fake.requests[0]
    assert req.full_url == "http://litellm.test:4000/v1/chat/completions"
    assert req.get_header("Authorization") == f"Bearer {KEY}"
    assert KEY not in req.full_url
    body = json.loads(req.data)
    assert body["temperature"] == 0 and body["stream"] is False
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is False
    assert body["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert (reply.content, reply.finish_reason, reply.prompt_tokens, reply.out_tokens) == \
        ('{"a": 1}', "stop", 120, 30)


def test_openai_without_a_key_sends_no_auth_header(monkeypatch):
    fake = _Fake(_openai_reply("{}"))
    monkeypatch.setattr(client.urllib.request, "urlopen", fake)
    chat_json(Endpoint(provider="openai", url="http://vllm.test/v1", model="m"), [], {})
    assert fake.requests[0].get_header("Authorization") is None


def test_ollama_request_and_reply(monkeypatch):
    fake = _Fake({"message": {"content": '{"a": 1}', "thinking": ""}, "done_reason": "stop",
                  "prompt_eval_count": 80, "eval_count": 12})
    monkeypatch.setattr(client.urllib.request, "urlopen", fake)
    llm = Endpoint(provider="ollama", url="http://ollama.test:11434", model="qwen3:14b")
    reply = chat_json(llm, [{"role": "user", "content": "hi"}], {"type": "object"}, num_ctx=8192)
    req = fake.requests[0]
    assert req.full_url == "http://ollama.test:11434/api/chat"
    body = json.loads(req.data)
    assert body["format"] == {"type": "object"} and body["think"] is False
    assert body["options"] == {"temperature": 0, "num_ctx": 8192}
    assert (reply.content, reply.prompt_tokens, reply.out_tokens) == ('{"a": 1}', 80, 12)


def test_reasoning_text_is_kept_for_diagnostics(monkeypatch):
    reply = _openai_reply("")
    reply["choices"][0]["message"]["reasoning_content"] = "x" * 40
    monkeypatch.setattr(client.urllib.request, "urlopen", _Fake(reply))
    out = chat_json(Endpoint(provider="openai", url="http://l.test/v1", model="m"), [], {})
    assert out.content == "" and len(out.thinking) == 40


def test_a_failed_extraction_call_never_carries_the_key(monkeypatch):
    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
    monkeypatch.setattr(client.urllib.request, "urlopen", refuse)
    from extract.llm import ExtractionFailed, LLMExtractor
    x = LLMExtractor(Endpoint(provider="openai", url="http://litellm.test:4000/v1", model="m",
                              api_key=KEY))
    with pytest.raises(ExtractionFailed) as ex:
        x.extract("A booking.")
    assert "401" in str(ex.value)
    assert KEY not in str(ex.value) and KEY not in json.dumps(ex.value.attempts)


def test_health_check_asks_the_right_path(monkeypatch):
    fake = _Fake({"data": []})
    monkeypatch.setattr(client.urllib.request, "urlopen", fake)
    client.list_models(Endpoint(provider="openai", url="http://l.test/v1", model="m", api_key=KEY))
    client.list_models(Endpoint(provider="ollama", url="http://o.test:11434", model="m"))
    assert [r.full_url for r in fake.requests] == ["http://l.test/v1/models",
                                                    "http://o.test:11434/api/tags"]
    assert fake.requests[0].get_header("Authorization") == f"Bearer {KEY}"
    assert fake.requests[1].get_header("Authorization") is None


def test_unknown_provider_is_refused_at_construction():
    with pytest.raises(ValueError):
        Endpoint(provider="anthropic", url="https://x.test", model="m")
