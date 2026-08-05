"""Same-turn failure supersede: 一轮先失败后成功时,最终结果说了算。

三个面:
1. db.chat_append_effect_with_cursor —— 真回复落库事务清掉同 turn 粘住的失败章
   (父消息 reply_error_class + 失败载体 turn_failure_*),严格按 parent 键,不跨 turn。
2. jobs_store._deliver_terminal_failure_reply —— parent 已被真回复回答的,迟到失败
   气泡直接 ack 不投递。
3. chat_activity.turn_response(turn_answered=...) —— activity 投影按真回复证据让位。

背景:prod 实测(2026-08-04)图片 turn 首次 vision 抖动失败、失败气泡已投递,随后
重试成功出真回复,但失败章永不撤销 → 客户端「图片暂时没有处理成功」横幅永久卡死。
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from core import chat_activity  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed supersede tests require the PostgreSQL test fixture",
)


# ── 纯单元:activity 投影 ────────────────────────────────────────────────────

_FAILED_JOB = {"id": 7, "status": "failed", "last_error": "vision_model_unavailable"}


def test_turn_answered_suppresses_failure_projection():
    resp = chat_activity.turn_response("t1", [_FAILED_JOB], [], turn_answered=True)
    assert "failure" not in resp


def test_unanswered_failure_projection_unchanged():
    resp = chat_activity.turn_response("t1", [_FAILED_JOB], [])
    assert resp["failure"]["code"] == "vision_model_unavailable"
    assert resp["failure"]["message_id"] == "t1"


# ── DB 集成 ──────────────────────────────────────────────────────────────────


def _seq_of(db, uid: str, msg_id: str) -> int:
    with db.get_pool().connection() as conn:
        cur = conn.execute(
            "SELECT seq FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (uid, msg_id),
        )
        row = cur.fetchone()
    assert row is not None, f"missing seeded message {msg_id}"
    return int(row[0] if not isinstance(row, dict) else row["seq"])


def _seed_failed_turn(db, uid: str, parent_id: str, carrier_id: str) -> int:
    """parent + 已投递的失败载体(盖章父消息),返回 parent seq。"""
    db.chat_append(
        uid, parent_id, 10.0,
        {"id": parent_id, "role": "user", "content": "[image]"}, 1000,
    )
    parent_seq = _seq_of(db, uid, parent_id)
    seq, inserted = db.chat_append_effect_with_cursor(
        uid, carrier_id, 11.0,
        {
            "id": carrier_id,
            "role": "openclaw",
            "source": "model_api",
            "content": "我这边出了点问题,稍后再试。",
            "reply_to_message_id": parent_id,
            "turn_failure_error_class": "vision_model_unavailable",
            "turn_failure_blame": "provider_transient",
            "turn_failure_user_text": "图片暂时没有处理成功",
            "terminal_failure_job_id": "111",
        },
        1000, parent_seq, require_cursor_advance=True,
    )
    assert inserted and seq
    stamped = db.chat_get_strict(uid, parent_id)
    assert stamped.get("reply_error_class") == "vision_model_unavailable"
    return parent_seq


@requires_db
def test_late_success_clears_same_turn_failure_stamp():
    import db
    from conftest import seed_user

    uid = "u_sup1"
    seed_user(uid)
    _seed_failed_turn(db, uid, "parent1", "fail1")

    seq, inserted = db.chat_append_effect_with_cursor(
        uid, "real1", 12.0,
        {
            "id": "real1",
            "role": "openclaw",
            "source": "model_api",
            "content": "哇看到啦!飞机内部的样子~",
            "reply_to_message_id": "parent1",
        },
        1000, None,
    )
    assert inserted and seq

    parent = db.chat_get_strict(uid, "parent1")
    assert not str(parent.get("reply_error_class") or "")
    assert not str(parent.get("reply_user_text") or "")
    assert parent.get("reply_message_id") == "real1"
    assert parent.get("reply_status") == "replied"
    carrier = db.chat_get_strict(uid, "fail1")
    assert not str(carrier.get("turn_failure_error_class") or "")
    assert not str(carrier.get("turn_failure_user_text") or "")
    # 载体消息本身仍在(不删消息),只剥失败章。
    assert carrier.get("reply_to_message_id") == "parent1"


@requires_db
def test_cross_turn_failure_untouched():
    import db
    from conftest import seed_user

    uid = "u_sup2"
    seed_user(uid)
    _seed_failed_turn(db, uid, "parentA", "failA")
    db.chat_append(
        uid, "parentB", 20.0,
        {"id": "parentB", "role": "user", "content": "另一轮"}, 1000,
    )

    seq, inserted = db.chat_append_effect_with_cursor(
        uid, "realB", 21.0,
        {
            "id": "realB",
            "role": "openclaw",
            "source": "model_api",
            "content": "B 轮的回复",
            "reply_to_message_id": "parentB",
        },
        1000, None,
    )
    assert inserted and seq

    parent_a = db.chat_get_strict(uid, "parentA")
    assert parent_a.get("reply_error_class") == "vision_model_unavailable"
    carrier_a = db.chat_get_strict(uid, "failA")
    assert carrier_a.get("turn_failure_error_class") == "vision_model_unavailable"


@requires_db
def test_kill_switch_off_preserves_old_behavior(monkeypatch):
    import db
    from conftest import seed_user

    monkeypatch.setenv("FEEDLING_V2_TURN_FAILURE_SUPERSEDE", "0")
    uid = "u_sup3"
    seed_user(uid)
    _seed_failed_turn(db, uid, "parentK", "failK")

    seq, inserted = db.chat_append_effect_with_cursor(
        uid, "realK", 12.0,
        {
            "id": "realK",
            "role": "openclaw",
            "source": "model_api",
            "content": "迟到的成功",
            "reply_to_message_id": "parentK",
        },
        1000, None,
    )
    assert inserted and seq
    parent = db.chat_get_strict(uid, "parentK")
    assert parent.get("reply_error_class") == "vision_model_unavailable"


@requires_db
def test_answered_parent_gates_failure_bubble():
    import db
    from conftest import seed_user
    from model_api_runtime.v2 import jobs_store

    uid = "u_sup4"
    seed_user(uid)
    db.chat_append(
        uid, "parentG", 10.0,
        {"id": "parentG", "role": "user", "content": "hi"}, 1000,
    )
    parent_seq = _seq_of(db, uid, "parentG")
    seq, inserted = db.chat_append_effect_with_cursor(
        uid, "realG", 11.0,
        {
            "id": "realG",
            "role": "openclaw",
            "source": "model_api",
            "content": "回答了",
            "reply_to_message_id": "parentG",
        },
        1000, parent_seq,
    )
    assert inserted and seq
    parent = db.chat_get_strict(uid, "parentG")
    assert parent.get("reply_message_id") == "realG"
    assert not str(parent.get("reply_error_class") or "")

    job_id = 987654321
    # 无 outbox 行时 ack 返回 False,但关键断言是:门挡住后绝不落失败气泡。
    jobs_store._deliver_terminal_failure_reply(
        {
            "job_id": job_id,
            "user_id": uid,
            "reply_frontier_seq": parent_seq,
            "reply_parent_message_id": "parentG",
            "error_code": "turn_failed:failed",
            "error_class": "upstream_unavailable",
        }
    )
    bubble_id = hashlib.sha256(
        f"v2-terminal-failure:{job_id}".encode("utf-8")
    ).hexdigest()[:32]
    assert db.chat_get_strict(uid, bubble_id) is None


@requires_db
def test_turn_answered_by_real_reply_evidence():
    import db
    from conftest import seed_user
    from model_api_runtime.v2 import jobs_store

    uid = "u_sup5"
    seed_user(uid)
    _seed_failed_turn(db, uid, "parentE", "failE")
    # 只有失败载体 → 不算被回答。
    assert jobs_store.turn_answered_by_real_reply(uid, "parentE") is False
    db.chat_append_effect_with_cursor(
        uid, "realE", 12.0,
        {
            "id": "realE",
            "role": "openclaw",
            "source": "model_api",
            "content": "成了",
            "reply_to_message_id": "parentE",
        },
        1000, None,
    )
    assert jobs_store.turn_answered_by_real_reply(uid, "parentE") is True
