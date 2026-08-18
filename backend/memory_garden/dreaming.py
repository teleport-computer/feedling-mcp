"""做梦的「值不值得」判据 —— 纯计算，不碰库、不看时钟、不调模型。

## 这一层管什么

    内核（本模块）  我这儿攒了多少新东西？乱到该整理了吗？
    宿主（io）      现在是不是合适的时机？夜里吗？上次那个跑完了吗？刚失败过吗？

拆开的理由：**「什么算攒够了」是 Garden 内部结构的知识**（只数哪种卡、签名怎么算），
换个记忆库实现就完全不同 —— 一个向量库可能永远不需要整理。而「夜间窗口 / 防重 /
失败退避」跟哪个记忆库无关，那是宿主的调度。

原实现把两者混在 ``proactive/dream_scheduler.py`` 的一个函数里，于是 Garden 的
知识住在 io 的调度代码里。本模块把前一半搬回来。

## 一条不显然但关键的规则：只数「非做梦产生」的卡

做梦自己写出来的卡**不计入水位线**，否则会自激：做梦 → 产生新卡 → 又满足触发条件
→ 又做梦，每晚空转。而且做梦退休掉的旧种子卡**仍然算数**（种子集合从全部卡里筛，
不是从可用卡里筛），所以这条线只增不减。

## 时间不进这里

「距上次多久」「是不是夜里」都需要看时钟，属于宿主。内核只比较两个数字：
现在的种子卡数 vs 上次做梦时的种子卡数。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: 整理产物的来源标记。内核只需要认出**这一个**语义：
#: 「这张卡是不是上次整理产出的」——用来防止整理产物自己喂自己
#: （整理完立刻又满足「攒够了」，无限循环）。
#:
#: 取值是 io 的历史约定，留作默认值；别的宿主用别的名字时，
#: 把自己的值传给 `seed_cards(..., consolidated_source=...)` 即可 ——
#: 内核不需要知道宿主还有哪些别的来源（那 17 个已于 2026-08-17 搬回 io）。
DREAM_SOURCE = "memory_dream"

#: 参与签名的字段。只用不可变或极少变的字段 —— 签名要能代表「花园的形状」，
#: 不能因为某张卡被读了一次（last_referenced_at 变了）就变。
_SIGNATURE_FIELDS = ("id", "source", "created_at", "occurred_at")


@dataclass(frozen=True)
class DreamSnapshot:
    """当前花园的形状。全部来自明文元数据，不需要解密。"""

    card_count: int
    seed_card_count: int
    signature: str


@dataclass(frozen=True)
class DreamLedger:
    """上次做梦时记下的账。"""

    last_seed_card_count: int = 0
    last_signature: str = ""


@dataclass(frozen=True)
class DreamVerdict:
    """值不值得整理，以及为什么。

    ``reason`` 沿用原实现的字符串，调用方的日志与指标口径不变。
    """

    needed: bool
    reason: str
    new_cards: int


def seed_cards(all_cards: Sequence[Mapping[str, Any]], *,
               consolidated_source: str | None = None) -> list[Mapping[str, Any]]:
    """筛出计入水位线的卡：**排除做梦自己产出的**。

    注意入参是「全部卡」而不是「可用卡」—— 被做梦退休掉的旧种子卡仍要算数，
    否则水位线会因为一次整理而回退，下次又触发。
    """
    marker = str(consolidated_source or DREAM_SOURCE)
    return sorted(
        (c for c in all_cards if str(c.get("source") or "").strip() != marker),
        key=lambda c: str(c.get("id") or ""),
    )


def snapshot_signature(cards: Sequence[Mapping[str, Any]]) -> str:
    """花园形状的指纹。同一批卡（顺序无关）恒等，增删任何一张就变。"""
    material = "\n".join(
        "|".join(str(card.get(key) or "") for key in _SIGNATURE_FIELDS)
        for card in cards
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def dream_snapshot(
    *,
    available_cards: Sequence[Mapping[str, Any]],
    all_cards: Sequence[Mapping[str, Any]],
    consolidated_source: str | None = None,
) -> DreamSnapshot:
    """从两份已过滤的卡列表算出快照。

    ``available_cards`` —— 当前可用的卡（宿主已按可见性/归属过滤）。
    ``all_cards``       —— 含已被取代的全部卡，用来算只增不减的水位线。
    """
    seeds = seed_cards(all_cards, consolidated_source=consolidated_source)
    return DreamSnapshot(
        card_count=len(available_cards),
        seed_card_count=len(seeds),
        signature=snapshot_signature(seeds),
    )


def needs_dream(
    snapshot: DreamSnapshot,
    ledger: DreamLedger,
    *,
    min_new_cards: int,
) -> DreamVerdict:
    """值不值得整理。**只看数量与指纹，不看时间、不看内容。**

    不看内容是硬约束而非偷懒：桶名和正文都在密文里，要判断「有没有重复的桶」
    就得把整个花园解密一遍 —— 每天对每个用户做一次，enclave 扛不住。
    相似桶检测只能放进真正的整理阶段。

    ``min_new_cards`` 由宿主传入（它是可配置的运行参数，不是 Garden 的常量）。
    """
    if snapshot.card_count <= 0:
        return DreamVerdict(False, "no_memory_cards", 0)

    new_cards = max(0, snapshot.seed_card_count - max(0, ledger.last_seed_card_count))

    if ledger.last_signature and ledger.last_signature == snapshot.signature:
        return DreamVerdict(False, "already_dreamed", new_cards)

    if new_cards < max(0, min_new_cards):
        return DreamVerdict(False, "not_enough_new_cards", new_cards)

    return DreamVerdict(True, "due", new_cards)


def dream_idempotency_key(
    ledger: DreamLedger,
    snapshot: DreamSnapshot,
    *,
    extra: Sequence[Any] = (),
) -> str:
    """同一个花园状态重复触发时产出同一个键，避免排重复的活。

    ``extra`` 让宿主把自己那侧的材料也拌进来（io 拌的是对话轮数）。
    它是参数而不是内核字段 —— 轮数要查聊天记录，不属于花园的形状。
    """
    material = "|".join(
        [
            str(ledger.last_signature or ""),
            str(snapshot.signature or ""),
            str(snapshot.seed_card_count),
            *(str(item) for item in extra),
        ]
    )
    return "dream:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
