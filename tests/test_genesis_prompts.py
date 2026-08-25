from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from genesis import prompts  # noqa: E402


def test_voice_map_prompt_demands_grounded_exemplars():
    messages = prompts.voice_map_messages("user: hi\nta: 嗯")
    assert messages[0]["role"] == "system"
    assert "Verbatim, multi-turn" in messages[0]["content"]
    assert "invent nothing" in messages[0]["content"]
    assert messages[1]["content"] == "user: hi\nta: 嗯"


def test_fact_write_prompt_preserves_identity_firewall():
    messages = prompts.fact_write_messages([{"summary": "用户喜欢草莓拿铁"}])
    system = messages[0]["content"]
    assert "goes ONLY into memory" in system
    assert "must never become the agent's personality" in system
    assert "Do not write self_introduction or signature" in system
    payload = json.loads(messages[1]["content"])
    assert payload["fact_digest"] == [{"summary": "用户喜欢草莓拿铁"}]


def test_fact_write_prompt_asks_for_the_5_user_layer_fields_grounded():
    # B2 (reverses I7): the onboarding distiller's output contract now includes
    # the 5 user-layer fields, with an explicit carve-out from the TA-identity
    # firewall (they describe the USER, so the source rule runs backwards).
    system = prompts.fact_write_messages([{"summary": "s"}])[0]["content"]
    for field in ("user_preferred_name", "custom_persona_prompt", "language_preference",
                  "relationship_anchor", "stable_definitions"):
        assert field in system, field
    assert "Exception (applies only to these five" in system
    assert "only from the person's profile or from the person's own words" in system


def test_memory_recheck_prompt_is_grounded_and_memory_only():
    messages = prompts.memory_recheck_messages(
        "用户说自己养了一只叫蛋子的狗。",
        [{"summary": "用户住在杭州"}],
        ["用户喜欢草莓拿铁"],
    )
    system = messages[0]["content"]
    assert "closing SECOND PASS" in system
    assert "Never fabricate" in system
    assert "Output memory cards only" in system
    assert '{"memories"' in system
    payload = json.loads(messages[1]["content"])
    assert payload["original_material"] == "用户说自己养了一只叫蛋子的狗。"
    assert payload["written_memories"] == [{"summary": "用户住在杭州"}]
    assert payload["known_memories"] == ["用户喜欢草莓拿铁"]


def test_fact_map_prompt_names_user_profile_source_firewall():
    messages = prompts.fact_map_messages("source_kind=user_profile\n用户叫 Seven。")
    assert "source_kind=user_profile" in messages[0]["content"]
    assert "不能推断 TA" in messages[0]["content"]


def test_combined_map_prompt_extracts_fact_and_voice_without_touching_reducers():
    messages = prompts.combined_map_messages("user: 我叫 Z\nta: 别急,我在。")
    system = messages[0]["content"]
    assert "fact_candidates" in system
    assert "voice_candidates" in system
    assert "verbatim" in system
    assert "Never invent" in system
    assert messages[1]["content"] == "user: 我叫 Z\nta: 别急,我在。"


def test_persona_build_prompt_outputs_system_prompt_markdown():
    messages = prompts.persona_build_messages(
        "你叫 Kai。",
        ["短句为主"],
        [{"turns": [{"role": "ta", "text": "别急。"}], "founding": True}],
    )
    assert "## 你是谁" in messages[0]["content"] and "## 你怎么说话" in messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert payload["persona_material"] == "你叫 Kai。"
    assert payload["behavior_notes"] == ["短句为主"]


def test_genesis_prompts_apply_preferred_name_only_to_visible_prose():
    builders = [
        prompts.fact_map_messages("用户喜欢咖啡", user_name="Seven"),
        prompts.combined_map_messages("用户喜欢咖啡", user_name="Seven"),
        prompts.fact_write_messages([], user_name="Seven"),
        prompts.persona_build_messages("", [], [], user_name="Seven"),
    ]

    for messages in builders:
        system = messages[0]["content"]
        assert "提到 Seven 就用「Seven」这个名字" in system
        assert 'The fixed value "user|relationship" of `about`' in system


def test_genesis_prompts_use_neutral_rule_when_name_is_unknown():
    system = prompts.fact_write_messages([], user_name="用户")[0]["content"]

    assert "优先省略主语" in system
    # 2026-08-09:「对方」降为最后一档 —— 有依据就用 他/她(见 user_naming
    # 的 UNKNOWN_PERSON_LABEL 注释)。
    assert "判断性别" in system and "用「他」或「她」" in system
    assert "线索不足以判断，才用中性的「对方」" in system


# --------------------------------------------------------------------------- #
# 输出语言：每个蒸馏 builder 都要显式约束，不能靠「指令是中文所以模型答中文」
# --------------------------------------------------------------------------- #


def test_every_distill_builder_pins_the_output_language():
    """七个 builder 全都要带输出语言约束。

    **这条是踩出来的。** 2026-08-23 把蒸馏提示词改成英文指令时，四个 builder
    （persona_build / voice_map / voice_reduce / combined_map）压根没有任何语言
    约束 —— 此前靠的是「指令是中文，模型自然答中文」这个**隐式**保证，指令一换
    语言那个保证就没了。

    后果最重的是 persona_build：它产出的是 agent 的 system prompt，只在 onboarding
    生成一次、直接决定这个伴侣怎么说话。中文用户拿到一份英文人格，事后很难重来，
    而且线上第一时间也不容易归因到「提示词换语言」这件事上。

    注意 persona_build 不走 _STRICT_JSON_SUFFIX（它输出 markdown），所以只挂在
    那个后缀上覆盖不到它 —— 这正是当初漏掉的原因。
    """
    from genesis import prompts as gp

    builders = {
        "voice_map": gp.voice_map_messages("聊天记录"),
        "voice_reduce": gp.voice_reduce_messages([]),
        "persona_build": gp.persona_build_messages("素材", [], []),
        "fact_map": gp.fact_map_messages("块"),
        "combined_map": gp.combined_map_messages("块"),
        "fact_write": gp.fact_write_messages([]),
        "memory_recheck": gp.memory_recheck_messages("素材", [], []),
    }
    missing = [n for n, msgs in builders.items()
               if "Output language:" not in msgs[0]["content"]]
    assert not missing, f"这些 builder 没有输出语言约束：{missing}"


def test_output_language_follows_the_material_not_the_instructions():
    """依据必须是**素材**，不是指令语言，也不是用户当前说什么。

    导入一批中文历史，产出就该是中文 —— 哪怕指令本身是英文写的。
    写成「用你们对话的语言」会在导入场景下判错：那时根本没有「对话」。
    """
    from genesis import prompts as gp

    rule = gp._OUTPUT_LANGUAGE_RULE
    assert "language of the source material" in rule
    assert "NOT the language of these instructions" in rule
    # 固定的 JSON 键和枚举值不许被"翻译" —— 翻了下游解析就崩
    assert "user|relationship" in rule and "fact|event|quote|moment" in rule
