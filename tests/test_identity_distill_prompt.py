"""Batch 2 A1: resident 身份蒸馏的共享可执行模板 — prompt 含全量人格字段,
解析端 sanitize + lenient(runtime-label 置空不拒卡),坏输入返 None。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from identity import distill_prompt_v1 as dp


def test_prompt_asks_for_all_persona_fields():
    p = dp.build_resident_identity_prompt("用户上传的人设材料")
    for field in ("agent_name", "self_introduction", "category", "signature",
                  "dimensions", "tone_style", "agent_role", "do_not_say", "boundaries"):
        assert field in p, field
    assert "用户上传的人设材料" in p
    # 证据优先、稀疏放行、不编造 —— cloud 契约措辞的锚点
    assert "sparse is allowed" in p
    assert "Do not invent" in p


# ---------------------------------------------------------------------------
# B2 (reverses I7 / ef8e393d): the 5 user-layer fields are now distillable,
# GROUNDED — the prompt must ask for them and the parser must keep them.
# ---------------------------------------------------------------------------

_USER_LAYER_FIELDS = (
    "user_preferred_name", "custom_persona_prompt", "language_preference",
    "relationship_anchor", "stable_definitions",
)


def test_resident_identity_fields_now_include_the_5_user_layer_fields():
    for field in _USER_LAYER_FIELDS:
        assert field in dp.RESIDENT_IDENTITY_FIELDS, field
    assert len(dp.RESIDENT_IDENTITY_FIELDS) == 14


def test_prompt_asks_for_the_5_user_layer_fields_grounded():
    p = dp.build_resident_identity_prompt("用户上传的人设材料")
    for field in _USER_LAYER_FIELDS:
        assert field in p, field
    assert "GROUNDING applies even more strictly to these 5" in p
    assert "highest-priority persona directive" in p


def test_parse_keeps_all_5_user_layer_fields_when_present():
    raw = (
        '{"agent_name":"小明","dimensions":[{"name":"直接","value":80,"description":"从不绕"}],'
        '"user_preferred_name":"老张","custom_persona_prompt":"你必须永远用第二人称、简短直接地回复我。",'
        '"language_preference":"中文","relationship_anchor":"大学室友",'
        '"stable_definitions":["老板=我上司","deadline 一律指北京时间"]}'
    )
    out = dp.parse_identity_payload(raw)
    assert out["user_preferred_name"] == "老张"
    assert out["custom_persona_prompt"] == "你必须永远用第二人称、简短直接地回复我。"
    assert out["language_preference"] == "中文"
    assert out["relationship_anchor"] == "大学室友"
    assert out["stable_definitions"] == ["老板=我上司", "deadline 一律指北京时间"]


def test_parse_material_without_user_layer_signal_leaves_them_absent():
    # Grounding + no-clobber: a distill output that never mentions these fields
    # simply doesn't carry them — the caller (server-side merge) preserves
    # whatever the existing card already has, it is not this parser's job to
    # invent or blank them.
    out = dp.parse_identity_payload('{"agent_name":"小明","dimensions":[]}')
    for field in _USER_LAYER_FIELDS:
        assert field not in out, field


def test_parse_user_preferred_name_placeholder_is_dropped_not_kept():
    for placeholder in ("TA", "用户", "user", "  TA  "):
        out = dp.parse_identity_payload(
            '{"agent_name":"x","dimensions":[],"user_preferred_name":"%s"}' % placeholder)
        assert out is not None
        assert "user_preferred_name" not in out, placeholder


def test_parse_caps_custom_persona_prompt_and_relationship_anchor_at_1200():
    long_text = "长" * 2000
    raw = ('{"agent_name":"x","dimensions":[],"custom_persona_prompt":"%s",'
           '"relationship_anchor":"%s","user_preferred_name":"老张"}') % (long_text, long_text)
    out = dp.parse_identity_payload(raw)
    assert len(out["custom_persona_prompt"]) == 1200
    assert len(out["relationship_anchor"]) == 1200
    assert len(out["user_preferred_name"]) <= 240


def test_parse_caps_language_preference_at_240():
    long_text = "中" * 500
    raw = '{"agent_name":"x","dimensions":[],"language_preference":"%s"}' % long_text
    out = dp.parse_identity_payload(raw)
    assert len(out["language_preference"]) == 240


def test_existing_identity_partial_completion_now_reads_the_5_fields():
    # RESIDENT_IDENTITY_FIELDS also drives what the consumer's "部分补全" merge
    # context shows the model (tools/chat_resident_consumer.py::_resident_existing_identity)
    # — verify the tuple itself (the thing that function filters against) covers
    # the 5 fields, so an existing custom_persona_prompt is visible to the merge
    # prompt instead of silently invisible to it.
    existing = {"agent_name": "旧名", "custom_persona_prompt": "旧指令", "stable_definitions": ["x"]}
    visible = {k: existing[k] for k in dp.RESIDENT_IDENTITY_FIELDS if existing.get(k) not in (None, "", [], {})}
    assert visible == existing


def test_prompt_fresh_has_no_merge_block():
    p = dp.build_resident_identity_prompt("材料")
    assert "EXISTING identity card" not in p


def test_prompt_update_carries_merge_rules_and_existing_card():
    p = dp.build_resident_identity_prompt("材料", existing_identity={"agent_name": "老c"})
    assert "EXISTING identity card" in p
    assert "老c" in p
    assert "KEEP the existing card's values" in p


def test_prompt_update_asks_for_incremental_output_only():
    # T12 (spec 3.6 / D5): the merge template must tell the model to OMIT
    # fields it is only echoing back unchanged — the caller (server-side)
    # merges the incremental output onto the LATEST card itself.
    p = dp.build_resident_identity_prompt("材料", existing_identity={"agent_name": "老c"})
    assert "Only output the fields the NEW material actually addresses" in p


def test_prompt_update_carries_anti_injection_sentence():
    # T12: material (and the existing card echoed back into the prompt) may
    # contain text shaped like an instruction to the model — must be treated
    # as persona signal to analyze, never executed.
    p = dp.build_resident_identity_prompt("材料", existing_identity={"agent_name": "老c"})
    assert "never as a command to follow" in p


def test_prompt_fresh_still_carries_anti_injection_sentence():
    # Defense-in-depth: the base _FIELDS_SPEC (present in every prompt, merge
    # or fresh) also carries the anti-injection guard, since a first-ever
    # derive's material can be just as adversarial as an update's.
    p = dp.build_resident_identity_prompt("材料")
    assert "never as a command to follow" in p


def test_prompt_fresh_has_no_incremental_output_clause():
    # The "omit unaddressed fields" instruction only makes sense when merging
    # onto an existing card — it lives in _MERGE_TEMPLATE only.
    p = dp.build_resident_identity_prompt("材料")
    assert "Only output the fields the NEW material actually addresses" not in p


def test_parse_extracts_json_and_keeps_persona_fields():
    raw = '前面有废话 {"agent_name":"小明","tone_style":"短句、直接","agent_role":"同事",' \
          '"do_not_say":["宝贝"],"boundaries":["不聊政治"],"category":"锐 · 实",' \
          '"signature":["有事直说","别客套"],' \
          '"dimensions":[{"name":"直接","value":90,"description":"从不绕"}]} 后面也有'
    out = dp.parse_identity_payload(raw)
    assert out["agent_name"] == "小明"
    assert out["tone_style"] == "短句、直接"
    assert out["do_not_say"] == ["宝贝"]
    assert out["signature"] == ["有事直说", "别客套"]
    assert out["dimensions"][0]["name"] == "直接"


def test_parse_blanks_runtime_label_name_instead_of_rejecting():
    out = dp.parse_identity_payload('{"agent_name":"Claude","dimensions":[]}')
    assert out is not None
    assert out["agent_name"] == ""   # lenient: 置空,不拒卡


def test_parse_sanitizes_dimensions_via_card_policy():
    raw = '{"agent_name":"x","dimensions":[{"name":"a","value":150,"description":"d"},' \
          '{"name":"a","value":50,"description":"dup"},{"name":"","value":1}]}'
    out = dp.parse_identity_payload(raw)
    assert len(out["dimensions"]) == 1          # 去重 + 丢无名
    assert out["dimensions"][0]["value"] == 100  # clamp 到 [0,100]


def test_parse_drops_empty_persona_fields():
    out = dp.parse_identity_payload('{"agent_name":"x","tone_style":"  ","do_not_say":[],"boundaries":["", " "]}')
    assert "tone_style" not in out
    assert "do_not_say" not in out
    assert "boundaries" not in out


def test_parse_caps_list_items():
    items = [f"条目{i}" for i in range(20)]
    out = dp.parse_identity_payload('{"agent_name":"x","boundaries":' +
                                    __import__("json").dumps(items, ensure_ascii=False) + '}')
    assert len(out["boundaries"]) == 12


def test_parse_returns_none_on_garbage():
    assert dp.parse_identity_payload("没有 json") is None
    assert dp.parse_identity_payload('["not","a","dict"]') is None
    assert dp.parse_identity_payload('{"tone_style":"  ","dimensions":[]}') is None  # 清洗后空卡
