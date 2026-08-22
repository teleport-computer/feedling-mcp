"""「模型吐出来的垃圾长什么样」—— 由宿主提供，内核只负责怎么权衡。

## 为什么要拆出来

内核里有一条通用规则：**一张记忆卡的字段值不该是模型的原始输出残片**。这条规则
换个宿主完全成立 —— 谁都不想让 `analysis to=functions.memory_write` 变成一个桶名。

但**残片具体长什么样，是每个宿主自己的协议决定的**：

    io 的样子     harmony 特殊 token、`to=functions.<name>` 工具路由、
                  `output error code: 400` 报错回显、
                  自家协议的键名（messages / actions / memory.* 动作）
    别人的样子    完全不同 —— 他们的 agent 协议、provider、工具调用格式都不一样

把 io 那几条正则写死在内核里，等于给外部使用者一个照着 io 线格式调的检测器：
拦不住他们该拦的，还可能误伤他们正常的文本。这跟写死中文桶名、写死 17 个来源
枚举是同一类问题 —— **通用规则里夹带了宿主专有的具体值**。

## 分界线

留在内核的是**判据强弱怎么权衡**，这部分是纯策略、与协议无关：

    强证据            → 直接判脏
    硬字段(summary/content) → 需要 ≥2 个弱证据（误杀=整卡丢弃，代价高，从严）
    软字段(bucket/threads)  → 任一弱证据即判脏（误杀=就地清洗，代价低，从宽）

宿主提供的是**识别器本身**。

## 默认值：只给真正通用的那几条

``GENERIC_SIGNALS`` 只含跟任何 agent 协议都无关的判据（结构性 JSON 残尾）。
它**不是** io 那套的替代品 —— io 必须传自己的 ``LeakSignals``，否则它自己的
harmony / 工具路由残片一个都拦不住。

默认给通用集而不是空集，是因为空集会让守卫**静默失效**：调用方以为有防护，
实际什么都不拦。给一个弱但真实的默认，比给一个假的安全感好。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

#: 一个识别器：命中就返回理由标签，没命中返回 None。
#: 理由标签会落进日志和拒绝记录，所以要是闭集里的短标识，不是自由文本。
Detector = Callable[[str], "str | None"]


@dataclass(frozen=True)
class LeakSignals:
    """一个宿主的「垃圾识别器」集合。不可变 —— 换宿主＝换实例。"""

    #: 强证据：单独命中即判脏。正常内容几乎不可能出现的指纹。
    strong: tuple[Detector, ...] = ()
    #: 弱证据：单独命中不足以判脏（用户可能在正当讨论一段 JSON）。
    weak: tuple[Detector, ...] = ()
    #: 已知的机器味桶名，精确匹配。形状规则区分不了合法的自定义桶，
    #: 所以只能精确列举观测到的残片 —— 那当然是宿主自己观测到的。
    bucket_denylist: frozenset[str] = field(default_factory=frozenset)

    def strong_reason(self, text: str) -> str | None:
        for detect in self.strong:
            reason = detect(text)
            if reason:
                return reason
        return None

    def weak_reasons(self, text: str) -> list[str]:
        out: list[str] = []
        for detect in self.weak:
            reason = detect(text)
            if reason:
                out.append(reason)
        return out


def _orphan_json_tail(text: str) -> str | None:
    """结构性 JSON 残尾：闭括号比开括号多，且带 JSON 记号。

    纯括号配平，不认任何协议键名 —— 所以对任何宿主都成立。
    高召回（用户在正文里贴 JSON 也会命中），因此只作弱证据。
    """
    from agent_protocol_core import protocol_leak

    return "torn_json_tail" if protocol_leak.is_orphan_json_tail(text) else None


#: 与协议无关的最小集合。宿主应当在此之上叠加自己的识别器。
GENERIC_SIGNALS = LeakSignals(weak=(_orphan_json_tail,))


def combine(*signal_sets: LeakSignals) -> LeakSignals:
    """把几组识别器合成一组（宿主通常是 GENERIC_SIGNALS + 自己那套）。"""
    strong: list[Detector] = []
    weak: list[Detector] = []
    denylist: set[str] = set()
    for s in signal_sets:
        strong.extend(s.strong)
        weak.extend(s.weak)
        denylist |= set(s.bucket_denylist)
    return LeakSignals(
        strong=tuple(strong), weak=tuple(weak), bucket_denylist=frozenset(denylist)
    )
