"""T776: wake lanes bound each provider wire at more than the 60s default.

prod 2026-09-26..28 (T770): GLM heartbeat first calls slowed to p90 ~100s after
T723's look round, and GLM round-1 timeouts rose from 1.2% to 8.5% of calls
(60s httpx timeout x 2 attempts). Scheduled reminders on GLM failed the same way.

A longer wire is only safe if the hosted stall clock sees at most one wire of
silence, so the wake path must also report a progress boundary at every
attempt and carry a real wall-clock wire deadline.
"""
from __future__ import annotations

import asyncio
import inspect

import httpx

import test_v2_wake_look_first as L
from model_api_runtime.v2 import jobs_store, pool_config, tool_loop, worker
import provider_client

pytestmark = L.pytestmark

WIRE = worker.WAKE_PROVIDER_WIRE_TIMEOUT_SEC


def _provider_default_timeout():
    return inspect.signature(provider_client.chat_completion_async).parameters[
        "timeout"
    ].default


def _run_heartbeat(monkeypatch, responses, *, uid_suffix):
    """Real _run_wake + real reliable retry; each response may be an exception."""
    uid = f"u_wake_wire_{uid_suffix}"
    L.W.conftest.seed_user(uid)
    L.W._reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat", trace_id=f"trace-{uid}")
    claimed_by = L.W._claim(job_id)
    it = iter(responses)
    seen = []

    async def _chat(config, messages, *, tools=None, **kwargs):
        seen.append((kwargs.get("timeout"), provider_client._WIRE_DEADLINE_SEC.get()))
        item = next(it)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(provider_client, "chat_completion_async", _chat)
    L.T2._patch_real_write(monkeypatch)
    L.T2._patch_tool_effect_encryption(monkeypatch)
    deps = L.T2._wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        sink_calls=[],
    )
    stages = []
    token = worker._TURN_PROGRESS_CB.set(stages.append)
    try:
        status = asyncio.run(worker._run_wake(
            job_id, uid, "heartbeat", deps, L.W._BYOK, asyncio.Semaphore(4),
            claimed_by, trace_id=f"trace-{uid}",
        ))
    finally:
        worker._TURN_PROGRESS_CB.reset(token)
    return status, seen, stages


def test_wake_wire_timeout_is_longer_than_the_provider_default():
    assert WIRE > _provider_default_timeout()


def test_every_wake_provider_wire_carries_the_timeout_and_wall_deadline(monkeypatch):
    status, seen, _ = _run_heartbeat(
        monkeypatch, [L._lookup(), L._silent()], uid_suffix="bounds")
    assert status == "completed"
    assert seen == [(WIRE, WIRE)] * 2


def test_a_timed_out_attempt_is_a_stall_clock_boundary(monkeypatch):
    # codex4 review (T776 r1): without progress_cb the retry wrapper reported no
    # attempt boundary, so two full wires counted as one silence.
    status, seen, stages = _run_heartbeat(
        monkeypatch,
        [httpx.ReadTimeout("slow first byte"), L._lookup(), L._silent()],
        uid_suffix="retry_progress",
    )
    assert status == "completed"
    assert len(seen) == 3
    first_round = stages[:stages.index("provider_complete")]
    assert [s for s in first_round if s.startswith("provider_attempt_")] == [
        "provider_attempt_start_1",
        "provider_attempt_failed_1",
        "provider_attempt_start_2",
        "provider_attempt_complete_2",
    ]


def test_tool_loop_without_the_option_keeps_the_provider_defaults():
    params = inspect.signature(tool_loop.run_tool_loop).parameters
    assert params["provider_wire_timeout_sec"].default is None


def test_one_wake_wire_fits_inside_every_wake_slot_stall_budget():
    # With a boundary at every attempt and wire and a wall-clock wire deadline,
    # the longest silence the watchdog sees is one wire; keep the same 30s
    # margin the MCP call timeout keeps below the stall budget.
    wake_lanes = worker._WAKE_LANES
    slots = [
        s for s in pool_config.RuntimePoolConfig.from_env().slots
        if s.lanes & wake_lanes
    ]
    assert {lane for s in slots for lane in s.lanes} >= wake_lanes
    for slot in slots:
        assert WIRE + 30.0 < slot.stall_budget_sec


def _loop_with_tagged_image_rejection(monkeypatch, **loop_kwargs):
    import test_v2_tool_loop as TL

    responses = [
        provider_client.ProviderError("images unsupported", status_code=404),
        {"reply": "text fallback", "tool_calls": [], "usage": {}},
    ]
    seen = []

    async def _chat(config, messages, *, tools=None, **kwargs):
        seen.append((kwargs.get("timeout"), provider_client._WIRE_DEADLINE_SEC.get()))
        item = responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(provider_client, "chat_completion_async", _chat)
    tagged = {
        "role": "user",
        "content": [
            {"type": "text", "text": "untrusted frame"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        ],
        "_screen_test": True,
    }
    stages = []
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=TL._TEST_PROVIDER_CONFIG,
        build_messages=lambda _transcript: [tagged, {"role": "user", "content": "hi"}],
        dispatch_tools=TL._RecordingDispatch(),
        on_reply=TL._RecordingReply(),
        fold_new_messages=TL._RecordingFold([]),
        add_usage=TL._noop_add_usage,
        max_calls=3,
        tagged_image_message_key="_screen_test",
        on_tagged_images_rejected=lambda exc: None,
        on_progress=stages.append,
        **loop_kwargs,
    ))
    assert outcome.final_text == "text fallback"
    return seen, [s for s in stages if s.startswith("provider_attempt_")]


def test_tagged_image_text_retry_keeps_the_wire_bound_and_boundaries(monkeypatch):
    seen, attempt_stages = _loop_with_tagged_image_rejection(
        monkeypatch, provider_wire_timeout_sec=WIRE)
    assert seen == [(WIRE, WIRE)] * 2
    assert attempt_stages == [
        "provider_attempt_start_1",
        "provider_attempt_failed_1",
        "provider_attempt_start_1",
        "provider_attempt_complete_1",
    ]


def test_without_the_option_the_loop_is_unchanged(monkeypatch):
    seen, attempt_stages = _loop_with_tagged_image_rejection(monkeypatch)
    assert seen == [(None, None)] * 2
    assert attempt_stages == []
