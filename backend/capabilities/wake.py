"""Self-wake capabilities — facade over proactive/scheduled_wake_v2.ScheduledWakeServiceV2.

分层：本模块只 import `proactive.*`。**绝不** import 上层的 v2 运行时 —— v2 是
capabilities 的上层，反向 import 会成环（AST 守卫 + 源码 grep 测试都盯着这条方向，连字面
引用也不许出现）。

于是 `submit_wake` 在这里**不入队**：timer 已经持久化，下一次 scheduler tick（≤30s）会经
`fire_due_timers` 正常捞起并产出 `scheduled` lane 的 agent_job。让 scheduler 做唯一的入队者，
也顺带保证「只有一个地方产生 scheduled job」。代价是最坏 30s 延迟——对「稍后叫我」无所谓。
"""
from __future__ import annotations

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err

_OWNER_ID = "hosted_runtime_v2"


class _Accepted:
    """submit_wake 的返回契约：只需要一个 `.accepted` 为真的对象。"""
    accepted = True
    reason = "persisted_for_scheduler"


def _apply(user_id: str, action: dict, *, self_wake: bool = False) -> CapabilityResult:
    from proactive import scheduled_wake_v2
    from proactive.store_v2 import DBProactiveSettingsStoreV2

    settings = DBProactiveSettingsStoreV2().load(user_id)
    service = scheduled_wake_v2.ScheduledWakeServiceV2(
        scheduled_wake_v2.DBScheduledWakeStoreV2(), owner_id=_OWNER_ID)
    try:
        results = service.apply_turn_actions(
            user_id,
            [action],
            settings=settings,
            self_wake=self_wake,
            submit_wake=lambda _event: _Accepted(),
        )
    except Exception as e:  # noqa: BLE001
        return err(errors.UPSTREAM, f"scheduled wake failed: {type(e).__name__}", retryable=True)
    return ok(data={"results": [r.as_dict() for r in results] if results else []})


def schedule(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    at = str(params.get("at") or "").strip()
    if not at:
        return err(errors.INVALID, "schedule_wake needs an `at` time", retryable=False)
    action = {"type": "schedule_wake", "at": at}
    for key in ("tz", "reason"):
        value = str(params.get(key) or "").strip()
        if value:
            action[key] = value
    if action.get("reason"):
        action["note"] = action["reason"]
    return _apply(
        store.user_id,
        action,
        self_wake=params.get("_self_wake") is True,
    )


def cancel(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    wake_id = str(params.get("wake_id") or params.get("id") or "").strip()
    if not wake_id:
        return err(errors.INVALID, "cancel_wake needs a `wake_id`", retryable=False)
    action = {"type": "cancel_wake", "wake_id": wake_id}
    reason = str(params.get("reason") or "").strip()
    if reason:
        action["reason"] = reason
    return _apply(store.user_id, action)
