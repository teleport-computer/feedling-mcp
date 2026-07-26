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
    parse_capture_cards,
)

_FENCE = "`" * 3


def test_prompt_renders_with_context_and_escaped_json():
    p = build_capture_prompt(
        ai_name="小柒", user_name="Seven",
        buckets="工作, 关系", threads="加班, 吵架",
        identity="伴侣三个月", window="[user] 今天开了一天会",
    )
    assert "小柒" in p and "Seven" in p
    assert "今天开了一天会" in p
    # the JSON template braces survived .format()
    assert '"cards": []' in p
    assert '"action": "add | merge | supersede | noop"' in p


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
    assert "不要用「TA」指代对方" in p


def test_prompt_naming_rule_without_name_uses_relationship_referent():
    """No name yet → omit the subject, or use neutral 对方 when necessary."""
    p = build_capture_prompt(
        ai_name="", user_name="", buckets="", threads="", identity="", window="",
    )
    assert "优先省略主语" in p
    assert "确实需要主语时只用中性的「对方」" in p
    assert "猜测性别的他/她" in p
    assert "第二人称「你」" in p
    assert '永远不要用"用户"/"user"' in p
    assert "不要用「TA」指代对方" in p


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


def test_parse_garbage_returns_reason():
    cards, err = parse_capture_cards("not json at all")
    assert cards == [] and err == "no_json_object"


def test_parse_bounces_hollow_card():
    # 无 summary/content 的空壳卡以前静默丢弃;现在严格模式打回,放宽模式才只丢这一张。
    raw = '{"cards":[{"action":"add","type":"event"}]}'
    cards, err = parse_capture_cards(raw)
    assert cards == [] and err == "invalid_card_content:summary_empty"
    cards, err = parse_capture_cards(raw, strict=False)
    assert cards == [] and err is None


def test_parse_caps_threads_at_eight():
    raw = ('{"cards":[{"action":"add","summary":"标题","content":"正文",'
           '"threads":["a","b","c","d","e","f","g","h","i","j"]}]}')
    cards, err = parse_capture_cards(raw)
    assert len(cards[0]["threads"]) == 8


# --- A9 bucket convergence: one shared bilingual canonical vocabulary ----------
# onboarding + capture + migration must steer toward the SAME reusable bucket set
# instead of each card minting a fresh near-synonym (工作/职业/事业) or scattering.

def test_capture_prompt_carries_canonical_buckets():
    from memory.prompts_v1 import COMMON_BUCKETS_V1, _COMMON_BUCKETS_ZH, _COMMON_BUCKETS_EN
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
    from memory.prompts_v1 import _COMMON_BUCKETS_ZH
    from memory.migrate_prompt_v1 import build_migrate_prompt
    from genesis.prompts import FACT_WRITE_PROMPT
    mig = build_migrate_prompt(ai_name="io", user_name="hx", old_cards="c", vocab="（暂无）")
    assert _COMMON_BUCKETS_ZH in mig
    # onboarding (genesis FACT_WRITE) had NO bucket guidance before A9 — now it converges too
    assert _COMMON_BUCKETS_ZH in FACT_WRITE_PROMPT
    assert "桶名收敛" in FACT_WRITE_PROMPT
