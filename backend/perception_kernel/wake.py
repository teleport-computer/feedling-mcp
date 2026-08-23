"""「这次上报算不算一件事」「值不值得戳一下 agent」的纯判据。

★ wake ≠ 该开口了。这里只回答要不要戳；戳醒之后 agent 继续睡 / 只看一眼 /
  开口说话，是三个平行选项，内核不参与。``should_wake`` 的第二个返回值是
  **给日志和回执用的机器可读原因**，不是给模型看的措辞，更不是「该说什么」。

★ 零 I/O：不查库、不看时钟、不发 metrics。时间由调用方传进来
  （``now`` / ``last_wake_ts`` / ``observed`` / ``previous_seen``），
  这样才可测、才能被两条运行时共用。

★ 判据搬过来，机制一行不动：Postgres 事务、``SELECT ... FOR UPDATE`` 行锁、
  HMAC 指纹比对、metrics 全部留在 io 侧的 perception/signal_state_v2.py 与
  perception/differ_v2.py 里。内核只回答「算不算 / 值不值得」。
"""
from __future__ import annotations

from collections.abc import Sequence

# ---------------------------------------------------------------------------
# 感知叫醒源
# ---------------------------------------------------------------------------
# 🔴 **刻意不叫 "wake kind"。** io 里已经有两套各不相同的 `wake_kind` 词表，
#    这里这套是第三种含义，重名会让接线的人以为它们能互相传：
#
#      proactive/gate.py:89  `_proactive_v2_wake_kind`
#          → {"screen_watch", "screen", "presence"}   ——「这次叫醒走哪条投递通道」
#      model_api_runtime/v2/effect_outbox.py:78  `_COLLISION_WAKE_KINDS`
#          → {"heartbeat", "manual_wake", "screen_watch"} ——「哪几类叫醒要防撞」
#      本模块 `PERCEPTION_WAKE_SOURCES`
#          → 下面五个                              ——「这次戳是被什么感知到的」
#
#    三套集合两两都不相等（只有 "screen_watch" 三边都出现，但含义各不相同）。
#    本模块这套讲的是**感知来源**，不是运行时通道、也不是防撞分类，故用
#    `PERCEPTION_WAKE_SOURCES` 这个名字，和上面两套划清界限。
#    不要为了「统一」去改 gate.py 或 effect_outbox.py——那是两个独立的既有契约。
#
# 名字取自 io 侧真实在用的开关与 trigger，不另造词表：
#
#   source        proactive/controls_v2 开关          differ_v2 发出的 trigger
#   ------------  ---------------------------------  ---------------------------
#   arrival       SWITCH_ARRIVAL_WAKE_ENABLED        arrived_at_anchor
#   unlock        SWITCH_UNLOCK_WAKE_ENABLED         unlock_after_absence
#   photo         SWITCH_PHOTO_WAKE_ENABLED          photo_added
#   screen_watch  SWITCH_SCREEN_WATCH_ENABLED        scene_change
#   broadcast     （沿用 screen_watch 那个开关）       broadcast_opened / _closed
#
# broadcast 没有独立开关——evaluate_wake_control_v2 把 broadcast_opened /
# broadcast_closed 一起挂在 screen_watch_enabled 下；但它在 catalog 里是独立的
# 一个 wake capability（自带 60s debounce），所以去重要分开算。两件事，都保留。
PERCEPTION_WAKE_SOURCES: tuple[str, ...] = (
    "arrival",
    "unlock",
    "photo",
    "screen_watch",
    "broadcast",
)


# ---------------------------------------------------------------------------
# 「值变了算不算一件事」
# ---------------------------------------------------------------------------
# 这份名单是从 perception/differ_v2.py 的 _events_for 里原样搬出来的：值即使真
# 变了，这些信号也不发叫醒事件。语义是**默认算数 + 一张明确的否决名单**，不是
# 「白名单里才算」——真正的叫醒源（photo_added / screen_phash /
# unlock_after_absence / 三个 anchor / broadcast_state）压根不在 catalog 的
# SIGNALS 表里（那张表装的是 iOS 上报字段的 key，两套词表交集为空），用白名单
# 会把每一个真实叫醒源都判成「不算」。
#
# motion 的特例写在 catalog 的注释里：它是可拉取的上下文，但变得太频繁，故意
# 不作为叫醒源。注意 catalog 里 Signal("motion_state") 的 significant 用的是
# 默认值 True——那个字段管的是 /report 那条腿，管不到这里，别拿来当判据。
NOT_WAKE_WORTHY_SIGNALS: frozenset[str] = frozenset({
    "motion_state",
    "battery",
    "now_playing",
    "time",
    "place_label",
})


def is_wake_worthy_signal(signal: str) -> bool:
    """这个信号变了，值不值得发一次叫醒事件（不问值本身变没变）。

    给已经在别处 durable 地判完「变没变」的调用方用——比如 differ_v2，它拿到的
    ``changed`` 是 signal_state_v2 在行锁里比对 HMAC 指纹得出的结论。
    """
    return signal not in NOT_WAKE_WORTHY_SIGNALS


def is_significant_change(signal: str, previous, current) -> bool:
    """值变了、且这个信号的变化本身值得注意，才算一件事。

    调用方同时握着新旧两个值时用这个；只握着「变没变」这个结论时用
    ``is_wake_worthy_signal``。两者是同一条判据的两半，不是两套。
    """
    if previous == current:
        return False
    return is_wake_worthy_signal(signal)


# ---------------------------------------------------------------------------
# 「这条上报是不是迟到 / 撞点了」
# ---------------------------------------------------------------------------
# 纯粹的先后判断。io 侧在 FOR UPDATE 行锁里读出上一条的时间戳之后调它——
# **锁、事务、指纹比对都留在 io**，内核只回答先后关系。
OBSERVATION_STALE = "stale"          # 比上一条还早：迟到的乱序上报
OBSERVATION_SAME_TS = "same_ts"      # 和上一条同一时刻：可能是重复，也可能是撞点冲突
OBSERVATION_NEWER = "newer"          # 比上一条新：正常的下一条


def observation_order(observed, previous_seen) -> str:
    """比较两个时刻，返回 ``stale`` / ``same_ts`` / ``newer`` 之一。

    只用 ``<`` 和 ``==``，对 float 和 tz-aware datetime 都成立；不做任何转换，
    免得把 io 侧原本的比较语义改掉。
    """
    if observed < previous_seen:
        return OBSERVATION_STALE
    if observed == previous_seen:
        return OBSERVATION_SAME_TS
    return OBSERVATION_NEWER


# ---------------------------------------------------------------------------
# 「值不值得戳一下 agent」
# ---------------------------------------------------------------------------
# ⚠️ 接线时待定（hx 拍板）：下面这几个原因串是**内核自己的词**，和 io 现在写进
#    感知事件流、admin data_track 看得到的那几个**对不上**：
#
#      内核                     io（proactive/controls_v2.evaluate_wake_control_v2）
#      ---------------------    ----------------------------------------------
#      source_disabled          photo_wake_disabled / arrival_wake_disabled /
#                               unlock_wake_disabled / screen_watch_disabled
#      debounced                capability_debounce（写在事件的 reason 字段）
#
#    io 那几个是**按 source 分开命名**的，内核这个是**合并成一个**的。真接线那一批
#    要决定「统一成一套」还是「留个映射表」——这是用户可见的行为变更（reason 字段会
#    出现在事件流里），**归 hx 拍板，本批不自作主张改**。
def should_wake(
    source: str,
    *,
    enabled_sources: Sequence[str],
    last_wake_ts: float | None,
    now: float,
    debounce_sec: float,
) -> tuple[bool, str]:
    """返回 ``(要不要戳, 原因)``。

    原因是给日志和回执用的机器可读短语，**不是给模型看的**：这里不产出、不暗示
    任何跟「该说什么」有关的东西。戳醒之后 agent 接着睡、只查一个工具、还是开口
    说话，是三个平行且同等合法的结局，内核不参与那个决定。
    """
    if source not in PERCEPTION_WAKE_SOURCES:
        return False, "unknown_source"
    if source not in tuple(enabled_sources or ()):
        return False, "source_disabled"
    if last_wake_ts is not None and (now - last_wake_ts) < debounce_sec:
        return False, "debounced"
    return True, source
