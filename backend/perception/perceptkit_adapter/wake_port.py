"""把 kit 产生的事件交给 io 现有的唤醒链路。

kit 判断「发生了一件值得说的事」，**要不要真的开口**仍然由 io 决定 —— 免打扰
时段、频率闸、用户的主动性设置、这个用户激活没有，全在 io 这边，一条都没搬过
去。规范 §24 就是这么划的：kit 规定什么事件被产生、为什么产生、怎么可靠投递；
runtime 决定怎么运行、要不要跟用户说话。

## 幂等由这里兜住

崩溃重投是常态：投出去了、回执还没落库，进程挂掉，重启一定会再投一次。所以
``wake()`` 按 ``event_id`` 去重 —— 认得就回 ``duplicate``，不真的再排一次队。
不然用户会为同一件事被提醒两遍。

## 「io 拒绝了」不是异常

免打扰时段里不叫人，是**正常应答**，用 ``suppressed`` 表达。异常留给真正的意外
（连不上、序列化炸了），调用方会当成投递失败去重试 —— 把「不该叫」当成「没叫
成功」，会让它一直重试到闸放行为止，正好绕过那道闸。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

#: kit 的事件类型 -> io 唤醒链路认的 source。
#: 老路把屏幕相关的走 `scene_change`，其余走 `perception_event`，
#: 而下游的频率闸是**按 source 配的** —— 换个 source 就等于换了一套闸。
_SOURCE_BY_TYPE = {
    "broadcast_opened": "scene_change",
    "broadcast_closed": "scene_change",
    "scene_change": "scene_change",
}
_DEFAULT_SOURCE = "perception_event"


class FeedlingWakePort:
    """io 的 WakePort 实现。

    ``submit`` 是注入进来的（默认是 io 那条既有的 V2 兼容投递），这样测试
    可以在不碰真队列的情况下验投递语义，也让「谁最终决定说不说话」这件事
    留在一个可替换的位置上。
    """

    def __init__(self, submit=None, *, seen=None) -> None:
        self._submit = submit
        #: 本进程内已投过的 event_id。**这不是幂等的全部** —— 真正的幂等靠
        #: 外发箱里的 wake receipt（跨进程、跨重启）。这里只挡住同一个进程
        #: 里的重复投递，省掉一次没必要的排队。
        self._seen: set[str] = seen if seen is not None else set()

    def wake(self, event: Any, attempt: Any):
        from perceptkit.contracts.receipt import WakeReceipt

        now = datetime.now(timezone.utc)
        attempt_id = getattr(attempt, "attempt_id", None) or "1"
        if event.event_id in self._seen:
            return WakeReceipt(event_id=event.event_id, attempt_id=attempt_id,
                               status="duplicate", received_at=now)
        try:
            accepted = self._deliver(event)
        except Exception as exc:                   # noqa: BLE001
            # 真正的意外。让调用方安排重试 —— 但**不要**把它说成拒绝，
            # 那会让一次连接抖动看起来像用户设置的静音。
            log.warning("perceptkit wake delivery failed (%s): %s",
                        event.event_id, exc)
            raise
        self._seen.add(event.event_id)
        return WakeReceipt(
            event_id=event.event_id, attempt_id=attempt_id,
            # 契约里表达「runtime 收到了但决定不说话」的词是
            # `conversation_suppressed`，不是 `suppressed` —— 后者不在允许值
            # 里，写进去会被当成投递失败，然后**一直重试到闸放行为止**，
            # 正好绕过那道闸。
            status="accepted" if accepted else "conversation_suppressed",
            received_at=now,
            reason=None if accepted else "host_gate",
        )

    def _deliver(self, event: Any) -> bool:
        """排进 io 的唤醒队列。返回 False = io 这边的闸把它挡下了。"""
        submit = self._submit
        if submit is None:
            from .. import service
            # from_kit=True 绕过老路那道「谁来投」的闸 —— 那道闸挡的是
            # 老路自己，不是我们。
            submit = lambda ev: service._submit_wake_event_v2_compat(ev, from_kit=True)

        # 复用老路那个唤醒事件的形状，不另造一个 —— 下游（频率闸、
        # 主动性设置、consumer）全按它的字段在读，换个形状就是一次跨层改动。
        from ..differ_v2 import DifferEventV2
        from ..ingress_v2 import wake_event_from_differ_event_v2

        source = _SOURCE_BY_TYPE.get(event.type, _DEFAULT_SOURCE)
        return bool(submit(wake_event_from_differ_event_v2(
            event.subject_id,
            DifferEventV2(
                source=source,
                trigger=event.type,
                # 老路用它做「同一件事别叫两遍」的指纹。kit 的 event_id 已经是
                # 按 (规则, 主体, 事实) 算出来的稳定值，直接当指纹用。
                change_digest=event.event_id,
                payload=dict(event.context or {}),
            ),
            ts=event.occurred_at.timestamp(),
            origin_refs=(f"perceptkit:{event.definition_id}",),
        )))


__all__ = ["FeedlingWakePort"]
