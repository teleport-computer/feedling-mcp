"""挑卡的插口 —— 宿主可以整个换掉「怎么查」。

## 为什么是这个形状

hx 2026-08-17 的要求（原话）：「配置好挑卡策略？？？这不是还是 hard code 了？
我的想法是 garden 可以查，有查的能力，那么怎么查可不可以在外部配置呢？」

所以只给参数旋钮不算数 —— 宿主必须能传一个**自己实现的**策略进来。

## 三条设计约束（codex 2026-08-17 评审拍出，都踩过）

1. **注入点必须在顶层。** 此前 io 有两条挑卡路径（分桶那套走 relevance.py，
   readside 那套走 selector.py），插口装在其中一条上，另一条流量完全不受控 ——
   宿主以为换掉了「怎么查」，实际只换了一半。

2. **内核只返回 card_id 和结构化证据，不返回整张卡。** 返回整张卡的话，
   第三方策略可以篡改或伪造候选；trace 也该由宿主拿原卡渲染
   （内核手上的卡是翻译产物，没有 title 之类的展示字段）。

3. **生命周期与权限过滤在宿主侧、且在翻译之前完成。** 内核拿到的候选集
   应当已经满足宿主的可见性约束 —— 它不认识 io 的 is_archived 之类的字段。

## 分层

    宿主过滤 → 宿主翻译（含 search_text）→ SelectionPolicy → 宿主回填原卡 + 渲染 trace
                                             ↑ 这里是插口
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class Pick:
    """选中一张卡的结果。**只带 id 和证据，不带卡本身。**"""

    card_id: str
    #: 哪一段选中的（relevance / turning_point / recent / correction …）。
    #: 叫 stage 不叫 bucket —— 卡片自己有个 bucket 字段，同名会读混。
    stage: str
    score: float = 0.0
    reason: str = ""
    confidence: str = ""


@dataclass(frozen=True)
class SelectionResult:
    picks: tuple[Pick, ...] = ()
    #: 没选中的证据（内容无关：id + 理由）。宿主决定要不要渲染成 trace。
    diagnostics: tuple[Mapping[str, Any], ...] = ()

    @property
    def card_ids(self) -> list[str]:
        return [p.card_id for p in self.picks]


@runtime_checkable
class SelectionPolicy(Protocol):
    """顶层插口。宿主实现它，就等于换掉了「怎么查」。"""

    def select(
        self,
        cards: Sequence[Mapping[str, Any]],
        query: str,
        *,
        limit: int,
    ) -> SelectionResult: ...


@runtime_checkable
class Stage(Protocol):
    """Chain 里的一段。只负责「在剩下的卡里，按我这段的规矩挑几张」。

    去重、总量上限、id 合法性由 Chain 统一管 —— 每段不必各自实现，
    也不允许各自实现（否则总量会被某一段绕过）。
    """

    def pick(
        self,
        remaining: Sequence[Mapping[str, Any]],
        query: str,
        *,
        budget: int,
    ) -> Sequence[Pick]: ...


@dataclass(frozen=True)
class Chain:
    """按顺序跑几段，去重，填到 limit 为止。

    `io` 用它复刻现有的两套行为；外部使用者可以任意组合，
    或者干脆不用它、自己实现 SelectionPolicy。
    """

    stages: tuple[Stage, ...] = ()

    def select(self, cards, query, *, limit) -> SelectionResult:
        by_id = {str(c.get("id") or ""): c for c in cards if str(c.get("id") or "")}
        chosen: dict[str, Pick] = {}
        diagnostics: list[Mapping[str, Any]] = []

        for stage in self.stages:
            if len(chosen) >= limit:
                break
            remaining = [c for cid, c in by_id.items() if cid not in chosen]
            for pick in stage.pick(remaining, query, budget=limit - len(chosen)):
                cid = str(pick.card_id or "")
                # id 合法性由 Chain 校验 —— 第三方 stage 不能凭空造 id
                if not cid or cid not in by_id or cid in chosen:
                    continue
                chosen[cid] = pick
                if len(chosen) >= limit:
                    break
            note = getattr(stage, "diagnostics", None)
            if callable(note):
                diagnostics.extend(note())

        return SelectionResult(picks=tuple(chosen.values()), diagnostics=tuple(diagnostics))


# --------------------------------------------------------------------------- #
# 自带的几段 —— **默认实现，不是限制**
#
# 宿主可以只用其中几段、换参数、或者一段都不用、自己写。
# 它们放在内核里只是因为「按角色挑」「按时间挑」「按相关性挑」是通用能力；
# 而**哪几段、各几张**是产品策略，由宿主组合（io 的组合见 memory/selection_policies.py）。
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RoleStage:
    """按角色挑：转折点、纠正…… 角色由宿主在翻译时打进 `roles`。

    内核不认识「转折｜」这类前缀 —— 那是宿主的命名约定
    （io 的识别逻辑见 memory/card_shape.py::roles_of）。
    """

    role: str
    limit: int = 3
    #: 按哪个时间字段倒序。宿主的卡片时间语义不一定相同，所以可配。
    order_by: str = "occurred_at"

    def pick(self, remaining, query, *, budget) -> list[Pick]:
        matched = [c for c in remaining if self.role in (c.get("roles") or [])]
        matched.sort(key=lambda c: str(c.get(self.order_by) or ""), reverse=True)
        take = min(self.limit, budget)
        return [Pick(card_id=str(c.get("id")), stage=self.role, reason="role_match")
                for c in matched[:take]]


@dataclass(frozen=True)
class RecentStage:
    """按时间挑最近的几张 —— 不看查询，纯粹是「最近发生了什么」。"""

    limit: int = 2
    order_by: str = "created_at"

    def pick(self, remaining, query, *, budget) -> list[Pick]:
        ordered = sorted(remaining, key=lambda c: str(c.get(self.order_by) or ""), reverse=True)
        take = min(self.limit, budget)
        return [Pick(card_id=str(c.get("id")), stage="recent", reason="recency")
                for c in ordered[:take]]


@dataclass(frozen=True)
class RelevanceStage:
    """按相关性挑 —— 用内核自带的打分。

    ⚠️ 阈值绑在这一段上，不是全局配置：它们是对**这套打分算法**校准的。
    宿主换了打分实现（自己写一个 Stage），这些数字就没有意义了
    （codex 2026-08-17 指出）。
    """

    limit: int = 3
    #: 强匹配的分数门槛
    strong_min: float = 0.0
    #: 中等匹配的分数门槛；低于 strong_min 的匹配要更高的证据要求
    medium_min: float = 0.0
    #: 这些理由不算数（例如泛词碰巧撞上）
    excluded_reasons: tuple[str, ...] = ()
    #: True 时**只看分数**，不要求 confidence 达到 strong/medium。
    #:
    #: io 的 resident 那套原本就是「score > 0 就要」—— 宽松是有意的：
    #: 陪伴场景宁可多带一张弱相关的，也不要该想起来的想不起来。
    #: 2026-08-17 实测踩到：不给这个开关，弱相关卡全被滤掉，
    #: 同一个用户问「我的狗是什么品种」也召不回狗卡。
    any_score: bool = False

    def pick(self, remaining, query, *, budget) -> list[Pick]:
        from .scoring.relevance import memory_relevance_details

        scored = []
        for card in remaining:
            rel = memory_relevance_details(query, card)
            conf = str(rel.get("confidence") or "none")
            score = float(rel.get("score") or 0.0)
            reason = str(rel.get("reason") or "")
            if reason in self.excluded_reasons:
                continue
            if self.any_score:
                if score <= 0:
                    continue
            elif conf == "strong" and score >= self.strong_min:
                pass
            elif conf == "medium" and score >= self.medium_min:
                pass
            else:
                continue
            scored.append((score, str(card.get("occurred_at") or ""), card, rel))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        take = min(self.limit, budget)
        return [
            Pick(card_id=str(c.get("id")), stage="relevance", score=s,
                 reason=str(rel.get("reason") or ""), confidence=str(rel.get("confidence") or ""))
            for s, _, c, rel in scored[:take]
        ]
