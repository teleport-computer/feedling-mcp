"""DB-backed tests for the history-search read-only queries in jobs_store.

覆盖 spec §8.2/§8.3 的存储侧：level-0 全叶子快照查询（不走 canonical
cover）、候选元数据查询的 SQL 侧可见性过滤 / 时间范围 / LIMIT / 元数据-only
契约。纯逻辑（planner/cursor）见 test_v2_history_search_unit.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
from core import store as core_store
from model_api_runtime.v2 import jobs_store


def _reset(uid: str) -> None:
    conftest.seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM v2_conversation_summary_segments WHERE user_id=%s", (uid,)
        )
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))


def _append(uid: str, index: int, *, ts: float, role: str = "user", **doc_extra) -> int:
    message_id = f"{uid}-m{index}"
    doc = {"id": message_id, "role": role, "body_ct": f"ct-{index}", "K_enclave": "k"}
    doc.update(doc_extra)
    db.chat_append_strict(uid, message_id, ts, doc, core_store.MAX_CHAT_MESSAGES)
    return int(db.chat_seq_for_msg_id(uid, message_id))


# ---------------------------------------------------------------------------
# level-0 全叶子快照查询
# ---------------------------------------------------------------------------


def test_level0_leaves_include_checkpoint_covered_children():
    uid = "u_hist_leaves"
    _reset(uid)
    seqs = [_append(uid, i, ts=float(i + 1)) for i in range(5)]

    assert jobs_store.append_summary_leaf_cas(
        uid,
        summary_envelope={"plaintext": "- leaf one"},
        head_summary_envelope={"plaintext": "- leaf one"},
        start_seq=seqs[0],
        end_seq=seqs[1],
        source_message_count=2,
        watermark_ts=2.0,
        expected_version=0,
        previous_watermark_seq=0,
    )
    assert jobs_store.append_summary_leaf_cas(
        uid,
        summary_envelope={"plaintext": "- leaf two"},
        head_summary_envelope={"plaintext": "- leaf one\n- leaf two"},
        start_seq=seqs[2],
        end_seq=seqs[3],
        source_message_count=2,
        watermark_ts=4.0,
        expected_version=1,
        previous_watermark_seq=seqs[1],
    )
    state = jobs_store.get_summary_frontier_state(uid)
    child_ids = tuple(row["segment_id"] for row in state["segments"])
    assert jobs_store.insert_summary_checkpoint(
        uid,
        summary_envelope={"plaintext": "- parent"},
        head_summary_envelope={"plaintext": "- parent"},
        level=1,
        start_seq=seqs[0],
        end_seq=seqs[3],
        source_message_count=4,
        child_segment_ids=child_ids,
        expected_version=2,
        expected_watermark_seq=seqs[3],
    )

    # canonical cover 只剩 checkpoint 一个节点……
    assert len(jobs_store.get_summary_frontier_state(uid)["segments"]) == 1
    # ……但 history 扫描提示必须拿到全部 level-0 叶子（被覆盖的也要），
    # 且不包含 level-1 checkpoint 本身。end_seq 降序（recent-first）。
    leaves = jobs_store.list_level0_summary_leaves(uid, through_seq=seqs[4])
    assert [row["level"] for row in leaves] == [0, 0]
    assert [(row["start_seq"], row["end_seq"]) for row in leaves] == [
        (seqs[2], seqs[3]),
        (seqs[0], seqs[1]),
    ]
    assert leaves[0]["summary_envelope"] == {"plaintext": "- leaf two"}
    assert leaves[0]["coverage_kind"] == "exact"

    # 快照上界过滤：through_seq 落在第一片叶子内 → 只有第一片
    early = jobs_store.list_level0_summary_leaves(uid, through_seq=seqs[1])
    assert [(row["start_seq"], row["end_seq"]) for row in early] == [(seqs[0], seqs[1])]
    assert jobs_store.list_level0_summary_leaves(uid, through_seq=0) == []


def test_level0_leaves_include_legacy_opaque_seed():
    uid = "u_hist_leaves_legacy"
    _reset(uid)
    assert jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"plaintext": "- legacy blob"},
        watermark_ts=1.0,
        expected_version=0,
        watermark_seq=900,
    )
    assert jobs_store.seed_legacy_summary_segment(
        uid, expected_version=1, translated_watermark_seq=900
    )
    leaves = jobs_store.list_level0_summary_leaves(uid, through_seq=1_000)
    assert len(leaves) == 1
    assert leaves[0]["coverage_kind"] == "legacy_opaque"
    assert leaves[0]["start_seq"] == 0
    assert leaves[0]["end_seq"] == 900
    assert leaves[0]["legacy_opaque_through_seq"] == 900


# ---------------------------------------------------------------------------
# chat_messages 候选元数据查询
# ---------------------------------------------------------------------------


def test_candidate_rows_visibility_filter_happens_in_sql_before_limit():
    uid = "u_hist_candidates"
    _reset(uid)
    visible = []
    visible.append(_append(uid, 0, ts=10.0, role="user"))
    visible.append(_append(uid, 1, ts=11.0, role="openclaw"))
    visible.append(_append(uid, 2, ts=12.0, role="human"))
    # assistant 侧三个历史 role（openclaw/assistant/agent）都可见——V1 时代的
    # 回复写 'agent'，漏掉会让老用户历史搜不到。排除项：system 角色、
    # verify/maintenance 合成流量。
    visible.append(_append(uid, 3, ts=13.0, role="agent"))
    visible.append(_append(uid, 4, ts=14.0, role="assistant"))
    _append(uid, 5, ts=15.0, role="system")
    _append(uid, 6, ts=16.0, role="user", source="verify_ping")
    _append(uid, 7, ts=17.0, role="user", source="resident_maintenance")
    tail = _append(uid, 8, ts=18.0, role="user")
    visible.append(tail)

    rows = jobs_store.chat_history_candidate_rows(
        uid, min_seq=1, max_seq=tail, limit=100
    )
    # seq 降序 = 扫描优先级顺序；被排除的行一条不出现
    assert [row["seq"] for row in rows] == sorted(visible, reverse=True)
    assert {row["role"] for row in rows} == {
        "user", "openclaw", "human", "agent", "assistant"
    }

    # LIMIT 之前过滤：limit=2 拿到的是最新两条**可见**行，而不是被一串
    # 合成行/agent 行挤掉真实候选
    top2 = jobs_store.chat_history_candidate_rows(uid, min_seq=1, max_seq=tail, limit=2)
    assert [row["seq"] for row in top2] == sorted(visible, reverse=True)[:2]


def test_candidate_rows_metadata_only_and_ciphertext_presence():
    uid = "u_hist_meta_only"
    _reset(uid)
    ok = _append(uid, 0, ts=1.0, role="user")
    # 本地-only：无 body_ct / 无 K_enclave → 不可解密（unavailable 候选）
    no_ct = f"{uid}-noct"
    db.chat_append_strict(
        uid, no_ct, 2.0, {"id": no_ct, "role": "user"}, core_store.MAX_CHAT_MESSAGES
    )
    # 有 body_ct 但 K_enclave 缺失 → 同样不可解密（serve_worker 判据）
    no_key = f"{uid}-nokey"
    db.chat_append_strict(
        uid,
        no_key,
        3.0,
        {"id": no_key, "role": "user", "body_ct": "ct"},
        core_store.MAX_CHAT_MESSAGES,
    )
    image = _append(uid, 1, ts=4.0, role="user", content_type="image")

    rows = jobs_store.chat_history_candidate_rows(uid, min_seq=1, max_seq=image, limit=10)
    by_id = {row["msg_id"]: row for row in rows}
    assert by_id[f"{uid}-m0"]["has_ciphertext"] is True
    assert by_id[no_ct]["has_ciphertext"] is False
    assert by_id[no_key]["has_ciphertext"] is False
    assert by_id[f"{uid}-m1"]["content_type"] == "image"
    assert by_id[f"{uid}-m0"]["seq"] == ok
    # 元数据-only 契约：绝不把正文/密文列带出去
    for row in rows:
        assert set(row) == {
            "seq", "msg_id", "ts", "role", "content_type", "has_ciphertext",
        }


def test_candidate_rows_seq_and_time_windows():
    uid = "u_hist_windows"
    _reset(uid)
    seqs = [_append(uid, i, ts=float(100 + i)) for i in range(6)]  # ts 100..105

    # seq 窗口：只取中段
    mid = jobs_store.chat_history_candidate_rows(
        uid, min_seq=seqs[1], max_seq=seqs[3], limit=10
    )
    assert [row["seq"] for row in mid] == [seqs[3], seqs[2], seqs[1]]

    # 时间范围：start inclusive / end exclusive
    ranged = jobs_store.chat_history_candidate_rows(
        uid, min_seq=1, max_seq=seqs[-1], start_ts=101.0, end_ts=104.0, limit=10
    )
    assert [row["ts"] for row in ranged] == [103.0, 102.0, 101.0]

    # 空窗口与非法边界
    assert jobs_store.chat_history_candidate_rows(
        uid, min_seq=seqs[3], max_seq=seqs[1], limit=10
    ) == []
    try:
        jobs_store.chat_history_candidate_rows(uid, min_seq=-1, max_seq=5, limit=10)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("negative min_seq must be rejected")
