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


def test_seq_native_path_keeps_same_timestamp_messages_in_db_order():
    messages = [
        {**_msg("m2", "user", 100.0, "second"), "seq": 12},
        {**_msg("m1", "user", 100.0, "first"), "seq": 11},
        {**_msg("old", "user", 99.0, "already replied"), "seq": 10},
    ]

    coalesced, cursor = v2_coalesce.coalesce_pending(messages, since_seq=10)

    assert [(m["id"], m["seq"]) for m in coalesced] == [("m1", 11), ("m2", 12)]
    assert cursor == 12


def test_seq_native_path_preserves_current_image_routing_metadata():
    image = {
        **_msg("image-1", "user", 100.0, "这是什么东西"),
        "seq": 11,
        "has_image": True,
        "image_mime": "image/jpeg",
        "vision_route_id": "vision-route-1",
    }

    coalesced, cursor = v2_coalesce.coalesce_pending([image], since_seq=10)

    assert cursor == 11
    assert coalesced == [{
        "id": "image-1",
        "ts": 100.0,
        "content": "这是什么东西",
        "seq": 11,
        "has_image": True,
        "image_mime": "image/jpeg",
        "vision_route_id": "vision-route-1",
    }]


def test_seq_native_path_preserves_complete_voice_correlation():
    message = {
        **_msg("voice-1", "user", 100.0, "你好"),
        "seq": 11,
        "voice_call_id": "vcall_123",
        "voice_turn_id": "1",
    }

    coalesced, _ = v2_coalesce.coalesce_pending([message], since_seq=10)

    assert coalesced[0]["voice_call_id"] == "vcall_123"
    assert coalesced[0]["voice_turn_id"] == "1"


def test_seq_native_path_fails_closed_on_missing_identity():
    import pytest

    with pytest.raises(ValueError, match="seq"):
        v2_coalesce.coalesce_pending(
            [_msg("m1", "user", 100.0, "must not disappear")],
            since_seq=0,
        )


def test_coalesce_rejects_ambiguous_dual_cursor():
    import pytest

    with pytest.raises(ValueError, match="exactly one"):
        v2_coalesce.coalesce_pending([], since_ts=1.0, since_seq=1)
