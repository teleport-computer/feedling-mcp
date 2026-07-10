"""V2 agent loop：`decide → act → observe → decide` 的**纯**状态机。

设计见 docs/superpowers/specs/2026-07-10-hosted-runtime-v2-agent-loop-design.md。

为什么在这里而不在 executor 里：executor 是无状态批量调度器，不认识模型、不持有 BYOK
key、不拼 wire。把循环塞进去等于把 V2 花力气拆开的「决定」与「执行」重新焊死。为什么不在
responder 里：responder 必须保持纯（无副作用），而工具里有写操作（memory_write /
identity_patch）——让"写回复的模块"顺手改用户的记忆是错的。

本模块只依赖 stdlib。两个注入回调：
  decide(round_idx, prior_results) -> Decision
  run_tools(actions)               -> {"action_results": {...}, "action_digest": {...}}

停止条件（四种，全部交给调用方决定要不要强制回复——本模块**绝不**产出占位文本）：
  wants_reply  planner 发了 final_response 哨兵：收手去回复
  no_actions   planner 什么也不要：同样收手
  no_progress  本轮 plan 与上轮逐字相同，或本轮工具**全部**失败：别再烧用户的 key
  max_rounds   撞轮数上限
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

DEFAULT_MAX_ROUNDS = 3

WANTS_REPLY = "wants_reply"
NO_ACTIONS = "no_actions"
NO_PROGRESS = "no_progress"
MAX_ROUNDS = "max_rounds"


@dataclass
class Decision:
    """一轮 decide 的产出。

    `final_text` 是给**原生 tool-calling 后端**留的缝：那种后端里，停止发工具的那个模型
    顺手就把回复写了；不接住它就得丢掉这次生成再让 responder 重写一遍，白烧一次 token。
    默认的 json_planner 后端恒为 None（散文由 responder 写，见 spec §4）。
    """

    actions: list[dict[str, Any]] = field(default_factory=list)
    wants_reply: bool = False
    final_text: str | None = None


@dataclass
class LoopResult:
    action_results: dict[str, list[dict[str, Any]]]
    action_digest: dict[str, Any]
    final_text: str | None
    rounds: int
    stop_reason: str


def _signature(actions: list[dict[str, Any]]) -> frozenset:
    """plan 的顺序无关指纹。payload 用 sort_keys 序列化，键序不同不算"变了"。"""
    return frozenset(
        (str(a.get("type") or ""), json.dumps(a.get("payload") or {}, sort_keys=True, ensure_ascii=False))
        for a in actions
    )


def _merge_results(acc: dict, new: dict) -> None:
    for action_type, runs in (new or {}).items():
        acc.setdefault(action_type, []).extend(runs or [])


def _any_ok(results: dict) -> bool:
    return any(
        isinstance(r, dict) and r.get("ok")
        for runs in (results or {}).values()
        for r in (runs or [])
    )


async def run_turn(
    *,
    decide: Callable[[int, dict], Awaitable[Decision]],
    run_tools: Callable[[list[dict]], Awaitable[dict]],
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> LoopResult:
    """驱动至多 `max_rounds` 轮 decide→act。返回累积结果 + 停止原因。

    **绝不**因为撞上限或无进展而产出文本——调用方负责用手上的结果强制一次真正的 responder
    调用（no-filler 铁律）。
    """
    acc_results: dict[str, list[dict[str, Any]]] = {}
    acc_digest: dict[str, Any] = {}
    prev_sig: frozenset | None = None

    for round_idx in range(max_rounds):
        decision = await decide(round_idx, acc_results)

        # 无进展检测在**跑工具之前**：同一个 plan 再跑一遍不会有新观测，只会白烧一轮。
        # wants_reply 的那一轮豁免——它带的 action 是收手前最后一批，不是空转。
        if decision.actions and not decision.wants_reply:
            sig = _signature(decision.actions)
            if sig == prev_sig:
                return LoopResult(acc_results, acc_digest, None, round_idx + 1, NO_PROGRESS)
            prev_sig = sig

        round_results: dict = {}
        if decision.actions:
            executed = await run_tools(decision.actions)
            round_results = (executed or {}).get("action_results") or {}
            _merge_results(acc_results, round_results)
            acc_digest.update((executed or {}).get("action_digest") or {})

        if decision.wants_reply:
            return LoopResult(acc_results, acc_digest, decision.final_text, round_idx + 1, WANTS_REPLY)
        if not decision.actions:
            return LoopResult(acc_results, acc_digest, decision.final_text, round_idx + 1, NO_ACTIONS)
        if not _any_ok(round_results):
            # 本轮工具全挂：再规划一轮也是拿着同样的空手，停。失败结果照样留给 responder，
            # 让它知道"查过了但没查到"，而不是凭空回答。
            return LoopResult(acc_results, acc_digest, None, round_idx + 1, NO_PROGRESS)

    return LoopResult(acc_results, acc_digest, None, max_rounds, MAX_ROUNDS)
