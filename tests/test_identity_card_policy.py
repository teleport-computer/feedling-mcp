from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from identity import card_policy  # noqa: E402


def test_is_runtime_label_matches_known_and_ignores_case():
    assert card_policy.is_runtime_label("Claude") is True
    assert card_policy.is_runtime_label(" hermes ") is True
    assert card_policy.is_runtime_label("阿锐") is False
    assert card_policy.is_runtime_label("") is False


def test_dimensions_structure_accepts_sparse_and_clustered():
    # 契约 B:2 维稀疏、全部聚集在高位,都是合法结构
    sparse = [{"name": "锐利", "value": 90, "description": "x"},
              {"name": "直接", "value": 88, "description": "y"}]
    assert card_policy.validate_dimensions_structure(sparse) == (True, "")
    clustered = [{"name": f"d{i}", "value": 85, "description": "z"} for i in range(7)]
    assert card_policy.validate_dimensions_structure(clustered) == (True, "")


def test_dimensions_structure_rejects_bad_shape():
    assert card_policy.validate_dimensions_structure("nope")[0] is False
    assert card_policy.validate_dimensions_structure(
        [{"name": "", "value": 50, "description": "x"}]) == (False, "dimension_name_empty")
    assert card_policy.validate_dimensions_structure(
        [{"name": "a", "value": 150, "description": "x"}]) == (False, "dimension_value_out_of_range")
    assert card_policy.validate_dimensions_structure(
        [{"name": "a", "value": "hi", "description": "x"}]) == (False, "dimension_value_not_number")
    assert card_policy.validate_dimensions_structure(
        [{"name": "a", "value": 50, "description": "x"},
         {"name": "A", "value": 60, "description": "y"}]) == (False, "dimension_name_duplicate")
    assert card_policy.validate_dimensions_structure(
        [{"name": "a", "value": True, "description": "x"}]) == (False, "dimension_value_not_number")


def test_full_card_structure_only_lenient():
    ok_card = {"agent_name": "阿锐", "self_introduction": "hi",
               "dimensions": [{"name": "锐利", "value": 90, "description": "x"}]}
    assert card_policy.validate_full_identity_card(ok_card) == (True, "")
    # 稀疏(1 维)在契约 B 下合法
    assert card_policy.validate_full_identity_card(
        {"agent_name": "阿锐", "dimensions": []}) == (True, "")
    # 空名字放行(hx 定 0712:优先 onboarding 成功率,名字可后补;不为缺名字卡住 onboarding)
    assert card_policy.validate_full_identity_card(
        {"agent_name": "", "dimensions": []}) == (True, "")
    # 但非空名字仍不能是 runtime label
    assert card_policy.validate_full_identity_card(
        {"agent_name": "Claude", "dimensions": []}) == (False, "agent_name_is_runtime_label")


def test_profile_patch_only_checks_present_fields():
    # 只改名字:旧卡维度稀疏也不该因此被拒
    assert card_policy.validate_profile_patch({"agent_name": "阿锐"}) == (True, "")
    assert card_policy.validate_profile_patch({"tone_style": "sharp"}) == (True, "")
    assert card_policy.validate_profile_patch({"agent_name": "gpt"}) == (False, "agent_name_is_runtime_label")
    assert card_policy.validate_profile_patch(
        {"dimensions": [{"name": "a", "value": 150, "description": "x"}]}) == (False, "dimension_value_out_of_range")


def test_profile_patch_rejects_reserved_or_empty_user_preferred_name():
    assert card_policy.validate_profile_patch({"user_preferred_name": "小雨"}) == (True, "")
    for reserved in ("用户", "user", "USER", "ta", "TA", "「用户」", "`user`"):
        assert card_policy.validate_profile_patch(
            {"user_preferred_name": reserved}
        ) == (False, "user_preferred_name_is_reserved")
    assert card_policy.validate_profile_patch(
        {"user_preferred_name": "  "}
    ) == (False, "user_preferred_name_empty")


def test_dimension_nudge_range_only():
    assert card_policy.validate_dimension_nudge("锐利", 70) == (True, "")
    assert card_policy.validate_dimension_nudge("锐利", 150) == (False, "dimension_value_out_of_range")
    assert card_policy.validate_dimension_nudge("", 50) == (False, "dimension_name_empty")


def test_service_runtime_labels_are_card_policy_source():
    from identity import service as identity_service
    assert identity_service._IDENTITY_RUNTIME_LABELS is card_policy.RUNTIME_LABELS
    # 既有判定不回归
    assert "claude" in identity_service._IDENTITY_RUNTIME_LABELS
    assert "hermes" in identity_service._IDENTITY_RUNTIME_LABELS
    # 之前被误删的 12 个 label 不回归(google/bard/deepseek 等错误被判定为合法名字)
    for label in ("google", "bard", "deepseek", "agent", "io", "feedling"):
        assert label in card_policy.RUNTIME_LABELS


def test_dimensions_structure_rejects_too_many_and_non_dict():
    thirteen = [{"name": f"d{i}", "value": 50, "description": "x"} for i in range(13)]
    assert card_policy.validate_dimensions_structure(thirteen) == (False, "too_many_dimensions")
    assert card_policy.validate_dimensions_structure(["not-a-dict"]) == (False, "dimension_must_be_object")


def test_runtime_labels_full_set_locked():
    # locks the full 36-label set so a future accidental drop is caught
    assert len(card_policy.RUNTIME_LABELS) == 36


def test_sanitize_clamps_dedups_truncates_drops():
    dirty = {"agent_name": "阿锐", "dimensions": [
        {"name": "锐利", "value": 150, "description": "x"},   # 越界 → 夹到 100
        {"name": "锐利", "value": 30, "description": "dup"},   # 重名 → 丢
        {"name": "温情", "value": -5, "description": "y"},     # 越界 → 夹到 0
        {"name": "坏", "value": "hi", "description": "z"},      # 非数字 → capture-more 补 50
        {"name": "", "value": 60},                              # 无名 → 丢
        42,                                                      # 非 dict/str → 丢
    ]}
    out = card_policy.sanitize_identity_card(dirty)
    dims = out["dimensions"]
    assert [d["name"] for d in dims] == ["锐利", "温情", "坏"]
    assert dims[0]["value"] == 100 and dims[1]["value"] == 0 and dims[2]["value"] == 50
    # sanitize 后必然通过强校验(结构)
    assert card_policy.validate_dimensions_structure(dims) == (True, "")


def test_normalize_dimension_value_rescales_and_rounds():
    n = card_policy.normalize_dimension_value
    # 0<v<=1 判定为 0–1 概率刻度误用 → ×100 还原
    assert n(0.95) == 95
    assert n(0.9) == 90
    assert n(0.6) == 60
    assert n(0.85) == 85
    assert n(1) == 100      # 1.0 视为满分而非 1 分(高分维度语义)
    # 常规 0–100 刻度 → 取整不变
    assert n(95) == 95
    assert n(0) == 0
    assert n(100) == 100
    assert n(87.4) == 87
    # 越界 → clamp
    assert n(150) == 100
    assert n(-5) == 0
    # 结果永远是 int(iOS JSONDecoder 只认整数)
    for v in (0.95, 95, 0, 1, 150, -5, 87.4):
        assert type(n(v)) is int


def test_sanitize_rounds_non_integer_dimension_values():
    # 自托管弱模型把 0–100 刻度当 0–1 概率吐出的浮点,必须被还原成整数分值,
    # 否则 iOS JSONDecoder 撞上 0.95 抛 dataCorrupted → 整卡解析失败误报 decrypt failed
    dirty = {"agent_name": "阿锐", "dimensions": [
        {"name": "锐利", "value": 0.95, "description": "x"},
        {"name": "温情", "value": 0.9, "description": "y"},
        {"name": "直接", "value": 0.6, "description": "z"},
        {"name": "克制", "value": 0.85, "description": "w"},
        {"name": "已是整数", "value": 95, "description": "v"},
        {"name": "满分", "value": 1, "description": "u"},
    ]}
    out = card_policy.sanitize_identity_card(dirty)
    got = {d["name"]: d["value"] for d in out["dimensions"]}
    assert got == {"锐利": 95, "温情": 90, "直接": 60, "克制": 85, "已是整数": 95, "满分": 100}
    for d in out["dimensions"]:
        assert type(d["value"]) is int
    assert card_policy.validate_dimensions_structure(out["dimensions"]) == (True, "")


def test_sanitize_truncates_to_max():
    many = {"agent_name": "阿锐", "dimensions": [
        {"name": f"d{i}", "value": 50, "description": "x"} for i in range(20)]}
    assert len(card_policy.sanitize_identity_card(many)["dimensions"]) == card_policy.MAX_DIMENSIONS


def test_sanitize_leaves_name_untouched():
    # 空名/runtime 名字 sanitize 不动(名字是强校验/引导层的事,不在这瞎编)
    assert card_policy.sanitize_identity_card({"agent_name": "", "dimensions": []})["agent_name"] == ""
    assert card_policy.sanitize_identity_card({"agent_name": "Claude", "dimensions": []})["agent_name"] == "Claude"


def test_sanitize_normalizes_non_list_dimensions_to_empty_list():
    # dimensions 不是 list(None/缺失/错类型)也必须归一成 [],让结果永远过结构校验
    assert card_policy.sanitize_identity_card({"dimensions": None})["dimensions"] == []
    assert card_policy.sanitize_identity_card({"dimensions": "nope"})["dimensions"] == []
    assert card_policy.sanitize_identity_card({})["dimensions"] == []


def test_sanitize_adopts_score_key_as_value():
    # prod 实卡形态(usr_56e0/usr_a7ea):弱模型用 score 键代替 value。
    # capture-more:收编成 value(过同一套 normalize),不丢维度。
    dirty = {"agent_name": "X", "dimensions": [
        {"name": "占有欲", "score": 82},
        {"name": "intellectual partner", "score": 95, "evidence": "…"},
        {"name": "概率刻度", "score": 0.8},
    ]}
    out = card_policy.sanitize_identity_card(dirty)
    got = {d["name"]: d["value"] for d in out["dimensions"]}
    assert got == {"占有欲": 82, "intellectual partner": 95, "概率刻度": 80}
    for d in out["dimensions"]:
        assert type(d["value"]) is int
        assert "score" not in d  # 收编后丢弃,别让卡里留两套分值
    # 其它键(evidence)保留
    assert out["dimensions"][1]["evidence"] == "…"


def test_sanitize_value_wins_over_score_when_both_present():
    out = card_policy.sanitize_identity_card({"dimensions": [
        {"name": "a", "value": 70, "score": 10}]})
    assert out["dimensions"][0]["value"] == 70


def test_sanitize_converts_bare_string_dimension():
    # prod 实卡形态(usr_6876):维度是裸字符串。转成 {name, value:50} 保内容。
    out = card_policy.sanitize_identity_card({"dimensions": [
        "温柔但有原则", "ISTJ", "", 42]})
    dims = out["dimensions"]
    assert [d["name"] for d in dims] == ["温柔但有原则", "ISTJ"]
    assert all(d["value"] == 50 for d in dims)
    # 空串和非 dict/str 元素仍然丢
    assert len(dims) == 2


def test_sanitize_defaults_named_dimension_without_numeric_value_to_50():
    # prod 实卡形态(usr_c37c):只有 name+summary 没分值 → 补中位 50,保住 summary。
    out = card_policy.sanitize_identity_card({"dimensions": [
        {"name": "表达方式", "summary": "先给结论"},
        {"name": "布尔坑", "value": True},
        {"name": "文字分值", "value": "高", "description": "d"},
    ]})
    got = {d["name"]: d["value"] for d in out["dimensions"]}
    assert got == {"表达方式": 50, "布尔坑": 50, "文字分值": 50}
    assert out["dimensions"][0]["summary"] == "先给结论"
    assert out["dimensions"][2]["description"] == "d"
    assert card_policy.validate_dimensions_structure(out["dimensions"]) == (True, "")
