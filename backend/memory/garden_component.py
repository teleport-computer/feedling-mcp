"""io 侧的 Garden 组件适配层 —— 两条 runtime 共用。

## 它解决什么

在这之前，「落一次卡」的**拼装说明书**散在 io 的 23 个文件里：
拼提示词 → 调模型 → 解析 → 过闸 → 归一化 → 吐脏了怎么重问。
Garden 内部改个函数名，io 就编译不过；换一套记忆系统，这些调用点全部作废。

现在 io 只调 ``GardenComponent`` 的方法，编排在包里。

## 边界没变

    组件负责   什么值得记 · 写成几张 · 归哪个桶 · 挑哪几张 · 该不该整理
    io  负责   调模型（key 在 io）· 加解密 · 写库 · 定时器 · 权限 · 溯源

所以组件返回的是**「该这么改」的指令和原始卡**，不是「已经写好了」。
io 拿到之后自己封信封、自己落库。

## 为什么要这一层，而不是各处直接 new 一个

三件事必须两条 runtime 一致，各写一份就会漂：

1. **泄漏信号**（``IO_LEAK_SIGNALS``）—— 漏传等于 io 自己的 harmony 残片
   一个都拦不住，而闸门看起来还在工作
2. **重问上限** —— V1 和 V2 给不同的次数，同一个模型在托管和自建上
   会得到不同的重问行为
3. **轨迹口径** —— 两条 lane 的观测字段必须能横向对比，
   否则「只有 V2 变差了」这种问题查不出来
"""
from __future__ import annotations

from typing import Any, Callable

from memgarden import GardenComponent
from memgarden import component as _kernel_component
from memgarden.contracts import Step

from memory.card_leak_signals import IO_LEAK_SIGNALS
from memory.capture_prompt_v1 import (
    CONVERSATION_CAPTURE,
    IO_CONVERSATION_CAPTURE_POLICY,
)


# memgarden 0.16.0 的 prompt 以对象 identity 限定唯一已实现的模板；replace
# 出来的同档 policy 因而会在解析前被误认成「另一档」。只在 prompt 边界把
# io 的同档副本还原成钉版对象：rubric/prompt 字节不变，状态机随后仍拿原 request
# 里的 replace policy 做 parse，max_cards=50 才真正生效。升级内核后删掉此垫片。
_kernel_build_capture_prompt = _kernel_component.build_capture_prompt


def _build_capture_prompt_with_io_policy(**kwargs):
    if kwargs.get("policy") is IO_CONVERSATION_CAPTURE_POLICY:
        kwargs["policy"] = CONVERSATION_CAPTURE
    return _kernel_build_capture_prompt(**kwargs)


_kernel_component.build_capture_prompt = _build_capture_prompt_with_io_policy

#: 打回重问最多一次。两条 runtime 共用 —— 各给各的次数，
#: 同一个模型在托管和自建上会得到不同的重问行为。
MAX_CAPTURE_RETRIES = 1


class CallableModel:
    """把 io 现成的「给我一段提示词、还我一段文本」包成组件要的模型端口。

    io 的两条 runtime 各有自己的调用方式（V1 是 ``call_agent``，
    V2 是 provider 客户端），但都能收敛成这一个函数签名。
    **key 始终在 io 手里，组件拿不到。**
    """

    def __init__(self, call: Callable[[str], str]) -> None:
        self._call = call

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        return self._call(prompt)


class AsyncCallableModel:
    """异步版 —— V2 全程 async，同步阻塞会卡住事件循环。"""

    def __init__(self, call) -> None:
        self._call = call

    async def complete(self, prompt: str, *, purpose: str = "") -> str:
        return await self._call(prompt)


def build_garden(
    model: Any,
    *,
    selection_policy=None,
    on_step: Callable[[Step], None] | None = None,
) -> GardenComponent:
    """给 io 用的组件实例。

    ⚠️ ``signals=IO_LEAK_SIGNALS`` 是**必须的**。漏了它，io 自己的
    harmony 标记、工具路由残片、报错回显一个都拦不住 —— 而闸门看起来还在工作，
    脏卡照样落库。这类 bug 只有用户在记忆列表里看到乱码时才会暴露。
    """
    return GardenComponent(
        model=model,
        selection_policy=selection_policy,
        signals=IO_LEAK_SIGNALS,
        max_capture_retries=MAX_CAPTURE_RETRIES,
        on_step=on_step,
    )


# --------------------------------------------------------------------------- #
# 观测：把组件汇报的步骤翻译成 io 原有的口径
# --------------------------------------------------------------------------- #

class BounceTracker:
    """把组件的步骤流翻译成 io 一直在用的 ``bounce`` 三态。

    **口径必须原样保住** —— 这几个值进了日志和指标，改了口径等于把历史数据
    和新数据割开，而看板不会告诉你这件事。

        ""               没重问
        bounced_ok       重问后救回来了
        bounced_empty    重问后模型选择「宁可留空」—— 这是 prompt 想要的结果，
                         不是失败
        bounced_failed   重问后还是脏的
    """

    def __init__(self) -> None:
        self.retried = False
        self.steps: list[Step] = []
        #: 重问之后仍没过语义检查、被组件丢掉的卡数。
        #:
        #: 必须传下去，否则 admin 上「模型想覆盖但没说覆盖哪张」会退化成
        #: 「这轮没什么值得记」—— 一次模型失败被伪装成正常的空结果，查不出来。
        #: 组件把坏卡丢在自己那一层（对的：不能吐出 target_id 为空的
        #: supersede），代价就是宿主原来靠数 cards 得到的这个计数没了，
        #: 只能由组件显式告诉宿主。
        self.dropped_semantic = 0

    def __call__(self, step: Step) -> None:
        self.steps.append(step)
        if step.kind == "retrying":
            self.retried = True
        elif step.kind == "dropped" and step.detail.get("why") == "semantic":
            self.dropped_semantic += int(step.detail.get("cards") or 0)

    def bounce(self, *, cards: list, error: str | None) -> str:
        if not self.retried:
            return ""
        if error:
            return "bounced_failed"
        if not cards:
            # 模型接受了「宁可留空」这条出路。
            return "bounced_empty"
        return "bounced_ok"

    def drain(self) -> list[Step]:
        """取走攒下的步骤并清空 —— 给异步宿主用。

        ``on_step`` 是同步回调，而 io 的 trajectory 是 async。中间隔一层缓冲：
        组件同步地攒，宿主在自己的 await 点把它们发出去。

        不这么做的话，会话模式下宿主会**丢掉 parse_bounced / semantic_bounced
        这些事件** —— 而它们正是「这轮为什么多花了一次调用」的唯一线索。
        换成组件之后可观测性净退步，那这层门面就是亏的。
        """
        out, self.steps = self.steps, []
        return out

    @property
    def reask_trigger(self) -> str:
        """这次重问是被什么触发的（format / semantic / truncation）。"""
        for s in self.steps:
            if s.kind == "retrying":
                kind = str(s.detail.get("kind") or "")
                return kind or "format"
        return ""
