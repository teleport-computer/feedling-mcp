"""Task 7 (PR B / B3): native async transport for anthropic / gemini /
openai-responses in `chat_completion_async` — no more `anyio.to_thread`
bridge to the sync `chat_completion` for these 3 wires.

Monkeypatches `pc._async_http_client` to a fake async client and asserts:
  - the parsed dict shape matches the sync handlers' return (incl. normalized
    `usage` and a `tool_calls` key)
  - `anyio.to_thread.run_sync` is never invoked for these providers.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc


class _AResp:
    status_code = 200

    def __init__(self, b):
        self._b = b

    def json(self):
        return self._b

    def raise_for_status(self):
        pass


def _fake_async_client(body):
    class C:
        async def post(self, *a, **k):
            return _AResp(body)

    return C()


def _no_thread_bridge(monkeypatch):
    import anyio.to_thread

    def _boom(*a, **k):
        raise AssertionError("must not use thread bridge")

    monkeypatch.setattr(anyio.to_thread, "run_sync", _boom)


ANTHROPIC_BODY = {
    "id": "msg_1",
    "content": [{"type": "text", "text": "hi"}],
    "usage": {"input_tokens": 2, "output_tokens": 3},
    "stop_reason": "end_turn",
}

GEMINI_BODY = {
    "responseId": "resp_1",
    "candidates": [
        {
            "content": {"parts": [{"text": "hi"}]},
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3},
}

RESPONSES_BODY = {
    "id": "resp_abc",
    "status": "completed",
    "output": [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi"}],
        }
    ],
    # _normalize_usage's "openai" branch (shared by the compat and responses
    # wires) reads prompt_tokens/completion_tokens — matches the sync path's
    # existing (pre-Task-7) behavior; not something this task changes.
    "usage": {"prompt_tokens": 2, "completion_tokens": 3},
}


def _cfg_anthropic():
    return pc.ProviderConfig("anthropic", "claude-x", "k", "https://api.anthropic.com/v1")


def _cfg_gemini():
    return pc.ProviderConfig(
        "gemini", "gemini-2.5-flash", "k", "https://generativelanguage.googleapis.com/v1beta"
    )


def _cfg_responses():
    return pc.ProviderConfig("openai", "gpt-5.2", "k", "https://api.openai.com/v1")


@pytest.mark.parametrize(
    "cfg_fn,body",
    [
        (_cfg_anthropic, ANTHROPIC_BODY),
        (_cfg_gemini, GEMINI_BODY),
        (_cfg_responses, RESPONSES_BODY),
    ],
    ids=["anthropic", "gemini", "responses"],
)
def test_native_async_no_thread_bridge(monkeypatch, cfg_fn, body):
    monkeypatch.setattr(pc, "_async_http_client", lambda: _fake_async_client(body))
    _no_thread_bridge(monkeypatch)
    cfg = cfg_fn()
    res = asyncio.run(
        pc.chat_completion_async(cfg, [{"role": "user", "content": "hi"}])
    )
    assert res["reply"] == "hi"
    assert res["usage"]["prompt_tokens"] == 2
    assert res["usage"]["completion_tokens"] == 3
    assert res["tool_calls"] == []


def test_anthropic_native_async_matches_sync_shape(monkeypatch):
    monkeypatch.setattr(pc, "_async_http_client", lambda: _fake_async_client(ANTHROPIC_BODY))
    _no_thread_bridge(monkeypatch)
    cfg = _cfg_anthropic()
    res = asyncio.run(
        pc.chat_completion_async(cfg, [{"role": "user", "content": "hi"}])
    )
    assert res["provider"] == "anthropic"
    assert res["model"] == "claude-x"
    assert res["stop_reason"] == "end_turn"
    assert res["raw_id"] == "msg_1"


def test_gemini_native_async_matches_sync_shape(monkeypatch):
    monkeypatch.setattr(pc, "_async_http_client", lambda: _fake_async_client(GEMINI_BODY))
    _no_thread_bridge(monkeypatch)
    cfg = _cfg_gemini()
    res = asyncio.run(
        pc.chat_completion_async(cfg, [{"role": "user", "content": "hi"}])
    )
    assert res["provider"] == "gemini"
    assert res["raw_id"] == "resp_1"


def test_responses_native_async_matches_sync_shape(monkeypatch):
    monkeypatch.setattr(pc, "_async_http_client", lambda: _fake_async_client(RESPONSES_BODY))
    _no_thread_bridge(monkeypatch)
    cfg = _cfg_responses()
    res = asyncio.run(
        pc.chat_completion_async(cfg, [{"role": "user", "content": "hi"}])
    )
    assert res["provider"] == "openai"
    assert res["raw_id"] == "resp_abc"
    assert res["stop_reason"] == "completed"
