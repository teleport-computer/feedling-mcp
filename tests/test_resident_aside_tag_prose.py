"""T687 (2026-09-21): resident V1 asks for its aside in a tag; prose stays
outside JSON.

8c693dc4 moved the resident aside into the reply JSON and told CLI models
"最终回复请使用 JSON". Models behind relays then hand-wrote their reply inside
a JSON string and left ASCII quotes / newlines unescaped (你问"你怎么知道"的
时候); json.loads refused, the visible-protocol scanner saw only debris, and
the consumer dropped the whole turn as ``protocol_leak`` — one user lost 11 of
16 turns in an afternoon, fleet parse failures went 0–1/h → 5–11/h.

Seven's call: prose must not live inside a JSON string the model types by
hand. The resident lane renders the tag instruction again (tag chosen by
``_self_thinking_tag()``), a locally parsed tag block is the self-authored
aside, and the JSON envelope (with its optional ``aside`` field) stays
accepted for multi-bubble replies. Runtime V2 keeps ``reply(aside, text)``.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

from agent_protocol_core import self_thinking as st


def _load_consumer(monkeypatch):
    monkeypatch.setenv("FEEDLING_API_URL", "http://x")
    monkeypatch.setenv("FEEDLING_USER_ID", "u")
    monkeypatch.setenv("FEEDLING_API_KEY", "k")
    root = pathlib.Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "crc_aside_tag_prose", root / "tools" / "chat_resident_consumer.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _turn(crc, raw: str):
    turn = crc._agent_turn_from_raw(raw)
    crc._suppress_torn_protocol_leaks(turn, lane="chat")
    return turn


def _wrapped_by_pi_native_thinking(crc, reply: str) -> str:
    # Production path for `-thinking` models on pi: native reasoning arrives
    # at the event layer and the consumer folds the model text in as one
    # message string before the downstream parse.
    return crc._attach_provider_reasoning(
        reply, "她今天挺开心的", source="pi_thinking",
        kind="provider_reasoning_summary", native=True,
    )


# The measured shape, as prose after a tag instead of inside a JSON string:
# ASCII quotes used as Chinese quotation marks, half-width commas, blank lines.
PROSE = '你问"你怎么知道"的时候眼睛是亮的。她说"好啊",然后就笑了。\n\n我当然看到了。'


def test_tagged_aside_plus_prose_with_quotes_is_delivered_intact(monkeypatch):
    crc = _load_consumer(monkeypatch)
    for tag in (st.TAG_THINK, st.TAG_ASIDE):
        raw = f"<{tag}>她在等我夸她</{tag}>{PROSE}"
        for via, text in (("bare", raw), ("pi", _wrapped_by_pi_native_thinking(crc, raw))):
            turn = _turn(crc, text)
            assert turn.messages == [PROSE], (tag, via)
            assert turn.thinking_summary == "她在等我夸她", (tag, via)
            assert turn.thinking_self_authored is True, (tag, via)
            assert turn.thinking_kind == "agent_summary", (tag, via)
            assert turn.thinking_native is False, (tag, via)
            assert turn.sanitizer_reason == "", (tag, via)


def test_the_json_string_shape_that_failed_in_production_still_fails_closed(monkeypatch):
    """Negative control for the fix's direction: the failing shape was the
    model's prose *inside* a JSON string. That shape is not repaired here
    (Seven declined a lax-JSON repair); B removes the instruction that
    produced it. Pin that the parser is unchanged for it."""
    crc = _load_consumer(monkeypatch)
    raw = '{"aside":"她在等我夸她","messages":["你问"你怎么知道"的时候眼睛是亮的。"]}'
    turn = _turn(crc, _wrapped_by_pi_native_thinking(crc, raw))
    assert turn.messages == []
    assert turn.sanitizer_reason == "protocol_leak"


def test_optional_json_envelope_and_json_aside_field_still_work(monkeypatch):
    crc = _load_consumer(monkeypatch)
    multi = _turn(crc, '<think>稳</think>{"messages":["一","二"]}')
    assert multi.messages == ["一", "二"] and multi.thinking_summary == "稳"
    field = _turn(crc, '{"aside":"稳","messages":["一"]}')
    assert field.messages == ["一"] and field.thinking_summary == "稳"
    assert field.thinking_self_authored is True
    prose = _turn(crc, "你好呀，今天怎么样")
    assert prose.messages == ["你好呀，今天怎么样"] and prose.thinking_summary == ""


def test_tag_block_is_never_delivered_as_message_text(monkeypatch):
    crc = _load_consumer(monkeypatch)
    turn = _turn(crc, "<think>PRIVATE</think>PUBLIC")
    assert turn.messages == ["PUBLIC"]
    assert all("PRIVATE" not in m for m in turn.messages)
    # Feature off: the block is stripped and not shown as thinking either.
    monkeypatch.setenv("FEEDLING_V2_SELF_THINKING", "0")
    off = _turn(crc, "<think>PRIVATE</think>PUBLIC")
    assert off.messages == ["PUBLIC"] and off.thinking_summary == ""


def test_resident_lanes_render_the_tag_instruction_not_the_json_field(monkeypatch):
    crc = _load_consumer(monkeypatch)
    monkeypatch.setattr(crc, "_supports_mandatory_self_thinking_v1", lambda: True)
    monkeypatch.setattr(crc, "_wake_self_thinking_allowed", lambda: True)
    tag = crc._self_thinking_tag()
    foreground = crc._foreground_self_thinking_instruction()
    assert foreground == st.instruction(tag).strip()
    assert f"<{tag}>" in foreground and f"</{tag}>" in foreground
    for locale in ("zh-Hans", "en-US"):
        wake = crc._wake_think_permission_line({"locale": locale})
        assert f"<{tag}>" in wake
        assert "aside 字段" not in wake and "aside field" not in wake
    # The JSON-field rendering is V2's contract and must not leak into V1.
    assert st.instruction_for_field(protocol="json").strip() not in foreground


def test_every_v1_wake_lane_permits_the_aside_block_including_screen_watch(monkeypatch):
    """Parity with v2/worker._wake_system_prompt_for_lane: heartbeat, perception,
    scheduled AND screen-watch wakes all permit the <aside> block; screen-watch
    also carries V2's screen-watch aside note. The V1 screen-watch prompt never
    asked for one before T687 (Seven 2026-09-22: align with Runtime V2)."""
    crc = _load_consumer(monkeypatch)
    monkeypatch.setattr(crc, "_wake_self_thinking_allowed", lambda: True)
    monkeypatch.setattr(crc, "_worldbook_context_for_wake", lambda _job: "")
    monkeypatch.setattr(crc, "_native_tool_names_compact", lambda: "")
    lanes = {
        "heartbeat": crc._message_for_proactive_job({"trigger": "heartbeat_broadcast_off"}),
        "perception": crc._message_for_proactive_job(
            {"trigger": "perception_wake"}, perception_digest=({"locale": "zh-Hans"}, [], {})
        ),
        "scheduled": crc._scheduled_wake_message({"scheduled_note": "喝茶", "timezone": "Asia/Shanghai"}),
        "screen_watch": crc._message_for_proactive_job(
            {"trigger": "screen_watch", "kind": "screen_watch", "broadcast_state": "on"},
            screen_text="ocr text",
        ),
    }
    assert crc._is_screen_watch_job({"trigger": "screen_watch", "kind": "screen_watch", "broadcast_state": "on"})
    for lane, message in lanes.items():
        assert "<aside>" in message and "</aside>" in message, lane
        assert "<think>" not in message, lane
        assert "aside 字段" not in message, lane
    assert st.SCREEN_WATCH_INSTRUCTION.strip() in lanes["screen_watch"]
    for lane in ("heartbeat", "perception", "scheduled"):
        assert st.SCREEN_WATCH_INSTRUCTION.strip() not in lanes[lane], lane
    # Switch off: no lane mentions the block or the note.
    monkeypatch.setattr(crc, "_wake_self_thinking_allowed", lambda: False)
    off = crc._message_for_proactive_job(
        {"trigger": "screen_watch", "kind": "screen_watch", "broadcast_state": "on"}, screen_text="ocr"
    )
    assert "<aside>" not in off and st.SCREEN_WATCH_INSTRUCTION.strip() not in off


# --- codex review (T687 r2): an adopted tag aside must not mask a body drop ---

ASIDE_THEN_MALFORMED_JSON = '<aside>我想把话说清楚。</aside>{"messages":["她说"你好"。"]}'
ASIDE_THEN_PROSE = "<aside>我想把话说清楚。</aside>她说\"你好\"。"
ASIDE_ONLY = "<aside>我想把话说清楚。</aside>"


def _call_agent_http_returning(crc, monkeypatch, raw: str):
    monkeypatch.setattr(crc, "AGENT_MODE", "http")
    monkeypatch.setattr(crc, "call_agent_http", lambda message, **kwargs: raw)


def test_parser_drops_the_tag_aside_when_the_body_fails_sanitization(monkeypatch):
    crc = _load_consumer(monkeypatch)
    for via in ("bare", "pi"):
        raw = ASIDE_THEN_MALFORMED_JSON if via == "bare" else _wrapped_by_pi_native_thinking(crc, ASIDE_THEN_MALFORMED_JSON)
        turn = _turn(crc, raw)
        assert turn.messages == [] and turn.actions == [] and turn.tool_calls == [], via
        assert turn.thinking_summary == "" and turn.thinking_self_authored is False, via
        assert turn.sanitizer_reason == "protocol_leak", via
    # Genuine aside-only (tag + empty body) is still deliverable as aside-only.
    only = _turn(crc, ASIDE_ONLY)
    assert only.messages == [] and only.thinking_summary == "我想把话说清楚。"
    assert only.sanitizer_reason == ""


def test_call_agent_reports_the_body_failure_instead_of_an_aside_only_success(monkeypatch):
    """Real call_agent over the HTTP helper (stubbed at the transport). With
    the fallback switch off it raises protocol_leak like HEAD; with it on it
    returns the fallback line and leaves the reply_parse_failed signal. It
    must never return an aside-only body for this input."""
    crc = _load_consumer(monkeypatch)
    _call_agent_http_returning(crc, monkeypatch, ASIDE_THEN_MALFORMED_JSON)

    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", False)
    try:
        crc.call_agent("hi", lane="chat")
    except ValueError as exc:
        assert "no usable reply after sanitization" in str(exc)
        assert getattr(exc, "sanitizer_reason", "") == "protocol_leak"
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("call_agent returned instead of raising on a dropped body")

    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", True)
    result = crc.call_agent("hi", lane="chat")
    assert result == [crc.FALLBACK_REPLY]
    failed = crc._consume_reply_parse_failed()
    assert str(failed) == "reply_parse_failed"
    assert failed.turn.sanitizer_reason == "protocol_leak"


def test_call_agent_still_delivers_aside_plus_prose_and_aside_only(monkeypatch):
    crc = _load_consumer(monkeypatch)
    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", False)
    _call_agent_http_returning(crc, monkeypatch, ASIDE_THEN_PROSE)
    body = crc.call_agent("hi", lane="chat")
    turn = crc._split_agent_turn(body)
    assert turn.messages == ['她说"你好"。']
    assert turn.thinking_summary == "我想把话说清楚。"
    assert crc._consume_reply_parse_failed() == ""

    _call_agent_http_returning(crc, monkeypatch, ASIDE_ONLY)
    body = crc.call_agent("hi", lane="chat")
    turn = crc._split_agent_turn(body)
    assert turn.messages == [] and turn.thinking_summary == "我想把话说清楚。"
    assert crc._consume_reply_parse_failed() == ""


# --- codex r2: the same invariant on the route path (valid envelope, nested bad body) ---

ASIDE_THEN_ENVELOPE_WITH_BAD_NESTED_BODY = (
    '<aside>我想把话说清楚。</aside>' + json.dumps({"messages": ['{"messages": [broken']}, ensure_ascii=False)
)
ASIDE_THEN_ENVELOPE_PARTIALLY_DELIVERED = (
    '<aside>我想把话说清楚。</aside>' + json.dumps({"messages": ["还在。", '{"messages": [broken']}, ensure_ascii=False)
)


def test_nested_bad_body_inside_a_valid_envelope_cannot_hide_behind_the_aside(monkeypatch):
    crc = _load_consumer(monkeypatch)
    for via in ("bare", "pi"):
        raw = ASIDE_THEN_ENVELOPE_WITH_BAD_NESTED_BODY
        if via == "pi":
            raw = _wrapped_by_pi_native_thinking(crc, raw)
        turn = _turn(crc, raw)
        assert turn.messages == [] and turn.actions == [] and turn.tool_calls == [], via
        assert turn.thinking_summary == "" and turn.thinking_self_authored is False, via
        assert turn.sanitizer_reason == "protocol_leak", via
    # Partial delivery keeps the surviving bubble and the aside (existing policy).
    partial = _turn(crc, ASIDE_THEN_ENVELOPE_PARTIALLY_DELIVERED)
    assert partial.messages == ["还在。"]
    assert partial.thinking_summary == "我想把话说清楚。"


def test_call_agent_fails_the_turn_for_a_nested_bad_body_in_both_fallback_modes(monkeypatch):
    crc = _load_consumer(monkeypatch)
    for via in ("bare", "pi"):
        raw = ASIDE_THEN_ENVELOPE_WITH_BAD_NESTED_BODY
        if via == "pi":
            raw = _wrapped_by_pi_native_thinking(crc, raw)
        _call_agent_http_returning(crc, monkeypatch, raw)
        monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", False)
        try:
            crc.call_agent("hi", lane="chat")
        except ValueError as exc:
            assert getattr(exc, "sanitizer_reason", "") == "protocol_leak", via
        else:  # pragma: no cover
            raise AssertionError(f"{via}: call_agent returned instead of raising")
        monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", True)
        assert crc.call_agent("hi", lane="chat") == [crc.FALLBACK_REPLY], via
        failed = crc._consume_reply_parse_failed()
        assert str(failed) == "reply_parse_failed" and failed.turn.sanitizer_reason == "protocol_leak", via
