"""optional 重放窗口锚定的热路径行为测试。

与 tests/test_v2_tail_anchor_store.py 的分工：那里测锚点的存取（读写、单调、
CASCADE），这里测锚点如何影响 prompt 组装。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed optional-anchor tests require the PostgreSQL test fixture",
)


def _append(uid, role, text, *, source=None):
    """插入一条真实 chat 行并返回真实 seq。

    seq 是全表共享的 identity 列，绝不等于该用户自己的第几条消息。

    ``store.append_chat`` 的 doc["source"] 来自其第二个位置参数（见
    backend/chat/chat_core.py 里 `store.append_chat("user", "verify_ping",
    synthetic_env)` 的真实用法），不是 envelope 内的字段，因此这里把
    ``source`` 覆盖到那个位置参数上，而不是塞进 envelope。
    """
    from core import store as core_store

    store = core_store.get_store(uid)
    envelope = {
        "v": 1,
        "body_ct": text,
        "nonce": "n",
        "K_user": "k_test",
        "id": f"{uid}-{text}",
    }
    row_source = source or ("user_message" if role == "user" else "model_api")
    row = store.append_chat(
        role,
        row_source,
        envelope,
        strict=True,
    )
    seq = db.chat_seq_for_msg_id(uid, row["id"])
    assert seq is not None
    return seq


def test_genuine_turn_count_after_seq_counts_only_real_user_turns():
    """与 chat_recent_genuine_turn_boundary_seq 同口径：只数 user/human，
    且排除 verify_ping / resident_maintenance 合成行。"""
    seed_user("u_optanchor_cnt")
    first = _append("u_optanchor_cnt", "user", "m1")
    _append("u_optanchor_cnt", "assistant", "r1")
    _append("u_optanchor_cnt", "user", "m2")
    _append("u_optanchor_cnt", "user", "m3")
    _append("u_optanchor_cnt", "user", "ping", source="verify_ping")
    through = db.chat_max_seq("u_optanchor_cnt")

    assert db.chat_genuine_turn_count_after_seq(
        "u_optanchor_cnt", after_seq=first, through_seq=through
    ) == 2, "verify_ping 行不得计入"
    assert db.chat_genuine_turn_count_after_seq(
        "u_optanchor_cnt", after_seq=0, through_seq=through
    ) == 3


def test_anchor_hysteresis_ceiling_exceeds_target():
    """没有滞后区就退化回逐轮滑动窗口——即本次要修的 bug 本身。"""
    from model_api_runtime.v2 import worker

    assert worker._CHAT_TAIL_ANCHOR_MAX_TURNS > worker._CHAT_TAIL_MAX_TURNS
