"""Exercise correction failures through the real loop and worker trace projection."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import provider_client
from model_api_runtime.v2 import tool_loop, worker

ROUTE = ("anthropic", "claude-synthetic")
SECRET = "PRIVATE-body-key-endpoint"


@pytest.mark.parametrize("correction", [False, True])
@pytest.mark.parametrize("wire_attempts", [None, 0, 1, 3])
def test_provider_error_metadata(monkeypatch, correction, wire_attempts):
    events, calls, order = [], [], []
    clock = SimpleNamespace(now=1000.0)
    error = TimeoutError(SECRET)
    if wire_attempts is not None:
        error.feedling_provider_attempt_trace = {
            "attempts": [{"kind": "http_attempt", "body": SECRET}
                         for _ in range(wire_attempts)]
        }
    monkeypatch.setattr(tool_loop, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def fake_provider(config, messages, **kwargs):
        calls.append(kwargs)
        if correction and len(calls) == 1:
            clock.now += 7
            return {"reply": "PRIVATE-unpublished-draft", "tool_calls": [],
                    "usage": {}, "stop_reason": "end_turn"}
        clock.now += 3
        raise error

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_provider)

    async def noop(*args, **kwargs):
        return []

    async def fold(*args, **kwargs):
        clock.now += 20
        return []

    async def correction_event(outcome, **kwargs):
        order.append(outcome)

    async def failure(exc):
        assert exc is error
        order.append("failure_callback")
        raise ValueError("callback failure must not replace provider exception")

    def emit(uid, kind, **fields):
        events.append({"type": kind, **fields})
        order.append(kind)

    trace = worker._ProviderRoundtripTrace(
        deps=SimpleNamespace(emit_debug_trace=emit), user_id="synthetic-user",
        lane="heartbeat", trace_id="synthetic-trace", job_id="synthetic-job")
    with pytest.raises(TimeoutError) as caught:
        asyncio.run(tool_loop.run_tool_loop(
            provider_config=provider_client.ProviderConfig(*ROUTE, "fake-no-auth"),
            build_messages=lambda transcript: [{"role": "user", "content": SECRET}],
            dispatch_tools=noop, on_reply=noop, fold_new_messages=fold,
            add_usage=lambda usage: None, max_calls=2 if correction else 1,
            regular_wake_choice_required=True, on_stay_silent=noop,
            require_reply=False, reply_tool_enabled=True,
            on_provider_call_event=trace.record_model_call,
            on_wake_direct_text_correction=correction_event,
            on_provider_failure=failure,
        ))
    assert caught.value is error
    expected = ["agent.model.call.start", "agent.model.call.error"]
    if correction:
        expected = ["agent.model.call.start", "agent.model.call.done", *expected]
        assert calls[-1]["max_attempts"] == 1
        assert order[-3:] == ["still_invalid", "agent.model.call.error", "failure_callback"]
        done = events[1]
        assert done["dur_ms"] == 7000
    assert [event["type"] for event in events] == expected
    assert len(calls) == (2 if correction else 1)
    failed = events[-1]
    assert (failed["detail"]["provider"], failed["detail"]["model"]) == ROUTE
    assert failed["detail"]["finish_reason"] == "provider_error"
    assert failed["detail"]["exception_type"] == "TimeoutError"
    assert failed["detail"]["round"] == len(calls)
    assert failed["dur_ms"] == 3000
    assert failed["job_id"] == "synthetic-job"
    assert failed["trace_id"] == "synthetic-trace"
    if wire_attempts:
        assert failed["detail"]["transport_retry_count"] == wire_attempts - 1
    else:
        assert "transport_retry_count" not in failed["detail"]
    serialized = json.dumps(events)
    assert SECRET not in serialized
    assert "PRIVATE-unpublished-draft" not in serialized
