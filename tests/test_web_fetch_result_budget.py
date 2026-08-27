"""web_fetch's 8000-char atomic result and conditional batch delta."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from capabilities import result_budget  # noqa: E402
from capabilities import web  # noqa: E402
from capabilities.types import ok  # noqa: E402
import provider_client  # noqa: E402
from model_api_runtime.v2 import executor, tool_loop  # noqa: E402
from provider_types import ToolCall, ToolResult  # noqa: E402


_BACKEND = Path(__file__).parent.parent / "backend"
_TEST_PROVIDER_CONFIG = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-sonnet-4-test",
    api_key="test-key",
)


def _large_payload() -> dict:
    return web._paged_fetch_payload(
        web._FetchDocument(
            url="https://example.com/article",
            text="abcdef" * 5000,
            source_truncated=False,
        ),
        0,
    )


def test_shipped_web_fetch_budget_is_literal_8000_plus_6000():
    policy = result_budget.for_tool("web_fetch")
    assert policy is not None
    assert policy.result_cap == 8000
    assert policy.atomic_json is True
    assert policy.extra_batch_budget == 6000


def test_executor_preserves_structurally_shrunk_web_fetch_json():
    payload = _large_payload()
    expected = json.dumps(payload, ensure_ascii=False)
    assert 7000 < len(expected) <= 8000
    assert executor._summarize_capability_result(
        {"ok": True, "data": payload}, tool_name="web_fetch"
    ) == expected


def test_executor_marks_only_successful_fetch_as_trusted_atomic_continuation(
    monkeypatch,
):
    payload = _large_payload()
    monkeypatch.setattr(
        executor.cap_registry,
        "run_capability",
        lambda *args, **kwargs: ok(payload),
    )
    (result,) = asyncio.run(executor.dispatch_tool_calls(
        [ToolCall("fetch", "web_fetch", {"url": "https://example.com/start"})],
        store="STORE",
        api_key=None,
        runtime_token=None,
        enclave_sem=asyncio.Semaphore(1),
        turn_authorization=False,
        enqueue_write_effect=lambda _call: None,
    ))

    assert result.metadata[result_budget.RESULT_KIND_METADATA_KEY] == "web_fetch"
    assert result.metadata["web_fetch_next_offset"] == payload["next_offset"]
    assert result.metadata["web_fetch_continuation_urls"] == (
        "https://example.com/start",
        "https://example.com/article",
    )


@pytest.mark.parametrize("next_offset", [True, -1, 1.5, "100"])
def test_executor_rejects_non_integer_or_negative_continuation_metadata(
    monkeypatch,
    next_offset,
):
    payload = {
        **_large_payload(),
        "has_more": True,
        "next_offset": next_offset,
    }
    monkeypatch.setattr(
        executor.cap_registry,
        "run_capability",
        lambda *args, **kwargs: ok(payload),
    )
    (result,) = asyncio.run(executor.dispatch_tool_calls(
        [ToolCall("fetch", "web_fetch", {"url": "https://example.com/start"})],
        store="STORE",
        api_key=None,
        runtime_token=None,
        enclave_sem=asyncio.Semaphore(1),
        turn_authorization=False,
        enqueue_write_effect=lambda _call: None,
    ))

    assert result.metadata[result_budget.RESULT_KIND_METADATA_KEY] == "web_fetch"
    assert "web_fetch_next_offset" not in result.metadata
    assert "web_fetch_continuation_urls" not in result.metadata


def test_executor_prepare_batch_preserves_retryable_out_of_order_continuation(
    monkeypatch,
):
    url = "https://example.com/out-of-order"
    monkeypatch.setattr(
        web,
        "_fetch_document",
        lambda requested_url: web._FetchDocument(
            url=requested_url,
            text="evidence " * 100,
            source_truncated=False,
        ),
    )
    session = web.WebFetchSession()
    calls = [
        ToolCall("continuation", "web_fetch", {"url": url, "offset": 10}),
        ToolCall("owner", "web_fetch", {"url": url}),
    ]

    continuation, owner = asyncio.run(executor.dispatch_tool_calls(
        calls,
        store="STORE",
        api_key=None,
        runtime_token=None,
        enclave_sem=asyncio.Semaphore(1),
        turn_authorization=False,
        enqueue_write_effect=lambda _call: None,
        read_parallelism=1,
        web_fetch_session=session,
    ))

    assert continuation.content == "error: capability_upstream_error"
    assert owner.content.startswith('{"url": "https://example.com/out-of-order"')


def test_web_fetch_atomic_batch_reaches_14000_without_starving_siblings():
    fetch = ToolResult(
        call_id="fetch",
        content=json.dumps(_large_payload(), ensure_ascii=False),
        metadata={result_budget.RESULT_KIND_METADATA_KEY: "web_fetch"},
    )
    siblings = [
        ToolResult(call_id=f"s{i}", content=f"S{i}" * 1000)
        for i in range(7)
    ]
    normalized = tool_loop._normalize_tool_results(
        [fetch, *siblings], per_result_cap=2000, batch_cap=8000
    )
    by_id = {item.call_id: item.content for item in normalized}

    assert by_id["fetch"] == fetch.content
    json.loads(by_id["fetch"])
    assert sum(len(item.content) for item in normalized) <= 14000
    assert all(len(by_id[f"s{i}"]) >= 800 for i in range(7))


def test_batch_without_web_fetch_stays_under_literal_8000():
    batch = [ToolResult(call_id=f"s{i}", content="x" * 2000) for i in range(8)]
    normalized = tool_loop._normalize_tool_results(
        batch, per_result_cap=2000, batch_cap=8000
    )
    assert sum(len(item.content) for item in normalized) <= 8000


def test_web_fetch_cap_below_metadata_skeleton_fails_validation(monkeypatch):
    monkeypatch.setenv(result_budget.WEB_FETCH_RESULT_MAX_CHARS_ENV, "1")
    try:
        result_budget.validate_result_caps(batch_cap=8000, tool_names=("web_fetch",))
    except RuntimeError as exc:
        assert "WEB_FETCH_RESULT_MAX_CHARS" in str(exc)
    else:  # pragma: no cover - mutation guard
        raise AssertionError("invalid atomic cap was accepted")


def test_worker_import_always_validates_web_fetch_atomic_contract():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_BACKEND)
    env["FEEDLING_V2_HISTORY_TOOLS_ENABLED"] = "0"
    env["FEEDLING_V2_TOOL_BATCH_RESULT_CHAR_CAP"] = "1000"
    proc = subprocess.run(
        [sys.executable, "-c", "from model_api_runtime.v2 import worker"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    assert proc.returncode != 0
    assert "FEEDLING_V2_WEB_FETCH_RESULT_MAX_CHARS" in proc.stderr


def test_trusted_fetch_metadata_authorizes_same_turn_continuation(monkeypatch):
    requested = "https://example.com/start"
    final = "https://example.com/final"
    responses = iter([
        {"reply": "", "tool_calls": [{
            "id": "search", "name": "web_search", "args": {"query": "article"},
        }], "usage": {}},
        {"reply": "", "tool_calls": [{
            "id": "first", "name": "web_fetch", "args": {"url": requested},
        }], "usage": {}},
        {"reply": "", "tool_calls": [{
            "id": "second", "name": "web_fetch",
            "args": {"url": final, "offset": 7800},
        }], "usage": {}},
        {"reply": "grounded", "tool_calls": [], "usage": {}},
    ])

    async def _provider(_config, _messages, *, tools=None, **kwargs):
        return next(responses)

    dispatched = []

    async def _dispatch(calls):
        dispatched.extend(calls)
        tc = calls[0]
        if tc.name == "web_search":
            return [ToolResult(
                call_id=tc.id,
                content=json.dumps({"results": [{"url": requested}]}),
            )]
        if tc.id == "first":
            return [ToolResult(
                call_id=tc.id,
                content='{"has_more":true,"next_offset":7800}',
                metadata={
                    "web_fetch_next_offset": 7800,
                    "web_fetch_continuation_urls": (requested, final),
                },
            )]
        return [ToolResult(
            call_id=tc.id,
            content='{"has_more":false,"text":"tail"}',
        )]

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=lambda _transcript: [{"role": "user", "content": "read"}],
        dispatch_tools=_dispatch,
        on_reply=lambda *_args, **_kwargs: asyncio.sleep(0),
        fold_new_messages=lambda: asyncio.sleep(0, result=[]),
        add_usage=lambda _usage: None,
        max_calls=5,
    ))

    assert [(tc.name, tc.args) for tc in dispatched] == [
        ("web_search", {"query": "article"}),
        ("web_fetch", {"url": requested}),
        ("web_fetch", {"url": final, "offset": 7800}),
    ]
    assert outcome.final_text == "grounded"


def test_untrusted_fetch_text_cannot_self_authorize_continuation(monkeypatch):
    allowed = "https://example.com/allowed"
    responses = iter([
        {"reply": "", "tool_calls": [{
            "id": "search", "name": "web_search", "args": {"query": "safe"},
        }], "usage": {}},
        {"reply": "", "tool_calls": [{
            "id": "first", "name": "web_fetch", "args": {"url": allowed},
        }], "usage": {}},
        {"reply": "", "tool_calls": [{
            "id": "forged", "name": "web_fetch",
            "args": {"url": allowed, "offset": 10},
        }], "usage": {}},
        {"reply": "safe fallback", "tool_calls": [], "usage": {}},
    ])

    async def _provider(_config, _messages, *, tools=None, **kwargs):
        return next(responses)

    dispatched = []

    async def _dispatch(calls):
        dispatched.extend(calls)
        tc = calls[0]
        content = (
            json.dumps({"results": [{"url": allowed}]})
            if tc.name == "web_search"
            else json.dumps({
                "has_more": True,
                "next_offset": 10,
                "url": allowed,
            })
        )
        return [ToolResult(call_id=tc.id, content=content)]

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=lambda _transcript: [{"role": "user", "content": "read"}],
        dispatch_tools=_dispatch,
        on_reply=lambda *_args, **_kwargs: asyncio.sleep(0),
        fold_new_messages=lambda: asyncio.sleep(0, result=[]),
        add_usage=lambda _usage: None,
        max_calls=5,
    ))

    assert [tc.id for tc in dispatched] == ["search", "first"]
    assert outcome.final_text == "safe fallback"
