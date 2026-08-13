"""One trusted result-budget policy, read by every layer that truncates.

History search spec §3.3.  A tool result passes two independent truncations on
its way to the model: the executor's per-result cap
(``executor._summarize_capability_result``) and the tool loop's per-result cap
plus same-batch water-fill (``tool_loop._normalize_tool_results``).  Both cut
**strings after serialization**, so raising only the executor's cap is useless:
the water-fill cuts the result back to ~2000 — and under the worst batch shape
(seven maxed-out siblings) to ~1000 — which slices a JSON payload mid-string.

So the numbers live here once and all three layers read them:

    history_fetch:  result_cap=4500  atomic_json=True  extra_batch_budget=2500
    history_search: result_cap=1800  atomic_json=True

* ``atomic_json`` — the water-fill reserves this result's full length up front
  and shares only the remainder with its siblings.  It is never cut, at any
  batch shape.  Only declare it for producers that shrink **structurally**
  before serializing (the history facade does); the cap is then a contract the
  producer already met, not a blind guillotine.
* ``extra_batch_budget`` — how much the same-batch total may rise while such a
  result is present.  Without it a 4500-char fetch would leave the other seven
  results ~500 characters each, which is a real product regression.

The facade decides *how* to shrink; it does not decide the numbers.  It marks
the result kind through the trusted ``ToolResult.metadata`` channel
(``RESULT_KIND_METADATA_KEY``) — the same channel the memory tools already use
for their truncation marker — and the loop looks the policy up from there.
Provider-supplied text can never reach this channel.

Values are env-overridable so a deployment can dial them back without a code
change; the override is read at call time so all three layers always agree.
An override that breaks the contract is rejected at process start by
``validate_result_caps`` — see its docstring for the two ways a "harmless"
number silently produces invalid JSON or blows the batch total.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

# metadata key carrying the trusted result kind (set at the capability boundary).
RESULT_KIND_METADATA_KEY = "result_budget_kind"

# Stable code for "the producer could not meet its own atomic contract".  Every
# layer that would otherwise slice an atomic JSON payload emits exactly this
# instead — one short, valid, parseable result rather than a broken object.
OVERFLOW_ERROR_CODE = "result_budget_exceeded"
OVERFLOW_RESULT_CONTENT = f"error: {OVERFLOW_ERROR_CODE}"

# 用户自建 MCP 的返回。2026-08-12 加:通用逐条上限是 2000 字符,对记忆型 MCP
# (Ombre Brain 之类,一次 breath 可能带回好几条记忆)来说模型看到的是碎片 ——
# 「AI 不认可我的记忆」里有一部分就是这么来的:它拿到的本来就不完整。
#
# ⚠️ 刻意 **不** 声明 atomic_json:那面旗只该给「序列化前已按结构收缩」的生产者
# (见本文件开头)。MCP 服务器返回什么就是什么,没有这个契约;声明成原子的话,
# 超限的返回会被整份换成一条 error,比截断更糟。
USER_MCP_RESULT_MAX_CHARS_ENV = "FEEDLING_V2_USER_MCP_RESULT_MAX_CHARS"
DEFAULT_USER_MCP_RESULT_MAX_CHARS = 8000
USER_MCP_RESULT_KIND = "user_mcp"

HISTORY_SEARCH_RESULT_MAX_CHARS_ENV = "FEEDLING_V2_HISTORY_SEARCH_RESULT_MAX_CHARS"
HISTORY_FETCH_RESULT_MAX_CHARS_ENV = "FEEDLING_V2_HISTORY_FETCH_RESULT_MAX_CHARS"
DEFAULT_HISTORY_SEARCH_RESULT_MAX_CHARS = 1800
DEFAULT_HISTORY_FETCH_RESULT_MAX_CHARS = 4500
DEFAULT_HISTORY_FETCH_EXTRA_BATCH_CHARS = 2500

# history_search.CURSOR_MAX_CHARS.  Duplicated as a plain number on purpose:
# model_api_runtime already imports this module (executor / tool_loop), so
# importing back into model_api_runtime here would close the loop.  A unit test
# asserts the two stay equal.
_CURSOR_MAX_CHARS = 1024


def _skeleton_chars(payload: Any) -> int:
    """Rendered size under the exact serialization every layer measures with."""
    return len(json.dumps(payload, ensure_ascii=False))


# Smallest payload each producer's structural shrink can still emit.  A cap
# below this is unsatisfiable: the facade shrinks as far as it can, is still
# over, and hands a too-large object to a string-slicing layer downstream.
#
# ``next_cursor`` is the reason search's floor is large: the three-state
# semantics hang off it (spec §3.1), so the shrink may never drop it, and it is
# variable-length up to the cursor ceiling.  Truncation must never be able to
# masquerade as "the scan finished".
MIN_RESULT_CAPS: Mapping[str, int] = {
    "history_search": _skeleton_chars({
        "matches": [],
        "complete": False,
        "scanned_count": 0,
        "unavailable_count": 0,
        "coverage_gap": False,
        "next_cursor": "x" * _CURSOR_MAX_CHARS,
    }),
    # anchor is never dropped (spec §3.2), and its excerpt shrinks to "" at
    # worst; the neighbor lists and the two omission counters always stay.
    "history_fetch": _skeleton_chars({
        "anchor": {
            "message_id": "x" * 64,
            "ts": 1720000000.0,
            "role": "assistant",
            "content": "",
            "content_truncated": True,
            "unavailable": True,
            "content_type": "file",
        },
        "before": [], "after": [],
        "unavailable_count": 0,
        "omitted_before": 0, "omitted_after": 0,
    }),
}

ATOMIC_TOOL_NAMES = tuple(MIN_RESULT_CAPS)


@dataclass(frozen=True)
class ResultBudget:
    """Per-tool serialization budget shared by executor and tool loop."""

    result_cap: int
    atomic_json: bool = False
    extra_batch_budget: int = 0


def _int_env(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return int(default)
    return value if value > 0 else int(default)


def for_tool(tool_name) -> ResultBudget | None:
    """Policy for one tool name, or None = the caller's generic default."""
    name = str(tool_name or "")
    if name == "history_search":
        return ResultBudget(
            result_cap=_int_env(
                HISTORY_SEARCH_RESULT_MAX_CHARS_ENV,
                DEFAULT_HISTORY_SEARCH_RESULT_MAX_CHARS,
            ),
            atomic_json=True,
        )
    if name == USER_MCP_RESULT_KIND:
        return ResultBudget(
            result_cap=_int_env(
                USER_MCP_RESULT_MAX_CHARS_ENV,
                DEFAULT_USER_MCP_RESULT_MAX_CHARS,
            ),
            atomic_json=False,
        )
    if name == "history_fetch":
        return ResultBudget(
            result_cap=_int_env(
                HISTORY_FETCH_RESULT_MAX_CHARS_ENV,
                DEFAULT_HISTORY_FETCH_RESULT_MAX_CHARS,
            ),
            atomic_json=True,
            extra_batch_budget=DEFAULT_HISTORY_FETCH_EXTRA_BATCH_CHARS,
        )
    return None


def for_metadata(metadata: Mapping[str, Any] | None) -> ResultBudget | None:
    """Policy for one already-produced ToolResult, via its trusted metadata."""
    if not isinstance(metadata, Mapping):
        return None
    return for_tool(metadata.get(RESULT_KIND_METADATA_KEY))


_ENV_BY_TOOL = {
    "history_search": HISTORY_SEARCH_RESULT_MAX_CHARS_ENV,
    "history_fetch": HISTORY_FETCH_RESULT_MAX_CHARS_ENV,
}
_BATCH_CAP_ENV = "FEEDLING_V2_TOOL_BATCH_RESULT_CHAR_CAP"


def validate_result_caps(
    *, batch_cap: int, tool_names=ATOMIC_TOOL_NAMES
) -> None:
    """Fail fast on a result-cap configuration that cannot hold its contract.

    Called once at process start (worker module import) with the deployment's
    real ``tool_batch_result_char_cap``.

    ``tool_names`` narrows the check to the atomic producers that are actually
    live.  A producer behind a kill switch imposes no contract while it is off:
    validating it anyway would make the switch unable to restore the pre-feature
    startup behaviour, which is the one thing a rollback valve must do.  The
    caller that turns such a producer back on is responsible for re-running this
    check (see the worker's ``_history_tools_enabled_for_turn``).

    Two independent ways a plausible number silently breaks the model's input:

    1. **Too small.**  ``result_cap=1`` cannot be met by any structural shrink
       — the smallest legal payload is already hundreds of characters.  The
       facade emits an over-cap object, the string-slicing layer below cuts it
       to ``"{...[truncated]"``, and the model receives invalid JSON.
    2. **Too large.**  An atomic result is reserved *in full* by the batch
       water-fill, and the batch budget only rises by that policy's
       ``extra_batch_budget``.  A ``result_cap`` above
       ``batch_cap + extra_batch_budget`` therefore pushes the whole batch past
       the ceiling spec §3.3 promises (10500 by default).  Lowering the generic
       ``batch_cap`` breaks the same inequality from the other side.

    Both are configuration mistakes, not runtime conditions: refusing to start
    is strictly better than shipping a deployment whose tool results are
    unparseable or whose prompt budget is quietly blown.
    """
    batch_cap = int(batch_cap)
    policies = {name: for_tool(name) for name in tool_names}
    for name, policy in policies.items():
        if policy is None:  # pragma: no cover — ATOMIC_TOOL_NAMES is the source
            continue
        env_name = _ENV_BY_TOOL[name]
        minimum = MIN_RESULT_CAPS[name]
        if policy.result_cap < minimum:
            raise RuntimeError(
                f"{env_name} is below the smallest legal {name} payload "
                f"({policy.result_cap} < {minimum}); the structural shrink "
                "cannot meet it and the result would be string-truncated into "
                "invalid JSON"
            )
        if not policy.atomic_json:  # pragma: no cover — both are atomic today
            continue
        room = batch_cap + int(policy.extra_batch_budget)
        if policy.result_cap > room:
            raise RuntimeError(
                f"{env_name} ({policy.result_cap}) exceeds what a batch can "
                f"reserve for one atomic result ({_BATCH_CAP_ENV}={batch_cap} "
                f"+ extra {policy.extra_batch_budget} = {room}); raise "
                f"{_BATCH_CAP_ENV} or lower {env_name}"
            )
    atomic = [p for p in policies.values() if p is not None and p.atomic_json]
    total_caps = sum(p.result_cap for p in atomic)
    total_room = batch_cap + sum(p.extra_batch_budget for p in atomic)
    if total_caps > total_room:
        raise RuntimeError(
            f"atomic result caps total {total_caps}, more than a batch "
            f"carrying all of them can hold ({_BATCH_CAP_ENV}={batch_cap} "
            f"+ extras = {total_room})"
        )
