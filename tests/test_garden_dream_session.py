"""Dream 的共用入口（``memory.garden_component.open_dream_session``）。

V1 consumer 和 V2 worker 都经这一处开整理会话。这里钉住三件两条 runtime
都依赖、但各自的集成测试不一定覆盖到的事：

1. 老卡字段名翻成组件认识的名字，模型才看得到正文
2. 披露面（模型实际看过哪些卡、哪些被截断）和组件渲染的一致
3. 截断硬闸只丢碰了截断卡的方案
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pytest  # noqa: E402

from memory import garden_component as gc  # noqa: E402


def _garden():
    return gc.build_garden(gc.CallableModel(lambda _prompt: ""))


def _open(cards, **over):
    params = dict(locale="zh-Hans", ai_name="小柒", user_name="小雨",
                  recent_conversations="- 小雨: 最近在学游泳")
    params.update(over)
    return gc.open_dream_session(_garden(), cards=cards, **params)


def _cards(n, **extra):
    return [{"id": f"m{i}", "summary": f"摘要 {i}", "content": f"正文 {i}。", **extra}
            for i in range(n)]


def test_legacy_field_names_reach_the_prompt_as_summary_body_and_bucket():
    """V1 读到的老卡用 title/body/category —— 不翻译的话组件渲染不出正文。"""
    legacy = [{"id": f"m{i}", "title": f"标题 {i}", "body": f"老正文 {i}。",
               "category": "生活", "thread": "游泳"} for i in range(10)]
    legacy.append({"id": "m10", "description": "只有描述", "plaintext": "明文正文。"})
    legacy.append({"id": "m11", "summary": "", "title": "空摘要回退到标题",
                   "content": "", "text": "text 字段的正文。"})

    session, disclosure = _open(legacy)
    prompt = session.next_prompt()

    assert "- id=m0 | bucket=生活 | threads=游泳" in prompt
    assert "summary: 标题 0" in prompt and "老正文 0。" in prompt
    assert "summary: 只有描述" in prompt and "明文正文。" in prompt
    assert "summary: 空摘要回退到标题" in prompt and "text 字段的正文。" in prompt
    assert disclosure.rendered_ids == tuple(f"m{i}" for i in range(12))
    assert not disclosure.truncated_ids and disclosure.omitted == 0


def test_unrenderable_and_duplicate_cards_are_dropped_before_the_component():
    """披露面按「组件实际渲染了前 N 张」推出来 —— 列表里不许混进组件会跳过的卡。"""
    cards = [{"id": "", "summary": "没有 id"}, {"id": "blank"}, *_cards(10),
             {"id": "m0", "summary": "重复 id", "content": "第二张"}]

    session, disclosure = _open(cards)
    prompt = session.next_prompt()

    assert [card["id"] for card in disclosure.cards] == [f"m{i}" for i in range(10)]
    assert disclosure.rendered_ids == tuple(f"m{i}" for i in range(10))
    assert "没有 id" not in prompt and "id=blank" not in prompt and "第二张" not in prompt


def test_disclosure_matches_the_budget_the_component_applied():
    # 20 张各约 4000 字：6 万字预算只放得下前 14 张。
    cards = _cards(20)
    for card in cards:
        card["content"] = "正文" * 2000
    cards[2]["content"] = "长" * 6000       # 超过单卡 5000 字：截断并标出

    session, disclosure = _open(cards)
    prompt = session.next_prompt()

    assert disclosure.needed and disclosure.partial
    assert disclosure.rendered_ids == tuple(f"m{i}" for i in range(14))
    assert disclosure.omitted == 6
    assert disclosure.truncated_ids == frozenset({"m2"})
    heads = [line for line in prompt.splitlines() if line.startswith("- id=")]
    assert heads[-1] == "- id=m13" and "- id=m14" not in heads
    assert heads[2] == "- id=m2 | TRUNCATED"
    editable = [card["id"] for card in disclosure.editable_cards()]
    assert "m2" not in editable and "m14" not in editable
    assert editable == [f"m{i}" for i in range(14) if i != 2]


def test_small_garden_is_a_skip_with_nothing_disclosed():
    session, disclosure = _open(_cards(3))

    assert session.next_prompt() is None
    assert disclosure.skip_reason == "not_enough_new_cards"
    assert not disclosure.needed and not disclosure.partial
    assert disclosure.rendered_ids == () and disclosure.editable_cards() == []


def test_placeholder_user_name_is_not_written_into_the_prompt_as_a_name():
    session, _ = _open(_cards(10), user_name="用户")
    prompt = session.next_prompt()

    assert "用户's companion" not in prompt
    assert "这个人's companion" in prompt


def test_open_fails_closed_on_a_memgarden_without_body_rendering(monkeypatch):
    """自建 VPS 自更新时依赖可能没装上：旧组件只会给模型标题，不能用。"""

    @dataclasses.dataclass
    class _OldMaintenanceRequest:
        cards: list = dataclasses.field(default_factory=list)
        known_ids: tuple = ()

    assert gc.dream_kernel_renders_card_bodies() is True
    monkeypatch.setattr(gc, "MaintenanceRequest", _OldMaintenanceRequest)
    assert gc.dream_kernel_renders_card_bodies() is False
    with pytest.raises(gc.DreamKernelOutdated) as raised:
        _open(_cards(12))
    assert str(raised.value) == gc.DREAM_KERNEL_OUTDATED == "dream_kernel_outdated"


def test_truncated_guard_drops_only_proposals_touching_a_truncated_card():
    rows = [
        {"op": "thicken", "card_ids": ["m2"], "result": {}},
        {"op": "merge", "card_ids": ["m5", " m2 "], "result": {}},
        {"op": "merge", "card_ids": ["m5", "m6"], "result": {}},
        {"op": "supersede", "card_ids": "m2", "result": {}},   # 非列表：交给 mapper 判
    ]

    kept, rejected = gc.reject_truncated_consolidations(rows, frozenset({"m2"}))

    assert rejected == 2
    assert kept == [rows[2], rows[3]]
    assert gc.reject_truncated_consolidations(rows, ()) == (rows, 0)
