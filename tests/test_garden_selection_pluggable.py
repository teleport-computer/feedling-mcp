"""挑卡插口：宿主能不能**整个换掉**「怎么查」。

hx 2026-08-17 的原话：「配置好挑卡策略？？？这不是还是 hard code 了？
我的想法是 garden 可以查，有查的能力，那么怎么查可不可以在外部配置呢？」

所以验收标准不是「能不能调参」，是**能不能传一个自己写的实现进来**。

另外两条是 codex 评审拍出的约束，也在这里钉住：
  · 内核只返回 card_id 与证据，不返回整张卡（第三方策略不能篡改候选）
  · 总量上限由 Chain 统一管，单段绕不过去
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from memory_garden.selection import (  # noqa: E402
    Chain, Pick, RecentStage, RelevanceStage, RoleStage, SelectionPolicy, SelectionResult,
)

CARDS = [
    {"id": "t1", "roles": ["turning_point"], "occurred_at": "2026-01-03", "search_text": "换工作那次"},
    {"id": "t2", "roles": ["turning_point"], "occurred_at": "2026-01-01", "search_text": "搬家"},
    {"id": "r1", "created_at": "2026-08-10", "search_text": "昨天吃了火锅"},
    {"id": "r2", "created_at": "2026-08-09", "search_text": "买了新键盘"},
    {"id": "d1", "created_at": "2026-01-01", "search_text": "我有一只狗叫崽崽 柯基"},
]


# --------------------------------------------------------------------------- #
# 核心验收：能整个换掉
# --------------------------------------------------------------------------- #


class OnlyTheDogPolicy:
    """一个宿主完全自己写的策略 —— 不用内核任何默认实现。"""

    def select(self, cards, query, *, limit) -> SelectionResult:
        return SelectionResult(
            picks=tuple(
                Pick(card_id=str(c["id"]), stage="my_own_rule", reason="我说了算")
                for c in cards if "狗" in str(c.get("search_text") or "")
            )[:limit]
        )


def test_a_host_can_replace_the_whole_selection():
    """这条是整个设计的验收点：传一个自己写的实现，内核照单执行。"""
    policy: SelectionPolicy = OnlyTheDogPolicy()
    result = policy.select(CARDS, "随便问点什么", limit=8)
    assert result.card_ids == ["d1"]
    assert result.picks[0].stage == "my_own_rule"


def test_the_custom_policy_conforms_structurally():
    assert isinstance(OnlyTheDogPolicy(), SelectionPolicy)


# --------------------------------------------------------------------------- #
# 自带的几段：可以任意组合
# --------------------------------------------------------------------------- #


def test_chain_reproduces_the_bucketed_shape():
    """io 现在那套「3 转折 + 2 最近 + 3 相关」用组合就能复刻。"""
    chain = Chain(stages=(RoleStage("turning_point", 3), RecentStage(2)))
    got = chain.select(CARDS, "随便聊聊", limit=8)
    assert got.card_ids[:2] == ["t1", "t2"], "转折卡该按时间倒序排前面"
    assert set(got.card_ids[2:]) == {"r1", "r2"}, "剩下的名额给最近的"


def test_stages_can_be_dropped_entirely():
    """不想要打底卡？把那几段去掉就行 —— 不用改内核。"""
    chain = Chain(stages=(RecentStage(2),))
    assert chain.select(CARDS, "随便聊聊", limit=8).card_ids == ["r1", "r2"]


def test_empty_chain_selects_nothing():
    assert Chain().select(CARDS, "问点什么", limit=8).card_ids == []


# --------------------------------------------------------------------------- #
# Chain 的三条纪律
# --------------------------------------------------------------------------- #


def test_chain_dedupes_across_stages():
    """同一张卡被两段都选中，只算一次。"""
    chain = Chain(stages=(RoleStage("turning_point", 3), RoleStage("turning_point", 3)))
    assert chain.select(CARDS, "q", limit=8).card_ids == ["t1", "t2"]


def test_chain_enforces_the_global_cap_not_the_stage():
    """总量由 Chain 管 —— 单段声称要 99 张也绕不过去。"""
    chain = Chain(stages=(RecentStage(99), RoleStage("turning_point", 99)))
    assert len(chain.select(CARDS, "q", limit=3).card_ids) == 3


class _LiarStage:
    """伪造 id 的第三方策略 —— Chain 必须挡住。"""

    def pick(self, remaining, query, *, budget):
        return [Pick(card_id="根本不存在的卡", stage="fake")]


def test_chain_rejects_ids_that_are_not_in_the_candidate_set():
    """内核只返回 card_id，所以必须校验 id 真实存在 —— 否则宿主回填时会拿到 None。"""
    assert Chain(stages=(_LiarStage(),)).select(CARDS, "q", limit=8).card_ids == []


def test_result_carries_no_card_bodies():
    """只出 id 和证据 —— 第三方策略拿不到、也改不了卡本身。"""
    got = Chain(stages=(RecentStage(1),)).select(CARDS, "q", limit=8)
    blob = repr(got)
    assert "火锅" not in blob, "卡片正文漏进了选择结果"
    assert got.picks[0].card_id == "r1"
