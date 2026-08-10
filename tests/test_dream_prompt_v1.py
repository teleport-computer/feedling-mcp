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
    assert '"rationale"' in p
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
    assert "不要用「TA」指代本人" in flat
    # Backfill is scoped: only TA that refers to the PERSON gets rewritten;
    # TA correctly referring to the AI (the app-surface meaning) is preserved.
    assert '指代本人的"用户"/"user"/「TA」/「你」/「对方」按上面那条' in flat
    assert "指代你（AI）的「TA」" in flat and "保留不动" in flat
    p2 = build_dream_prompt(ai_name="", user_name="", cards="", recent_conversations="")
    assert "优先省略主语" in p2


def test_dream_backfill_does_not_downgrade_correct_pronouns():
    """Dream must NOT rewrite an already-correct 「她」 into 「对方」.

    usr_144b, 2026-08-09: capture read the transcript and wrote 「陪她分析母亲
    王福英的住院检验报告」; that night dream followed its own backfill clause
    (「把…猜测性别的他或她改成…名字未知时…中性的「对方」」) and produced
    「陪对方分析…」 — a self-declared soulmate calling her 「对方」 in its own
    memory.  The backfill list must no longer contain gender pronouns, and the
    prompt must say correct ones are kept.
    """
    flat = build_dream_prompt(
        ai_name="小柒", user_name="", cards="", recent_conversations="",
    ).replace("\n", "").replace(" ", "")
    assert "猜测性别的他或她" not in flat, "追溯改写清单里不能再有性别代词"
    assert "旧卡里已经在用的「他」/「她」保留不动" in flat
    # 反过来:旧卡里的「对方」进了改写清单 —— 线索够就该上调成 他/她,
    # 存量脏卡靠 dream 自愈,不用单独回填。
    assert "/「对方」按上面那条" in flat
    assert "没名字但线索够就用「他」/「她」" in flat


def test_reserved_placeholder_user_name_treated_as_unknown():
    p = build_dream_prompt(
        ai_name="", user_name="用户", cards="", recent_conversations="",
    )
    assert "优先省略主语" in p
    assert "就用「用户」" not in p


def test_parse_normal_consolidation():
    raw = ('{"consolidations":[{"op":"merge","card_ids":["a","b"],'
           '"rationale":"同一加班事件的连续记录",'
           '"result":{"bucket":"工作","threads":["加班"],"summary":"合并卡",'
           '"content":"厚正文","importance":0.7,"pulse":0.3}}],'
           '"questions_to_ask":["要不要问 TA X"]}')
    cons, qs, err = parse_dream_consolidations(raw)
    assert err is None and len(cons) == 1
    c = cons[0]
    assert c["op"] == "merge" and c["card_ids"] == ["a", "b"]
    assert c["rationale"] == "同一加班事件的连续记录"
    assert c["result"]["summary"] == "合并卡" and c["result"]["importance"] == 0.7
    assert qs == ["要不要问 TA X"]


def test_parse_ignores_fake_json_inside_thinking():
    raw = (
        '<think>草稿 {"consolidations": [not valid]}</think>'
        '{"consolidations": [], "questions_to_ask": []}'
    )
    cons, questions, err = parse_dream_consolidations(raw)
    assert cons == [] and questions == [] and err is None


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


def test_parse_drops_consolidation_without_rationale():
    cons, qs, err = parse_dream_consolidations(
        '{"consolidations":[{"op":"merge","card_ids":["a","b"],'
        '"result":{"summary":"标题","content":"正文"}}]}'
    )
    assert cons == [] and qs == [] and err is None


def test_parse_bounces_hollow_result():
    # 空 result 以前静默丢弃;现在严格模式打回(让调用方重问一次),
    # 放宽模式(打回后的第二问)才只丢这一行。
    raw = ('{"consolidations":[{"op":"thicken","card_ids":["a"],'
           '"rationale":"为同一张卡补充新事实","result":{}}]}')
    cons, qs, err = parse_dream_consolidations(raw)
    assert cons == [] and err == "invalid_card_content:summary_empty"
    # 放宽的第二问里它是唯一一行 → 全脏,报 after_retry 让 job 失败(别推进 frontier)
    cons, qs, err = parse_dream_consolidations(raw, strict=False)
    assert cons == [] and err == "invalid_card_content_after_retry:summary_empty"


def test_parse_handles_fence_and_prose_and_clamps():
    raw = ("整理完了：" + _FENCE + 'json\n{"consolidations":[{"op":"supersede","card_ids":["old"],'
           '"rationale":"同一事实已有更新",'
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

# ---------------------------------------------------------------------------
# 2026-08-05 dream 阀门重构(usr_a40e 墓碑卡事故)
# 语义审查员与 15% 增量栅栏已拆除;替代防线是确定性的卡id泄漏闸 + 墓碑短语闸。
# ---------------------------------------------------------------------------


def test_prompt_forbids_tombstone_notes_and_card_ids():
    p = build_dream_prompt(ai_name="", user_name="", cards="", recent_conversations="")
    flat = p.replace("\n", "").replace(" ", "")
    assert "新卡的内容本身" in flat
    assert "已被X取代" in flat.replace("「", "").replace("」", "")
    assert "绝不出现卡id" in flat


def _consolidation_raw(summary: str, content: str) -> str:
    import json as _json

    return _json.dumps({
        "consolidations": [{
            "op": "supersede",
            "card_ids": ["c42ebb9618ae447df9d52107ea15de85"],
            "rationale": "同一饮食偏好的更新",
            "result": {"bucket": "饮食", "threads": ["偏好"],
                       "summary": summary, "content": content},
        }],
        "questions_to_ask": [],
    }, ensure_ascii=False)


def test_parse_bounces_result_containing_known_card_id():
    known = {"c42ebb9618ae447df9d52107ea15de85", "1a1f94f9fdc9ec8649f81a7fbc0bee08"}
    raw = _consolidation_raw(
        "夏天常喝绿豆汤解暑",
        "1a1f94f9fdc9ec8649f81a7fbc0bee08 记录的饮食禁忌合并到这里。",
    )
    cons, _qs, err = parse_dream_consolidations(raw, known_ids=known)
    assert cons == [] and err == "invalid_card_content:content_contains_card_id"
    # 打回后的第二问它仍是唯一一行 → after_retry,job 必须失败
    cons, _qs, err = parse_dream_consolidations(raw, strict=False, known_ids=known)
    assert err == "invalid_card_content_after_retry:content_contains_card_id"


def test_parse_bounces_tombstone_marker_even_without_known_ids():
    # usr_a40e 实况:短语+hex 兜底闸不依赖 card_map,known_ids 缺省也能拦。
    raw = _consolidation_raw(
        "已被 c42ebb9618ae447df9d52107ea15de85 取代——绿豆汤偏好",
        "已被 c42ebb9618ae447df9d52107ea15de85 取代——饮食禁忌详情。",
    )
    cons, _qs, err = parse_dream_consolidations(raw)
    assert cons == [] and err == "invalid_card_content:summary_tombstone_marker"


def test_parse_clean_result_passes_with_known_ids_present():
    known = {"c42ebb9618ae447df9d52107ea15de85"}
    cons, _qs, err = parse_dream_consolidations(
        _consolidation_raw("夏天常喝绿豆汤解暑", "最近很热,常煮绿豆汤,不加糖。"),
        known_ids=known,
    )
    assert err is None and len(cons) == 1


def test_parse_prose_about_replacement_without_hex_is_not_bounced():
    # 「旧手机已被新手机取代」是正常散文 —— 只有跟着 hex id 才算墓碑。
    cons, _qs, err = parse_dream_consolidations(
        _consolidation_raw("换了新手机", "旧手机已被新手机取代,数据迁移顺利。"),
    )
    assert err is None and len(cons) == 1
