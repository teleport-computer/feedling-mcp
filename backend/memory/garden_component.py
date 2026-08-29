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
from memgarden.contracts import Step

from memory.card_leak_signals import IO_LEAK_SIGNALS

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

    def __call__(self, step: Step) -> None:
        self.steps.append(step)
        if step.kind == "retrying":
            self.retried = True

    def bounce(self, *, cards: list, error: str | None) -> str:
        if not self.retried:
            return ""
        if error:
            return "bounced_failed"
        if not cards:
            # 模型接受了「宁可留空」这条出路。
            return "bounced_empty"
        return "bounced_ok"

    @property
    def reask_trigger(self) -> str:
        """这次重问是被什么触发的（format / semantic / truncation）。"""
        for s in self.steps:
            if s.kind == "retrying":
                kind = str(s.detail.get("kind") or "")
                return kind or "format"
        return ""
