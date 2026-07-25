"""V2 推送投递语义：wake_kind 标记 + 回合级推送槽位。

推送本身是 best-effort 的附加动作，这里断言的核心是「它绝不能反过来伤到消息」：
落库照旧、回合结果不受影响，而推送只在这条回复真的 applied 之后发一次。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import worker  # noqa: E402


class _FakeStore:
    user_id = "u_push_delivery"


def _fake_envelope(monkeypatch):
    """与 tests/test_v2_atomic_reply_cursor.py:589 同一个 patch 点与返回形状
    （`(envelope, error_str)`）—— 单测里不做真实 enclave 信封往返。"""
    def _build(_store, plaintext, *, item_id=None):
        return {"id": item_id, "body_ct": "ct", "nonce": "n", "K_user": "k",
                "visibility": "shared", "owner_user_id": _store.user_id}, ""

    monkeypatch.setattr(
        worker.core_envelope, "_build_shared_envelope_for_store", _build)


def test_wake_kind_is_carried_on_the_effect_payload(monkeypatch):
    _fake_envelope(monkeypatch)
    payload = worker._build_encrypted_reply_effect_payload(
        _FakeStore(), "hello", effect_id="job1:reply:0", wake_kind="heartbeat")
    assert payload["wake_kind"] == "heartbeat"


def test_chat_lane_omits_wake_kind(monkeypatch):
    _fake_envelope(monkeypatch)
    payload = worker._build_encrypted_reply_effect_payload(
        _FakeStore(), "hello", effect_id="job1:reply:0")
    assert "wake_kind" not in payload
