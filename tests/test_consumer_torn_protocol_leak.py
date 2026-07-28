"""V1 resident-consumer wiring for the torn-protocol-JSON leak (B2).

Verifies `_suppress_torn_protocol_leaks` mutates an AgentTurn per lane policy,
clears the paired reasoning when the head was torn, and leaves normal turns
byte-for-byte untouched. Uses the same env-then-import bootstrap the other
consumer tests use.
"""
import os
import sys
from pathlib import Path

# Same bootstrap as test_chat_resident_consumer.py — MUST import via
# `tools.chat_resident_consumer` (not the bare `chat_resident_consumer`), or the
# two files load two copies of the module and monkeypatches cross the streams
# when run together (see FEATURE_LOG dual-import trap).
os.environ.setdefault("FEEDLING_API_URL", "http://localhost:5001")
os.environ.setdefault("FEEDLING_API_KEY", "test_key_00000000")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import tools.chat_resident_consumer as crc  # noqa: E402


# The real head that landed in the reasoning channel, and the tail that leaked
# into the bubble — one envelope, torn at the channel boundary.
HEAD = '{"messages":[],"actions":[{"type":"pro'
TAIL = 'active.sleep","reason":"7点了 还在睡 不打扰了 醒了会找我"}]}'


def _turn(messages, *, thinking=""):
    t = crc.AgentTurn()
    t.messages = list(messages)
    t.thinking_summary = thinking
    if thinking:
        t.thinking_kind = "provider_reasoning"
        t.thinking_native = True
    return t


# ---------------------------------------------------------------------------
# Proactive lane (the reported bug): torn tail must never reach the bubble.
# ---------------------------------------------------------------------------

def test_proactive_drops_torn_tail_with_reasoning_head():
    t = _turn([TAIL], thinking=HEAD)
    crc._suppress_torn_protocol_leaks(t, lane="background")
    assert t.messages == []
    # Reasoning head cleared — never render it, and the turn now reads as empty.
    assert t.thinking_summary == ""
    assert t.thinking_native is None


def test_proactive_drops_orphan_tail_even_without_reasoning():
    # No reasoning captured (head lost). Proactive still drops the weak tail.
    t = _turn([TAIL], thinking="")
    crc._suppress_torn_protocol_leaks(t, lane="background")
    assert t.messages == []


def test_proactive_keeps_real_message_drops_only_fragment():
    t = _turn(["宝贝早上好呀", TAIL], thinking="")
    crc._suppress_torn_protocol_leaks(t, lane="background")
    assert t.messages == ["宝贝早上好呀"]


# ---------------------------------------------------------------------------
# Foreground lane: strong evidence drops; a weak orphan tail is KEPT (could be a
# user pasting JSON — dropping it would eat a real message).
# ---------------------------------------------------------------------------

def test_foreground_drops_when_reasoning_head_present():
    t = _turn([TAIL], thinking=HEAD)
    crc._suppress_torn_protocol_leaks(t, lane="chat")
    assert t.messages == []
    assert t.thinking_summary == ""


def test_foreground_keeps_weak_orphan_tail():
    # No reasoning head, no transport signal -> weak. Foreground must keep it.
    t = _turn([TAIL], thinking="")
    crc._suppress_torn_protocol_leaks(t, lane="chat")
    assert t.messages == [TAIL]


def test_foreground_keeps_user_pasting_json():
    t = _turn(['删掉多余的 }，把 "port": 8080 改成 8081'], thinking="")
    crc._suppress_torn_protocol_leaks(t, lane="chat")
    assert t.messages == ['删掉多余的 }，把 "port": 8080 改成 8081']


# ---------------------------------------------------------------------------
# Normal turns: untouched (byte-for-byte), no reasoning collateral.
# ---------------------------------------------------------------------------

def test_normal_reply_untouched_both_lanes():
    for lane in ("chat", "background"):
        t = _turn(["晚安，做个好梦 🌙"], thinking="在想她今天累不累")
        crc._suppress_torn_protocol_leaks(t, lane=lane)
        assert t.messages == ["晚安，做个好梦 🌙"]
        assert t.thinking_summary == "在想她今天累不累"  # real reasoning survives


def test_empty_turn_is_noop():
    t = _turn([], thinking="")
    crc._suppress_torn_protocol_leaks(t, lane="chat")
    assert t.messages == []
