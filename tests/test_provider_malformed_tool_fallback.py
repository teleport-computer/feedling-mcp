"""Malformed provider tool-call containers fail closed into one text fallback."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client
from provider_types import ToolResult
from capabilities import registry as cap_registry
from model_api_runtime.v2 import tool_loop


_TEST_PROVIDER_CONFIG = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-sonnet-4-test",
    api_key="test-key",
)


_CASES = (
    (
        "openai_chat",
        provider_client._decode_tool_calls_openai_chat,
        {"choices": [{"message": {"tool_calls": ["not-an-object"]}}]},
    ),
    (
        "openai_responses",
        provider_client._decode_tool_calls_openai_responses,
        {"output": ["not-an-object"]},
    ),
    (
        "anthropic",
        provider_client._decode_tool_calls_anthropic,
        {"content": ["not-an-object"]},
    ),
    (
        "gemini",
        provider_client._decode_tool_calls_gemini,
        {"candidates": [{"content": {"parts": ["not-an-object"]}}]},
    ),
)

_MALFORMED_CONTAINERS = (
    (
        "openai_chat",
        provider_client._decode_tool_calls_openai_chat,
        {"choices": [{"message": {"tool_calls": {}}}]},
    ),
    (
        "openai_responses",
        provider_client._decode_tool_calls_openai_responses,
        {"output": {}},
    ),
    (
        "anthropic",
        provider_client._decode_tool_calls_anthropic,
        {"content": {}},
    ),
    (
        "gemini",
        provider_client._decode_tool_calls_gemini,
        {"candidates": [{"content": {"parts": {}}}]},
    ),
)


@pytest.mark.parametrize("_wire,decoder,body", _CASES)
def test_non_object_tool_element_normalizes_to_failed_call(_wire, decoder, body):
    calls = decoder(body)

    assert len(calls) == 1
    assert calls[0]["args"] == {}
    assert calls[0]["args_ok"] is False


@pytest.mark.parametrize("_wire,decoder,body", _MALFORMED_CONTAINERS)
def test_non_list_tool_container_normalizes_to_failed_call(_wire, decoder, body):
    calls = decoder(body)

    assert len(calls) == 1
    assert calls[0]["args"] == {}
    assert calls[0]["args_ok"] is False


@pytest.mark.parametrize("_wire,decoder,body", _CASES)
def test_malformed_wire_uses_exactly_one_tools_disabled_fallback(
    monkeypatch, _wire, decoder, body,
):
    provider_tools = []
    decoded_calls = []

    async def _provider(_config, _messages, *, tools=None, **kwargs):
        provider_tools.append(tools)
        if tools is None:
            return {"reply": "plain fallback", "tool_calls": [], "usage": {}}
        calls = decoder(body)
        decoded_calls.extend(calls)
        return {"reply": "", "tool_calls": calls, "usage": {}}

    async def _dispatch(_calls):
        raise AssertionError("malformed tool calls must never be dispatched")

    replies = []

    async def _on_reply(text, *, final, reasoning=""):
        replies.append((text, final))

    async def _fold():
        return []

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=lambda _transcript: [{"role": "user", "content": "hello"}],
        dispatch_tools=_dispatch,
        on_reply=_on_reply,
        fold_new_messages=_fold,
        add_usage=lambda _usage: None,
        max_calls=5,
    ))

    assert decoded_calls
    assert all(call["args_ok"] is False for call in decoded_calls)
    assert sum(tools is None for tools in provider_tools) == 1
    assert provider_tools[-1] is None
    assert replies == [("plain fallback", True)]
    assert outcome.final_text == "plain fallback"


def test_content_400_raises_without_a_wasted_tools_disabled_retry(monkeypatch):
    """A 400 whose cause is the message content (not the tool schema) must
    propagate immediately. The old code treated EVERY tools-enabled 400 as
    'tool_schema_rejected', dropped tools, and re-sent the SAME bad history —
    a second billed call that 400s again and masks the real error. Here the
    provider raises a content 400 (the OpenAI Responses assistant/input_text
    case); the loop must raise on the first call and never retry with tools=None."""
    seen_tools = []

    async def _provider(_config, _messages, *, tools=None, **kwargs):
        seen_tools.append(tools)
        # Raised through the real classifier, not hand-built: the upstream body
        # no longer travels inside the message, so an exception fabricated with
        # the body inline would test a shape production cannot produce.
        provider_client._raise_for_provider_status(httpx.Response(
            400,
            json={"error": {"message": "Invalid value: 'input_text'. "
                                       "Supported values are: 'output_text' and 'refusal'."}},
        ))

    async def _dispatch(_calls):
        raise AssertionError("no tool calls in this scenario")

    async def _on_reply(text, *, final, reasoning=""):
        raise AssertionError("no reply expected")

    async def _fold():
        return []

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)
    with pytest.raises(provider_client.ProviderError):
        asyncio.run(tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=lambda _t: [{"role": "user", "content": "hi"}],
            dispatch_tools=_dispatch,
            on_reply=_on_reply,
            fold_new_messages=_fold,
            add_usage=lambda _u: None,
            max_calls=5,
        ))
    # exactly one provider call, with tools enabled — no tools-disabled retry
    assert seen_tools == [seen_tools[0]] and seen_tools[0] is not None
    assert len(seen_tools) == 1


def test_tool_schema_400_still_falls_back_to_text(monkeypatch):
    """A genuine tool-schema 400 (error text implicates the function/tools) must
    still degrade to exactly one tools-disabled retry that yields text — the
    fallback's intended purpose is preserved."""
    seen_tools = []

    async def _provider(_config, _messages, *, tools=None, **kwargs):
        seen_tools.append(tools)
        if tools is None:
            return {"reply": "text without tools", "tool_calls": [], "usage": {}}
        provider_client._raise_for_provider_status(httpx.Response(
            400,
            json={"error": {"message": "Invalid schema for function 'do_thing': "
                                       "parameters.type must be 'object'."}},
        ))

    async def _dispatch(_calls):
        raise AssertionError("tools were rejected; never dispatched")

    replies = []

    async def _on_reply(text, *, final, reasoning=""):
        replies.append((text, final))

    async def _fold():
        return []

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=lambda _t: [{"role": "user", "content": "hi"}],
        dispatch_tools=_dispatch,
        on_reply=_on_reply,
        fold_new_messages=_fold,
        add_usage=lambda _u: None,
        max_calls=5,
    ))
    assert seen_tools[0] is not None and seen_tools[-1] is None
    assert sum(t is None for t in seen_tools) == 1
    assert outcome.final_text == "text without tools"
    assert replies == [("text without tools", True)]


def test_web_observation_revokes_durable_writes_for_later_rounds(monkeypatch):
    provider_tools = []
    responses = iter([
        {
            "reply": "",
            "tool_calls": [{
                "id": "web-1", "name": "web_fetch",
                "args": {"url": "https://example.com"},
            }],
            "usage": {},
        },
        # A broken/compromised relay invents a write even though it was not in
        # round two's offered catalog.  It must never reach dispatch.
        {
            "reply": "",
            "tool_calls": [{
                "id": "write-1", "name": "identity_patch",
                "args": {"signature": "injected"},
            }],
            "usage": {},
        },
        {"reply": "safe final", "tool_calls": [], "usage": {}},
    ])

    async def _provider(_config, _messages, *, tools=None, **kwargs):
        provider_tools.append(tools)
        return next(responses)

    dispatched = []

    async def _dispatch(calls):
        dispatched.extend(calls)
        return [ToolResult(call_id=tc.id, content="external page text") for tc in calls]

    replies = []

    async def _on_reply(text, *, final, reasoning=""):
        replies.append((text, final))

    async def _fold():
        return []

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=lambda _transcript: [{"role": "user", "content": "look this up"}],
        dispatch_tools=_dispatch,
        on_reply=_on_reply,
        fold_new_messages=_fold,
        add_usage=lambda _usage: None,
        max_calls=4,
    ))

    first_names = {spec.name for spec in provider_tools[0]}
    second_names = {spec.name for spec in provider_tools[1]}
    assert cap_registry.WRITE_ACTIONS <= first_names
    assert cap_registry.WRITE_ACTIONS.isdisjoint(second_names)
    assert tool_loop.provenance.EXTERNAL_READS.isdisjoint(second_names)
    assert {spec.name for spec in provider_tools[2]} == {"web_fetch"}
    assert [tc.name for tc in dispatched] == ["web_fetch"]
    assert replies == [("safe final", True)]
    assert outcome.final_text == "safe final"


def test_web_search_allows_only_exact_returned_url_for_followup_fetch(monkeypatch):
    provider_tools = []
    allowed_url = "https://example.com/article?id=7"
    responses = iter([
        {
            "reply": "",
            "tool_calls": [{
                "id": "search-1", "name": "web_search",
                "args": {"query": "example article"},
            }],
            "usage": {},
        },
        {
            "reply": "",
            "tool_calls": [{
                "id": "fetch-1", "name": "web_fetch",
                "args": {"url": allowed_url},
            }],
            "usage": {},
        },
        {"reply": "grounded answer", "tool_calls": [], "usage": {}},
    ])

    async def _provider(_config, _messages, *, tools=None, **kwargs):
        provider_tools.append(tools)
        return next(responses)

    dispatched = []

    async def _dispatch(calls):
        dispatched.extend(calls)
        return [
            ToolResult(
                call_id=tc.id,
                content=(
                    '{"results":[{"title":"Example","url":"'
                    + allowed_url
                    + '"}]}'
                    if tc.name == "web_search"
                    else "trusted fetch result wrapper containing untrusted page text"
                ),
            )
            for tc in calls
        ]

    replies = []

    async def _on_reply(text, *, final, reasoning=""):
        replies.append((text, final))

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=lambda _transcript: [{"role": "user", "content": "look this up"}],
        dispatch_tools=_dispatch,
        on_reply=_on_reply,
        fold_new_messages=lambda: asyncio.sleep(0, result=[]),
        add_usage=lambda _usage: None,
        max_calls=4,
    ))

    second_names = {spec.name for spec in provider_tools[1]}
    assert "web_fetch" in second_names
    assert "web_search" not in second_names
    assert cap_registry.WRITE_ACTIONS.isdisjoint(second_names)
    assert [(tc.name, tc.args) for tc in dispatched] == [
        ("web_search", {"query": "example article"}),
        ("web_fetch", {"url": allowed_url}),
    ]
    assert replies == [("grounded answer", True)]
    assert outcome.final_text == "grounded answer"


def test_web_search_result_cannot_redirect_model_to_fresh_fetch_url(monkeypatch):
    allowed_url = "https://example.com/allowed"
    responses = iter([
        {
            "reply": "",
            "tool_calls": [{
                "id": "search-1", "name": "web_search", "args": {"query": "safe"},
            }],
            "usage": {},
        },
        {
            "reply": "",
            "tool_calls": [{
                "id": "fetch-evil", "name": "web_fetch",
                "args": {"url": "https://attacker.invalid/injected"},
            }],
            "usage": {},
        },
        {"reply": "safe fallback", "tool_calls": [], "usage": {}},
    ])

    async def _provider(_config, _messages, *, tools=None, **kwargs):
        return next(responses)

    dispatched = []

    async def _dispatch(calls):
        dispatched.extend(calls)
        return [ToolResult(
            call_id=tc.id,
            content='{"results":[{"url":"' + allowed_url + '"}]}',
        ) for tc in calls]

    replies = []

    async def _on_reply(text, *, final, reasoning=""):
        replies.append((text, final))

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=lambda _transcript: [{"role": "user", "content": "search"}],
        dispatch_tools=_dispatch,
        on_reply=_on_reply,
        fold_new_messages=lambda: asyncio.sleep(0, result=[]),
        add_usage=lambda _usage: None,
        max_calls=4,
    ))

    assert [tc.name for tc in dispatched] == ["web_search"]
    assert replies == [("safe fallback", True)]
    assert outcome.final_text == "safe fallback"


def test_reply_and_durable_write_same_batch_fail_closed(monkeypatch):
    provider_tools = []
    responses = iter([
        {
            "reply": "",
            "tool_calls": [
                {
                    "id": "write-1", "name": "identity_patch",
                    "args": {"signature": "new"},
                },
                {"id": "reply-1", "name": "reply", "args": {"text": "saved"}},
            ],
            "usage": {},
        },
        {"reply": "I couldn't safely apply that change.", "tool_calls": [], "usage": {}},
    ])

    async def _provider(_config, _messages, *, tools=None, **kwargs):
        provider_tools.append(tools)
        return next(responses)

    async def _dispatch(_calls):
        raise AssertionError("mixed reply+write batch must not dispatch")

    replies = []

    async def _on_reply(text, *, final, reasoning=""):
        replies.append((text, final))

    async def _fold():
        return []

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=lambda _transcript: [{"role": "user", "content": "remember this"}],
        dispatch_tools=_dispatch,
        on_reply=_on_reply,
        fold_new_messages=_fold,
        add_usage=lambda _usage: None,
        max_calls=3,
    ))

    assert provider_tools[0] is not None
    assert provider_tools[1] is None
    assert replies == [("I couldn't safely apply that change.", True)]
    assert outcome.final_text == "I couldn't safely apply that change."
