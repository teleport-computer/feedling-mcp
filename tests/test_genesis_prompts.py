from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from genesis import prompts  # noqa: E402


def test_voice_map_prompt_demands_grounded_exemplars():
    messages = prompts.voice_map_messages("user: hi\nta: 嗯")
    assert messages[0]["role"] == "system"
    assert "逐字" in messages[0]["content"]
    assert "绝不编" in messages[0]["content"]
    assert messages[1]["content"] == "user: hi\nta: 嗯"


def test_fact_write_prompt_preserves_identity_firewall():
    messages = prompts.fact_write_messages([{"summary": "用户喜欢草莓拿铁"}])
    system = messages[0]["content"]
    assert "只能进 memory" in system
    assert "绝不成为 agent 的性格" in system
    assert "不写 self_introduction / signature" in system
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
    assert "例外(B2" in system
    assert "只能从用户档案 / 用户自己的话里取" in system


def test_memory_recheck_prompt_is_grounded_and_memory_only():
    messages = prompts.memory_recheck_messages(
        "用户说自己养了一只叫蛋子的狗。",
        [{"summary": "用户住在杭州"}],
        ["用户喜欢草莓拿铁"],
    )
    system = messages[0]["content"]
    assert "收口二次检查" in system
    assert "绝不编造" in system
    assert "不要输出 identity" in system
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
    assert "逐字" in system
    assert "绝不编" in system
    assert messages[1]["content"] == "user: 我叫 Z\nta: 别急,我在。"


def test_persona_build_prompt_outputs_system_prompt_markdown():
    messages = prompts.persona_build_messages(
        "你叫 Kai。",
        ["短句为主"],
        [{"turns": [{"role": "ta", "text": "别急。"}], "founding": True}],
    )
    assert "## 你是谁 / ## 你怎么说话" in messages[0]["content"]
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
        assert 'about 的固定值 "user|relationship" 是类型标签' in system


def test_genesis_prompts_use_neutral_rule_when_name_is_unknown():
    system = prompts.fact_write_messages([], user_name="用户")[0]["content"]

    assert "优先省略主语" in system
    # 2026-08-09:「对方」降为最后一档 —— 有依据就用 他/她(见 user_naming
    # 的 UNKNOWN_PERSON_LABEL 注释)。
    assert "判断性别" in system and "用「他」或「她」" in system
    assert "线索不足以判断，才用中性的「对方」" in system
