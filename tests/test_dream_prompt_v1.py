"""Unit tests for the Dream prompt + parser (A-full tail-2 / PR D, no DB)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from memory.dream_prompt_v1 import (  # noqa: E402
    DREAM_OPS,
    build_dream_prompt,
    parse_dream_consolidations,
)

_FENCE = "`" * 3


def test_prompt_renders_with_context_and_escaped_json():
    p = build_dream_prompt(
        ai_name="小柒", user_name="Seven",
        cards="[卡1] 我们的关系: ...", recent_conversations="[user] ...",
    )
    assert "小柒" in p and "Seven" in p
    assert "我们的关系" in p
    assert '"consolidations": []' in p
    assert '"op": "merge | thicken | supersede"' in p
    # red line present
    assert "superseded" in p and "不删" in p


def test_prompt_falls_back_to_neutral_defaults():
    p = build_dream_prompt(ai_name="", user_name="", cards="", recent_conversations="")
    assert "（暂无卡）" in p and "（这几天没有新对话）" in p


def test_prompt_naming_rule_and_backfill_instruction():
    """Dream shares capture's naming rule and additionally rewrites the old
    wording (用户/user/TA) out of existing cards while consolidating — the
    self-healing path for cards polluted before the fix."""
    p = build_dream_prompt(
        ai_name="小柒", user_name="Seven", cards="", recent_conversations="",
    )
    assert "提到 Seven 就用「Seven」" in p
    flat = p.replace("\n", "").replace(" ", "")
    assert '永远不要用"用户"/"user"' in flat
    assert "不要用「TA」指代对方" in flat
    # Backfill is scoped: only TA that refers to the PERSON gets rewritten;
    # TA correctly referring to the AI (the app-surface meaning) is preserved.
    assert '指代对方本人的"用户"/"user"/「TA」/「你」/猜测性别的他或她' in flat
    assert "名字未知时优先省略主语" in flat
    assert "中性的「对方」" in flat
    assert "指代你（AI）的「TA」" in flat and "保留不动" in flat
    p2 = build_dream_prompt(ai_name="", user_name="", cards="", recent_conversations="")
    assert "优先省略主语" in p2


def test_reserved_placeholder_user_name_treated_as_unknown():
    p = build_dream_prompt(
        ai_name="", user_name="用户", cards="", recent_conversations="",
    )
    assert "优先省略主语" in p
    assert "就用「用户」" not in p


def test_parse_normal_consolidation():
    raw = ('{"consolidations":[{"op":"merge","card_ids":["a","b"],'
           '"result":{"bucket":"工作","threads":["加班"],"summary":"合并卡",'
           '"content":"厚正文","importance":0.7,"pulse":0.3}}],'
           '"questions_to_ask":["要不要问 TA X"]}')
    cons, qs, err = parse_dream_consolidations(raw)
    assert err is None and len(cons) == 1
    c = cons[0]
    assert c["op"] == "merge" and c["card_ids"] == ["a", "b"]
    assert c["result"]["summary"] == "合并卡" and c["result"]["importance"] == 0.7
    assert qs == ["要不要问 TA X"]


def test_parse_empty_is_clean():
    cons, qs, err = parse_dream_consolidations('{"consolidations": [], "questions_to_ask": []}')
    assert cons == [] and qs == [] and err is None


def test_parse_drops_unknown_op():
    cons, qs, err = parse_dream_consolidations(
        '{"consolidations":[{"op":"delete","card_ids":["a"],"result":{"summary":"标题","content":"正文"}}]}'
    )
    assert cons == [] and err is None  # delete is not a Dream op (no hard delete)


def test_parse_drops_consolidation_without_card_ids():
    cons, qs, err = parse_dream_consolidations(
        '{"consolidations":[{"op":"merge","card_ids":[],"result":{"summary":"标题","content":"正文"}}]}'
    )
    assert cons == [] and err is None  # Dream only edits existing cards


def test_parse_bounces_hollow_result():
    # 空 result 以前静默丢弃;现在严格模式打回(让调用方重问一次),
    # 放宽模式(打回后的第二问)才只丢这一行。
    raw = '{"consolidations":[{"op":"thicken","card_ids":["a"],"result":{}}]}'
    cons, qs, err = parse_dream_consolidations(raw)
    assert cons == [] and err == "invalid_card_content:summary_empty"
    # 放宽的第二问里它是唯一一行 → 全脏,报 after_retry 让 job 失败(别推进 frontier)
    cons, qs, err = parse_dream_consolidations(raw, strict=False)
    assert cons == [] and err == "invalid_card_content_after_retry:summary_empty"


def test_parse_handles_fence_and_prose_and_clamps():
    raw = ("整理完了：" + _FENCE + 'json\n{"consolidations":[{"op":"supersede","card_ids":["old"],'
           '"result":{"summary":"标题","content":"正文","importance":5,"pulse":-2}}]}\n' + _FENCE)
    cons, qs, err = parse_dream_consolidations(raw)
    assert err is None and len(cons) == 1
    assert cons[0]["op"] == "supersede"
    assert cons[0]["result"]["importance"] == 1.0 and cons[0]["result"]["pulse"] == 0.0


def test_parse_garbage_returns_reason():
    cons, qs, err = parse_dream_consolidations("not json")
    assert cons == [] and qs == [] and err == "no_json_object"


def test_parse_keeps_questions_even_when_no_consolidations():
    cons, qs, err = parse_dream_consolidations(
        '{"consolidations": [], "questions_to_ask": ["矛盾A","矛盾B"]}'
    )
    assert cons == [] and qs == ["矛盾A", "矛盾B"] and err is None


def test_dream_ops_are_merge_thicken_supersede():
    assert set(DREAM_OPS) == {"merge", "thicken", "supersede"}
