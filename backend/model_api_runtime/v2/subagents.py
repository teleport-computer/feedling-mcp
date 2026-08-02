"""Bounded native subagent orchestration for Runtime V2.

Subagents are ordinary invocations of the same provider-native loop with an
isolated transcript and a restricted dispatcher supplied by the parent worker.
This module owns only the batch/deadline/result contract; it deliberately does
not know about user replies, platform effects, MCP, or a concrete workspace.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from capabilities.tool_schema import TASK_TOOL
from provider_types import ToolResult


DEFAULT_MAX_TASKS_PER_ROUND = 4
DEFAULT_MAX_PARALLEL_TASKS = 4
DEFAULT_CHILD_DEADLINE_SEC = 45.0
DEFAULT_CHILD_RESULT_CHAR_CAP = 12_000
DEFAULT_PARENT_MAX_PROVIDER_CALLS = 12
DEFAULT_PARENT_MAX_TOKENS = 131_072
DEFAULT_PROVIDER_CALL_TOKEN_RESERVATION = 32_768


@dataclass(frozen=True)
class ChildTask:
    call_id: str
    prompt: str
    label: str = ""
    workspace_mode: str = "read_only"


@dataclass(frozen=True)
class ChildTaskResult:
    summary: str
    workspace_changes: tuple[dict[str, Any], ...] = ()


class SubagentBatchError(ValueError):
    """The parent attempted to dispatch an invalid task batch."""


class SubagentBudgetExhausted(RuntimeError):
    """A child provider call would exceed the shared parent-turn budget."""


def _usage_tokens(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None

    def _value(*names: str) -> int | None:
        for name in names:
            raw = usage.get(name)
            if isinstance(raw, bool):
                continue
            try:
                parsed = int(raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if parsed >= 0:
                return parsed
        return None

    total = _value("total_tokens")
    prompt = _value("prompt_tokens", "input_tokens")
    completion = _value("completion_tokens", "output_tokens")
    components = None if prompt is None and completion is None else (prompt or 0) + (
        completion or 0
    )
    if total is None:
        return components
    if components is None:
        return total
    return max(total, components)


@dataclass
class SharedSubagentBudget:
    """Synchronous reservation ledger shared by every child in one parent turn.

    Child loops run concurrently on one asyncio thread. Reserving a complete
    per-call context window before provider I/O makes the aggregate token ceiling
    effective even when four children start at once. Completion refunds unused
    reservation; missing usage telemetry consumes the full reservation.
    """

    max_provider_calls: int = DEFAULT_PARENT_MAX_PROVIDER_CALLS
    max_tokens: int = DEFAULT_PARENT_MAX_TOKENS
    provider_call_token_reservation: int = DEFAULT_PROVIDER_CALL_TOKEN_RESERVATION
    provider_calls_started: int = 0
    tokens_charged: int = 0
    tokens_reserved: int = 0
    calls_in_flight: int = 0

    def __post_init__(self) -> None:
        self.max_provider_calls = _positive_int(
            self.max_provider_calls,
            name="max_provider_calls",
        )
        self.max_tokens = _positive_int(self.max_tokens, name="max_tokens")
        self.provider_call_token_reservation = _positive_int(
            self.provider_call_token_reservation,
            name="provider_call_token_reservation",
        )
        if self.provider_call_token_reservation > self.max_tokens:
            raise ValueError("provider call token reservation exceeds parent budget")

    def before_provider_call(self) -> None:
        reservation = self.provider_call_token_reservation
        if self.provider_calls_started >= self.max_provider_calls:
            raise SubagentBudgetExhausted("subagent provider-call budget exhausted")
        if self.tokens_charged + self.tokens_reserved + reservation > self.max_tokens:
            raise SubagentBudgetExhausted("subagent token budget exhausted")
        self.provider_calls_started += 1
        self.calls_in_flight += 1
        self.tokens_reserved += reservation

    def complete_provider_call(self, usage: Any) -> None:
        if self.calls_in_flight <= 0:
            raise RuntimeError("subagent usage arrived without a reserved call")
        reservation = self.provider_call_token_reservation
        self.calls_in_flight -= 1
        self.tokens_reserved -= reservation
        tokens = _usage_tokens(usage)
        self.tokens_charged += reservation if tokens is None else tokens


def _positive_int(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float(value: float, *, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be positive") from exc
    if parsed <= 0 or not math.isfinite(parsed):
        raise ValueError(f"{name} must be positive")
    return parsed


def _parse_task(call: Any) -> ChildTask:
    if str(getattr(call, "name", "") or "") != TASK_TOOL:
        raise SubagentBatchError("subagent dispatcher received a non-task call")
    call_id = str(getattr(call, "id", "") or "")
    args = getattr(call, "args", None)
    if not call_id or not isinstance(args, dict):
        raise SubagentBatchError("subagent task is missing id or arguments")
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise SubagentBatchError("subagent task prompt is required")
    mode = str(args.get("workspace_mode") or "read_only").strip().lower()
    if mode != "read_only":
        raise SubagentBatchError("unsupported subagent workspace mode")
    return ChildTask(
        call_id=call_id,
        prompt=prompt,
        label=str(args.get("label") or "").strip()[:120],
        workspace_mode=mode,
    )


def _bounded_json(result: ChildTaskResult, *, char_cap: int) -> str:
    payload = {
        "status": "completed",
        "summary": str(result.summary or ""),
        "workspace_changes": list(result.workspace_changes),
    }
    rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= char_cap:
        return rendered

    # Preserve a valid, explicit result envelope. Workspace merge metadata is
    # omitted before the human-readable summary because the parent can recover
    # from a bounded explanation, while half of a CAS record is unusable.
    payload["workspace_changes"] = []
    payload["truncated"] = True
    summary = payload["summary"]
    low, high = 0, len(summary)
    while low < high:
        midpoint = (low + high + 1) // 2
        payload["summary"] = summary[:midpoint]
        candidate = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(candidate) <= char_cap:
            low = midpoint
        else:
            high = midpoint - 1
    payload["summary"] = summary[:low]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


async def run_task_batch(
    calls: Sequence[Any],
    *,
    run_child: Callable[[ChildTask], Awaitable[ChildTaskResult]],
    max_tasks_per_round: int = DEFAULT_MAX_TASKS_PER_ROUND,
    max_parallel_tasks: int = DEFAULT_MAX_PARALLEL_TASKS,
    child_deadline_sec: float = DEFAULT_CHILD_DEADLINE_SEC,
    child_result_char_cap: int = DEFAULT_CHILD_RESULT_CHAR_CAP,
) -> list[ToolResult]:
    """Run independent child tasks concurrently and preserve provider order.

    The parent supplies ``run_child`` and is therefore responsible for the
    child's restricted tool catalog, isolated transcript/workspace overlay,
    provider-call/token budget, and explicit merge. Exceptions and timeouts are
    converted to stable tool results so one failed child never cancels siblings.
    """
    max_tasks = _positive_int(max_tasks_per_round, name="max_tasks_per_round")
    parallelism = _positive_int(max_parallel_tasks, name="max_parallel_tasks")
    result_cap = _positive_int(child_result_char_cap, name="child_result_char_cap")
    if result_cap < 128:
        raise ValueError("child_result_char_cap must be at least 128")
    deadline = _positive_float(child_deadline_sec, name="child_deadline_sec")
    if len(calls) > max_tasks:
        raise SubagentBatchError("subagent task batch exceeds the per-round limit")

    tasks = [_parse_task(call) for call in calls]
    child_ids = [task.call_id for task in tasks]
    if len(child_ids) != len(set(child_ids)):
        raise SubagentBatchError("subagent task batch has duplicate child id")
    gate = asyncio.Semaphore(parallelism)

    async def _one(task: ChildTask) -> ToolResult:
        try:
            async with gate:
                result = await asyncio.wait_for(run_child(task), timeout=deadline)
            if not isinstance(result, ChildTaskResult):
                raise TypeError("child returned an invalid result")
            content = _bounded_json(result, char_cap=result_cap)
        except asyncio.TimeoutError:
            content = json.dumps(
                {"status": "error", "error": "subagent_deadline_exceeded"},
                separators=(",", ":"),
            )
        except SubagentBudgetExhausted:
            content = json.dumps(
                {"status": "error", "error": "subagent_budget_exhausted"},
                separators=(",", ":"),
            )
        except Exception:  # noqa: BLE001 - child/provider errors are untrusted
            content = json.dumps(
                {"status": "error", "error": "subagent_failed"},
                separators=(",", ":"),
            )
        return ToolResult(call_id=task.call_id, content=content)

    return list(await asyncio.gather(*(_one(task) for task in tasks)))
