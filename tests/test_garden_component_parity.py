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


def test_identity_as_a_rendered_string_still_counts_as_language_evidence():
    """V2 手里的 identity 是**字符串**，也必须能当语言证据。

    这条是 2026-08-31 在 V2 上真跑探针才发现的：V2 的调用点有个
    ``isinstance(..., dict)`` 守卫，而它拿到的一直是渲染好的正文字符串 ——
    那个分支永远走 None，于是 V2 判语言时只看「这一批新消息」。用户临时说
    一句英文，整个中文花园就被判成英文；V1 传 dict 所以锁得住。

    **单测抓不到的原因**：两条 runtime 的假 ctx 各按各自的类型写，谁都不会
    拿对方的类型来试。所以这里明确把两种类型都跑一遍。
    """
    from chat.reply_language import garden_language_decision

    zh_identity_text = "他叫 hx，做前端，说话直接，不爱绕弯子。平时用中文。"
    english_turn = (
        "Had a rough week at work - manager kept changing specs, "
        "stayed until 11pm three nights in a row."
    )

    as_str = garden_language_decision(zh_identity_text, written=english_turn)
    as_dict = garden_language_decision(
        {"self_introduction": zh_identity_text}, written=english_turn
    )
    assert as_str["locale"] == as_dict["locale"], (
        f"同样的证据，字符串形态判成 {as_str['locale']}、dict 形态判成 "
        f"{as_dict['locale']} —— 两条 runtime 会各判各的"
    )
    assert as_str["locale"].startswith("zh"), (
        "中文身份卡 + 一句英文 → 花园仍应是中文；判成英文就是 08-24 那次事故的形状"
    )


def test_no_identity_at_all_still_follows_what_the_person_wrote():
    """没有身份卡时退回「他自己写的字」—— 不是退回默认值。"""
    from chat.reply_language import garden_language_decision

    assert garden_language_decision(None, written="今天真的好累，加班到十一点")[
        "locale"
    ].startswith("zh")
    assert garden_language_decision("", written=(
        "Had a rough week at work - manager kept changing specs, "
        "stayed until 11pm three nights in a row, totally drained."
    ))["locale"] == "en"


def test_an_established_garden_language_wins_over_one_window_in_both_runtimes():
    """花园定了语言之后，说一句别的语言不该把它翻掉 —— 两条 runtime 都要守住。

    这条盯的是 2026-08-31 实测出来的真实差异：同一个中文花园、同一句英文抱怨，
    V1 判成中文、V2 判成英文 —— 不是判定逻辑不同，是**取证窗口宽窄不同**
    （V2 的 capture 是增量的，窗口里只有新增那几句）。用户看到的是「我用英文
    说了一句话，我的中文记忆开始变英文」。

    hx 拍板走「锚优先」：archive_language 压过单轮书写，窗口宽窄不再决定结果。
    """
    from chat.reply_language import garden_language_decision

    english_turn = (
        "Rough week at work - manager kept changing specs, "
        "stayed until 11pm three nights in a row."
    )

    # 增量窗口(V2 的形状)：这一轮全是英文
    v2_shape = garden_language_decision(
        "", written=english_turn, archive_language="zh-Hans"
    )
    # 较宽窗口(V1 的形状)：还含着之前那句中文
    v1_shape = garden_language_decision(
        "", written="我不吃辣，一吃就胃疼\n" + english_turn, archive_language="zh-Hans"
    )
    assert v2_shape["locale"] == v1_shape["locale"] == "zh-Hans", (
        f"窗口宽窄仍然改变结果：窄={v2_shape['locale']} 宽={v1_shape['locale']}"
    )
    assert v2_shape["basis"] == "established_language"


def test_a_brand_new_garden_still_follows_what_the_person_writes():
    """还没有锚的时候（全新用户）仍然跟着他写的字走 —— 锚不能变成写死中文。"""
    from chat.reply_language import garden_language_decision

    got = garden_language_decision("", written=(
        "Rough week at work - manager kept changing specs, "
        "stayed until 11pm three nights in a row."
    ), archive_language="")
    assert got["locale"] == "en", "没有锚时应当跟随书写语言，而不是回落到默认中文"
