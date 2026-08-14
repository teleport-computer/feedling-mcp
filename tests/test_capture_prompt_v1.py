"""Unit tests for the 落卡 capture prompt + parser (A-full PR C, no DB).

Pure-function coverage of capture_prompt_v1: prompt rendering and the agent
reply parser (parse_capture_cards). DB-free so it runs anywhere.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from memory.capture_prompt_v1 import (  # noqa: E402
    CAPTURE_TYPES,
    build_capture_prompt,
    build_capture_semantic_retry_prompt,
    capture_semantic_retry_reasons,
    parse_capture_cards,
)
from memory_garden.text.card_text import extract_json_block  # noqa: E402
from agent_protocol_core import self_thinking  # noqa: E402

_FENCE = "`" * 3


def test_prompt_renders_with_context_and_escaped_json():
    p = build_capture_prompt(
        ai_name="小柒", user_name="Seven",
        buckets="工作, 关系", threads="加班, 吵架",
        identity="伴侣三个月", window="[user] 今天开了一天会",
        cards="- [mom_123] （桶：工作）长期加班",
    )
    assert "小柒" in p and "Seven" in p
    assert "今天开了一天会" in p
    # the JSON template braces survived .format()
    assert '"cards": []' in p
    assert '"action": "add | merge | supersede | noop"' in p
    assert "[mom_123]" in p
    assert "只能从这里复制确切 target_id" in p


def test_prompt_falls_back_to_neutral_defaults():
    p = build_capture_prompt(
        ai_name="", user_name="", buckets="", threads="", identity="", window="",
    )
    assert "（暂无）" in p and "（空）" in p


def test_prompt_naming_rule_uses_known_name():
    """User-visible card text: the name when known — never "用户"/"user"
    (usr_fee1 complaint 2026-07-17: cards narrated her as "用户" although the
    agent knew her name)."""
    p = build_capture_prompt(
        ai_name="小柒", user_name="Seven",
        buckets="", threads="", identity="", window="",
    )
    assert "提到 Seven 就用「Seven」" in p
    assert '永远不要用"用户"/"user"' in p
    # TA is an instruction/transcript marker only — outputs must not use it.
    assert "不要用「TA」指代本人" in p


def test_prompt_naming_rule_without_name_uses_relationship_referent():
    """No name yet → omit the subject; 对方 is the LAST resort, not the default.

    2026-08-09: the old rule degraded to 「对方」 whenever ``user_name`` was
    empty and flatly banned 他/她 as "gender guessing".  usr_144b never typed a
    name into settings (she only ever talked to her partner), so every card
    about her said 「对方」 — including the ones capture had originally written
    correctly as 「她」.  "Field not filled in" is not "we don't know who this
    person is": an evidence-backed pronoun is not a guess.
    """
    p = build_capture_prompt(
        ai_name="", user_name="", buckets="", threads="", identity="", window="",
    )
    assert "优先省略主语" in p
    # 线索够就用 他/她,「对方」只兜最后一档。
    assert "判断性别" in p and "用「他」或「她」" in p
    assert "线索不足以判断，才用中性的「对方」" in p
    assert "猜测性别" not in p
    # 仍然禁的三样,一样都不能松。
    assert "第二人称「你」" in p
    assert '永远不要用"用户"/"user"' in p
    assert "不要用「TA」指代本人" in p


def test_capture_prompt_marks_对方_as_a_placeholder_label_not_a_referent():
    """转写标签 > prompt(2026-07-27 教训):名字未知时每一行都标「对方:」,
    模型会照抄标签。所以放开称呼规则的同时必须点明这个标签只是占位。"""
    p = build_capture_prompt(
        ai_name="", user_name="", buckets="", threads="", identity="",
        window="- 对方: 老公我到家了",
    )
    assert "那只是标签" in p
    assert "卡里怎么称呼，按上面那条规则判断" in p


def test_reserved_placeholder_names_are_treated_as_unknown():
    """A stored placeholder "name" (用户/user/TA, any case) must not be
    instructed as a real name — `提到 用户 就用「用户」` would re-pollute the
    very cards this rule fixes."""
    from memory.capture_prompt_v1 import sanitize_user_name

    for reserved in ("用户", "user", "USER", "ta", "TA", "「用户」", "`user`", "", "  "):
        assert sanitize_user_name(reserved) == "TA", repr(reserved)
        p = build_capture_prompt(
            ai_name="", user_name=reserved,
            buckets="", threads="", identity="", window="",
        )
        assert "优先省略主语" in p, repr(reserved)
        assert "就用「用户」" not in p and "就用「user」" not in p, repr(reserved)
    assert sanitize_user_name("Seven") == "Seven"
    assert sanitize_user_name("小雨") == "小雨"


def test_parse_normal_card():
    raw = ('{"cards":[{"action":"add","type":"event","target_id":null,'
           '"bucket":"工作","threads":["加班","心率"],"summary":"开了一天会",'
           '"content":"厚正文","importance":0.8,"pulse":0.4}]}')
    cards, err = parse_capture_cards(raw)
    assert err is None and len(cards) == 1
    c = cards[0]
    assert c["action"] == "add" and c["type"] == "event"
    assert c["importance"] == 0.8 and c["pulse"] == 0.4
    assert c["threads"] == ["加班", "心率"]


def test_parse_empty_is_clean():
    cards, err = parse_capture_cards('{"cards": []}')
    assert cards == [] and err is None


def test_parse_drops_noop():
    cards, err = parse_capture_cards('{"cards":[{"action":"noop","summary":"x"}]}')
    assert cards == [] and err is None


def test_parse_coerces_insight_reflection_out():
    # capture never writes insight/reflection (those need anchors / are Dream's job)
    for bad in ("insight", "reflection", "weird"):
        raw = '{"cards":[{"action":"add","type":"%s","summary":"标题","content":"正文"}]}' % bad
        cards, err = parse_capture_cards(raw)
        assert len(cards) == 1 and cards[0]["type"] in CAPTURE_TYPES
        assert cards[0]["type"] == "event"  # default


def test_parse_handles_json_fence():
    raw = "好的\n" + _FENCE + 'json\n{"cards":[{"action":"add","summary":"标题","content":"正文"}]}\n' + _FENCE
    cards, err = parse_capture_cards(raw)
    assert err is None and len(cards) == 1


def test_parse_handles_prose_wrapped_and_clamps():
    raw = '我想了想：{"cards":[{"action":"merge","target_id":"mom_1","summary":"标题","content":"正文","importance":2.0,"pulse":-1}]} 就这些'
    cards, err = parse_capture_cards(raw)
    assert err is None and len(cards) == 1
    assert cards[0]["action"] == "merge" and cards[0]["target_id"] == "mom_1"
    assert cards[0]["importance"] == 1.0 and cards[0]["pulse"] == 0.0


def test_parse_ignores_balanced_fake_json_inside_thinking():
    """Prod regression: a thinking relay put brace-shaped reasoning before JSON.

    The legacy extractor stopped at the first balanced braces, so this exact
    shape returned ``json_decode_error`` even though the public reply was valid.
    """
    raw = (
        '<think>先试草稿 {"cards": [not valid]}，再给正式答案。</think>\n'
        '{"cards":[{"action":"add","summary":"记得按时休息",'
        '"content":"她说最近连续熬夜，希望以后提醒她早点休息。"}]}'
    )

    cards, err = parse_capture_cards(raw)

    assert err is None
    assert [card["summary"] for card in cards] == ["记得按时休息"]


def test_parse_thinking_without_braces_keeps_existing_behavior():
    raw = '<think>我先判断这件事值得长期记住。</think>{"cards": []}'
    cards, err = parse_capture_cards(raw)
    assert cards == [] and err is None


def test_parse_truncated_json_stays_no_json_object():
    cards, err = parse_capture_cards('{"cards":[{"action":"add"}')
    assert cards == [] and err == "no_json_object"


def test_extract_json_falls_back_to_raw_when_thinking_strip_fails():
    raw = (
        '<think>outer <thinking>nested</thinking></think>'
        '{"cards": []}'
    )
    status, _thinking, reply = self_thinking.strip_all_thinking(
        raw, sanitize=False
    )
    assert status == self_thinking.FAILED and reply == ""
    assert extract_json_block(raw) == '{"cards": []}'


def test_parse_garbage_returns_reason():
    cards, err = parse_capture_cards("not json at all")
    assert cards == [] and err == "no_json_object"


def test_parse_bounces_hollow_card():
    # 无 summary/content 的空壳卡以前静默丢弃;现在严格模式打回,放宽模式才只丢这一张。
    raw = '{"cards":[{"action":"add","type":"event"}]}'
    cards, err = parse_capture_cards(raw)
    assert cards == [] and err == "invalid_card_content:summary_empty"
    # 放宽的第二问里它是唯一一张 → 全脏,报 after_retry 让 job 失败(别推进 frontier)
    cards, err = parse_capture_cards(raw, strict=False)
    assert cards == [] and err == "invalid_card_content_after_retry:summary_empty"


def test_parse_caps_threads_at_eight():
    raw = ('{"cards":[{"action":"add","summary":"标题","content":"正文",'
           '"threads":["a","b","c","d","e","f","g","h","i","j"]}]}')
    cards, err = parse_capture_cards(raw)
    assert len(cards[0]["threads"]) == 8


def test_capture_semantic_retry_only_flags_missing_target():
    assert capture_semantic_retry_reasons(
        [{"action": "supersede", "target_id": ""}]
    )
    assert capture_semantic_retry_reasons(
        [{"action": "merge", "target_id": "mom_1"}]
    ) == []
    assert capture_semantic_retry_reasons([{"action": "add"}]) == []


def test_capture_semantic_retry_requires_the_complete_batch():
    prompt = build_capture_semantic_retry_prompt("ORIGINAL", ["missing target"])
    assert "包括上次已经合法的卡" in prompt
    assert "不要只输出失败的卡" in prompt
    assert "只重答失败的卡" not in prompt


# --- A9 bucket convergence: one shared bilingual canonical vocabulary ----------
# onboarding + capture + migration must steer toward the SAME reusable bucket set
# instead of each card minting a fresh near-synonym (工作/职业/事业) or scattering.

def test_capture_prompt_carries_canonical_buckets():
    from memory_garden.prompts.buckets import COMMON_BUCKETS_V1, _COMMON_BUCKETS_ZH, _COMMON_BUCKETS_EN
    p = build_capture_prompt(
        ai_name="io", user_name="hx", buckets="（暂无）", threads="（暂无）",
        identity="x", window="y",
    )
    # zh + en presented as SEPARATE lists (not 工作/Work pairs) so the model writes one
    # single-language word, not the "健康/Health" pair verbatim (real bug e2e caught).
    assert _COMMON_BUCKETS_ZH in p and _COMMON_BUCKETS_EN in p
    assert "宠物" in p and "Pets" in p          # both languages available, single-word
    assert "别写成「健康/Health」" in p          # explicit anti-mixing example
    assert COMMON_BUCKETS_V1 and all(zh.strip() and en.strip() for zh, en in COMMON_BUCKETS_V1)
    # companion-tuned set (hx): 14 buckets incl. the relationship/emotion/boundary ones
    assert len(COMMON_BUCKETS_V1) == 14
    for pair in (("宠物", "Pets"), ("偏好与边界", "Preferences & boundaries"),
                 ("个性与价值观", "Personality & values"), ("我们的关系", "Our relationship")):
        assert pair in COMMON_BUCKETS_V1


def test_migrate_and_genesis_share_the_same_canonical_buckets():
    from memory_garden.prompts.buckets import _COMMON_BUCKETS_ZH
    from memory_garden.prompts.migrate import build_migrate_prompt
    from genesis.prompts import FACT_WRITE_PROMPT
    mig = build_migrate_prompt(ai_name="io", user_name="hx", old_cards="c", vocab="（暂无）")
    assert _COMMON_BUCKETS_ZH in mig
    # onboarding (genesis FACT_WRITE) had NO bucket guidance before A9 — now it converges too
    assert _COMMON_BUCKETS_ZH in FACT_WRITE_PROMPT
    assert "桶名收敛" in FACT_WRITE_PROMPT
