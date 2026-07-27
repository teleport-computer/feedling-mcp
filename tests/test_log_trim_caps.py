"""High-frequency user_logs streams must stay bounded.

Each append-only stream that is written on a background/per-turn cadence has a
``log_trim`` call right after its append so the row count can't grow without
bound (the chronic-bloat fix). These tests drive each call site past a
deliberately tiny cap and assert the stream is trimmed to the newest N.

The db-level ``log_trim`` mechanics are covered by test_db.py; here we only
verify the call sites are wired and honour their configurable cap.
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from core import store as core_store  # noqa: E402
from perception import store as perception_store  # noqa: E402
from hosted import config_store  # noqa: E402

from conftest import seed_user  # noqa: E402

# This module doesn't import app, so the schema isn't created as a side effect
# of import the way the app-importing test modules get it. Create it explicitly
# (idempotent) against the conftest-provisioned throwaway DB.
db.init_schema()


def _uid() -> str:
    return "usr_" + uuid.uuid4().hex[:12]


def test_gate_decision_stream_is_trimmed(monkeypatch):
    monkeypatch.setattr(core_store, "GATE_DECISION_MAX", 5)
    store = core_store.UserStore(_uid())
    seed_user(store.user_id)
    for i in range(12):
        store.append_gate_decision({"decision_id": f"gd_{i}", "ts": float(i)})
    kept = db.log_read_all(store.user_id, "gate_decisions")
    assert [d["decision_id"] for d in kept] == [f"gd_{i}" for i in range(7, 12)]


def test_gate_review_stream_is_trimmed(monkeypatch):
    monkeypatch.setattr(core_store, "GATE_REVIEW_MAX", 3)
    store = core_store.UserStore(_uid())
    seed_user(store.user_id)
    for i in range(9):
        store.append_gate_review({"review_id": f"gr_{i}", "ts": float(i)})
    kept = db.log_read_all(store.user_id, "gate_reviews")
    assert [r["review_id"] for r in kept] == [f"gr_{i}" for i in range(6, 9)]


def test_perception_event_stream_is_trimmed(monkeypatch):
    monkeypatch.setattr(perception_store, "EVENT_MAX", 4)
    uid = _uid()
    seed_user(uid)
    for i in range(10):
        perception_store.append_event(uid, {"n": i, "ts": float(i)}, float(i))
    kept = db.log_read_all(uid, perception_store.EVENT_STREAM)
    assert [e["n"] for e in kept] == [6, 7, 8, 9]


def test_app_usage_stream_is_trimmed(monkeypatch):
    monkeypatch.setattr(perception_store, "APP_USAGE_MAX", 4)
    uid = _uid()
    seed_user(uid)
    for i in range(10):
        perception_store.append_app_open(uid, {"n": i, "ts": float(i)}, float(i))
    kept = db.log_read_all(uid, perception_store.APP_USAGE_STREAM)
    assert [e["n"] for e in kept] == [6, 7, 8, 9]


def test_app_close_stream_is_trimmed(monkeypatch):
    monkeypatch.setattr(perception_store, "APP_CLOSE_MAX", 4)
    uid = _uid()
    seed_user(uid)
    for i in range(10):
        perception_store.append_app_close(uid, {"n": i, "ts": float(i)}, float(i))
    kept = db.log_read_all(uid, perception_store.APP_CLOSE_STREAM)
    assert [e["n"] for e in kept] == [6, 7, 8, 9]


def test_app_close_stream_is_separate_from_app_usage(monkeypatch):
    """两条流互不干扰：close 的写入不占用也不污染 open 的流。"""
    uid = _uid()
    seed_user(uid)
    perception_store.append_app_open(uid, {"app": "douyin", "ts": 1.0}, 1.0)
    perception_store.append_app_close(uid, {"app": "douyin", "ts": 2.0}, 2.0)
    opens = db.log_read_all(uid, perception_store.APP_USAGE_STREAM)
    closes = db.log_read_all(uid, perception_store.APP_CLOSE_STREAM)
    assert [e["ts"] for e in opens] == [1.0]
    assert [e["ts"] for e in closes] == [2.0]


def test_log_trim_only_statuses_keeps_non_terminal_rows():
    # An in-flight (non-terminal) row must survive trim regardless of age — only
    # rows whose status is in ``only_statuses`` are eligible for deletion.
    uid = _uid()
    seed_user(uid)
    db.log_append(uid, "s", {"id": "inflight", "status": "queued"}, ts=0.0)
    for i in range(8):
        db.log_append(uid, "s", {"id": f"done_{i}", "status": "ok"}, ts=float(i + 1))
    db.log_trim(uid, "s", 3, only_statuses=["ok", "completed", "failed", "skipped"])
    kept = {r["id"] for r in db.log_read_all(uid, "s")}
    # The oldest row is non-terminal → kept despite being well outside newest-3.
    assert "inflight" in kept
    # Terminal rows are trimmed to the newest 3.
    assert {"done_5", "done_6", "done_7"} <= kept
    assert "done_0" not in kept


def test_action_trace_trim_preserves_queued_until_patched(monkeypatch):
    # Regression for the Codex P2: a queued background trace appended first must
    # not be evicted by the cap before its completion patch lands, even when many
    # later terminal traces push it past MODEL_API_ACTION_TRACE_MAX.
    monkeypatch.setattr(config_store, "MODEL_API_ACTION_TRACE_MAX", 3)
    store = core_store.UserStore(_uid())
    seed_user(store.user_id)
    config_store._append_model_api_action_trace(store, {"trace_id": "mat_inflight", "status": "queued"})
    for i in range(6):
        config_store._append_model_api_action_trace(store, {"trace_id": f"mat_{i}", "status": "ok"})
    ids = {t["trace_id"] for t in db.log_read_all(store.user_id, config_store.MODEL_API_ACTION_TRACE_STREAM)}
    assert "mat_inflight" in ids  # survived despite being the oldest row
    # The completion patch still finds the row (would be None if it were trimmed).
    patched = config_store._patch_model_api_action_trace(store, "mat_inflight", {"status": "completed"})
    assert patched is not None and patched["status"] == "completed"
