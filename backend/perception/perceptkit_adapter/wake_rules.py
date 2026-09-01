"""io 在什么情况下主动叫醒用户 —— 用 kit 的规则语言写一遍。

**这些是产品决定，不是内核的事**，所以住在 io 这边。kit 提供的是「什么算一个
事件」的语法（变了、进入、离开、发生了一次、连续 N 天…），至于「Wi-Fi 换了要
不要叫人」是 io 说了算。规范 §16 的中立性要求就是这个意思：内核里不该出现
io 的唤醒 kind。

## 这五条是照着老路现有行为写的，不是新设计

切换的验收标准是「同一份上报，两条路叫醒的时机和理由一样」。所以这里逐条对着
`differ_v2._events_for` 抄，包括它那些看起来奇怪的地方：

    锚点变了才叫，值为 None 不叫    —— 断网那一刻不该被解释成「到了某处」
    broadcast 只认得开/关两种值      —— 其它值（空、未知）不产生事件
    照片没有防抖                     —— 抖动在上一层按 30 秒成簇处理过了

## 为什么不做成用户可配

kit 的规则本来就支持用户自定义阈值（走 `subject_id`），但那是另一个产品功能。
这一批只做「把现有行为搬过来」——**同时换实现又换行为，出了问题分不清是谁的**。
"""
from __future__ import annotations

from perceptkit.rules import EventDefinition
from perceptkit.rules.types import Lifecycle

#: 老路给「广播」和「位置」配的防抖是 60 秒，照片是 0。
#: 这个数字直接决定用户被打扰的频率，所以从老路的目录里抄过来、不重新拍。
_ANCHOR_DEBOUNCE_SEC = 60.0
_BROADCAST_DEBOUNCE_SEC = 60.0


def _every_time(cooldown: float = 0.0) -> Lifecycle:
    """每次条件成立都算一件事，可选一个冷却。

    ``scope="forever"`` 是因为老路的判据里没有「一天一次」这回事 —— 换了
    Wi-Fi 就是换了，不管今天换过几次。``fire="every"`` 同理。
    冷却为 0 时用 ``rearm="never"``：引擎在 ``fire="every"`` 下不看它，
    但契约不允许 ``rearm="cooldown"`` 配 0 秒（配了不生效比配不出来更糟）。
    """
    if cooldown > 0:
        return Lifecycle(scope="forever", fire="every", rearm="cooldown",
                         cooldown_seconds=cooldown)
    return Lifecycle(scope="forever", fire="every", rearm="never")

#: 和老路 `differ_v2` 的 trigger 一一对应。事件类型这个字符串会一路传到
#: 唤醒决策和日志里，**必须和老路用同一个词** —— 换个说法就意味着所有按
#: trigger 做节流、做统计、做排查的地方都要跟着改。
ARRIVED_AT_ANCHOR = "arrived_at_anchor"
UNLOCK_AFTER_ABSENCE = "unlock_after_absence"
BROADCAST_OPENED = "broadcast_opened"
BROADCAST_CLOSED = "broadcast_closed"
SCENE_CHANGE = "scene_change"
PHOTO_ADDED = "photo_added"


def wake_definitions() -> tuple[EventDefinition, ...]:
    """全局规则（不绑定用户）。版本号变了就是行为变了，会被写进事件里。"""
    return (
        # 到了一个新的 Wi-Fi 锚点。老路对三种 anchor 信号一视同仁，
        # kit 这边它们已经收敛成一个 proximity_anchor。
        EventDefinition(
            definition_id="io.perception.anchor_changed",
            version=1,
            signal="proximity_anchor",
            condition_type="changed",
            field_name="anchor_id",
            event_type=ARRIVED_AT_ANCHOR,
            wake_enabled=True,
            # 每个锚点各自去重：从家到公司再回家，是两件事。
            dedupe_field="anchor_id",
            lifecycle=_every_time(_ANCHOR_DEBOUNCE_SEC),
        ),
        # 锁屏一段时间之后回来。
        EventDefinition(
            definition_id="io.perception.presence_recovered",
            version=1,
            signal="presence_recovery",
            condition_type="occurrence",
            event_type=UNLOCK_AFTER_ABSENCE,
            wake_enabled=True,
            lifecycle=_every_time(),
        ),
        # 开始 / 停止屏幕采集。老路把它拆成两个 trigger，因为下游的
        # 唤醒节流是按 trigger 配的 —— 合成一个会让「开」和「关」共用一个闸。
        EventDefinition(
            definition_id="io.perception.broadcast_opened",
            version=1,
            signal="broadcast",
            condition_type="enters",
            field_name="is_active",
            value=True,
            event_type=BROADCAST_OPENED,
            wake_enabled=True,
            lifecycle=_every_time(_BROADCAST_DEBOUNCE_SEC),
        ),
        EventDefinition(
            definition_id="io.perception.broadcast_closed",
            version=1,
            signal="broadcast",
            condition_type="leaves",
            field_name="is_active",
            value=True,
            event_type=BROADCAST_CLOSED,
            wake_enabled=True,
            lifecycle=_every_time(_BROADCAST_DEBOUNCE_SEC),
        ),
        # 画面变了。
        EventDefinition(
            definition_id="io.perception.scene_changed",
            version=1,
            signal="screen_change",
            condition_type="occurrence",
            event_type=SCENE_CHANGE,
            wake_enabled=True,
            lifecycle=_every_time(),
        ),
        # 新增照片。**不设防抖** —— 连拍成簇的去重在照片入口那层按 30 秒
        # 做过了，这里再来一道会把两次真实的拍照吃掉一次。
        EventDefinition(
            definition_id="io.perception.photo_added",
            version=1,
            signal="photo_library_added",
            condition_type="occurrence",
            event_type=PHOTO_ADDED,
            wake_enabled=True,
            lifecycle=_every_time(),
        ),
    )


__all__ = [
    "ARRIVED_AT_ANCHOR", "UNLOCK_AFTER_ABSENCE", "BROADCAST_OPENED",
    "BROADCAST_CLOSED", "SCENE_CHANGE", "PHOTO_ADDED", "wake_definitions",
]
