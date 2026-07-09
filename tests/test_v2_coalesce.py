"""V2 coalesce (§7.1): fold every unanswered user message into ONE turn.

Pure-unit (no DB, no app). Add to conftest._PURE_UNIT so a no-Postgres dev box
runs it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import coalesce as v2_coalesce  # noqa: E402


def _msg(mid, role, ts, content):
    return {"id": mid, "role": role, "ts": ts, "content": content}


def test_three_user_messages_coalesce_into_one_turn():
    # A, B, C sent after the last assistant reply → one coalesced turn.
    messages = [
        _msg("a0", "assistant", 100.0, "hi"),
        _msg("m1", "user", 101.0, "A"),
        _msg("m2", "user", 102.0, "B"),
        _msg("m3", "user", 103.0, "C"),
    ]
    since = v2_coalesce.last_replied_ts(messages)
    assert since == 100.0
    coalesced, cursor = v2_coalesce.coalesce_pending(messages, since_ts=since)
    assert [m["content"] for m in coalesced] == ["A", "B", "C"]
    assert cursor == 103.0


def test_already_replied_messages_are_excluded():
    messages = [
        _msg("m1", "user", 101.0, "old"),
        _msg("a1", "assistant", 102.0, "answered"),
        _msg("m2", "user", 103.0, "new"),
    ]
    since = v2_coalesce.last_replied_ts(messages)  # 102.0
    coalesced, cursor = v2_coalesce.coalesce_pending(messages, since_ts=since)
    assert [m["content"] for m in coalesced] == ["new"]
    assert cursor == 103.0


def test_dedupe_by_id_and_drop_empty_and_order():
    messages = [
        _msg("m2", "user", 102.0, "second"),
        _msg("m1", "user", 101.0, "first"),
        _msg("m2", "user", 102.0, "second"),   # dup id
        _msg("m3", "user", 103.0, "   "),       # empty after strip
    ]
    coalesced, cursor = v2_coalesce.coalesce_pending(messages, since_ts=0.0)
    assert [m["content"] for m in coalesced] == ["first", "second"]
    assert cursor == 102.0


def test_injected_decrypt_is_used():
    messages = [{"id": "m1", "role": "user", "ts": 5.0, "content": "CIPHER"}]
    coalesced, _ = v2_coalesce.coalesce_pending(
        messages, since_ts=0.0, decrypt=lambda m: "PLAIN")
    assert coalesced[0]["content"] == "PLAIN"
