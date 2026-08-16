"""Route B（聊天上下文注入）的敏感卡闸门。

2026-08-16 查实的洞：`context_moment_to_index_item` 把每张卡硬写成
`is_sensitive=False`，而 `scoring/selector.py` 的敏感闸正是看这个字段 ——
于是「标了敏感的记忆不进普通对话」这条保护在 Route B 上**永远不生效**。

`moments_to_cards` 的解密投影里也没有任何 sensitivity 字段，所以标记
从解密那一步就丢了，不是在闸门那里丢的。两处都要补。

这批单独先上：下一批会把大量此前不可见的卡送进这条路，闸门必须先真的管用。
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from enclave import readside  # noqa: E402


def _card(**overrides) -> dict:
    card = {
        "id": "m_secret",
        # ⚠️ 匹配词要落在 description 上：Route B 的摘要取的是
        # `description or title or context`，description 非空时 title 根本不参与匹配。
        # 这是本批**不修**的既有短板（下一批的 card-shape 修复才动它），
        # 这里只是避开它，别让对照组因为无关原因失败。
        "title": "体检",
        "description": "体检结果显示需要复查甲状腺",
        "occurred_at": "2026-08-01",
        "created_at": "2026-08-01T10:00:00Z",
        "is_sensitive": True,
    }
    card.update(overrides)
    return card


# --------------------------------------------------------------------------- #
# 投影必须带上标记
# --------------------------------------------------------------------------- #


def test_index_item_carries_the_real_sensitive_flag():
    item = readside.context_moment_to_index_item(_card())
    assert item["is_sensitive"] is True, "敏感标记在投影这一步就丢了 —— 闸门拿不到它"


def test_index_item_defaults_to_not_sensitive_when_absent():
    """缺字段时按不敏感处理 —— 与既有卡的表现一致，不制造新的静默拦截。"""
    card = _card()
    card.pop("is_sensitive")
    assert readside.context_moment_to_index_item(card)["is_sensitive"] is False


def test_index_item_coerces_truthy_values():
    for raw in (1, "yes", ["scope"]):
        assert readside.context_moment_to_index_item(_card(is_sensitive=raw))["is_sensitive"] is True


# --------------------------------------------------------------------------- #
# 闸门真的拦得住
# --------------------------------------------------------------------------- #


def test_sensitive_card_is_not_selected_for_an_ordinary_question():
    """普通提问 —— 即使字面高度相关，敏感卡也不该被选中。"""
    cards = [_card()]
    selected, trace = readside.select_context_memories_via_readside(
        cards, "我的体检结果怎么样", cap=8
    )
    assert [c["id"] for c in selected] == [], "敏感卡被选进了普通对话的上下文"

    skipped = trace.get("rejected_sample") or trace.get("selector_trace", {}).get("skipped_sample") or []
    reasons = {str(item.get("reason") or "") for item in skipped}
    assert "sensitive_not_allowed_for_query" in reasons, (
        f"没被敏感闸拦下，而是走了别的跳过理由：{reasons or '(无记录)'}"
    )


def test_non_sensitive_card_with_the_same_text_is_still_selected():
    """对照组：同样的文字、只是没标敏感 —— 必须照常选中。

    没有这一条，上面那条测试用「什么都选不中」也能通过。
    """
    cards = [_card(is_sensitive=False)]
    selected, _ = readside.select_context_memories_via_readside(
        cards, "我的体检结果怎么样", cap=8
    )
    assert [c["id"] for c in selected] == ["m_secret"], "闸门收得过紧，把普通卡也拦了"
