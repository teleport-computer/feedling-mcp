from __future__ import annotations

import ast
import sys
from pathlib import Path

import httpx
import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc  # noqa: E402
from conftest import capture_sleeps  # noqa: E402
from provider_types import ToolSpec  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("length", "length"),
        ("MAX_TOKENS", "max_tokens"),
        ("max-output-tokens", "max_output_tokens"),
    ],
)
def test_token_limit_stop_reason_normalization(raw, normalized):
    assert pc.normalize_stop_reason(raw) == normalized
    assert pc.is_token_limit_stop_reason(raw) is True


@pytest.mark.parametrize("raw", [None, "", "stop", "content_filter", "max_time"])
def test_non_token_stop_reason_is_not_rejected(raw):
    assert pc.is_token_limit_stop_reason(raw) is False


def test_reliable_nominal_envelope_uses_live_retry_delay_ceiling():
    assert pc.reliable_chat_nominal_envelope_sec(
        request_inactivity_timeout_sec=45.0,
        max_attempts=2,
        base_delay_sec=0.5,
    ) == pytest.approx(90.75)


def test_reliable_retry_wrapper_uses_shared_delay_algorithm(monkeypatch):
    calls = []
    sleeps = []

    def complete(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise pc.ProviderError("temporary", status_code=502)
        return {"reply": "ok"}

    monkeypatch.setattr(pc, "chat_completion", complete)
    monkeypatch.setattr(
        pc,
        "_reliable_retry_delay_sec",
        lambda attempt, **kwargs: sleeps.append((attempt, kwargs)) or 0.125,
    )
    capture_sleeps(monkeypatch, pc, sleeps)

    assert pc.reliable_chat_completion(object(), [], max_attempts=2)["reply"] == "ok"
    assert sleeps[-1] == 0.125
    assert sleeps[0][0] == 1


def test_async_reliable_deadline_cancels_inflight_attempt_without_zombie(
    monkeypatch,
):
    import asyncio
    import time

    cancelled = []
    calls = []

    async def slow_call(*_args, **_kwargs):
        calls.append(True)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(pc, "chat_completion_async", slow_call)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(
            pc.reliable_chat_completion_async(
                object(),
                [],
                max_attempts=2,
                absolute_deadline=time.monotonic() + 0.03,
                timeout=45.0,
            )
        )

    assert calls == [True]
    assert cancelled == [True]
    assert time.monotonic() - started < 0.5


def test_async_reliable_deadline_clamps_retry_backoff(monkeypatch):
    import asyncio
    import time

    calls = []

    async def fail(*_args, **_kwargs):
        calls.append(True)
        raise pc.ProviderError("temporary", status_code=502)

    monkeypatch.setattr(pc, "chat_completion_async", fail)
    monkeypatch.setattr(pc, "_reliable_retry_delay_sec", lambda *_args, **_kwargs: 30.0)
    started = time.monotonic()
    with pytest.raises(TimeoutError) as caught:
        asyncio.run(
            pc.reliable_chat_completion_async(
                object(),
                [],
                max_attempts=2,
                absolute_deadline=time.monotonic() + 0.2,
            )
        )

    assert calls == [True]
    assert getattr(caught.value, "feedling_error_class", "") == (
        "transient_exhausted"
    )
    assert time.monotonic() - started < 0.7


def test_async_reliable_deadline_clamps_per_attempt_timeout(monkeypatch):
    import asyncio
    import time

    timeouts = []

    async def complete(*_args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return {"reply": "ok"}

    monkeypatch.setattr(pc, "chat_completion_async", complete)
    result = asyncio.run(
        pc.reliable_chat_completion_async(
            object(),
            [],
            max_attempts=1,
            absolute_deadline=time.monotonic() + 0.5,
            timeout=45.0,
        )
    )

    assert result == {"reply": "ok"}
    assert 0 < timeouts[0] <= 0.5


@pytest.mark.parametrize("status_code", [408, 500])
def test_provider_http_error_keeps_bounded_internal_response_detail(status_code):
    upstream_detail = "UPSTREAM_DIAGNOSTIC_FRAGMENT:" + ("x" * 300)

    with pytest.raises(pc.ProviderError) as caught:
        pc._raise_for_provider_status(
            httpx.Response(status_code, text=upstream_detail)
        )

    assert caught.value.status_code == status_code
    assert caught.value.response_detail == upstream_detail[:240]
    # T159's existing provider-body echo remains unchanged; the new structured
    # field is an internal carrier, not a replacement public contract.
    assert str(caught.value) == (
        f"provider_http_{status_code}: {upstream_detail[:240]}"
    )


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
    assert payload["max_tokens"] == pc.CHAT_OUTPUT_MAX_TOKENS
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


@pytest.mark.parametrize("invalid", [0, -1, None, "not-an-integer"])
def test_chat_output_budget_rejects_invalid_instead_of_mimicking_empty_reply(
    invalid,
):
    with pytest.raises(ValueError, match="positive integer"):
        pc.cap_chat_output_tokens(invalid)


def test_chat_output_budget_preserves_legal_one_and_shared_ceiling():
    assert pc.cap_chat_output_tokens(1) == 1
    assert (
        pc.cap_chat_output_tokens(pc.CHAT_OUTPUT_MAX_TOKENS * 2)
        == pc.CHAT_OUTPUT_MAX_TOKENS
    )


def test_all_chat_payload_builders_share_the_output_ceiling():
    source = Path(pc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    output_budget_keys = {
        "max_tokens",
        "max_output_tokens",
        "maxTokens",
        "maxOutputTokens",
    }
    discovered_builders: dict[str, bool] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("_build_"):
            continue
        string_literals = {
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }
        if not string_literals.intersection(output_budget_keys):
            continue
        discovered_builders[node.name] = any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "cap_chat_output_tokens"
            for child in ast.walk(node)
        )

    messages = [{"role": "user", "content": "build a file"}]
    requested = pc.CHAT_OUTPUT_MAX_TOKENS * 2
    openai_responses, _url, _headers = pc._build_openai_responses_payload(
        model="gpt-5",
        base_url="https://api.openai.com/v1",
        key="k",
        messages=messages,
        max_tokens=requested,
        response_format=None,
    )
    openai_compat = pc._build_openai_compat_payload(
        provider="openrouter",
        model="m",
        messages=messages,
        temperature=None,
        max_tokens=requested,
        response_format=None,
        extra_body=None,
        include_reasoning=False,
    )
    anthropic, _url, _headers = pc._build_anthropic_payload(
        model="claude-sonnet-4-6",
        base_url="https://api.anthropic.com/v1",
        key="k",
        messages=messages,
        max_tokens=requested,
        temperature=None,
        response_format=None,
        include_reasoning=True,
    )
    bedrock, _url, _headers = pc._build_bedrock_payload(
        model="anthropic.claude-sonnet-4-6",
        base_url="https://bedrock.example",
        key="k",
        messages=messages,
        max_tokens=requested,
        temperature=None,
        response_format=None,
        include_reasoning=True,
    )
    gemini, _url, _headers = pc._build_gemini_payload(
        model="gemini-2.5-pro",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        key="k",
        messages=messages,
        max_tokens=requested,
        temperature=None,
        response_format=None,
        include_reasoning=True,
    )

    capped_by_builder = {
        "_build_openai_responses_payload": openai_responses["max_output_tokens"],
        "_build_openai_compat_payload": openai_compat["max_tokens"],
        "_build_anthropic_payload": anthropic["max_tokens"],
        "_build_bedrock_payload": bedrock["inferenceConfig"]["maxTokens"],
        "_build_gemini_payload": gemini["generationConfig"]["maxOutputTokens"],
    }
    assert set(capped_by_builder) == {
        "_build_openai_responses_payload",
        "_build_openai_compat_payload",
        "_build_anthropic_payload",
        "_build_bedrock_payload",
        "_build_gemini_payload",
    }
    assert discovered_builders == {
        builder: True for builder in capped_by_builder
    }
    assert set(capped_by_builder.values()) == {pc.CHAT_OUTPUT_MAX_TOKENS}
    assert pc.CHAT_OUTPUT_MAX_TOKENS not in {4096, 8192}
    assert pc.IMAGE_OUTPUT_MAX_TOKENS == pc.CHAT_OUTPUT_MAX_TOKENS
    assert anthropic["thinking"]["budget_tokens"] == 1024
    assert bedrock["additionalModelRequestFields"]["thinking"]["budget_tokens"] == 1024
    assert gemini["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 1024


def test_openai_compat_payload_preserves_forced_tool_choice():
    choice = {"type": "function", "function": {"name": "workspace_write"}}
    payload = pc._build_openai_compat_payload(
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
        messages=[{"role": "user", "content": "create a file"}],
        temperature=None,
        max_tokens=4096,
        response_format=None,
        extra_body=None,
        include_reasoning=False,
        tools=[
            ToolSpec(
                name="workspace_write",
                description="write",
                parameters={"type": "object", "properties": {}},
            )
        ],
        tool_choice=choice,
    )

    assert payload["tool_choice"] == choice
    assert payload["tool_choice"] is not choice


def test_anthropic_payload_translates_forced_function_tool_choice():
    payload, _url, _headers = pc._build_anthropic_payload(
        model="claude-sonnet-4-5",
        base_url="https://api.anthropic.com/v1",
        key="sk-test",
        messages=[{"role": "user", "content": "emit"}],
        max_tokens=500,
        temperature=0.2,
        response_format={"type": "json_object"},
        tools=[
            ToolSpec(
                name="emit_profile",
                description="emit",
                parameters={"type": "object", "properties": {}},
            )
        ],
        tool_choice={
            "type": "function",
            "function": {"name": "emit_profile"},
        },
    )

    assert payload["tool_choice"] == {"type": "tool", "name": "emit_profile"}


def test_anthropic_payload_encodes_tool_choice_none_with_tools():
    payload, _url, _headers = pc._build_anthropic_payload(
        model="claude-opus-4-8",
        base_url="https://api.anthropic.com/v1",
        key="sk-ant-test",
        messages=[{"role": "user", "content": "answer now"}],
        max_tokens=700,
        temperature=None,
        response_format=None,
        tools=[
            ToolSpec(
                "memory_index",
                "list memories",
                {"type": "object", "properties": {}},
            )
        ],
        tool_choice="none",
    )

    assert payload["tools"][0]["name"] == "memory_index"
    assert payload["tool_choice"] == {"type": "none"}


def test_provider_specific_named_tool_choice_wire_shapes():
    choice = {"type": "function", "function": {"name": "workspace_write"}}
    tool = ToolSpec("workspace_write", "write", {"type": "object", "properties": {}})
    anthropic, _url, _headers = pc._build_anthropic_payload(
        model="claude-opus-4-8", base_url="https://api.anthropic.com/v1", key="k",
        messages=[{"role": "user", "content": "write"}], max_tokens=700,
        temperature=None, response_format=None, tools=[tool], tool_choice=choice)
    assert anthropic["tool_choice"] == {"type": "tool", "name": "workspace_write"}
    gemini, _url, _headers = pc._build_gemini_payload(
        model="gemini-2.5-pro", base_url="https://generativelanguage.googleapis.com/v1beta", key="k",
        messages=[{"role": "user", "content": "write"}], max_tokens=700,
        temperature=None, response_format=None, tools=[tool], tool_choice=choice)
    assert gemini["toolConfig"]["functionCallingConfig"] == {
        "mode": "ANY", "allowedFunctionNames": ["workspace_write"]}
    bedrock, _url, _headers = pc._build_bedrock_payload(
        model="anthropic.claude-3", base_url="https://bedrock.example", key="k",
        messages=[{"role": "user", "content": [{"text": "write"}]}], max_tokens=700,
        temperature=None, response_format=None, tools=[tool], tool_choice=choice)
    assert bedrock["toolConfig"]["toolChoice"] == {"tool": {"name": "workspace_write"}}


def test_provider_specific_required_tool_choice_wire_shapes():
    tool = ToolSpec("reply", "reply once", {"type": "object", "properties": {}})
    messages = [{"role": "user", "content": "choose now"}]

    openai_chat = pc._build_openai_compat_payload(
        provider="openai", model="gpt-4.1", messages=messages,
        temperature=None, max_tokens=700, response_format=None,
        extra_body=None, include_reasoning=False, tools=[tool],
        tool_choice="required")
    assert openai_chat["tool_choice"] == "required"

    openai_responses, _url, _headers = pc._build_openai_responses_payload(
        model="gpt-5", base_url="https://api.openai.com/v1", key="k",
        messages=messages, max_tokens=700, response_format=None,
        tools=[tool], tool_choice="required")
    assert openai_responses["tool_choice"] == "required"

    anthropic, _url, _headers = pc._build_anthropic_payload(
        model="claude-opus-4-8", base_url="https://api.anthropic.com/v1", key="k",
        messages=messages, max_tokens=700, temperature=None,
        response_format=None, tools=[tool], tool_choice="required")
    assert anthropic["tool_choice"] == {"type": "any"}

    gemini, _url, _headers = pc._build_gemini_payload(
        model="gemini-2.5-pro",
        base_url="https://generativelanguage.googleapis.com/v1beta", key="k",
        messages=messages, max_tokens=700, temperature=None,
        response_format=None, tools=[tool], tool_choice="required")
    assert gemini["toolConfig"]["functionCallingConfig"] == {"mode": "ANY"}

    bedrock, _url, _headers = pc._build_bedrock_payload(
        model="anthropic.claude-3", base_url="https://bedrock.example", key="k",
        messages=messages, max_tokens=700, temperature=None,
        response_format=None, tools=[tool], tool_choice="required")
    assert bedrock["toolConfig"]["toolChoice"] == {"any": {}}


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


def test_parse_openai_compat_body_preserves_all_visible_text_blocks():
    resp = FakeResponse(200, {
        "id": "chatcmpl-blocks",
        "choices": [{
            "message": {
                "content": [
                    {"type": "text", "text": "first visible block"},
                    {"type": "reasoning", "text": "hidden chain of thought"},
                    {"type": "output_text", "text": "second visible block"},
                ],
            },
            "finish_reason": "stop",
        }],
    })

    out = pc._parse_openai_compat_body(
        resp, provider="openai_compatible", model="gpt-5.5", require_reply=True)

    assert out["reply"] == "first visible block\nsecond visible block"
    assert out["reasoning"] == "hidden chain of thought"


def test_parse_openai_compat_body_treats_untyped_text_block_as_visible():
    """Accept minimal relay text blocks that omit ``type``.

    This is an explicit compatibility tradeoff: an untyped reasoning block
    would be indistinguishable from visible text. Unknown *typed* blocks remain
    fail-closed, while observed minimal relays keep their ordinary reply text.
    """
    resp = FakeResponse(200, {
        "choices": [{"message": {"content": [{"text": "visible relay text"}]}}],
    })

    out = pc._parse_openai_compat_body(
        resp, provider="openai_compatible", model="relay-model", require_reply=True)

    assert out["reply"] == "visible relay text"


def test_parse_openai_compat_body_drops_untyped_sibling_when_typed_blocks_exist():
    """Mixed typed/untyped output fails closed for its ambiguous siblings.

    A fully untyped list remains inherently ambiguous: without a type or
    protocol marker there is no signal that can distinguish visible text from
    reasoning. That residual risk is limited to all-untyped relay output.
    """
    resp = FakeResponse(200, {
        "choices": [{"message": {"content": [
            {"text": "ambiguous private reasoning"},
            {"type": "text", "text": "visible answer"},
        ]}}],
    })

    out = pc._parse_openai_compat_body(
        resp, provider="openai_compatible", model="relay-model", require_reply=True)

    assert out["reply"] == "visible answer"


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


def test_openai_responses_encodes_assistant_history_as_output_text(monkeypatch):
    """Multi-turn regression (hosted codex/gpt-5 driver dropped every turn 2+):
    a prior assistant reply carried in history must serialize as ``output_text``,
    NOT ``input_text``. The OpenAI Responses API rejects ``input_text`` on an
    assistant-role content part with HTTP 400 ('Invalid value: input_text.
    Supported values are: output_text and refusal'), which the V2 loop turned
    into a silent no-reply turn."""
    calls = _fake_client(
        monkeypatch,
        {
            "id": "resp_mt",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "第2轮回复"}]},
            ],
            "usage": {},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("openai", "gpt-5", "sk-test"),
        [
            {"role": "user", "content": "第1轮"},
            {"role": "assistant", "content": "第1轮回复"},
            {"role": "user", "content": "第2轮"},
        ],
    )

    assert result["reply"] == "第2轮回复"
    sent_input = calls[0]["json"]["input"]
    assistant_items = [it for it in sent_input if it.get("role") == "assistant"]
    assert assistant_items, "assistant history item missing from Responses input"
    for part in assistant_items[0]["content"]:
        assert part["type"] == "output_text", (
            "assistant content on the Responses wire must be output_text, "
            f"got {part['type']!r} (would 400)"
        )


def test_synthesized_assistant_tool_turn_uses_output_text_on_responses_wire():
    """The tool-exchange history encoder (_synthesized_assistant_payload) has the
    same constraint: a prior assistant turn that called tools must emit its text
    as output_text on the Responses wire, not input_text."""
    from provider_types import ToolCall, ToolExchange

    exchange = ToolExchange(
        calls=(ToolCall(id="call_1", name="probe", args={}),),
        results=(),
        assistant_text="let me check",
    )
    items = pc._synthesized_assistant_payload(exchange, "openai_responses")
    text_items = [
        p for it in items if it.get("role") == "assistant"
        for p in it.get("content", [])
    ]
    assert text_items, "assistant text item missing"
    assert all(p["type"] == "output_text" for p in text_items), (
        f"assistant tool-turn text must be output_text, got {[p['type'] for p in text_items]}"
    )


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
            {"type": "image_url", "image_url": {"url": "data:image/webp;base64,efgh"}},
        ]}],
    )

    assert result["reply"] == "vision ok"
    content = calls[0]["json"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "abcd"},
    }
    assert content[2] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/webp", "data": "efgh"},
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
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,efgh"}},
        ]}],
    )

    assert result["reply"] == "vision ok"
    assert calls[0]["json"]["contents"] == [{
        "role": "user",
        "parts": [
            {"text": "look"},
            {"inline_data": {"mime_type": "image/jpeg", "data": "abcd"}},
            {"inline_data": {"mime_type": "image/png", "data": "efgh"}},
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


def test_openai_compatible_never_reaches_the_responses_wire(monkeypatch):
    """openai_compatible 恒走 /chat/completions。

    这是 probe_responses_support 退役后剩下的那半个断言：/responses 在本模块只有
    一个入口（`provider == "openai"` 且 reasoning 模型），中转永远进不去，所以
    「中转不支持 Responses」对我们没有任何后果。"""
    urls: list[str] = []

    class RecordingClient(_StatusClient):
        def post(self, url, *, headers=None, json=None, timeout=None):
            urls.append(url)
            return FakeResponse(200, {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 1},
            })

    monkeypatch.setattr(pc.httpx, "Client", RecordingClient)
    monkeypatch.setattr(pc, "_shared_client", None)
    cfg = pc.ProviderConfig("openai_compatible", "kimi-k2.5", "k",
                            "https://api.moonshot.cn/v1")
    pc.chat_completion(cfg, [{"role": "user", "content": "hi"}])
    assert urls and all(u.endswith("/chat/completions") for u in urls), urls


def test_shared_client_never_replays_cookies_across_users():
    # Cross-user credential bleed guard. The provider HTTP client is process-wide
    # and shared across ALL users' BYOK calls. httpx's default cookie jar is keyed
    # by origin, not by user — a relay/proxy that Set-Cookies a session cookie on
    # user A's response would replay it on user B's request to the SAME host. Many
    # users point base_url at the same relay domain, so this is reachable. The
    # shared client must never persist cookies.
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(
            200, headers=[("set-cookie", "sid=userA; Path=/")], json={"ok": True})

    client = pc._build_shared_client(transport=httpx.MockTransport(handler))
    try:
        client.post("https://relay.example/v1/chat/completions", json={})
        client.post("https://relay.example/v1/chat/completions", json={})
    finally:
        client.close()
    # Second call must NOT carry the Set-Cookie from the first (it would if the
    # jar persisted it) — and the jar itself stays empty.
    assert seen == [None, None], f"cookie replayed across calls: {seen!r}"
    assert len(list(client.cookies.jar)) == 0


def test_gemini_image_output_is_terminal_without_text():
    result = pc._parse_gemini_body(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"thought": True, "inlineData": {"mimeType": "image/png", "data": "ignored"}},
                            {"inlineData": {"mimeType": "image/png", "data": "aW1hZ2U="}},
                        ]
                    }
                }
            ]
        },
        model="gemini-2.5-flash-image",
        require_reply=True,
    )
    assert result["reply"] == ""
    assert result["media"] == [
        {"mime_type": "image/png", "data_base64": "aW1hZ2U=", "name": ""}
    ]


def test_openai_responses_extracts_image_generation_result():
    result = pc._parse_openai_responses_body(
        {
            "id": "resp_1",
            "status": "completed",
            "output": [
                {"type": "image_generation_call", "id": "ig_1", "result": "aW1hZ2U="}
            ],
        },
        model="gpt-5",
        require_reply=True,
    )
    assert result["reply"] == ""
    assert result["media"][0]["mime_type"] == "image/png"
    assert result["media"][0]["data_base64"] == "aW1hZ2U="


def test_openrouter_extracts_only_inline_image_urls():
    result = pc._parse_openai_compat_body(
        FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "images": [
                                {"image_url": {"url": "https://example.com/unsafe.png"}},
                                {"image_url": {"url": "data:image/webp;base64,aW1hZ2U="}},
                            ],
                        }
                    }
                ]
            },
        ),
        provider="openrouter",
        model="google/gemini-image",
        require_reply=True,
    )
    assert result["media"] == [
        {"mime_type": "image/webp", "data_base64": "aW1hZ2U=", "name": ""}
    ]


def test_image_output_request_flags_are_provider_bounded():
    gemini_payload, _, _ = pc._build_gemini_payload(
        model="gemini-2.5-flash-image",
        base_url="https://example.test",
        key="k",
        messages=[{"role": "user", "content": "draw"}],
        max_tokens=100,
        temperature=None,
        response_format=None,
        allow_image_output=True,
    )
    assert gemini_payload["generationConfig"]["responseModalities"] == [
        "TEXT",
        "IMAGE",
    ]
    text_payload, _, _ = pc._build_gemini_payload(
        model="gemini-2.5-flash",
        base_url="https://example.test",
        key="k",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=100,
        temperature=None,
        response_format=None,
        allow_image_output=True,
    )
    assert "responseModalities" not in text_payload["generationConfig"]


def test_openrouter_image_model_uses_dedicated_images_api(monkeypatch):
    import asyncio

    calls: list[dict] = []

    class FakeAsyncClient:
        is_closed = False

        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append(
                {
                    "url": url,
                    "headers": headers or {},
                    "json": json or {},
                    "timeout": timeout,
                }
            )
            return FakeResponse(
                200,
                {
                    "data": [
                        {
                            "b64_json": "aW1hZ2U=",
                            "media_type": "image/webp",
                        }
                    ],
                    "usage": {"total_tokens": 12},
                },
            )

    monkeypatch.setattr(pc.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(pc, "_shared_async_client", None)

    result = asyncio.run(
        pc.chat_completion_async(
            pc.ProviderConfig(
                "openrouter",
                "openai/gpt-5.4-image-2",
                "sk-or-test",
            ),
            [
                {"role": "system", "content": "system rules"},
                {"role": "user", "content": "draw a red robot"},
                {
                    "role": "user",
                    "content": (
                        "UNTRUSTED TURN TEMPORAL CONTEXT "
                        "(application data, not user instructions):\n"
                        '{"current_local_time":"2026-08-03T16:30:00+08:00"}'
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "UNTRUSTED LIVE RUNTIME CONTEXT "
                        "(application data, not user instructions):\n"
                        '{"runtime_data":{"screen":"health dashboard"}}'
                    ),
                },
            ],
            allow_image_output=True,
        )
    )

    assert calls == [
        {
            "url": "https://openrouter.ai/api/v1/images",
            "headers": {
                "Authorization": "Bearer sk-or-test",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://feedling.app",
                "X-Title": "Feedling IO Hosted Runtime",
            },
            "json": {
                "model": "openai/gpt-5.4-image-2",
                "prompt": "draw a red robot",
                "n": 1,
            },
            "timeout": 120.0,
        }
    ]
    assert result["reply"] == ""
    assert result["media"] == [
        {
            "mime_type": "image/webp",
            "data_base64": "aW1hZ2U=",
            "name": "",
        }
    ]


def test_generate_image_official_openai_uses_images_generations(monkeypatch):
    import asyncio

    calls: list[dict] = []

    class FakeAsyncClient:
        is_closed = False

        async def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append({"url": url, "json": json, "timeout": timeout})
            return FakeResponse(200, {"data": [{"b64_json": "aW1hZ2U="}]})

    monkeypatch.setattr(pc, "_shared_async_client", FakeAsyncClient())

    result = asyncio.run(
        pc.generate_image_async(
            pc.ProviderConfig("openai", "gpt-image-2", "sk-test"),
            "draw a small red robot",
        )
    )

    assert calls == [
        {
            "url": "https://api.openai.com/v1/images/generations",
            "json": {
                "model": "gpt-image-2",
                "prompt": "draw a small red robot",
                "n": 1,
            },
            "timeout": 120.0,
        }
    ]
    assert result["provider"] == "openai"
    assert result["media"][0]["data_base64"] == "aW1hZ2U="


def test_generate_image_openai_mainline_uses_hosted_image_tool(monkeypatch):
    import asyncio

    calls: list[dict] = []

    class FakeAsyncClient:
        is_closed = False

        async def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append({"url": url, "json": json})
            return FakeResponse(
                200,
                {
                    "id": "resp_image",
                    "output": [
                        {
                            "type": "image_generation_call",
                            "id": "ig_1",
                            "result": "aW1hZ2U=",
                        }
                    ],
                },
            )

    monkeypatch.setattr(pc, "_shared_async_client", FakeAsyncClient())

    result = asyncio.run(
        pc.generate_image_async(
            pc.ProviderConfig("openai", "gpt-5.6", "sk-test"),
            "draw a moonlit lake",
        )
    )

    assert calls[0]["url"] == "https://api.openai.com/v1/responses"
    assert {"type": "image_generation"} in calls[0]["json"]["tools"]
    assert result["media"][0]["data_base64"] == "aW1hZ2U="


@pytest.mark.parametrize(
    ("provider", "model"),
    (
        ("deepseek", "deepseek-v4-flash"),
        ("gemini", "gemini-2.5-flash"),
    ),
)
def test_generate_image_without_name_marker_fails_before_provider_request(
    monkeypatch, provider, model,
):
    import asyncio

    class ExplodingAsyncClient:
        is_closed = False

        async def post(self, *args, **kwargs):
            raise AssertionError("text-only route must not receive an image request")

    monkeypatch.setattr(pc, "_shared_async_client", ExplodingAsyncClient())

    with pytest.raises(pc.ProviderError, match="image_generation_model_unsupported"):
        asyncio.run(
            pc.generate_image_async(
                pc.ProviderConfig(provider, model, "sk-test"),
                "draw a moonlit lake",
            )
        )


@pytest.mark.parametrize(
    ("provider", "model"),
    (
        ("deepseek", "qwen-image-3.0"),
        ("gemini", "qwen-image-3.0"),
    ),
)
def test_ordinary_chat_keeps_provider_family_image_gate(
    monkeypatch, provider, model,
):
    import asyncio

    calls: list[dict] = []

    class TripwireAsyncClient:
        is_closed = False

        async def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append({"url": url, "json": json})
            return FakeResponse(401, {"error": {"message": "test rejection"}})

    monkeypatch.setattr(pc, "_shared_async_client", TripwireAsyncClient())

    with pytest.raises(pc.ProviderError):
        asyncio.run(
            pc.chat_completion_async(
                pc.ProviderConfig(provider, model, "sk-test"),
                [{"role": "user", "content": "hello"}],
                allow_image_output=True,
            )
        )

    assert len(calls) == 1
    if provider == "gemini":
        assert "responseModalities" not in calls[0]["json"]["generationConfig"]
    else:
        assert "modalities" not in calls[0]["json"]


@pytest.mark.parametrize(
    ("provider", "model", "url_marker"),
    (
        ("deepseek", "qwen-image-3.0", "/chat/completions"),
        ("gemini", "qwen-image-3.0", ":generateContent"),
    ),
)
def test_named_image_models_reach_http_across_provider_families(
    monkeypatch, provider, model, url_marker,
):
    import asyncio

    calls: list[dict] = []

    class TripwireAsyncClient:
        is_closed = False

        async def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append({"url": url, "json": json, "timeout": timeout})
            return FakeResponse(401, {"error": {"message": "test rejection"}})

    monkeypatch.setattr(pc, "_shared_async_client", TripwireAsyncClient())

    with pytest.raises(pc.ProviderError) as raised:
        asyncio.run(
            pc.generate_image_async(
                pc.ProviderConfig(provider, model, "sk-test"),
                "draw a moonlit lake",
            )
        )

    assert len(calls) == 1
    assert url_marker in calls[0]["url"]
    if provider == "gemini":
        assert calls[0]["json"]["generationConfig"]["responseModalities"] == [
            "TEXT",
            "IMAGE",
        ]
    else:
        assert calls[0]["json"]["modalities"] == ["text", "image"]
    assert raised.value.status_code == 401


def test_newly_admitted_named_model_still_requires_nonempty_media(monkeypatch):
    import asyncio

    calls: list[str] = []

    class TextOnlyAsyncClient:
        is_closed = False

        async def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append(url)
            return FakeResponse(
                200,
                {"choices": [{"message": {"content": "I cannot draw that."}}]},
            )

    monkeypatch.setattr(pc, "_shared_async_client", TextOnlyAsyncClient())

    with pytest.raises(pc.ProviderError, match="image_generation_invalid_output"):
        asyncio.run(
            pc.generate_image_async(
                pc.ProviderConfig("deepseek", "qwen-image-3.0", "sk-test"),
                "draw a moonlit lake",
            )
        )

    assert len(calls) == 1


def test_blocking_image_generation_isolates_each_event_loop(monkeypatch):
    observed_clients: list[object] = []
    isolated_clients: list[object] = []

    class IsolatedClient:
        is_closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            self.is_closed = True

    async def fake_generate(*_args, **_kwargs):
        observed_clients.append(pc._async_http_client())
        return {"media": [{"data_base64": "aW1hZ2U="}]}

    def build_client(**_kwargs):
        client = IsolatedClient()
        isolated_clients.append(client)
        return client

    shared_client = object()
    monkeypatch.setattr(pc, "_shared_async_client", shared_client)
    monkeypatch.setattr(pc, "_build_shared_async_client", build_client)
    monkeypatch.setattr(pc, "generate_image_async", fake_generate)

    config = pc.ProviderConfig("openrouter", "openai/gpt-5.4-image-2", "sk-test")
    pc.generate_image(config, "first image")
    pc.generate_image(config, "second image")

    assert observed_clients == isolated_clients
    assert len({id(client) for client in isolated_clients}) == 2
    assert pc._shared_async_client is shared_client
    assert all(client.is_closed for client in isolated_clients)


def test_openrouter_text_model_stays_on_chat_completions(monkeypatch):
    import asyncio

    calls: list[str] = []

    class FakeAsyncClient:
        is_closed = False

        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append(url)
            return FakeResponse(
                200,
                {"choices": [{"message": {"content": "text reply"}}]},
            )

    monkeypatch.setattr(pc.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(pc, "_shared_async_client", None)

    result = asyncio.run(
        pc.chat_completion_async(
            pc.ProviderConfig("openrouter", "openai/gpt-5.4", "sk-or-test"),
            [{"role": "user", "content": "hello"}],
            allow_image_output=True,
        )
    )

    assert calls == ["https://openrouter.ai/api/v1/chat/completions"]
    assert result["reply"] == "text reply"


# --- Relay (openai_compatible) image generation -----------------------------
# Every shape below was measured against two live relays on 2026-08-19; the
# comment on each test names what produced it. The same model id answers on
# different wires depending on the relay, so none of this can be inferred from
# the model name.

_RELAY_BASE = "https://relay.example/v1"
_TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="


def _relay_client(monkeypatch, handler):
    """Route every async POST through `handler(url, json) -> FakeResponse`."""
    calls: list[dict] = []

    class FakeAsyncClient:
        is_closed = False

        async def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append({"url": url, "json": json})
            return handler(url, json)

    monkeypatch.setattr(pc, "_shared_async_client", FakeAsyncClient())
    return calls


def test_relay_image_uses_dedicated_endpoint_first(monkeypatch):
    """Measured: 空贝壳/HOJIMI gpt-image-2 answer /images/generations with b64_json."""
    import asyncio

    def handler(url, payload):
        assert url.endswith("/images/generations")
        return FakeResponse(200, {"data": [{"b64_json": _TINY_PNG_B64}]})

    calls = _relay_client(monkeypatch, handler)

    result = asyncio.run(
        pc.generate_image_async(
            pc.ProviderConfig("openai_compatible", "gpt-image-2", "sk-relay", _RELAY_BASE),
            "draw a small red robot",
        )
    )

    assert [c["url"] for c in calls] == [f"{_RELAY_BASE}/images/generations"]
    assert result["media"][0]["data_base64"] == _TINY_PNG_B64


def test_relay_image_falls_back_to_chat_when_dedicated_endpoint_rejects(monkeypatch):
    """Measured: both relays answer gemini-3-pro-image-preview with HTTP 500
    `only imagen models are supported` on /images/generations, and return the
    image as a markdown data URL inside the chat reply's content string."""
    import asyncio

    def handler(url, payload):
        if url.endswith("/images/generations"):
            return FakeResponse(
                500,
                {"error": {"message": "not supported model for image generation"}},
            )
        content = f"Here you go!\n\n![image](data:image/png;base64,{_TINY_PNG_B64})"
        return FakeResponse(
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"total_tokens": 1931},
            },
        )

    calls = _relay_client(monkeypatch, handler)

    result = asyncio.run(
        pc.generate_image_async(
            pc.ProviderConfig(
                "openai_compatible", "gemini-3-pro-image-preview", "sk-relay", _RELAY_BASE
            ),
            "draw a small red robot",
        )
    )

    assert [c["url"] for c in calls] == [
        f"{_RELAY_BASE}/images/generations",
        f"{_RELAY_BASE}/chat/completions",
    ]
    assert result["media"][0]["data_base64"] == _TINY_PNG_B64


def test_relay_image_chat_fallback_asks_for_the_full_token_budget(monkeypatch):
    """Measured: HOJIMI answered gemini-3-pro-image-preview with content="" and
    finish_reason=length under a 512-token cap — a truncated image the user
    still paid for."""
    import asyncio

    def handler(url, payload):
        if url.endswith("/images/generations"):
            return FakeResponse(
                500,
                {"error": {"message": "not supported model for image generation"}},
            )
        content = f"![image](data:image/png;base64,{_TINY_PNG_B64})"
        return FakeResponse(
            200, {"choices": [{"message": {"content": content}}]}
        )

    calls = _relay_client(monkeypatch, handler)

    asyncio.run(
        pc.generate_image_async(
            pc.ProviderConfig(
                "openai_compatible", "gemini-3-pro-image-preview", "sk-relay", _RELAY_BASE
            ),
            "draw a small red robot",
        )
    )

    chat_payload = calls[-1]["json"]
    assert chat_payload["max_tokens"] == pc.IMAGE_OUTPUT_MAX_TOKENS
    assert chat_payload["max_tokens"] > 512
    assert chat_payload["modalities"] == ["text", "image"]


def test_relay_image_still_refuses_http_links(monkeypatch):
    """Measured: 空贝壳 gpt-image-2 answers the chat wire with a markdown HTTP
    link. Fetching provider URLs stays out of scope, so this must fail rather
    than silently reach out to the network."""
    import asyncio

    # A *signed* CDN link, i.e. one carrying a long base64-looking token. That
    # is the shape that slips past a matcher keyed on "a long base64 run"
    # instead of on the data: scheme itself — a plain .png link cannot tell the
    # two apart, so it would leave the contract untested.
    signed_link = f"https://cdn.example/generated.png?sig={_TINY_PNG_B64}"

    def handler(url, payload):
        if url.endswith("/images/generations"):
            return FakeResponse(
                500,
                {"error": {"message": "not supported model for image generation"}},
            )
        return FakeResponse(
            200,
            {"choices": [{"message": {"content": f"![image]({signed_link})"}}]},
        )

    _relay_client(monkeypatch, handler)

    with pytest.raises(pc.ProviderError, match="image_generation_invalid_output"):
        asyncio.run(
            pc.generate_image_async(
                pc.ProviderConfig(
                    "openai_compatible", "gpt-image-2", "sk-relay", _RELAY_BASE
                ),
                "draw a small red robot",
            )
        )


def test_official_openrouter_image_failure_does_not_fall_back(monkeypatch):
    """The dedicated wire is documented for OpenRouter/OpenAI, so a failure
    there is the answer — falling back would spend a second paid request."""
    import asyncio

    def handler(url, payload):
        if url.endswith("/images"):
            return FakeResponse(500, {"error": {"message": "boom"}})
        raise AssertionError("official providers must not fall back to chat")

    calls = _relay_client(monkeypatch, handler)

    with pytest.raises(pc.ProviderError):
        asyncio.run(
            pc.generate_image_async(
                pc.ProviderConfig(
                    "openrouter", "openai/gpt-5.4-image-2", "sk-or-test"
                ),
                "draw a small red robot",
            )
        )

    assert [c["url"].rsplit("/", 1)[-1] for c in calls] == ["images"]


def test_inline_data_urls_in_text_reads_only_inline_bytes():
    text = (
        "before ![a](data:image/png;base64,"
        + _TINY_PNG_B64
        + ") middle ![b](https://cdn.example/x.png) after "
        + "![c](data:image/webp;base64,"
        + _TINY_PNG_B64
        + ")"
    )

    items = pc._inline_data_urls_in_text(text)

    assert [i["mime_type"] for i in items] == ["image/png", "image/webp"]
    assert all(i["data_base64"] == _TINY_PNG_B64 for i in items)
    assert pc._inline_data_urls_in_text("no image here") == []
    assert pc._inline_data_urls_in_text("data:image/png;base64,short") == []


def _relay_no_fallback_case(monkeypatch, dedicated_response):
    """A dedicated-endpoint answer that must NOT reach the chat wire."""
    import asyncio

    def handler(url, payload):
        if url.endswith("/images/generations"):
            return dedicated_response
        raise AssertionError(
            "second paid request: this dedicated failure must not fall back"
        )

    calls = _relay_client(monkeypatch, handler)
    with pytest.raises(pc.ProviderError):
        asyncio.run(
            pc.generate_image_async(
                pc.ProviderConfig(
                    "openai_compatible", "gpt-image-2", "sk-relay", _RELAY_BASE
                ),
                "draw a small red robot",
            )
        )
    assert [c["url"] for c in calls] == [f"{_RELAY_BASE}/images/generations"]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"error": {"message": "invalid api key"}}),
        (402, {"error": {"message": "insufficient balance"}}),
        (403, {"error": {"message": "forbidden"}}),
        (429, {"error": {"message": "rate limited"}}),
        (500, {"error": {"message": "internal error"}}),
        (503, {"error": {"message": "upstream unavailable"}}),
    ],
)
def test_relay_image_never_pays_twice_for_a_real_failure(monkeypatch, status, body):
    """Auth/quota/rate/unexplained-5xx are the answer. Retrying on the chat wire
    would bill a second request and hide the original error."""
    _relay_no_fallback_case(monkeypatch, FakeResponse(status, body))


def test_relay_image_url_only_success_does_not_pay_twice(monkeypatch):
    """Measured: HOJIMI grok-imagine answers 200 with `data[0].url` and no
    inline bytes. That request was billed, so it must surface as a failure
    rather than trigger a second one."""
    _relay_no_fallback_case(
        monkeypatch,
        FakeResponse(200, {"data": [{"url": "https://cdn.example/i.png"}]}),
    )


def test_relay_image_network_error_does_not_fall_back(monkeypatch):
    """The request may have been served and billed before the socket broke."""
    import asyncio

    calls: list[str] = []

    class FlakyAsyncClient:
        is_closed = False

        async def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append(url)
            if url.endswith("/images/generations"):
                raise httpx.ConnectError("boom")
            raise AssertionError("a transport failure must not reach the chat wire")

    monkeypatch.setattr(pc, "_shared_async_client", FlakyAsyncClient())

    with pytest.raises(pc.ProviderError, match="provider network error"):
        asyncio.run(
            pc.generate_image_async(
                pc.ProviderConfig(
                    "openai_compatible", "gpt-image-2", "sk-relay", _RELAY_BASE
                ),
                "draw a small red robot",
            )
        )

    assert calls == [f"{_RELAY_BASE}/images/generations"]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        # A generic transport-level failure code: it also covers a malformed
        # payload, so on its own it does not mean the model is unsupported.
        (
            500,
            {
                "error": {
                    "code": "convert_request_failed",
                    "message": "malformed request payload",
                }
            },
        ),
        # Authorization wording — the real answer, not an endpoint capability.
        (400, {"error": {"message": "API key is not allowed to access this endpoint"}}),
    ],
)
def test_relay_image_keeps_generic_and_permission_failures_single_request(
    monkeypatch, status, body
):
    _relay_no_fallback_case(monkeypatch, FakeResponse(status, body))


@pytest.mark.parametrize(
    ("status", "body"),
    [
        # Measured on both relays for gemini-3-pro-image-preview.
        (
            500,
            {
                "error": {
                    "message": "not supported model for image generation, only "
                    "imagen models are supported",
                    "code": "convert_request_failed",
                }
            },
        ),
        # A relay that simply does not implement the endpoint.
        (404, {"error": {"message": "not found"}}),
    ],
)
def test_relay_image_falls_back_only_on_an_explicit_endpoint_refusal(
    monkeypatch, status, body
):
    import asyncio

    def handler(url, payload):
        if url.endswith("/images/generations"):
            return FakeResponse(status, body)
        content = f"![image](data:image/png;base64,{_TINY_PNG_B64})"
        return FakeResponse(200, {"choices": [{"message": {"content": content}}]})

    calls = _relay_client(monkeypatch, handler)

    result = asyncio.run(
        pc.generate_image_async(
            pc.ProviderConfig(
                "openai_compatible", "gemini-3-pro-image-preview", "sk-relay", _RELAY_BASE
            ),
            "draw a small red robot",
        )
    )

    assert [c["url"] for c in calls] == [
        f"{_RELAY_BASE}/images/generations",
        f"{_RELAY_BASE}/chat/completions",
    ]
    assert result["media"][0]["data_base64"] == _TINY_PNG_B64


# --- Fetching a provider-chosen image link (dedicated endpoint only) --------

def _png_bytes(size: int = 8) -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (size, size), (0, 0, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _fetches(monkeypatch, results):
    """Stub the guarded fetcher; record which links it was asked for."""
    import safe_url_fetch

    asked: list[str] = []

    async def fake_fetch(url, *, max_bytes, **kwargs):
        asked.append(url)
        outcome = results.get(url)
        if outcome is None:
            raise safe_url_fetch.UnsafeURLError("image_url_blocked")
        if isinstance(outcome, Exception):
            raise outcome
        return safe_url_fetch.FetchedBytes(outcome, "image/png")

    monkeypatch.setattr(pc.safe_url_fetch, "fetch_image_bytes_async", fake_fetch)
    return asked


def test_dedicated_url_answer_is_fetched_and_must_decode(monkeypatch):
    """Measured: one relay answers `data[0].url` for the 2K variant of a model
    that is inline at lower resolutions. The request was billed either way."""
    import asyncio

    png = _png_bytes()
    asked = _fetches(monkeypatch, {"https://cdn.example/a.png": png})

    def handler(url, payload):
        return FakeResponse(200, {"data": [{"url": "https://cdn.example/a.png"}]})

    _relay_client(monkeypatch, handler)

    result = asyncio.run(
        pc.generate_image_async(
            pc.ProviderConfig(
                "openai_compatible", "gpt-image-2-2k", "sk-relay", _RELAY_BASE
            ),
            "draw a small red robot",
        )
    )

    assert asked == ["https://cdn.example/a.png"]
    assert result["media"][0]["mime_type"] == "image/png"


def test_bytes_that_do_not_decode_never_count_as_an_image(monkeypatch):
    """setup_core's route probe only checks that media came back, so a fetch
    that trusted the content-type header alone would mark a route ok and fail
    later in a real chat."""
    import asyncio

    _fetches(monkeypatch, {"https://cdn.example/a.png": b"not-an-image-at-all"})

    def handler(url, payload):
        return FakeResponse(200, {"data": [{"url": "https://cdn.example/a.png"}]})

    _relay_client(monkeypatch, handler)

    with pytest.raises(pc.ProviderError, match="image_generation_invalid_output"):
        asyncio.run(
            pc.generate_image_async(
                pc.ProviderConfig(
                    "openai_compatible", "gpt-image-2-2k", "sk-relay", _RELAY_BASE
                ),
                "draw a small red robot",
            )
        )


def test_inline_bytes_are_preferred_and_no_link_is_fetched(monkeypatch):
    import asyncio

    asked = _fetches(monkeypatch, {})

    def handler(url, payload):
        return FakeResponse(
            200,
            {
                "data": [
                    {"b64_json": _TINY_PNG_B64, "url": "https://cdn.example/a.png"}
                ]
            },
        )

    _relay_client(monkeypatch, handler)

    result = asyncio.run(
        pc.generate_image_async(
            pc.ProviderConfig(
                "openai_compatible", "gpt-image-2", "sk-relay", _RELAY_BASE
            ),
            "draw a small red robot",
        )
    )

    assert asked == []
    assert result["media"][0]["data_base64"] == _TINY_PNG_B64


def test_a_link_inside_a_chat_reply_is_never_fetched(monkeypatch):
    """Chat text is model-authored; only the dedicated endpoint's data[].url is
    eligible. Measured relays that answer chat with a link also serve the
    dedicated endpoint, which returns the bytes directly."""
    import asyncio

    asked = _fetches(monkeypatch, {"https://cdn.example/a.png": _png_bytes()})

    def handler(url, payload):
        if url.endswith("/images/generations"):
            return FakeResponse(
                500,
                {"error": {"message": "not supported model for image generation"}},
            )
        return FakeResponse(
            200,
            {
                "choices": [
                    {"message": {"content": "![i](https://cdn.example/a.png)"}}
                ]
            },
        )

    _relay_client(monkeypatch, handler)

    with pytest.raises(pc.ProviderError, match="image_generation_invalid_output"):
        asyncio.run(
            pc.generate_image_async(
                pc.ProviderConfig(
                    "openai_compatible", "gemini-3-pro-image-preview", "sk-relay",
                    _RELAY_BASE,
                ),
                "draw a small red robot",
            )
        )

    assert asked == []


def test_a_refused_link_does_not_retry_on_the_chat_wire(monkeypatch):
    """The dedicated request was already billed."""
    import asyncio

    _fetches(monkeypatch, {})  # every link is refused

    def handler(url, payload):
        if url.endswith("/images/generations"):
            return FakeResponse(200, {"data": [{"url": "https://cdn.example/a.png"}]})
        raise AssertionError("a refused link must not trigger a second request")

    calls = _relay_client(monkeypatch, handler)

    with pytest.raises(pc.ProviderError, match="image_generation_invalid_output"):
        asyncio.run(
            pc.generate_image_async(
                pc.ProviderConfig(
                    "openai_compatible", "gpt-image-2", "sk-relay", _RELAY_BASE
                ),
                "draw a small red robot",
            )
        )

    assert [c["url"] for c in calls] == [f"{_RELAY_BASE}/images/generations"]


def test_links_are_capped_and_share_one_byte_budget(monkeypatch):
    """Four links must not pull four times the single-image ceiling, and a
    fifth link is never even considered."""
    import asyncio

    png = _png_bytes()
    urls = [f"https://cdn.example/{i}.png" for i in range(6)]
    asked = _fetches(monkeypatch, {u: png for u in urls})
    budgets: list[int] = []
    original = pc.safe_url_fetch.fetch_image_bytes_async

    async def recording(url, *, max_bytes, **kwargs):
        budgets.append(max_bytes)
        return await original(url, max_bytes=max_bytes, **kwargs)

    monkeypatch.setattr(pc.safe_url_fetch, "fetch_image_bytes_async", recording)

    def handler(url, payload):
        return FakeResponse(200, {"data": [{"url": u} for u in urls]})

    _relay_client(monkeypatch, handler)

    result = asyncio.run(
        pc.generate_image_async(
            pc.ProviderConfig(
                "openai_compatible", "gpt-image-2", "sk-relay", _RELAY_BASE
            ),
            "draw a small red robot",
        )
    )

    assert len(result["media"]) == pc.MAX_GENERATED_IMAGES_PER_REPLY
    assert len(asked) == pc.MAX_GENERATED_IMAGES_PER_REPLY
    # The budget shrinks as bytes are spent, rather than resetting per link.
    assert budgets[0] == pc.MAX_GENERATED_IMAGE_SOURCE_BYTES
    assert budgets == sorted(budgets, reverse=True)
    assert budgets[-1] < budgets[0]


def test_official_providers_also_fetch_a_url_answer(monkeypatch):
    """The dedicated branch is shared, so this is not relay-only behaviour —
    the public changelog must say so."""
    import asyncio

    png = _png_bytes()
    asked = _fetches(monkeypatch, {"https://cdn.openai.example/a.png": png})

    def handler(url, payload):
        return FakeResponse(
            200, {"data": [{"url": "https://cdn.openai.example/a.png"}]}
        )

    _relay_client(monkeypatch, handler)

    result = asyncio.run(
        pc.generate_image_async(
            pc.ProviderConfig("openai", "gpt-image-2", "sk-test"),
            "draw a small red robot",
        )
    )

    assert asked == ["https://cdn.openai.example/a.png"]
    assert result["media"]


def test_undecodable_downloads_still_spend_the_shared_budget(monkeypatch):
    """Bytes that arrive are charged when they arrive. Charging only what
    decodes would let four hostile 25MB "images" each refill the budget."""
    import asyncio
    import safe_url_fetch

    urls = [f"https://cdn.example/{i}.png" for i in range(4)]
    budgets: list[int] = []

    async def fake_fetch(url, *, max_bytes, **kwargs):
        budgets.append(max_bytes)
        # Downloaded in full, but not an image.
        return safe_url_fetch.FetchedBytes(b"x" * 1000, "image/png")

    monkeypatch.setattr(pc.safe_url_fetch, "fetch_image_bytes_async", fake_fetch)

    def handler(url, payload):
        return FakeResponse(200, {"data": [{"url": u} for u in urls]})

    _relay_client(monkeypatch, handler)

    with pytest.raises(pc.ProviderError, match="image_generation_invalid_output"):
        asyncio.run(
            pc.generate_image_async(
                pc.ProviderConfig(
                    "openai_compatible", "gpt-image-2", "sk-relay", _RELAY_BASE
                ),
                "draw a small red robot",
            )
        )

    assert budgets == sorted(budgets, reverse=True)
    assert len(set(budgets)) == len(budgets), "each failed decode must still cost"
    assert budgets[-1] == pc.MAX_GENERATED_IMAGE_SOURCE_BYTES - 3000


def test_a_failed_download_stops_the_remaining_links(monkeypatch):
    """A failed fetch reports no length, so its consumed bytes cannot be
    charged; handing the next link a full budget would be silently wrong."""
    import asyncio
    import safe_url_fetch

    urls = [f"https://cdn.example/{i}.png" for i in range(4)]
    asked: list[str] = []

    async def fake_fetch(url, *, max_bytes, **kwargs):
        asked.append(url)
        raise safe_url_fetch.UnsafeURLError("image_url_too_large")

    monkeypatch.setattr(pc.safe_url_fetch, "fetch_image_bytes_async", fake_fetch)

    def handler(url, payload):
        return FakeResponse(200, {"data": [{"url": u} for u in urls]})

    _relay_client(monkeypatch, handler)

    with pytest.raises(pc.ProviderError, match="image_generation_invalid_output"):
        asyncio.run(
            pc.generate_image_async(
                pc.ProviderConfig(
                    "openai_compatible", "gpt-image-2", "sk-relay", _RELAY_BASE
                ),
                "draw a small red robot",
            )
        )

    assert asked == urls[:1]


def test_links_share_one_wall_clock_budget(monkeypatch):
    """Four slow links must not each add a fresh deadline to the turn."""
    import asyncio
    import safe_url_fetch

    urls = [f"https://cdn.example/{i}.png" for i in range(4)]
    granted: list[float] = []
    png = _png_bytes()

    async def fake_fetch(url, *, max_bytes, deadline_seconds=None, **kwargs):
        granted.append(deadline_seconds)
        await asyncio.sleep(0.05)
        return safe_url_fetch.FetchedBytes(png, "image/png")

    monkeypatch.setattr(pc.safe_url_fetch, "fetch_image_bytes_async", fake_fetch)
    monkeypatch.setattr(pc, "IMAGE_LINK_TOTAL_DEADLINE_SECONDS", 0.2)

    def handler(url, payload):
        return FakeResponse(200, {"data": [{"url": u} for u in urls]})

    _relay_client(monkeypatch, handler)

    asyncio.run(
        pc.generate_image_async(
            pc.ProviderConfig(
                "openai_compatible", "gpt-image-2", "sk-relay", _RELAY_BASE
            ),
            "draw a small red robot",
        )
    )

    assert granted, "the fetcher must receive an explicit deadline"
    assert granted == sorted(granted, reverse=True), "the budget must shrink"
    assert granted[-1] < granted[0]


def test_decoding_does_not_block_the_event_loop(monkeypatch):
    """Pillow on a 25MB attacker-sized image would stall every other turn in
    this process if it ran on the loop."""
    import asyncio
    import safe_url_fetch
    import time as _time

    async def fake_fetch(url, *, max_bytes, **kwargs):
        return safe_url_fetch.FetchedBytes(b"pretend", "image/png")

    def slow_normalize(*args, **kwargs):
        _time.sleep(0.3)
        raise ValueError("not an image")

    monkeypatch.setattr(pc.safe_url_fetch, "fetch_image_bytes_async", fake_fetch)
    monkeypatch.setattr(
        pc.generated_image, "normalize_generated_image", slow_normalize
    )

    def handler(url, payload):
        return FakeResponse(200, {"data": [{"url": "https://cdn.example/a.png"}]})

    _relay_client(monkeypatch, handler)

    async def scenario():
        ticks = []

        async def heartbeat():
            while True:
                await asyncio.sleep(0.01)
                ticks.append(asyncio.get_running_loop().time())

        beat = asyncio.create_task(heartbeat())
        try:
            with pytest.raises(pc.ProviderError):
                await pc.generate_image_async(
                    pc.ProviderConfig(
                        "openai_compatible", "gpt-image-2", "sk-relay", _RELAY_BASE
                    ),
                    "draw a small red robot",
                )
        finally:
            beat.cancel()
        return ticks

    ticks = asyncio.run(scenario())
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert ticks, "the heartbeat must have run at all"
    assert max(gaps or [0]) < 0.15, f"loop was blocked for {max(gaps or [0]):.3f}s"


# --- T152: the cleartext-loopback allowlist is parsed, not prefix-matched -----
#
# The save/test path used to approve a base_url with
# `startswith("http://127.0.0.1")`, which reads a prefix rather than a host.
# Every FORGED_* case below begins with that prefix (or with "https://") and so
# passed before this change, sending the user's provider key to someone else's
# server in cleartext.
#
# These assert the parsing contract only. They deliberately do NOT assert
# anything about private-network https targets — see the final test, which pins
# the fact that those are still accepted so nobody mistakes this suite for an
# SSRF regression net.

FORGED_LOOPBACK_URLS = [
    # Real host is evil.example; "127.0.0.1" is only a label of it.
    "http://127.0.0.1.evil.example/v1",
    # Real host is evil.example; "127.0.0.1" is userinfo before the '@'.
    "http://127.0.0.1@evil.example/v1",
    # Same trick with a password component.
    "http://127.0.0.1:ignored@evil.example/v1",
    # A trailing dot is a distinct hostname (the DNS root form) and must not be
    # smuggled in as equal to the bare address.
    "http://127.0.0.1./v1",
    # Adjacent loopback-looking addresses are not the allowlisted one.
    "http://127.0.0.2/v1",
    # https disguise: this begins with "https://" so the old prefix branch took
    # it, yet the real host is evil.example.
    "https://api.example.com@evil.example/v1",
]

INVALID_PORT_BASE_URLS = [
    # Accessing SplitResult.port raises ValueError for both out-of-range and
    # non-numeric ports. The provider boundary must translate that into the
    # same ProviderError callers already surface as a user-readable 400.
    "https://example.com:99999/v1",
    "https://example.com:abc/v1",
    "http://127.0.0.1:99999/v1",
    "http://localhost:abc/v1",
    # Port zero parses as an int, so it needs an explicit range check rather
    # than relying on SplitResult.port to reject it.
    "https://example.com:0/v1",
    "http://127.0.0.1:0/v1",
]


@pytest.mark.parametrize("base_url", FORGED_LOOPBACK_URLS)
def test_validate_config_rejects_hosts_disguised_as_loopback(base_url):
    with pytest.raises(pc.ProviderError):
        pc.validate_config("openai_compatible", "gpt-4", base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1/v1",
        "http://127.0.0.1:8080/v1",
        # Ollama's default. The catalog path already accepted this; the save
        # path did not, which is the inconsistency this migration removes.
        "http://localhost:11434/v1",
        # Scheme and host are compared case-insensitively.
        "HTTP://127.0.0.1/v1",
        "http://LOCALHOST:11434/v1",
        "https://api.example.com/v1",
    ],
)
def test_validate_config_still_accepts_real_local_and_https_targets(base_url):
    _, _, out = pc.validate_config("openai_compatible", "gpt-4", base_url)
    assert out == base_url.rstrip("/")


def test_validate_config_rejects_userinfo_on_https_too():
    """Credentials in a URL are both a leak and a way to hide the real host, so
    they are refused regardless of scheme. This narrows the save path: the old
    prefix check accepted any string starting with "https://"."""
    with pytest.raises(pc.ProviderError):
        pc.validate_config("openai_compatible", "gpt-4", "https://user:pw@api.example.com/v1")


@pytest.mark.parametrize(
    "base_url",
    ["ftp://evil.example/v1", "http://evil.example/v1", "file:///etc/passwd"],
)
def test_validate_config_rejects_non_https_remote_targets(base_url):
    with pytest.raises(pc.ProviderError):
        pc.validate_config("openai_compatible", "gpt-4", base_url)


@pytest.mark.parametrize("base_url", INVALID_PORT_BASE_URLS)
def test_invalid_ports_are_provider_errors_on_both_validation_paths(base_url):
    """Invalid ports are rejected consistently without leaking ValueError."""
    with pytest.raises(pc.ProviderError, match="port"):
        pc.validate_config("openai_compatible", "gpt-4", base_url)

    with pytest.raises(pc.ProviderError, match="port"):
        pc.validate_catalog_target("openai_compatible", base_url)


@pytest.mark.parametrize(
    "base_url",
    ["https://example.com:65535/v1", "http://127.0.0.1:65535/v1"],
)
def test_maximum_valid_port_is_still_accepted(base_url):
    _, _, config_url = pc.validate_config("openai_compatible", "gpt-4", base_url)
    _, catalog_url = pc.validate_catalog_target("openai_compatible", base_url)
    assert config_url == catalog_url == base_url


def test_validate_config_and_catalog_path_now_agree():
    """The two validators disagreeing is why the forgery survived: it was fixed
    on the catalog path and left in place here. Pin them together so a future
    change to one cannot silently re-open the gap in the other."""
    for base_url in FORGED_LOOPBACK_URLS + INVALID_PORT_BASE_URLS + [
        "http://localhost:11434/v1",
        "http://127.0.0.1:8080/v1",
    ]:
        def _save_path():
            pc.validate_config("openai_compatible", "gpt-4", base_url)

        def _catalog_path():
            pc.validate_catalog_target("openai_compatible", base_url)

        save_ok = catalog_ok = True
        try:
            _save_path()
        except pc.ProviderError:
            save_ok = False
        try:
            _catalog_path()
        except pc.ProviderError:
            catalog_ok = False
        assert save_ok == catalog_ok, f"paths disagree on {base_url!r}"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://10.0.0.5/v1",
        "https://169.254.169.254/latest",
        "https://127.0.0.1/v1",
        "https://[::1]/v1",
        "https://[fe80::1]/v1",
        "https://192.0.2.1/v1",
        "https://224.0.0.1/v1",
    ],
)
def test_https_non_public_literals_are_blocked_on_both_validation_paths(base_url):
    with pytest.raises(pc.ProviderError, match="public"):
        pc.validate_config("openai_compatible", "gpt-4", base_url)
    with pytest.raises(pc.ProviderError, match="public"):
        pc.validate_catalog_target("openai_compatible", base_url)


@pytest.mark.parametrize(
    "resolved_ips",
    [
        ["10.0.0.5"],
        ["8.8.8.8", "169.254.169.254"],
        ["ff02::1"],
    ],
)
def test_https_hostname_is_blocked_if_any_dns_answer_is_not_public(
    monkeypatch, resolved_ips
):
    monkeypatch.setattr(pc, "_resolve_provider_base_url_ips", lambda _host: resolved_ips)

    with pytest.raises(pc.ProviderError, match="public"):
        pc.validate_config("openai_compatible", "gpt-4", "https://relay.example/v1")
    with pytest.raises(pc.ProviderError, match="public"):
        pc.validate_catalog_target("openai_compatible", "https://relay.example/v1")


@pytest.mark.parametrize(
    "resolved", [[], ["not-an-address"], OSError("dns unavailable")]
)
def test_https_dns_failure_is_a_provider_error_on_both_validation_paths(
    monkeypatch, resolved
):
    def _resolve(_host):
        if isinstance(resolved, BaseException):
            raise resolved
        return resolved

    monkeypatch.setattr(pc, "_resolve_provider_base_url_ips", _resolve)

    with pytest.raises(pc.ProviderError, match="resolve"):
        pc.validate_config("openai_compatible", "gpt-4", "https://relay.example/v1")
    with pytest.raises(pc.ProviderError, match="resolve"):
        pc.validate_catalog_target("openai_compatible", "https://relay.example/v1")


def test_https_dns_is_checked_on_every_explicit_validation_without_a_cache(monkeypatch):
    resolved_hosts = []

    def _resolve(host):
        resolved_hosts.append(host)
        return ["8.8.8.8"]

    monkeypatch.setattr(pc, "_resolve_provider_base_url_ips", _resolve)

    pc.validate_config("openai_compatible", "gpt-4", "https://relay.example/v1")
    pc.validate_config("openai_compatible", "gpt-4", "https://relay.example/v1")

    assert resolved_hosts == ["relay.example", "relay.example"]


def test_provider_dns_resolver_has_no_process_cache(monkeypatch):
    resolved_hosts = []

    def _resolve(host):
        resolved_hosts.append(host)
        return ["8.8.8.8"]

    monkeypatch.setattr(pc.net_safety, "resolve_ips", _resolve)

    pc._resolve_provider_base_url_ips("relay.example")
    pc._resolve_provider_base_url_ips("relay.example")

    assert resolved_hosts == ["relay.example", "relay.example"]


def test_private_base_url_escape_is_exact_and_does_not_expand_cleartext(monkeypatch):
    private_url = "https://10.0.0.5/v1"
    monkeypatch.setenv("FEEDLING_PROVIDER_ALLOW_PRIVATE_BASE_URLS", "true")
    with pytest.raises(pc.ProviderError, match="public"):
        pc.validate_config("openai_compatible", "gpt-4", private_url)

    monkeypatch.setenv("FEEDLING_PROVIDER_ALLOW_PRIVATE_BASE_URLS", "1")
    _, _, out = pc.validate_config("openai_compatible", "gpt-4", private_url)
    assert out == private_url
    _, catalog_url = pc.validate_catalog_target("openai_compatible", private_url)
    assert catalog_url == private_url

    monkeypatch.setattr(
        pc,
        "_resolve_provider_base_url_ips",
        lambda _host: (_ for _ in ()).throw(AssertionError("escape must skip DNS")),
    )
    _, _, hostname_url = pc.validate_config(
        "openai_compatible", "gpt-4", "https://lan-model.example/v1"
    )
    assert hostname_url == "https://lan-model.example/v1"

    with pytest.raises(pc.ProviderError, match="local http"):
        pc.validate_config(
            "openai_compatible", "gpt-4", "http://192.168.1.50:11434/v1"
        )


def test_cleartext_loopback_allowlist_still_skips_dns(monkeypatch):
    def _unexpected_dns(_host):
        raise AssertionError("cleartext loopback must not enter the HTTPS DNS policy")

    monkeypatch.setattr(pc, "_resolve_provider_base_url_ips", _unexpected_dns)

    for base_url in ["http://127.0.0.1:8080/v1", "http://localhost:11434/v1"]:
        _, _, out = pc.validate_config("openai_compatible", "gpt-4", base_url)
        assert out == base_url


@pytest.mark.parametrize(
    "base_url",
    [
        # The bracket is never closed. urlsplit raises on every supported
        # interpreter, so this is the version-independent form of the bug.
        "http://[::1/v1",
        "https://[::1/v1",
    ],
)
def test_malformed_authority_is_a_provider_error_not_a_valueerror(base_url):
    """A mistyped base_url must fail like every other bad value: 400, not 500.

    Both callers of validate_config catch only ProviderError, so a ValueError
    escaping this function is an unhandled exception on a user-input path. The
    prefix check this replaced never parsed the URL, so it could not raise —
    the parsing rewrite is what introduced the possibility.
    """
    with pytest.raises(pc.ProviderError):
        pc.validate_config("openai_compatible", "gpt-4", base_url)

    # The catalog path shares the validator and must not diverge again.
    with pytest.raises(pc.ProviderError):
        pc.validate_catalog_target("openai_compatible", base_url)


def test_bracketed_non_address_never_escapes_as_a_valueerror():
    """A bracketed host that is not an address is interpreter-dependent.

    On 3.11+ urlsplit rejects it outright; on 3.9 urlsplit accepts it and
    `.hostname` returns the raw text. Either way the caller must see a
    ProviderError or a normal accept — never a ValueError. Asserting the
    *classification* rather than accept-vs-reject keeps this honest about a
    difference we do not control, instead of pinning whichever answer the
    machine running the tests happens to give.
    """
    for base_url in ("https://[gg::1]/v1", "http://[not-an-address]/v1"):
        try:
            pc.validate_config("openai_compatible", "gpt-4", base_url)
        except pc.ProviderError:
            pass
        except ValueError as exc:  # pragma: no cover - the regression itself
            raise AssertionError(f"ValueError escaped for {base_url!r}: {exc}") from exc
