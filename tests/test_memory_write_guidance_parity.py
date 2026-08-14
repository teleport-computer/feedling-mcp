"""V2 徒手写卡这条路,规则一条都没有 —— 这是「用户喜欢…」的来源。

usr_7001b1df80e2024d(2026-08-10,test/V2)让 AI 记一条,落地的卡里管本人叫
「用户」。我们 2026-07 明确修过这件事,但修的是 **capture/dream** 的 prompt
(`_naming_rule` 注入)和 **genesis** 的确定性改写器(`rewrite_user_reference`)。

第四条写入路 —— agent 自己调 `memory_write`(capture_mode=agent_tool)—— 两样都没接:
  - `capabilities/tool_schema.py` 的工具描述里一个字没提称呼;
  - `model_api_runtime/v2/context.py` 的 CHAT_SYSTEM_PROMPT 里也没有;
  - `MEMORY_WRITE_GUIDANCE_V1` 只在 `hosted_runtime.py`(V1)注入过。

⚠️ 本文件专门钉住那个**看起来最顺手、其实是坑**的修法:直接把
`MEMORY_WRITE_GUIDANCE_V1` 整块注进 V2。那份文案写的是 V1 的 op 名
(`memory.add` / `memory.supersede`),而 V2 的 schema 只认 op='add'/'update'/'delete'。
灌进去等于教模型调用自己 schema 会拒绝的操作 —— 比不灌更糟。所以规则抽成
`MEMORY_WRITE_RULES_V1` 共享,op 名各留各家。

顺带钉住 bucket 短名:线上那张中文卡的 bucket 是 "preferences",而配对表里
只有 "Preferences & boundaries",于是确定性后盾直接放行。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from capabilities import tool_schema  # noqa: E402
from memory import prompts_v1  # noqa: E402


# --------------------------------------------------------------------------- #
# 称呼规则必须到达 V2 徒手路
# --------------------------------------------------------------------------- #

def test_v2_memory_write_tool_states_the_naming_rule():
    """模型在调 memory_write 之前读到的文字里,必须有「别叫用户」。"""
    desc = tool_schema.DESCRIPTIONS["memory_write"]

    assert "用户" in desc and "NEVER call them" in desc, (
        "V2 的 memory_write 描述没告诉模型该怎么称呼本人 —— "
        f"这正是「用户喜欢…」落地的原因:{desc!r}"
    )


def test_v2_naming_rule_also_bans_the_placeholder_and_second_person():
    """光禁「用户」不够:「TA」和「你」是同一个病的另外两种写法。"""
    desc = tool_schema.DESCRIPTIONS["memory_write"]

    assert "TA" in desc, "没禁内部占位标记「TA」"
    assert "你" in desc, "没禁用第二人称指代本人"


# --------------------------------------------------------------------------- #
# op 名不许串台 —— 本次改动最容易踩的坑
# --------------------------------------------------------------------------- #

def test_v2_tool_description_never_carries_v1_op_names():
    """把 MEMORY_WRITE_GUIDANCE_V1 整块注进 V2 就会让这条变红。

    V2 的 schema 只认 add/update/delete;教它 memory.supersede 等于教它调一个
    不存在的操作。这条是本次改动的护栏,不是可选断言。
    """
    desc = tool_schema.DESCRIPTIONS["memory_write"]

    assert "memory.add" not in desc, "V1 的 op 名泄漏进了 V2 工具描述"
    assert "memory.supersede" not in desc, "V1 的 op 名泄漏进了 V2 工具描述"


def test_v2_tool_description_still_states_its_own_ops():
    """加规则不能把原本的 schema 说明挤掉。"""
    desc = tool_schema.DESCRIPTIONS["memory_write"]

    assert "'add'" in desc and "'update'" in desc and "'delete'" in desc
    assert "target_id" in desc


def test_v2_tool_description_states_merge_limit_and_single_delete_target():
    desc = tool_schema.DESCRIPTIONS["memory_write"]

    assert "at most 20 per update" in desc
    assert "'delete' accepts one 'target_id' only" in desc


def test_v2_tool_description_explains_how_to_recover_from_stale_merge_ids():
    desc = tool_schema.DESCRIPTIONS["memory_write"]

    assert "supersede_targets_unavailable" in desc
    assert "supersede_targets_changed" in desc
    assert "do not retry the stale ids" in desc
    assert "memory_search/memory_index again" in desc


def test_v1_guidance_keeps_its_own_op_names():
    """V1 那条路的 op 名不许被这次重构顺走。"""
    guidance = prompts_v1.MEMORY_WRITE_GUIDANCE_V1

    assert "memory.add" in guidance
    assert "memory.supersede" in guidance


# --------------------------------------------------------------------------- #
# 两条 lane 共用同一份规则 —— 防止又变成「同一口径两套实现」
# --------------------------------------------------------------------------- #

def test_both_runtimes_share_one_rules_constant():
    """规则只有一份。谁改了它,两条运行时同时生效。"""
    rules = prompts_v1.MEMORY_WRITE_RULES_V1

    assert rules in prompts_v1.MEMORY_WRITE_GUIDANCE_V1, "V1 没在用共享规则"
    assert rules in tool_schema.DESCRIPTIONS["memory_write"], "V2 没在用共享规则"


@pytest.mark.parametrize("rule", [
    "durable user/relationship facts",
    "bilingual slash pair",
    "pulse means",
    "Do not claim",
])
def test_v1_guidance_lost_nothing_in_the_extraction(rule):
    """抽共享常量是纯重构,V1 收到的文字不许少一条。

    唯一的例外是三段结构那条,2026-08-10 由 Seven 本人确认取消(见
    test_card_body_format_is_not_prescribed)——它不在本清单里,是刻意删的,
    不是抽取时漏的。
    """
    assert rule in prompts_v1.MEMORY_WRITE_GUIDANCE_V1


def test_card_body_format_is_not_prescribed():
    """卡片正文不规定格式 —— 一段话就行。

    三段结构(记忆/上下文/使用提示)原本写在 V1 的 guidance 里,1c8293cd 抽共享
    常量时一起带进了 V2,于是 V2 手写的卡开始长成三段。2026-08-10 Seven 确认
    那是改错了:不需要三段,和之前一样一段话即可。

    删的是**共享**常量里那条,所以 V1 和 V2 同时恢复成不规定格式 —— 两条 runtime
    仍然一致。capture/dream/genesis 本来就从没要求过。
    """
    for text in (
        prompts_v1.MEMORY_WRITE_RULES_V1,
        prompts_v1.MEMORY_WRITE_GUIDANCE_V1,
        tool_schema.DESCRIPTIONS["memory_write"],
    ):
        assert "使用提示" not in text
        assert "three Markdown sections" not in text


# --------------------------------------------------------------------------- #
# bucket 短名 —— 线上那张卡的第二个毛病
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("written,expected", [
    ("preferences", "偏好与边界"),   # 线上真实值
    ("boundaries", "偏好与边界"),
    ("goals", "目标与成长"),
    ("relationship", "我们的关系"),
    ("feelings", "情绪与安抚"),
    ("values", "个性与价值观"),
    ("travel", "地点与旅行"),
])
def test_short_english_bucket_on_a_chinese_card_is_mapped_home(written, expected):
    assert prompts_v1.normalize_bucket_language(written, "他喜欢打篮球") == expected


@pytest.mark.parametrize("written", ["work", "WORK", "Health", "health"])
def test_bucket_matching_is_case_insensitive(written):
    """模型写小写是常态,大小写不该决定这张卡进不进正确的桶。"""
    got = prompts_v1.normalize_bucket_language(written, "他喜欢打篮球")

    assert got in {"工作", "健康"}, f"{written!r} 没被收进中文桶,得到 {got!r}"


def test_short_form_on_an_english_card_converges_to_the_canonical_name():
    """英文卡也要收敛:preferences 和 Preferences & boundaries 不能并存成两个桶。"""
    assert prompts_v1.normalize_bucket_language(
        "preferences", "He likes basketball"
    ) == "Preferences & boundaries"


@pytest.mark.parametrize("custom", ["妈妈", "the house", "陆家嘴那间办公室"])
def test_a_genuinely_custom_bucket_still_passes_through(custom):
    """收短名不能变成"什么都往通用桶里塞" —— 自定义桶必须原样活下来。"""
    assert prompts_v1.normalize_bucket_language(custom, "他喜欢打篮球") == custom
    assert prompts_v1.normalize_bucket_language(custom, "He likes it") == custom


def test_canonical_pairs_still_work_both_ways():
    """原有行为不许被别名表改掉。"""
    assert prompts_v1.normalize_bucket_language("Pets", "他养了一只狗") == "宠物"
    assert prompts_v1.normalize_bucket_language("宠物", "He has a dog") == "Pets"
    assert prompts_v1.normalize_bucket_language("", "他喜欢打篮球") == ""
