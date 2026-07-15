from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


def _fake_client(monkeypatch, response_body: dict) -> list[dict]:
    calls: list[dict] = []

    class FakeClient:
        # Provider calls now share one pooled client built by `_http_client()`,
        # so the fake must accept httpx.Client's kwargs (limits/timeout/...) and
        # take the per-request `timeout` on `.post`.
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append({"url": url, "headers": headers or {}, "json": json or {}})
            return FakeResponse(200, response_body)

    monkeypatch.setattr(pc.httpx, "Client", FakeClient)
    # Drop any client cached from a previous test so `_http_client()` rebuilds
    # against the fake just installed.
    monkeypatch.setattr(pc, "_shared_client", None)
    return calls


@pytest.mark.parametrize(
    ("provider", "model", "base_url"),
    [
        ("anthropic", "claude-sonnet-4-20250514", "https://api.anthropic.com/v1"),
        ("gemini", "gemini-2.5-flash", "https://generativelanguage.googleapis.com/v1beta"),
        ("deepseek", "deepseek-v4-flash", "https://api.deepseek.com"),
        ("custom", "some-model", "https://custom.example/v1"),
    ],
)
def test_validate_config_accepts_direct_providers(provider, model, base_url):
    normalized, out_model, out_base_url = pc.validate_config(provider, model, base_url if provider == "custom" else "")

    assert out_model == model
    assert out_base_url == base_url
    if provider == "custom":
        assert normalized == "openai_compatible"
    else:
        assert normalized == provider


def test_provider_calls_reuse_one_pooled_client(monkeypatch):
    # The whole point of the pooling change: two back-to-back provider calls must
    # share a single httpx.Client (built once) instead of opening a fresh client
    # — and therefore a fresh DNS+TLS handshake — per call.
    builds: list[int] = []

    class CountingClient:
        def __init__(self, *args, **kwargs):
            builds.append(1)

        def post(self, url: str, *, headers=None, json=None, timeout=None):
            return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(pc.httpx, "Client", CountingClient)
    monkeypatch.setattr(pc, "_shared_client", None)

    cfg = pc.ProviderConfig("deepseek", "deepseek-chat", "k")
    pc.chat_completion(cfg, [{"role": "user", "content": "one"}])
    pc.chat_completion(cfg, [{"role": "user", "content": "two"}])

    assert builds == [1]  # constructed exactly once across both calls
    assert pc._http_client() is pc._shared_client


def test_anthropic_chat_completion_uses_messages_api(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "msg_test",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 4, "output_tokens": 1},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("anthropic", "claude-sonnet-4-20250514", "sk-ant-test"),
        [
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "Say ok."},
        ],
        response_format={"type": "json_object"},
    )

    assert result["reply"] == "ok"
    assert result["provider"] == "anthropic"
    assert calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == "sk-ant-test"
    assert calls[0]["headers"]["anthropic-version"] == "2023-06-01"
    assert calls[0]["json"]["system"].startswith("system rules")
    assert calls[0]["json"]["messages"] == [{"role": "user", "content": "Say ok."}]


def test_gemini_chat_completion_uses_generate_content(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "responseId": "gemini_test",
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {"totalTokenCount": 5},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("gemini", "gemini-2.5-flash", "AIza-test"),
        [
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "Say ok."},
        ],
        response_format={"type": "json_object"},
    )

    assert result["reply"] == "ok"
    assert result["provider"] == "gemini"
    assert calls[0]["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    assert calls[0]["headers"]["x-goog-api-key"] == "AIza-test"
    assert calls[0]["json"]["systemInstruction"] == {"parts": [{"text": "system rules"}]}
    assert calls[0]["json"]["contents"] == [{"role": "user", "parts": [{"text": "Say ok."}]}]
    assert calls[0]["json"]["generationConfig"]["responseMimeType"] == "application/json"


def test_deepseek_legacy_chat_maps_to_v4_flash_non_thinking(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "chatcmpl-test",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"total_tokens": 5},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("deepseek", "deepseek-chat", "sk-ds-test"),
        [{"role": "user", "content": "Say ok."}],
    )

    assert result["reply"] == "ok"
    assert result["provider"] == "deepseek"
    assert calls[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-ds-test"
    assert calls[0]["json"]["model"] == "deepseek-v4-flash"
    assert calls[0]["json"]["thinking"] == {"type": "disabled"}


def test_deepseek_legacy_reasoner_maps_to_v4_flash_thinking(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "chatcmpl-test",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"total_tokens": 5},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("deepseek", "deepseek-reasoner", "sk-ds-test"),
        [{"role": "user", "content": "Say ok."}],
    )

    assert result["reply"] == "ok"
    assert result["provider"] == "deepseek"
    assert calls[0]["json"]["model"] == "deepseek-v4-flash"
    assert calls[0]["json"]["thinking"] == {"type": "enabled"}


def test_deepseek_v4_flash_defaults_to_non_thinking(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "chatcmpl-test",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"total_tokens": 5},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("deepseek", "deepseek-v4-flash", "sk-ds-test"),
        [{"role": "user", "content": "Say ok."}],
    )

    assert result["reply"] == "ok"
    assert result["provider"] == "deepseek"
    assert calls[0]["json"]["model"] == "deepseek-v4-flash"
    assert calls[0]["json"]["thinking"] == {"type": "disabled"}


def test_openrouter_legacy_deepseek_model_maps_to_v4_flash(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "chatcmpl-test",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"total_tokens": 5},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("openrouter", "deepseek/deepseek-chat", "sk-or-test"),
        [{"role": "user", "content": "Say ok."}],
    )

    assert result["reply"] == "ok"
    assert result["provider"] == "openrouter"
    assert calls[0]["json"]["model"] == "deepseek/deepseek-v4-flash"
    assert "thinking" not in calls[0]["json"]


def test_openai_compatible_chat_completion_preserves_image_parts(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "chatcmpl-test",
            "choices": [{"message": {"content": "vision ok"}}],
            "usage": {"total_tokens": 9},
        },
    )

    image_part = {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,abcd"},
    }
    result = pc.chat_completion(
        pc.ProviderConfig("openrouter", "openai/gpt-4.1-mini", "sk-or-test"),
        [{"role": "user", "content": [{"type": "text", "text": "look"}, image_part]}],
    )

    assert result["reply"] == "vision ok"
    content = calls[0]["json"]["messages"][0]["content"]
    assert content == [{"type": "text", "text": "look"}, image_part]


def test_openrouter_chat_completion_requests_and_extracts_reasoning(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "chatcmpl-test",
            "choices": [{
                "message": {
                    "content": "visible answer",
                    "reasoning": "provider reasoning summary",
                }
            }],
            "usage": {"total_tokens": 9},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("openrouter", "anthropic/claude-sonnet-4.5", "sk-or-test"),
        [{"role": "user", "content": "hello"}],
        include_reasoning=True,
    )

    assert result["reply"] == "visible answer"
    assert result["reasoning"] == "provider reasoning summary"
    assert calls[0]["json"]["reasoning"] == {"enabled": True, "exclude": False}


def test_openrouter_chat_completion_retries_without_reasoning_when_unsupported(monkeypatch):
    calls: list[dict] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append({"url": url, "headers": headers or {}, "json": json or {}})
            if len(calls) == 1:
                return FakeResponse(400, {"error": {"message": "reasoning is unsupported for this model"}})
            return FakeResponse(200, {
                "id": "chatcmpl-test",
                "choices": [{"message": {"content": "visible answer"}}],
                "usage": {"total_tokens": 9},
            })

    monkeypatch.setattr(pc.httpx, "Client", FakeClient)
    monkeypatch.setattr(pc, "_shared_client", None)

    result = pc.chat_completion(
        pc.ProviderConfig("openrouter", "openai/gpt-4.1-mini", "sk-or-test"),
        [{"role": "user", "content": "hello"}],
        include_reasoning=True,
    )

    assert result["reply"] == "visible answer"
    assert calls[0]["json"]["reasoning"] == {"enabled": True, "exclude": False}
    assert "reasoning" not in calls[1]["json"]


# ---- openai-compat 共享编解码（sync/async 必须单实现，防漂移）----

def test_build_openai_compat_payload_shape():
    payload = pc._build_openai_compat_payload(
        provider="openrouter", model="m",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.2, max_tokens=99999,
        response_format={"type": "json_object"},
        extra_body={"x": 1}, include_reasoning=True)
    assert payload["model"] == "m"
    assert payload["stream"] is False
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 8192  # 上限封顶
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["x"] == 1
    assert payload["reasoning"] == {"enabled": True, "exclude": False}

    p2 = pc._build_openai_compat_payload(
        provider="deepseek", model="m",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.2, max_tokens=10,
        response_format=None, extra_body=None, include_reasoning=True)
    assert "reasoning" not in p2  # reasoning 注入只对 openrouter
    assert "response_format" not in p2


def test_parse_openai_compat_body_result_shape():
    resp = FakeResponse(200, {
        "id": "chatcmpl-1",
        "choices": [{"message": {"content": "hi there", "reasoning": "why"},
                     "finish_reason": "stop"}],
        "usage": {"total_tokens": 3},
    })
    out = pc._parse_openai_compat_body(
        resp, provider="openrouter", model="m", require_reply=True)
    assert out["reply"] == "hi there"
    assert out["reasoning"] == "why"
    # PR B Task 2 (B4): usage is normalized in place to prompt/completion/total keys.
    assert out["usage"] == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": 3,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cache_miss_tokens": None,
    }
    assert out["raw_id"] == "chatcmpl-1"
    assert out["provider"] == "openrouter"
    assert out["model"] == "m"

    class NonJson:
        status_code = 200
        text = "x"

        def json(self):
            raise ValueError("nope")

    with pytest.raises(pc.ProviderError):
        pc._parse_openai_compat_body(
            NonJson(), provider="openrouter", model="m", require_reply=False)
    with pytest.raises(pc.ProviderError):
        pc._parse_openai_compat_body(
            FakeResponse(200, ["not-an-object"]),
            provider="openrouter", model="m", require_reply=False)


def test_parse_deepseek_body_extracts_reasoning_content():
    resp = FakeResponse(200, {
        "id": "chatcmpl-deepseek",
        "choices": [{"message": {
            "content": "visible answer",
            "reasoning_content": "deepseek reasoning summary",
        }}],
        "usage": {"total_tokens": 11},
    })

    out = pc._parse_openai_compat_body(
        resp, provider="deepseek", model="deepseek-reasoner", require_reply=True)

    assert out["reply"] == "visible answer"
    assert out["reasoning"] == "deepseek reasoning summary"
    # PR B Task 2 (B4): usage is normalized in place to prompt/completion/total keys.
    assert out["usage"] == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": 11,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cache_miss_tokens": None,
    }
    assert out["provider"] == "deepseek"


def test_async_openrouter_retries_without_reasoning_when_unsupported(monkeypatch):
    """chat_completion_async 与同步版同一 payload/降级契约（openrouter reasoning
    400/422 → 去掉 reasoning 重试一次）。共享编解码后由本测试与同步版测试共同钉住。"""
    import asyncio

    calls: list[dict] = []

    class FakeAsyncClient:
        is_closed = False

        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append({"url": url, "headers": headers or {}, "json": json or {}})
            if len(calls) == 1:
                return FakeResponse(422, {"error": {"message": "reasoning unsupported"}})
            return FakeResponse(200, {
                "id": "chatcmpl-test",
                "choices": [{"message": {"content": "visible answer"}}],
                "usage": {"total_tokens": 9},
            })

    monkeypatch.setattr(pc.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(pc, "_shared_async_client", None)

    result = asyncio.run(pc.chat_completion_async(
        pc.ProviderConfig("openrouter", "openai/gpt-4.1-mini", "sk-or-test"),
        [{"role": "user", "content": "hello"}],
        include_reasoning=True,
    ))

    assert result["reply"] == "visible answer"
    assert calls[0]["json"]["reasoning"] == {"enabled": True, "exclude": False}
    assert "reasoning" not in calls[1]["json"]


def test_openai_reasoning_model_uses_responses_api_and_extracts_summary(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "resp_test",
            "output": [
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "checked the arithmetic"}],
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "391"}],
                },
            ],
            "usage": {"output_tokens_details": {"reasoning_tokens": 192}},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("openai", "gpt-5", "sk-test"),
        [{"role": "system", "content": "final only"}, {"role": "user", "content": "17*23"}],
        include_reasoning=True,
    )

    assert result["reply"] == "391"
    assert result["reasoning"] == "checked the arithmetic"
    assert calls[0]["url"] == "https://api.openai.com/v1/responses"
    assert calls[0]["json"]["instructions"] == "final only"
    assert calls[0]["json"]["input"] == [{
        "role": "user",
        "content": [{"type": "input_text", "text": "17*23"}],
    }]
    assert calls[0]["json"]["reasoning"] == {"effort": "medium", "summary": "concise"}
    assert calls[0]["json"]["store"] is False


def test_anthropic_chat_completion_maps_image_parts(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "msg_test",
            "content": [{"type": "text", "text": "vision ok"}],
            "usage": {"input_tokens": 7, "output_tokens": 2},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("anthropic", "claude-sonnet-4-20250514", "sk-ant-test"),
        [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abcd"}},
        ]}],
    )

    assert result["reply"] == "vision ok"
    content = calls[0]["json"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "abcd"},
    }


def test_anthropic_chat_completion_extracts_thinking_block(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "msg_test",
            "content": [
                {"type": "thinking", "thinking": "anthropic thinking summary"},
                {"type": "text", "text": "visible answer"},
            ],
            "usage": {"input_tokens": 7, "output_tokens": 2},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("anthropic", "claude-sonnet-4-20250514", "sk-ant-test"),
        [{"role": "user", "content": "hello"}],
        include_reasoning=True,
        max_tokens=2048,
    )

    assert result["reply"] == "visible answer"
    assert result["reasoning"] == "anthropic thinking summary"
    assert calls[0]["json"]["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert "temperature" not in calls[0]["json"]


def test_gemini_chat_completion_maps_image_parts(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "responseId": "gemini_test",
            "candidates": [{"content": {"parts": [{"text": "vision ok"}]}}],
            "usageMetadata": {"totalTokenCount": 8},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("gemini", "gemini-2.5-flash", "AIza-test"),
        [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abcd"}},
        ]}],
    )

    assert result["reply"] == "vision ok"
    assert calls[0]["json"]["contents"] == [{
        "role": "user",
        "parts": [
            {"text": "look"},
            {"inline_data": {"mime_type": "image/jpeg", "data": "abcd"}},
        ],
    }]


def test_gemini_chat_completion_extracts_thought_parts(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "responseId": "gemini_test",
            "candidates": [{
                "content": {
                    "parts": [
                        {"thought": True, "text": "gemini thought summary"},
                        {"text": "visible answer"},
                    ]
                }
            }],
            "usageMetadata": {"totalTokenCount": 8},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("gemini", "gemini-2.5-flash", "AIza-test"),
        [{"role": "user", "content": "hello"}],
        include_reasoning=True,
        max_tokens=2048,
    )

    assert result["reply"] == "visible answer"
    assert result["reasoning"] == "gemini thought summary"
    assert calls[0]["json"]["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 1024,
        "includeThoughts": True,
    }


# ---------------------------------------------------------------------------
# Thinking/reasoning-model support: the setup self-test must tolerate an empty
# reply (a 2xx where the model spent its whole budget on reasoning), while the
# chat path stays strict and HTTP errors are never swallowed. See
# provider_client.test_provider_key / chat_completion(require_reply=...).
# ---------------------------------------------------------------------------

# Bodies that decode to an EMPTY reply for each provider shape.
_GEMINI_EMPTY = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]}
_OPENAI_EMPTY = {"choices": [{"message": {"content": ""}}]}
_ANTHROPIC_EMPTY = {"content": []}

_EMPTY_CASES = [
    (pc.ProviderConfig("gemini", "gemini-2.5-flash", "k"), _GEMINI_EMPTY),
    (pc.ProviderConfig("openai", "gpt-4o-mini", "k"), _OPENAI_EMPTY),
    (pc.ProviderConfig("anthropic", "claude-haiku-4-5", "k"), _ANTHROPIC_EMPTY),
]


def _fake_client_status(monkeypatch, status_code: int, response_body: dict) -> None:
    """Like _fake_client but lets the fake response carry a non-200 status."""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url: str, *, headers=None, json=None, timeout=None):
            return FakeResponse(status_code, response_body)

    monkeypatch.setattr(pc.httpx, "Client", FakeClient)
    monkeypatch.setattr(pc, "_shared_client", None)


@pytest.mark.parametrize(("cfg", "body"), _EMPTY_CASES)
def test_require_reply_false_allows_empty_reply(monkeypatch, cfg, body):
    _fake_client(monkeypatch, body)
    out = pc.chat_completion(cfg, [{"role": "user", "content": "Say ok."}], require_reply=False)
    assert out["reply"] == ""


@pytest.mark.parametrize(("cfg", "body"), _EMPTY_CASES)
def test_chat_path_still_requires_a_reply(monkeypatch, cfg, body):
    _fake_client(monkeypatch, body)
    with pytest.raises(pc.ProviderError):
        pc.chat_completion(cfg, [{"role": "user", "content": "Say ok."}])


def test_setup_self_test_passes_for_empty_thinking_reply(monkeypatch):
    # gemini-2.5-* / deepseek-reasoner can return a 2xx with no text when the
    # token budget is consumed by reasoning. That still proves the key works.
    _fake_client(monkeypatch, _GEMINI_EMPTY)
    out = pc.test_provider_key(pc.ProviderConfig("gemini", "gemini-2.5-flash", "k"))
    assert out["reply"] == ""


def test_setup_self_test_still_fails_on_http_error(monkeypatch):
    # An invalid / quota'd key surfaces as an HTTP 4xx and must NOT be swallowed.
    _fake_client_status(monkeypatch, 429, {"error": {"message": "You exceeded your current quota"}})
    with pytest.raises(pc.ProviderError) as ei:
        pc.test_provider_key(pc.ProviderConfig("openai", "gpt-4o-mini", "k"))
    assert ei.value.status_code == 429


def test_openai_compat_payload_omits_temperature_when_none():
    # `temperature` must be OMITTABLE, not merely settable: Claude 5 / GPT-5 class
    # models reject it outright ("`temperature` is deprecated for this model" → 400).
    payload = pc._build_openai_compat_payload(
        provider="openai_compatible", model="claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        temperature=None, max_tokens=256,
        response_format=None, extra_body=None, include_reasoning=False)
    assert "temperature" not in payload


def test_temperature_fallback_drops_temperature_on_a_temperature_400():
    """Runtime calls (genesis distill, legacy turn) legitimately pass temperature to get
    determinism, so we can't just stop sending it. Instead mirror the existing reasoning
    downgrade: when the provider 400s *about temperature*, retry once without it."""
    payload = {"model": "claude-sonnet-5", "temperature": 0.1, "messages": []}
    resp = FakeResponse(400, {"error": {"message": "`temperature` is deprecated for this model."}})
    out = pc._temperature_fallback_payload(payload, resp)
    assert out is not None and "temperature" not in out
    assert out["model"] == "claude-sonnet-5"  # everything else preserved
    assert "temperature" in payload  # caller's dict not mutated


def test_temperature_fallback_ignores_unrelated_400s():
    """Only downgrade when the 400 is actually ABOUT temperature — otherwise a bad-key or
    bad-model 400 would silently get retried with a different payload and mask the real error."""
    payload = {"model": "m", "temperature": 0.1, "messages": []}
    assert pc._temperature_fallback_payload(
        payload, FakeResponse(400, {"error": {"message": "invalid model"}})) is None
    # and never on a payload that has no temperature to drop
    assert pc._temperature_fallback_payload(
        {"model": "m"}, FakeResponse(400, {"error": {"message": "temperature bad"}})) is None


def _fake_client_then_ok(monkeypatch, first_status: int, first_body: dict, ok_body: dict) -> list[dict]:
    """First POST returns `first_status`, every later POST succeeds. Records each payload."""
    calls: list[dict] = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append({"url": url, "headers": headers or {}, "json": json or {}})
            if len(calls) == 1:
                return FakeResponse(first_status, first_body)
            return FakeResponse(200, ok_body)

    monkeypatch.setattr(pc.httpx, "Client", FakeClient)
    monkeypatch.setattr(pc, "_shared_client", None)
    return calls


def test_runtime_call_retries_without_temperature_on_temperature_400(monkeypatch):
    """End-to-end: a real chat turn against a temperature-deprecating model must still
    succeed. Without this, setup accepts the model but every genesis-distill / legacy turn
    400s — the user can add a model that is unusable in practice."""
    calls = _fake_client_then_ok(
        monkeypatch,
        400, {"error": {"message": "`temperature` is deprecated for this model."}},
        {"choices": [{"message": {"content": "ok"}}]},
    )
    out = pc.chat_completion(
        pc.ProviderConfig("openai_compatible", "claude-sonnet-5", "sk-x",
                          base_url="https://relay.example/v1"),
        [{"role": "user", "content": "hi"}],
        temperature=0.1,
    )
    assert out["reply"] == "ok"
    assert len(calls) == 2
    assert calls[0]["json"]["temperature"] == 0.1   # first attempt kept determinism
    assert "temperature" not in calls[1]["json"]    # retry dropped it


def test_anthropic_runtime_call_retries_without_temperature_on_temperature_400(monkeypatch):
    """The anthropic wire needs the same downgrade as the openai-compat one.

    Verified against the live Anthropic API: claude-sonnet-5 and claude-opus-4-8 BOTH
    reject `temperature` with 400 "`temperature` is deprecated for this model." (haiku-4-5
    still accepts it). Anthropic is a first-class provider (driver=claude), so without this
    a user can configure sonnet-5, pass setup, and then have genesis distillation — which
    passes temperature=0.0/0.1 for deterministic extraction — 400 on every call."""
    calls = _fake_client_then_ok(
        monkeypatch,
        400, {"type": "error", "error": {"type": "invalid_request_error",
                                         "message": "`temperature` is deprecated for this model."}},
        {"id": "msg_1", "content": [{"type": "text", "text": "ok"}],
         "usage": {"input_tokens": 3, "output_tokens": 1}},
    )
    out = pc.chat_completion(
        pc.ProviderConfig("anthropic", "claude-sonnet-5", "sk-ant-x"),
        [{"role": "user", "content": "hi"}],
        temperature=0.1,
    )
    assert out["reply"] == "ok"
    assert len(calls) == 2
    assert calls[0]["json"]["temperature"] == 0.1   # first attempt kept determinism
    assert "temperature" not in calls[1]["json"]    # retry dropped it


def test_provider_key_probe_sends_no_temperature(monkeypatch):
    # Regression: setup's live probe used to hard-code temperature=0.0, so configuring
    # a temperature-deprecating model (claude-sonnet-5 on an openai_compatible relay)
    # 400'd and the user simply could not add that model. Measured 2/6 failure rate
    # against a real relay, since only some upstream channels in the pool reject it.
    calls = _fake_client(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
    pc.test_provider_key(
        pc.ProviderConfig("openai_compatible", "claude-sonnet-5", "sk-x",
                          base_url="https://relay.example/v1")
    )
    assert "temperature" not in calls[0]["json"]


# A 2xx whose body is NOT a valid provider success shape (e.g. a gateway that
# answers 200 with `{}` or `{"error": ...}`) must still be rejected even on the
# lenient self-test path — otherwise setup "succeeds" but chat/send later fails
# on the same unusable body. The empty-reply allowance only applies when the
# provider's real success container is present (choices/candidates/content).
_MALFORMED_2XX = [
    (pc.ProviderConfig("gemini", "gemini-2.5-flash", "k"), {}),
    (pc.ProviderConfig("gemini", "gemini-2.5-flash", "k"), {"error": {"message": "boom"}}),
    (pc.ProviderConfig("openai", "gpt-4o-mini", "k"), {}),
    (pc.ProviderConfig("openai", "gpt-4o-mini", "k"), {"error": {"message": "boom"}}),
    (pc.ProviderConfig("anthropic", "claude-haiku-4-5", "k"), {}),
    (pc.ProviderConfig("anthropic", "claude-haiku-4-5", "k"), {"error": {"message": "boom"}}),
]


@pytest.mark.parametrize(("cfg", "body"), _MALFORMED_2XX)
def test_require_reply_false_still_rejects_malformed_2xx(monkeypatch, cfg, body):
    _fake_client(monkeypatch, body)  # HTTP 200, but not a valid provider success shape
    with pytest.raises(pc.ProviderError):
        pc.chat_completion(cfg, [{"role": "user", "content": "Say ok."}], require_reply=False)


def test_setup_self_test_rejects_malformed_2xx(monkeypatch):
    _fake_client(monkeypatch, {"error": {"message": "gateway returned 200 with an error body"}})
    with pytest.raises(pc.ProviderError):
        pc.test_provider_key(pc.ProviderConfig("openai", "gpt-4o-mini", "k"))


class _StatusClient:
    """Fake httpx.Client returning a fixed status for the /responses probe."""
    def __init__(self, *args, **kwargs):
        pass


def _fake_responses_probe(monkeypatch, status_code: int, body: dict | None = None):
    calls: list[dict] = []

    class FakeClient(_StatusClient):
        def post(self, url, *, headers=None, json=None, timeout=None):
            calls.append({"url": url, "json": json or {}})
            return FakeResponse(status_code, body or {})

    monkeypatch.setattr(pc.httpx, "Client", FakeClient)
    monkeypatch.setattr(pc, "_shared_client", None)
    return calls


def test_probe_responses_support_true_on_2xx(monkeypatch):
    calls = _fake_responses_probe(monkeypatch, 200, {"object": "response", "status": "completed"})
    cfg = pc.ProviderConfig("openai_compatible", "gpt-5.4", "k", "https://relay.host/v1")
    assert pc.probe_responses_support(cfg) is True
    # it must hit the relay's /responses endpoint
    assert calls and calls[0]["url"].rstrip("/").endswith("/responses")


def test_probe_responses_support_false_on_not_implemented(monkeypatch):
    # relays that only do chat completions return 404/500 "not implemented" here
    _fake_responses_probe(monkeypatch, 500, {"error": {"message": "not implemented"}})
    cfg = pc.ProviderConfig("openai_compatible", "m", "k", "https://relay.host/v1")
    assert pc.probe_responses_support(cfg) is False


def test_probe_responses_support_false_on_error_shaped_2xx(monkeypatch):
    # Some relays return HTTP 200 with an {"error": ...} body for an endpoint they
    # don't actually implement. Status alone would mark this as supported and route
    # codex through a broken /responses path — reject error-shaped 2xx.
    _fake_responses_probe(monkeypatch, 200, {"error": {"message": "responses not supported"}})
    cfg = pc.ProviderConfig("openai_compatible", "m", "k", "https://relay.host/v1")
    assert pc.probe_responses_support(cfg) is False


def test_probe_responses_support_true_on_2xx_response_object(monkeypatch):
    # A genuine Responses API success returns object="response" (no error key).
    _fake_responses_probe(monkeypatch, 200, {"object": "response", "status": "completed"})
    cfg = pc.ProviderConfig("openai_compatible", "gpt-5.4", "k", "https://relay.host/v1")
    assert pc.probe_responses_support(cfg) is True


def test_probe_responses_support_false_on_network_error(monkeypatch):
    class BoomClient(_StatusClient):
        def post(self, *a, **k):
            raise pc.httpx.ConnectError("boom")

    monkeypatch.setattr(pc.httpx, "Client", BoomClient)
    monkeypatch.setattr(pc, "_shared_client", None)
    cfg = pc.ProviderConfig("openai_compatible", "m", "k", "https://relay.host/v1")
    # ambiguous failure → safe default is the bridge (False), never crash
    assert pc.probe_responses_support(cfg) is False
