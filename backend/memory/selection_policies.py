"""io 自己的挑卡策略 —— **产品设计，不属于 Garden 内核**。

内核提供的是「按角色挑 / 按时间挑 / 按相关性挑」这些能力，
**哪几段、各几张、什么阈值**是 io 的陪伴产品在 2026-05 定下的选择，
换个产品就不成立 —— 所以组合放在这里。

外部使用者接 Garden 时不会继承这套：他们自己组合，或者传一个完全
自己实现的 SelectionPolicy（见 memory_garden/selection.py 的模块说明）。

## io 现在的两套

    resident（分桶）    3 张转折 + 2 张最近 + 3 张相关，cap 8
    model_api（严格）   ≤2 张纠正 + 相关卡（strong≥0.55 / medium≥0.35）

两套并存不是设计意图，是 2026-06 换管道时顺手丢掉了打底逻辑
（见 docs/superpowers/specs/2026-08-17-auto-injection-unification.md）。
统一成哪一套还等 hx 拍板 —— 但**统一之后只需要改这个文件**，
不用再动内核，这正是把它搬出来的价值。
"""
from __future__ import annotations

from memory_garden.selection import Chain, RecentStage, RelevanceStage, RoleStage

#: 转折卡与纠正卡的角色名。识别逻辑在 card_shape.roles_of()，内核只认这两个标签。
ROLE_TURNING_POINT = "turning_point"
ROLE_CORRECTION = "correction"

#: resident 那套：先给人设打底，再给相关的。
#: ⚠️ 相关卡只有 3 个名额 —— 这是「卡多了就想不起来」的根源之一，
#: 统一方案里准备把它放宽（等 hx 拍板）。
RESIDENT_POLICY = Chain(stages=(
    RoleStage(ROLE_TURNING_POINT, limit=3, order_by="occurred_at"),
    RecentStage(limit=2, order_by="created_at"),
    # any_score=True 复刻 resident 原有判据：「score > 0 就要」，不卡 confidence。
    # 陪伴场景宁可多带一张弱相关的，也不要该想起来的想不起来。
    RelevanceStage(limit=3, any_score=True),
))

#: model_api 那套：纠正优先（用户明确纠正过的事不能再记错），其余全给相关性。
#: 阈值绑在 RelevanceStage 上 —— 它们是对内核那套打分校准的，换打分实现就失效。
MODEL_API_POLICY = Chain(stages=(
    RoleStage(ROLE_CORRECTION, limit=2, order_by="created_at"),
    RelevanceStage(
        limit=8,
        strong_min=0.55,
        medium_min=0.35,
        excluded_reasons=("weak_generic_overlap",),
    ),
))


def policy_for(context_mode: str):
    """按调用方模式选策略。

    ⚠️ 这个分叉本身是历史包袱：`context_mode` 由调用方传，全仓只有
    hosted/turn.py 传 "model_api"，resident consumer 不传。统一之后
    这个函数应该只返回一个策略。
    """
    mode = str(context_mode or "").strip().lower()
    return MODEL_API_POLICY if mode in {"model_api", "strict"} else RESIDENT_POLICY
