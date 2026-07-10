"""V2 replan / invalidation 安全点状态机（spec §8）。

安全点：读批完成后 / 写操作前 / final_response 前。运行中新可见用户消息到达 → 现有 plan 的 pending
actions 置 invalidated，在下一个安全点带 A+B+C 上下文重规划。single-flight 唯一索引保证同 user 同 lane
至多一个活跃 job，故这是**同一 job 内**的重规划（replan_job_id = 该 job 自身）。

final_response 流式中被打断 → 默认写完（已产出有用回答则保留，§8 默认 finish）。
"""
from __future__ import annotations

from typing import Any

from model_api_runtime.v2 import jobs_store

# 三个允许重规划的安全点（§8）。在别处重规划会撕裂半应用的写。
SAFE_POINTS = ("after_reads", "before_write", "before_final_response")

CONTINUE = "continue"
REPLAN = "replan"
FINISH = "finish"

# replan_budget（§8 anti-thrash）：一个 job 内最多允许 replan 的次数。超预算后即使又来新消息，
# 也不再重规划——直接 CONTINUE 现有 plan 走到底（下一批消息由该 job 结束后的下一次 claim/coalesce
# 接住，绝不会因消息刷屏而无限重规划死循环）。
DEFAULT_REPLAN_BUDGET = 2


def new_visible_message_since(messages: list[dict[str, Any]], *, cursor_ts: float) -> bool:
    """Return whether a newer user row exists using metadata only.

    Production ``store.chat_messages`` rows contain encrypted ``body_ct`` and
    deliberately do not expose plaintext ``content``. Reusing
    ``coalesce_pending`` here therefore made the safe point work only for the
    plaintext fixtures used by tests: every real encrypted row was discarded as
    empty. Invalidation needs only role and timestamp, both non-sensitive
    metadata, so it must not depend on decryption or message text.
    """
    for message in messages:
        if str(message.get("role") or "") not in {"user", "human"}:
            continue
        try:
            message_ts = float(message.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if message_ts > cursor_ts:
            return True
    return False


def evaluate(
    messages: list[dict[str, Any]],
    *,
    safe_point: str,
    coalesced_cursor_ts: float,
    final_response_committed: bool = False,
    replan_count: int = 0,
    replan_budget: int = DEFAULT_REPLAN_BUDGET,
) -> str:
    """安全点上的状态机裁决（§8）。

    - final_response 已提交/流式中 → FINISH（默认不打断有用回答）。
    - 自 plan 构建以来有新可见用户消息：
        - 若本 job 已 replan 次数（``replan_count``）尚未达到 ``replan_budget`` → REPLAN（在此
          安全点带 A+B+C 重规划，调用方随后应把 replan_count + 1 传入下一次 evaluate）。
        - 若已达预算（anti-thrash）→ CONTINUE：不再重规划，执行现有 plan 走到底；未折入的新消息
          留给该 job 结束后的下一次 claim/coalesce 接住，避免消息刷屏下无限重规划。
    - 否则（无新消息）→ CONTINUE。
    """
    if safe_point not in SAFE_POINTS:
        raise ValueError(f"unknown safe point: {safe_point}")
    if safe_point == "before_final_response" and final_response_committed:
        return FINISH
    if new_visible_message_since(messages, cursor_ts=coalesced_cursor_ts):
        if replan_count < replan_budget:
            return REPLAN
        return CONTINUE
    return CONTINUE


def invalidate(job_id: int, *, replan_job_id: int) -> int:
    """把当前 plan 的 pending actions 置为 invalidated，worker 跳过并重规划（§8）。返回置无效条数。
    within-job replan 时 replan_job_id = job_id 自身（记录哪个 job 取代了它们）。"""
    return jobs_store.invalidate_pending_actions(job_id, by_job_id=replan_job_id)
