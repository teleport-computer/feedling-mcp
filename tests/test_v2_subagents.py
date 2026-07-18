from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import subagents  # noqa: E402
from provider_types import ToolCall  # noqa: E402


def _call(call_id: str, prompt: str, **extra) -> ToolCall:
    return ToolCall(
        call_id,
        subagents.TASK_TOOL,
        {"prompt": prompt, **extra},
    )


def test_task_batch_runs_in_parallel_and_preserves_provider_order() -> None:
    active = 0
    peak = 0

    async def child(task):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01 if task.call_id == "second" else 0.02)
        active -= 1
        return subagents.ChildTaskResult(summary=f"done:{task.prompt}")

    results = asyncio.run(
        subagents.run_task_batch(
            [_call("first", "one"), _call("second", "two")],
            run_child=child,
            max_parallel_tasks=2,
        )
    )

    assert peak == 2
    assert [result.call_id for result in results] == ["first", "second"]
    assert [json.loads(result.content)["summary"] for result in results] == [
        "done:one",
        "done:two",
    ]


def test_task_batch_isolates_failure_and_deadline() -> None:
    async def child(task):
        if task.prompt == "fail":
            raise RuntimeError("private provider error")
        if task.prompt == "slow":
            await asyncio.sleep(0.05)
        return subagents.ChildTaskResult(summary="ok")

    results = asyncio.run(
        subagents.run_task_batch(
            [
                _call("ok", "ok"),
                _call("failed", "fail"),
                _call("timed-out", "slow"),
            ],
            run_child=child,
            child_deadline_sec=0.01,
        )
    )
    bodies = {result.call_id: json.loads(result.content) for result in results}

    assert bodies["ok"]["status"] == "completed"
    assert bodies["failed"] == {"status": "error", "error": "subagent_failed"}
    assert bodies["timed-out"] == {
        "status": "error",
        "error": "subagent_deadline_exceeded",
    }
    assert "private" not in str(bodies)


def test_task_result_is_bounded_valid_json() -> None:
    async def child(_task):
        return subagents.ChildTaskResult(
            summary=('quoted "\\ value' * 200),
            workspace_changes=({"path": "/workspace/a.md", "revision": 2},),
        )

    (result,) = asyncio.run(
        subagents.run_task_batch(
            [_call("one", "inspect")],
            run_child=child,
            child_result_char_cap=256,
        )
    )

    assert len(result.content) <= 256
    body = json.loads(result.content)
    assert body["status"] == "completed"
    assert body["truncated"] is True
    assert body["workspace_changes"] == []


@pytest.mark.parametrize(
    "call",
    [
        ToolCall("id", "web_search", {"query": "x"}),
        _call("id", ""),
        _call("id", "x", workspace_mode="host_filesystem"),
    ],
)
def test_task_batch_rejects_invalid_task_contract(call) -> None:
    async def child(_task):  # pragma: no cover - validation happens first
        return subagents.ChildTaskResult(summary="unexpected")

    with pytest.raises(subagents.SubagentBatchError):
        asyncio.run(subagents.run_task_batch([call], run_child=child))


def test_task_batch_rejects_oversized_batch_before_execution() -> None:
    called = False

    async def child(_task):
        nonlocal called
        called = True
        return subagents.ChildTaskResult(summary="unexpected")

    with pytest.raises(subagents.SubagentBatchError, match="per-round"):
        asyncio.run(
            subagents.run_task_batch(
                [_call("one", "a"), _call("two", "b")],
                run_child=child,
                max_tasks_per_round=1,
            )
        )
    assert called is False
