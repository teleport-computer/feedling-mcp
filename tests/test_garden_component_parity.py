"""换成 GardenComponent 之后，产出必须和原来**逐字节相同**。

## 为什么验收标准是这个

搬的是判断逻辑 —— 什么值得记、写成几张、归哪个桶。一点漂移就是
**用户看得见的记忆变化**：同一段对话，昨天记住了三件事，今天记住两件，
而没有任何报错、也没有任何地方能查到为什么。

所以不是「新路径能跑通」，是「新旧两条路对同一批输入给出同一个结果」。

## 覆盖的形状

正常、模型吐脏后重问、模型选择留空、彻底失败 —— 四种都要对上，
因为它们在 io 那边对应四种不同的 job 状态。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from memgarden.prompts.capture import (  # noqa: E402
    build_capture_prompt,
    build_capture_retry_prompt,
    parse_capture_cards,
)
from memgarden.text.card_text import is_retryable_parse_error
from memory.card_leak_signals import IO_LEAK_SIGNALS
from memory.garden_component import BounceTracker, CallableModel, build_garden
from memgarden import CaptureRequest

WINDOW = "老王：我不吃辣，一吃就胃疼\n我：那以后点菜避开"
LOCALE = "zh-Hans"


def _clean_card(**over) -> dict:
    return {"action": "add", "bucket": "偏好与边界", "threads": ["饮食"],
            "summary": "他不吃辣", "content": "一吃辣就胃疼，点菜会主动避开辣的。",
            "importance": 0.6, "pulse": 0.2, **over}


def _reply(*cards: dict) -> str:
    return json.dumps({"cards": list(cards)}, ensure_ascii=False)


class ScriptedModel:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.calls += 1
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


def _legacy(replies: list[str]) -> tuple[list[dict], str | None, str]:
    """原来那条路：io 自己拼装的那几步，一步不改地复刻。"""
    model = ScriptedModel(*replies)
    prompt = build_capture_prompt(
        ai_name="io", user_name="老王", buckets="", threads="",
        identity="", window=WINDOW, locale=LOCALE,
    )
    cards, err = parse_capture_cards(prompt and model(prompt), strict=True,
                                     signals=IO_LEAK_SIGNALS)
    if not is_retryable_parse_error(err):
        return cards, err, ""
    retried, rerr = parse_capture_cards(
        model(build_capture_retry_prompt(prompt, err)), strict=False,
        signals=IO_LEAK_SIGNALS,
    )
    if rerr:
        return retried, rerr, "bounced_failed"
    if not retried:
        return retried, rerr, "bounced_empty"
    return retried, rerr, "bounced_ok"


def _component(replies: list[str]) -> tuple[list[dict], str | None, str]:
    """新路径：只调组件。"""
    tracker = BounceTracker()
    garden = build_garden(CallableModel(ScriptedModel(*replies)), on_step=tracker)
    out = garden.capture(CaptureRequest(
        window=WINDOW, locale=LOCALE, ai_name="io", user_name="老王",
    ))
    return out.cards, out.error, tracker.bounce(cards=out.cards, error=out.error)


CASES = {
    "一次就干净": [_reply(_clean_card())],
    "吐脏后重问救回": [
        _reply({"action": "add", "summary": "[摘要]", "content": "...", "bucket": "工作"}),
        _reply(_clean_card()),
    ],
    "重问后模型选择留空": [
        _reply({"action": "add", "summary": "[摘要]", "content": "...", "bucket": "工作"}),
        _reply(),
    ],
    "重问后仍然是脏的": [
        _reply({"action": "add", "summary": "[摘要]", "content": "...", "bucket": "工作"}),
        _reply({"action": "add", "summary": "...", "content": "[正文]", "bucket": "工作"}),
    ],
    "本来就没什么可记": [_reply()],
}


@pytest.mark.parametrize("name", list(CASES))
def test_the_component_path_matches_the_legacy_path(name: str) -> None:
    replies = CASES[name]
    old_cards, old_err, old_bounce = _legacy(replies)
    new_cards, new_err, new_bounce = _component(replies)

    assert new_cards == old_cards, f"「{name}」产出的卡不一样"
    assert new_err == old_err, f"「{name}」错误码不一样"
    assert new_bounce == old_bounce, (
        f"「{name}」bounce 口径不一样：{old_bounce!r} → {new_bounce!r}。"
        f"这几个值进了日志和指标，改口径等于把历史数据和新数据割开。"
    )


def test_the_component_path_asks_the_model_the_same_thing() -> None:
    """提示词也要逐字节相同 —— 提示词变了，模型的产出就会变，
    而那是**用户看得见**的变化。"""
    legacy_model = ScriptedModel(_reply(_clean_card()))
    legacy_model(build_capture_prompt(
        ai_name="io", user_name="老王", buckets="", threads="",
        identity="", window=WINDOW, locale=LOCALE,
    ))
    component_model = ScriptedModel(_reply(_clean_card()))
    build_garden(CallableModel(component_model)).capture(CaptureRequest(
        window=WINDOW, locale=LOCALE, ai_name="io", user_name="老王"))
    assert component_model.prompts == legacy_model.prompts


def test_the_leak_signals_are_actually_wired() -> None:
    """io 自己的协议残片必须被拦住。

    漏传 signals 的话闸门看起来还在工作，但 io 的 harmony 标记一个都拦不住 ——
    这类 bug 只有用户在记忆列表里看到乱码时才会暴露。
    """
    dirty = _reply({"action": "add", "bucket": "工作",
                    "summary": "他答应周末去看医生",
                    "content": "<|channel|>analysis<|message|>用户提到了健康问题"})
    _cards, err, _ = _component([dirty, dirty])
    assert err, "带 harmony 标记的卡应该被拦下来"


# --------------------------------------------------------------- 迁移

from memgarden.prompts.migrate import (  # noqa: E402
    build_migrate_prompt,
    parse_migrated_cards,
)
from memgarden import MigrateRequest  # noqa: E402

OLD_CARDS = "- [m_1] 面试挂了\n- [m_2] 换了工作"
VOCAB = "已有桶: 工作、健康\n已有线索: 面试、压力"
UPGRADE = {"id": "m_1", "bucket": "工作", "threads": ["面试"],
           "summary": "面试第三次挂在算法题",
           "content": "今年第三次没过，都卡在算法题上，他开始怀疑自己适不适合这行。"}


def _migrate_legacy(reply: str) -> tuple:
    """原路径：io 自己拼的那几步。"""
    prompt = build_migrate_prompt(
        ai_name="io", user_name="老王", old_cards=OLD_CARDS,
        vocab=VOCAB, locale=LOCALE)
    assert prompt
    return parse_migrated_cards(reply, allowed_ids={"m_1", "m_2"},
                                signals=IO_LEAK_SIGNALS)


def _migrate_component(reply: str) -> tuple:
    out = build_garden(CallableModel(lambda p: reply)).migrate(MigrateRequest(
        old_cards=OLD_CARDS, allowed_ids=("m_1", "m_2"), vocab=VOCAB,
        ai_name="io", user_name="老王", locale=LOCALE))
    return out.upgrades, out.unmigrated_ids, out.error


@pytest.mark.parametrize("name,reply", [
    ("一张升级成功", json.dumps({"upgrades": [UPGRADE]}, ensure_ascii=False)),
    ("两张都升级", json.dumps({"upgrades": [
        UPGRADE, {**UPGRADE, "id": "m_2", "summary": "换了份新工作",
                  "content": "上个月换到一家更小的公司，节奏慢一些。"}]},
        ensure_ascii=False)),
    ("模型返回了不该动的 id", json.dumps({"upgrades": [
        UPGRADE, {**UPGRADE, "id": "m_999"}]}, ensure_ascii=False)),
    ("整份格式坏了", "这不是 JSON"),
])
def test_migrate_component_matches_the_legacy_path(name: str, reply: str) -> None:
    old = _migrate_legacy(reply)
    new = _migrate_component(reply)
    assert new[0] == old[0], f"「{name}」升级结果不一样"
    assert sorted(new[1]) == sorted(old[1]), f"「{name}」未升级列表不一样"
    assert new[2] == old[2], f"「{name}」错误码不一样"


def test_migrate_component_asks_the_model_the_same_thing() -> None:
    """提示词逐字节相同 —— 提示词变了模型产出就会变，那是用户看得见的。"""
    legacy = build_migrate_prompt(
        ai_name="io", user_name="老王", old_cards=OLD_CARDS,
        vocab=VOCAB, locale=LOCALE)
    seen: list[str] = []
    build_garden(CallableModel(lambda p: seen.append(p) or "{}")).migrate(
        MigrateRequest(old_cards=OLD_CARDS, allowed_ids=("m_1", "m_2"),
                       vocab=VOCAB, ai_name="io", user_name="老王", locale=LOCALE))
    assert seen == [legacy]
