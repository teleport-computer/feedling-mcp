"""Pure-unit tests for the voice hangup summary module (no DB)."""

from __future__ import annotations

from voice import summary


def test_summary_messages_render_client_transcript_in_order():
    turns = [
        {"role": "user", "text": "我下周搬家"},
        {"role": "assistant", "text": "需要我帮你列清单吗"},
        {"role": "user", "text": "要退租还要找搬家公司"},
    ]
    messages = summary.build_summary_messages(turns)
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    body = messages[1]["content"]
    assert body.index("User: 我下周搬家") < body.index("Assistant: 需要我帮你列清单吗")
    assert body.index("需要我帮你列清单吗") < body.index("要退租还要找搬家公司")


def test_summary_messages_skip_blank_and_malformed_turns():
    turns = [
        {"role": "user", "text": "  "},
        "not-a-dict",
        {"role": "assistant", "text": "只有这句"},
    ]
    body = summary.build_summary_messages(turns)[1]["content"]
    assert "只有这句" in body
    assert body.count("User:") == 0
    assert body.count("Assistant:") == 1


def test_summary_message_id_is_deterministic_per_call():
    a = summary.summary_message_id("vcall_abc")
    assert a == summary.summary_message_id("vcall_abc")
    assert a != summary.summary_message_id("vcall_def")
