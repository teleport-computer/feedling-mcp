"""三个策略档位：共用结构，尺子必须保持不同。

这些测试守的是本批最容易被后人「顺手统一掉」的东西 —— 三把尺子看起来很像，
但统一成任何一把都是事故：

    统一成「少而厚」   → 用户手动整理的 100 条只落 2 张卡
    统一成「宁多勿漏」 → 日常聊天每句废话都变成卡
"""
from __future__ import annotations

import pathlib

import pytest

from memory_garden.policies import (
    CURATED_ARCHIVE,
    DEFAULT_POLICY,
    POLICIES,
    UnknownPolicyError,
    get_policy,
)


def test_three_policies_exist():
    assert set(POLICIES) == {
        "conversation_capture",
        "history_import",
        "curated_archive",
    }


def test_conversation_capture_is_few_and_thick():
    p = get_policy("conversation_capture")
    assert p.max_cards == 2, "日常聊天是少而厚，不能放开张数"
    assert p.prefer_merge is True
    assert "宁少勿多" in p.selection_rubric


def test_curated_archive_keeps_everything():
    p = get_policy("curated_archive")
    assert p.max_cards is None, "用户整理的档案宁多勿漏，不能有张数上限"
    assert p.keep_dates is True, "档案里的日期要原样保留"
    assert p.seed_threads_from_tags is True
    assert "宁多勿漏" in p.selection_rubric


def test_history_import_filters_one_off_events():
    p = get_policy("history_import")
    assert "一次性事件" in p.selection_rubric
    assert "闲聊" in p.selection_rubric


def test_rubrics_are_all_different():
    """三把尺子的文字必须真的不同 —— 被抹平就是本批要防的那个事故。"""
    rubrics = {name: p.selection_rubric for name, p in POLICIES.items()}
    assert len(set(rubrics.values())) == 3, f"尺子被抹平了: {list(rubrics)}"


def test_conversation_and_archive_are_opposites():
    """日常聊天与用户档案在「多与少」这件事上必须是相反的。"""
    chat = get_policy("conversation_capture")
    archive = get_policy("curated_archive")
    assert chat.max_cards is not None and archive.max_cards is None
    assert "宁少勿多" in chat.selection_rubric
    assert "宁多勿漏" in archive.selection_rubric


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_missing_policy_falls_back_to_default(empty):
    """没传 = 旧调用方，退回现行为是安全的。"""
    assert get_policy(empty) is DEFAULT_POLICY
    assert DEFAULT_POLICY.name == "conversation_capture"


@pytest.mark.parametrize(
    "typo", ["nonexistent_policy", "curated_archiv", "CONVERSATION_CAPTURE", "history import"]
)
def test_unknown_policy_name_raises(typo):
    """显式传错必须炸出来 —— 静默回落的后果不对称。

    ``curated_archive`` 拼错一个字母就会悄悄切成「宁少勿多」，把用户手工整理的
    上百条事实压成一两张卡，而且没有任何信号。
    """
    with pytest.raises(UnknownPolicyError) as excinfo:
        get_policy(typo)
    # 报错要指出可用值，否则调用方还得翻源码
    assert "conversation_capture" in str(excinfo.value)


def test_unknown_policy_error_is_a_valueerror():
    """沿用 ValueError 语义，调用方原有的 except ValueError 仍能兜住。"""
    assert issubclass(UnknownPolicyError, ValueError)


def test_policies_are_immutable():
    """档位是冻结的 dataclass —— 防止运行时被某条路径就地改掉。"""
    with pytest.raises(Exception):
        CURATED_ARCHIVE.max_cards = 2  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 共用的语言规则（已写好、尚未接线）
# --------------------------------------------------------------------------- #


def test_language_rule_is_shared_text_with_only_the_basis_swapped():
    """措辞/举例/标点全共用，只有「依据」不同 —— 那是必要差异，不是不一致。"""
    from memory_garden.policies import language_rule

    chat = language_rule("conversation_capture")
    imported = language_rule("history_import")

    assert chat != imported
    assert "TA 跟你对话" in chat
    assert "素材原文" in imported
    # 除了依据那几个字，其余逐字相同。
    # 注意「 TA 跟你对话」带前导空格（中英混排），换算时要连空格一起换；
    # 指代输入的那个词也跟着依据走（对话 / 素材），两边基线原文就是这么写的。
    assert chat.replace(" TA 跟你对话", "素材原文").replace("对话", "素材") == imported


def test_language_rule_is_conditional_not_unconditional():
    """规则必须按材料语言分支，不能有无条件的「别用英文桶」。

    第一版写成「英文就用英文；别归成英文桶/线索」，两句直接矛盾 ——
    后半句无条件生效会加剧「英文用户拿到中文卡」。
    （codex review 2026-08-14 指出。）
    """
    from memory_garden.policies import language_rule

    text = language_rule("conversation_capture")
    assert "中文对话就用中文" in text
    assert "英文对话就用英文" in text
    assert "别归成英文桶" not in text, "无条件句又回来了 —— 它与「英文对话就用英文」矛盾"
    assert "旅行" in text, "capture 原有的例子丢了"


def test_mixed_language_material_unifies_the_taxonomy_language():
    """混合语料必须按**整体主语言**统一，不许按每条事实各自判。

    真实回归（2026-08-14 本地 genesis 导入实测）：规则一度写成
    「混合材料按每条事实自身的主语言」——两边基线都没有这条 —— 结果一份
    中文为主、夹一句英文的档案导进去后，同一个桶裂成
    ``目标与成长`` 与 ``Goals & growth``。

    桶/线索是分类键，裂开等于同一类记忆被拆成两堆；而
    ``normalize_bucket_language`` 按每张卡自己的文字归一化，兜不住跨卡分裂。
    """
    from memory_garden.policies import language_rule

    for policy in ("conversation_capture", "history_import", "curated_archive"):
        text = language_rule(policy)
        assert "按整体主语言统一" in text, f"{policy} 缺混合语料的统一口径"
        assert "每条事实自身的主语言" not in text, (
            f"{policy} 又回到按条判语言 —— 这会让同一个桶裂成两种语言"
        )
        assert "工作" in text and "Work" in text, (
            f"{policy} 丢了「不能『工作』和 Work 并存」这个具体反例"
        )
    assert "专有名词" in text


def test_language_rule_rejects_unknown_policy_like_get_policy_does():
    """与 get_policy 同一口径 —— 不许从侧门放回 fail-open。

    上一轮把 get_policy 的静默回落改成抛错，但 language_rule 当时仍用
    dict.get 静默回落（codex review 2026-08-14 指出）。
    """
    import pytest as _pytest

    from memory_garden.policies import UnknownPolicyError, language_rule

    with _pytest.raises(UnknownPolicyError):
        language_rule("nonexistent")
    # None / 空串仍回落，代表「旧调用方没传」
    assert "TA 跟你对话" in language_rule(None)
    assert "TA 跟你对话" in language_rule("")


def test_language_rule_is_wired_into_both_sides():
    """语言规则已接线：capture 与 genesis 都从本模块取，不再各写一份。

    这是本批真正消除的第二处重复（第一处是 curated_archive 的两段）。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "backend"
    capture_src = (root / "memory_garden" / "prompts" / "capture.py").read_text(encoding="utf-8")
    genesis_src = (root / "genesis" / "prompts.py").read_text(encoding="utf-8")

    assert "{language_rule}" in capture_src, "capture 模板没有语言占位符"
    assert "policies_language_rule" in capture_src, "capture 没调 language_rule"
    assert "mg_policies.language_rule" in genesis_src, "genesis 没调 language_rule"

    # 两边都不许再出现内联的旧文本
    for name, src in (("capture", capture_src), ("genesis", genesis_src)):
        assert "用 TA 跟你对话的语言记" not in src, f"{name} 里还留着旧的内联文本"
        assert "用素材原文的语言——中文素材" not in src, f"{name} 里还留着旧的内联文本"


def test_language_basis_keeps_the_cjk_latin_spacing():
    """「TA 跟你对话」带前导空格 —— 中英混排时「用」和字母之间要留空格。

    丢了空格会产出「用TA 跟你对话的语言」，是排版退化。
    """
    from memory_garden.policies import language_rule

    assert "用 TA 跟你对话的语言" in language_rule("conversation_capture")
    assert "用素材原文的语言" in language_rule("history_import")


# --------------------------------------------------------------------------- #
# 与 genesis 的关系：curated 已是唯一来源，history 仍是副本
# --------------------------------------------------------------------------- #


def test_genesis_keep_all_comes_from_policies_not_its_own_copy():
    """curated_archive 那两段的重复**已真正消除** —— genesis 引用本模块。

    这条一旦红，说明有人在 genesis 那边又写回了一份字面量。
    """
    from genesis import prompts as gp
    from memory_garden.policies import KEEP_ALL_MAP_SUFFIX, KEEP_ALL_WRITE_SUFFIX

    assert gp.FACT_MAP_KEEP_ALL_SUFFIX == "\n\n" + KEEP_ALL_MAP_SUFFIX
    assert gp.FACT_WRITE_KEEP_ALL_SUFFIX == "\n\n" + KEEP_ALL_WRITE_SUFFIX


def test_history_import_is_also_single_source_now():
    """history_import 的副本**已消除** —— genesis 引用两个片段，不再自己写。

    原文在 FACT_MAP_PROMPT 里不连续（中间隔着防火墙段），所以拆成
    OPENING + FILTER 两段，由 genesis 在原位置原顺序拼回：既去重复，
    又不移动任何文本。
    """
    from genesis import prompts as gp
    from memory_garden.policies import (
        HISTORY_IMPORT_FILTER_RUBRIC,
        HISTORY_IMPORT_OPENING_RUBRIC,
    )

    assert HISTORY_IMPORT_OPENING_RUBRIC in gp.FACT_MAP_PROMPT
    assert HISTORY_IMPORT_FILTER_RUBRIC in gp.FACT_MAP_PROMPT
    # 顺序不许颠倒：开头讲抽什么，之后才是不抽什么
    assert gp.FACT_MAP_PROMPT.index(HISTORY_IMPORT_OPENING_RUBRIC) < gp.FACT_MAP_PROMPT.index(
        HISTORY_IMPORT_FILTER_RUBRIC
    )
    # genesis 里不许再出现字面量副本
    src = pathlib.Path(__file__).resolve().parents[1] / "backend" / "genesis" / "prompts.py"
    text = src.read_text(encoding="utf-8")
    assert "闲聊/临时情绪/玩笑/未确认猜测/一次性事件不抽。" not in text, "字面量副本又写回来了"


#: genesis 三个 prompt 的字节 golden。逐行 `in` 判断抓不住重排 / 重复 / 插入，
#: 所以这里用完整哈希（codex review 2026-08-14 指出原守卫不够）。
#: **有意改 prompt 时更新这些值，并在提交说明里写清改了什么、为什么。**
_GENESIS_PROMPT_GOLDEN = {
    "FACT_MAP_PROMPT": "26dd13bcc54e83ac",
    "COMBINED_MAP_PROMPT": "1f0721abc7aa00e6",
}


@pytest.mark.parametrize("name", sorted(_GENESIS_PROMPT_GOLDEN))
def test_genesis_prompt_is_byte_identical_to_golden(name):
    """完整 prompt 的字节 golden —— 引用重构不许改动任何一个字符。"""
    import hashlib

    from genesis import prompts as gp

    text = getattr(gp, name)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    assert digest == _GENESIS_PROMPT_GOLDEN[name], (
        f"{name} 变了。若是有意改动，更新 _GENESIS_PROMPT_GOLDEN 并说明原因；"
        f"若不是，说明引用重构改动了文本。"
    )


def test_keep_all_text_keeps_genesis_original_punctuation():
    """逐字保留意味着连半角标点都不许「顺手改成全角」—— 那也是改 prompt。"""
    from memory_garden.policies import KEEP_ALL_MAP_SUFFIX

    assert "不是聊天记录:" in KEEP_ALL_MAP_SUFFIX, "半角冒号被改掉了"
    assert "宁多勿漏。" in KEEP_ALL_MAP_SUFFIX


# --------------------------------------------------------------------------- #
# 档位必须真的进入执行路径，不能只是「描述数据」
# --------------------------------------------------------------------------- #


def _cards_json(n: int) -> str:
    import json

    return json.dumps(
        {
            "cards": [
                {
                    "action": "add", "type": "fact", "bucket": f"桶{i}",
                    "threads": ["x"], "summary": f"第{i}张卡的摘要内容",
                    "content": f"第{i}张卡的正文内容，足够长以通过内容闸。",
                    "importance": 0.5, "pulse": 0.3,
                }
                for i in range(n)
            ]
        },
        ensure_ascii=False,
    )


def test_max_cards_is_actually_enforced_not_just_declared():
    """「少而厚 ≤2 张」必须在代码上生效，不能只写在 prompt 里。

    codex review 2026-08-14 实测：此前传 3 张卡返回 3 张，max_cards 声明了
    却没有任何调用方消费 —— 三个档位只是「描述数据」，不是可执行策略。
    """
    from memory_garden.prompts.capture import parse_capture_cards

    cards, err = parse_capture_cards(_cards_json(3))
    assert cards == []
    assert err is not None and err.startswith("too_many_cards"), err


def test_over_limit_is_a_full_batch_retry_not_a_silent_truncation():
    """超限要打回重做整批，而不是静默截掉第三张 —— 截断会丢内容且无人知晓。"""
    from memory_garden.prompts.capture import parse_capture_cards

    _, err = parse_capture_cards(_cards_json(3), strict=True)
    assert "too_many_cards" in err


def test_after_retry_keeps_the_first_n_instead_of_failing_the_whole_job():
    """重问后仍超：保留前 N 张。**不能**整批失败 ——
    那会推进 capture frontier 把这段对话永久丢掉（与内容闸同一教训）。
    """
    from memory_garden.prompts.capture import parse_capture_cards

    cards, err = parse_capture_cards(_cards_json(3), strict=False)
    assert len(cards) == 2 and err is None


def test_unlimited_policies_do_not_truncate():
    """用户整理的档案不限张数 —— 传 3 张就该留 3 张。"""
    from memory_garden.prompts.capture import parse_capture_cards

    for name in ("curated_archive", "history_import"):
        cards, err = parse_capture_cards(_cards_json(3), policy=name)
        assert len(cards) == 3 and err is None, name


def test_within_limit_is_untouched():
    """没超限的批次行为完全不变（守住绝大多数正常路径）。"""
    from memory_garden.prompts.capture import parse_capture_cards

    for n in (0, 1, 2):
        cards, err = parse_capture_cards(_cards_json(n))
        assert len(cards) == n and err is None
