from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from core import chat_activity  # noqa: E402
from capabilities import activity_metadata  # noqa: E402
from capabilities.types import ok  # noqa: E402
from model_api_runtime.v2 import executor, worker  # noqa: E402
from provider_types import ToolCall, ToolResult  # noqa: E402


def test_projection_keeps_ids_and_status_but_never_result_body():
    rows = [
        {
            "id": 1,
            "job_id": 9,
            "kind": "tool_activity",
            "created_at": 10.0,
            "detail_json": {
                "activity_id": "9:1:1",
                "tool_name": "memory_search",
                "call_id": "call-1",
                "state": "running",
            },
        },
        {
            "id": 2,
            "job_id": 9,
            "kind": "tool_activity",
            "created_at": 10.5,
            "detail_json": {
                "activity_id": "9:1:1",
                "tool_name": "memory_search",
                "call_id": "call-1",
                "state": "success",
                "duration_ms": 500,
                "result_code": "ok",
                "effect_id": "effect-1",
                "effect_status": "applied",
            },
        },
    ]
    assert chat_activity.project_tool_events(rows) == [
        {
            "id": "9:1:1",
            "kind": "tool",
            "name": "memory_search",
            "status": "success",
            "job_id": "9",
            "call_id": "call-1",
            "started_at": 10.0,
            "finished_at": 10.5,
            "duration_ms": 500.0,
            "effect_id": "effect-1",
            "effect_status": "applied",
            "result_code": "ok",
        }
    ]


def test_projection_keeps_only_confirmed_scheduled_task_metadata():
    rows = [{
        "id": 1,
        "job_id": 12,
        "kind": "tool_activity",
        "created_at": 20.0,
        "detail_json": {
            "activity_id": "12:1:1",
            "tool_name": "schedule_wake",
            "call_id": "call-schedule",
            "state": "success",
            "schedule_operation": "schedule_wake",
            "schedule_status": "scheduled",
            "schedule_task_id": "sched_real_1",
            "schedule_next_trigger_at": "2026-07-27T08:00:00",
            "schedule_timezone": "Asia/Shanghai",
            "note": "private reminder body",
        },
    }]

    event = chat_activity.project_tool_events(rows)[0]
    assert event["schedule_operation"] == "schedule_wake"
    assert event["schedule_status"] == "scheduled"
    assert event["schedule_task_id"] == "sched_real_1"
    assert event["schedule_next_trigger_at"] == "2026-07-27T08:00:00"
    assert event["schedule_timezone"] == "Asia/Shanghai"
    assert "private reminder body" not in repr(event)


def test_chat_tool_callback_marks_rejected_schedule_as_failure(monkeypatch):
    captured = []
    monkeypatch.setattr(
        worker.jobs_store,
        "append_status_event",
        lambda _user_id, _kind, **kwargs: captured.append(kwargs["detail"]),
    )
    callback = worker._make_chat_tool_activity_callback(
        user_id="usr_schedule_failure",
        job_id=13,
        attempt_identity=1,
        recorder=None,
        effect_evidence_by_call={},
    )
    call = SimpleNamespace(id="call-schedule", name="schedule_wake")

    async def run():
        await callback(call, "tool_call_started", {})
        await callback(
            call,
            "tool_call_result",
            {
                "result": ToolResult(
                    call_id="call-schedule",
                    content='{"status":"rejected"}',
                    metadata={
                        "schedule_operation": "schedule_wake",
                        "schedule_status": "rejected",
                    },
                ),
            },
        )

    asyncio.run(run())
    assert captured[-1]["state"] == "failure"
    assert captured[-1]["result_code"] == "schedule_rejected"


def test_chat_tool_callback_emits_safe_result_metadata(monkeypatch):
    captured = []

    def append(user_id, kind, **kwargs):
        captured.append((user_id, kind, kwargs))
        return 1

    monkeypatch.setattr(worker.jobs_store, "append_status_event", append)
    evidence = {
        "call-1": {
            "effect_id": "effect-1",
            "effect_type": "workspace_write",
            "status": "applied",
        }
    }
    callback = worker._make_chat_tool_activity_callback(
        user_id="usr_1",
        job_id=7,
        attempt_identity=2,
        recorder=None,
        effect_evidence_by_call=evidence,
    )
    call = SimpleNamespace(id="call-1", name="workspace_write")

    async def run():
        await callback(call, "tool_call_started", {"phase": "platform_mutation"})
        await callback(
            call,
            "tool_call_result",
            {
                "duration_ms": 12.5,
                "result": ToolResult(
                    call_id="call-1",
                    content="secret workspace text that must never be stored",
                ),
            },
        )

    asyncio.run(run())
    assert [item[2]["detail"]["state"] for item in captured] == [
        "running",
        "success",
    ]
    assert captured[0][2]["detail"]["activity_id"] == "7:2:1"
    final = captured[1][2]["detail"]
    assert final["effect_id"] == "effect-1"
    assert final["effect_status"] == "applied"
    assert final["result_code"] == "applied"
    assert "secret" not in repr(captured)


def test_perception_result_metadata_classifies_values_without_retaining_them():
    assert activity_metadata.perception_result_metadata(
        "perception_snapshot",
        {
            "ok": True,
            "data": {"signals": {"steps": {"step_count": 0}}},
        },
    ) == {"perception_result_kind": "value"}
    assert activity_metadata.perception_result_metadata(
        "perception_snapshot",
        {
            "ok": True,
            "data": {"signals": {
                "steps": {"step_count": None},
                "sleep": {"disabled": True, "reason": "private reason"},
            }},
        },
    ) == {"perception_result_kind": "empty"}
    assert activity_metadata.perception_result_metadata(
        "perception_history",
        {"ok": True, "data": {"daily": [{"date": "2026-08-10", "doc": {}}]}},
    ) == {"perception_result_kind": "empty"}
    assert activity_metadata.perception_result_metadata(
        "perception_trend",
        {"ok": True, "data": {"trend": {"current": 0, "daily": []}}},
    ) == {"perception_result_kind": "value"}
    assert activity_metadata.perception_result_metadata(
        "perception_snapshot",
        {
            "ok": False,
            "error": {"code": "capability_invalid_input", "message": "secret"},
        },
    ) == {
        "perception_result_kind": "error",
        "perception_error_code": "capability_invalid_input",
    }
    assert activity_metadata.perception_result_metadata(
        "perception_snapshot",
        {
            "ok": False,
            "error": {
                "code": "capability_invalid_input",
                "message": "unknown_signals",
            },
        },
    )["perception_error_code"] == "unknown_signals"


def test_chat_tool_callback_emits_content_free_v2_debug_trace(monkeypatch):
    traces = []
    monkeypatch.setattr(worker.jobs_store, "append_status_event", lambda *_a, **_kw: 1)

    def emit(user_id, event_type, **kwargs):
        traces.append((user_id, event_type, kwargs))

    callback = worker._make_chat_tool_activity_callback(
        user_id="usr_trace",
        job_id=17,
        attempt_identity=1,
        recorder=None,
        effect_evidence_by_call={},
        emit_debug_trace=emit,
    )
    call = ToolCall(
        id="call-perception",
        name="perception_snapshot",
        args={
            "signals": ["steps", "sleep"],
            "secret_prompt": "private calendar title",
        },
    )

    async def run():
        await callback(call, "tool_call_started", {})
        await callback(call, "tool_call_result", {
            "duration_ms": 12.3456,
            "result": ToolResult(
                call_id=call.id,
                content='{"signals":{"steps":{"step_count":12345},"sleep":{"note":"private"}}}',
                metadata={"perception_result_kind": "value"},
            ),
        })

    asyncio.run(run())
    assert len(traces) == 1
    user_id, event_type, kwargs = traces[0]
    assert user_id == "usr_trace"
    assert event_type == "agent.tool.call"
    assert kwargs["status"] == "ok"
    assert kwargs["dur_ms"] == 12.346
    assert kwargs["detail"] == {
        "tool": "perception_snapshot",
        "args": {"signals": ["steps", "sleep"]},
        "result_status": "ok",
        "result_kind": "value",
        "dur_ms": 12.346,
    }
    assert "12345" not in repr(traces)
    assert "private" not in repr(traces)


def test_v2_debug_trace_defaults_snapshot_signals_and_keeps_safe_error_only(monkeypatch):
    traces = []
    monkeypatch.setattr(worker.jobs_store, "append_status_event", lambda *_a, **_kw: 1)
    callback = worker._make_chat_tool_activity_callback(
        user_id="usr_trace",
        job_id=18,
        attempt_identity=1,
        recorder=None,
        effect_evidence_by_call={},
        emit_debug_trace=lambda user_id, event_type, **kwargs: traces.append(kwargs),
    )
    call = ToolCall(
        id="call-default",
        name="perception_snapshot",
        args={"query": "very private text"},
    )

    asyncio.run(callback(call, "tool_call_result", {
        "duration_ms": 3,
        "result": ToolResult(
            call_id=call.id,
            content="error: capability_invalid_input private details",
            metadata={
                "perception_result_kind": "error",
                "perception_error_code": "capability_invalid_input",
            },
        ),
    }))

    assert traces[0]["detail"] == {
        "tool": "perception_snapshot",
        "args": {
            "signals": ["now", "location", "weather", "motion", "calendar"],
            "defaulted": True,
        },
        "result_status": "err",
        "result_kind": "error",
        "dur_ms": 3.0,
        "error_code": "capability_invalid_input",
    }
    assert "private" not in repr(traces)


def test_v2_tool_trace_never_promotes_arbitrary_error_tokens_to_codes():
    private_tokens = (
        "my_private_notes.txt",
        "girlfriend_name_liuyu",
        "dns-failed-internal.corp.example.com",
    )
    call = ToolCall(id="call-private-error", name="workspace_read", args={})

    for token in private_tokens:
        detail = worker._v2_tool_trace_detail(
            call,
            event_kind="tool_call_result",
            result=ToolResult(call_id=call.id, content=f"error: {token}"),
            duration_ms=1.0,
        )
        assert detail["error_code"] == "tool_error"
        assert token not in repr(detail)


def test_chat_tool_callback_survives_trace_projection_failure(monkeypatch):
    persisted = []
    emitted = []
    monkeypatch.setattr(
        worker.jobs_store,
        "append_status_event",
        lambda _user_id, _kind, **kwargs: persisted.append(kwargs["detail"]),
    )
    monkeypatch.setattr(
        worker,
        "_v2_tool_trace_detail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("trace bug")),
    )
    callback = worker._make_chat_tool_activity_callback(
        user_id="usr_trace_failure",
        job_id=19,
        attempt_identity=1,
        recorder=None,
        effect_evidence_by_call={},
        emit_debug_trace=lambda *_args, **_kwargs: emitted.append(1),
    )
    call = ToolCall(id="call-safe", name="perception_snapshot", args={})

    asyncio.run(callback(call, "tool_call_result", {
        "result": ToolResult(call_id=call.id, content="ok"),
    }))

    assert persisted[0]["state"] == "success"
    assert emitted == []


def test_chat_tool_callback_keeps_invocation_ids_unique_across_rounds(monkeypatch):
    captured = []
    monkeypatch.setattr(
        worker.jobs_store,
        "append_status_event",
        lambda _user_id, _kind, **kwargs: captured.append(kwargs["detail"]),
    )
    callback = worker._make_chat_tool_activity_callback(
        user_id="usr_1",
        job_id=7,
        attempt_identity=2,
        recorder=None,
        effect_evidence_by_call={},
    )

    async def run():
        for call_id in ("round-1", "round-2"):
            call = SimpleNamespace(id=call_id, name="memory_search")
            await callback(call, "tool_call_started", {})
            await callback(
                call,
                "tool_call_result",
                {"result": ToolResult(call_id=call_id, content="ok")},
            )

    asyncio.run(run())
    assert [item["activity_id"] for item in captured] == [
        "7:2:1", "7:2:1", "7:2:2", "7:2:2",
    ]


def test_turn_response_reports_backend_job_phase_separately_from_tools():
    response = chat_activity.turn_response(
        "turn-1",
        [{"id": 3, "status": "completed"}],
        [{"kind": "done", "created_at": 4.0, "detail_json": {}}],
    )
    assert response == {
        "turn_id": "turn-1",
        "runtime": "v2",
        "complete": True,
        "phase": "done",
        "jobs": [{"job_id": "3", "status": "completed"}],
        "events": [],
    }


def test_turn_response_exposes_only_bounded_failure_identity():
    response = chat_activity.turn_response(
        "turn-failed",
        [{
            "id": 41,
            "status": "failed",
            "last_error": "turn_failed:empty_reply secret words",
        }],
        [{"kind": "error", "created_at": 4.0, "detail_json": {}}],
    )

    assert response["failure"] == {
        "code": "turn_failed:failed",
        "job_id": "41",
        "message_id": "turn-failed",
    }
    assert response["complete"] is True


def test_visual_failure_projects_exact_message_model_and_provider():
    response = chat_activity.turn_response(
        "user-message-1",
        [{
            "id": 42,
            "status": "failed",
            "last_error": "turn_failed:vision_model_auth_invalid",
        }],
        [{
            "job_id": 42,
            "kind": "error",
            "detail_json": {
                "failure_model": "openai/gpt-vision",
                "failure_provider": "openrouter",
            },
        }],
    )

    assert response["failure"] == {
        "code": "turn_failed:vision_model_auth_invalid",
        "job_id": "42",
        "message_id": "user-message-1",
        "model": "openai/gpt-vision",
        "provider": "openrouter",
    }


def test_memory_projection_keeps_every_zero_discovery_before_positive():
    rows = []
    for index, (name, count) in enumerate(
        [("memory_search", 0), ("memory_search", 0), ("memory_index", 2)],
        start=1,
    ):
        rows.append({
            "id": index,
            "job_id": 9,
            "kind": "tool_activity",
            "created_at": float(index),
            "detail_json": {
                "activity_id": f"9:1:{index}",
                "tool_name": name,
                "call_id": f"call-{index}",
                "state": "success",
                "memory_count": count,
            },
        })

    events = chat_activity.project_tool_events(rows)
    assert [(event["name"], event["memory_count"]) for event in events] == [
        ("memory_search", 0),
        ("memory_search", 0),
        ("memory_index", 2),
    ]


def test_memory_projection_keeps_every_zero_without_positive_result():
    rows = [
        {
            "id": index,
            "job_id": 9,
            "kind": "tool_activity",
            "created_at": float(index),
            "detail_json": {
                "activity_id": f"9:1:{index}",
                "tool_name": "memory_search",
                "call_id": f"call-{index}",
                "state": "success",
                "memory_count": 0,
            },
        }
        for index in (1, 2)
    ]

    events = chat_activity.project_tool_events(rows)
    assert [event["call_id"] for event in events] == ["call-1", "call-2"]


def test_result_classifier_never_turns_error_body_into_metadata():
    assert chat_activity.result_code("error: private customer record") == "tool_error"
    assert chat_activity.result_code("queued: effect-123") == "queued"
    assert chat_activity.result_code("private customer record") == "ok"


def test_image_generation_result_code_is_allowlisted():
    assert chat_activity.image_generation_result_code(
        "image_generation_model_required"
    ) == "image_generation_model_required"
    assert chat_activity.image_generation_result_code("private_customer_record") == ""


def test_chat_tool_callback_keeps_image_generation_failure_code(monkeypatch):
    captured = []
    monkeypatch.setattr(
        worker.jobs_store,
        "append_status_event",
        lambda _user_id, _kind, **kwargs: captured.append(kwargs["detail"]),
    )
    callback = worker._make_chat_tool_activity_callback(
        user_id="usr_image_failure",
        job_id=17,
        attempt_identity=1,
        recorder=None,
        effect_evidence_by_call={},
    )
    call = SimpleNamespace(id="call-image", name="generate_image")

    async def run():
        await callback(call, "tool_call_started", {})
        await callback(
            call,
            "tool_call_result",
            {
                "result": ToolResult(
                    call_id="call-image",
                    content="error: private provider detail",
                    metadata={
                        "image_generation_result_code": (
                            "image_generation_model_required"
                        )
                    },
                )
            },
        )

    asyncio.run(run())
    assert captured[-1]["state"] == "failure"
    assert captured[-1]["result_code"] == "image_generation_model_required"
    assert "private provider detail" not in repr(captured)


def _memory_result(*buckets):
    return {
        "ok": True,
        "data": {"items": [{"id": f"m{index}", "bucket": bucket}
                           for index, bucket in enumerate(buckets)]},
    }


def test_memory_activity_zero_results_has_confirmed_zero_only():
    assert activity_metadata.memory_result_metadata(
        "memory_search", _memory_result()
    ) == {"memory_count": 0}


def test_memory_activity_one_result_has_canonical_category():
    assert activity_metadata.memory_result_metadata(
        "memory_fetch", _memory_result("我们的关系")
    ) == {
        "memory_count": 1,
        "memory_categories": [{"key": "relationship", "count": 1}],
    }


def test_memory_activity_multiple_results_groups_bilingual_canonical_buckets():
    assert activity_metadata.memory_result_metadata(
        "memory_search",
        _memory_result("我们的关系", "Our relationship", "我们的关系", "Family"),
    ) == {
        "memory_count": 4,
        "memory_categories": [
            {"key": "relationship", "count": 3},
            {"key": "family", "count": 1},
        ],
    }


def test_memory_index_activity_reports_exact_count_and_categories():
    assert activity_metadata.memory_result_metadata(
        "memory_index",
        _memory_result("我们的关系", "Our relationship", "Family", "我们的关系"),
    ) == {
        "memory_count": 4,
        "memory_categories": [
            {"key": "relationship", "count": 3},
            {"key": "family", "count": 1},
        ],
    }


def test_memory_index_activity_keeps_content_free_completeness_counts():
    result = _memory_result("Places & travel", "Places & travel")
    result["data"].update({"total": 103, "returned": 2})

    assert activity_metadata.memory_result_metadata("memory_index", result) == {
        "memory_count": 2,
        "memory_total": 103,
        "memory_returned": 2,
        "memory_query_kind": "memory_index",
        "memory_categories": [{"key": "travel", "count": 2}],
    }


def test_memory_activity_unknown_category_falls_back_to_total():
    assert activity_metadata.memory_result_metadata(
        "memory_search", _memory_result("妈妈", "Family")
    ) == {"memory_count": 2}


def test_memory_activity_eleven_results_with_custom_category_falls_back_to_total():
    assert activity_metadata.memory_result_metadata(
        "memory_search",
        _memory_result("妈妈", *("Family" for _ in range(10))),
    ) == {"memory_count": 11}


def test_chat_tool_callback_persists_only_safe_memory_summary(monkeypatch):
    captured = []
    monkeypatch.setattr(
        worker.jobs_store,
        "append_status_event",
        lambda _user_id, _kind, **kwargs: captured.append(kwargs["detail"]),
    )
    callback = worker._make_chat_tool_activity_callback(
        user_id="usr_1",
        job_id=8,
        attempt_identity=1,
        recorder=None,
        effect_evidence_by_call={},
    )
    call = SimpleNamespace(id="call-memory", name="memory_search")

    async def run():
        await callback(call, "tool_call_result", {
            "result": ToolResult(
                call_id="call-memory",
                content='{"items":[{"summary":"private"}]}',
                metadata={
                    "memory_count": 4,
                    "memory_categories": [
                        {"key": "relationship", "count": 3},
                        {"key": "family", "count": 1},
                    ],
                },
            ),
        })

    asyncio.run(run())
    assert captured[0]["memory_count"] == 4
    assert captured[0]["memory_categories"] == [
        {"key": "relationship", "count": 3},
        {"key": "family", "count": 1},
    ]
    assert "private" not in repr(captured)


def test_executor_keeps_memory_count_before_provider_result_truncation(monkeypatch):
    monkeypatch.setattr(
        executor.cap_registry,
        "run_capability",
        lambda *_args, **_kwargs: ok(data={
            "items": [
                {"id": "m1", "bucket": "Family", "summary": "x" * 2000},
                {"id": "m2", "bucket": "Family", "summary": "y" * 2000},
            ]
        }),
    )

    async def run():
        return await executor.dispatch_tool_calls(
            [ToolCall(id="c1", name="memory_search", args={"query": "family"})],
            store=SimpleNamespace(),
            api_key=None,
            runtime_token="rt",
            enclave_sem=asyncio.Semaphore(1),
            turn_authorization=True,
            enqueue_write_effect=lambda _tc: None,
        )

    result = asyncio.run(run())[0]
    assert result.content.endswith("...[truncated]")
    assert result.metadata == {
        "memory_count": 2,
        "memory_categories": [{"key": "family", "count": 2}],
    }


def test_executor_projects_perception_empty_metadata_at_trusted_boundary(monkeypatch):
    monkeypatch.setattr(
        executor.cap_registry,
        "run_capability",
        lambda *_args, **_kwargs: ok(data={
            "signals": {"steps": {"step_count": None}},
        }),
    )

    async def run():
        return await executor.dispatch_tool_calls(
            [ToolCall(
                id="c-perception",
                name="perception_snapshot",
                args={"signals": ["steps"]},
            )],
            store=SimpleNamespace(),
            api_key=None,
            runtime_token="rt",
            enclave_sem=asyncio.Semaphore(1),
            turn_authorization=True,
            enqueue_write_effect=lambda _tc: None,
        )

    result = asyncio.run(run())[0]
    assert "step_count" in result.content
    assert result.metadata == {"perception_result_kind": "empty"}


def test_executor_memory_index_truncation_guides_partition_browsing(monkeypatch):
    monkeypatch.setattr(
        executor.cap_registry,
        "run_capability",
        lambda *_args, **_kwargs: ok(data={
            "items": [
                {"id": f"m{index}", "bucket": "Travel", "summary": "x" * 100}
                for index in range(50)
            ],
            "total": 103,
            "returned": 50,
        }),
    )

    async def run():
        return await executor.dispatch_tool_calls(
            [ToolCall(id="c-index", name="memory_index", args={"bucket": "Travel"})],
            store=SimpleNamespace(),
            api_key=None,
            runtime_token="rt",
            enclave_sem=asyncio.Semaphore(1),
            turn_authorization=True,
            enqueue_write_effect=lambda _tc: None,
        )

    result = asyncio.run(run())[0]
    assert "returned 50 of 103 total cards" in result.content
    assert "bucket or thread filters" in result.content
    assert len(result.content) == executor._RESULT_CHAR_CAP + len("...[truncated]")
