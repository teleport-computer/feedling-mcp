"""serve_worker：screen_watch lane 的生产装配（D-screen_watch Task 4）——纯 gate + 只读
proactive oracle 的两关判定、frame-id 消费的有意修正（相对 resident）、无论结果都推进
next_screen_watch_at。不起真 worker/真 enclave/真 provider；两个 gate 输入全走明文路径。

Parity matrix 行：§B `screen-watch`（producer=`serve_worker._tick_screen_watch_for_user`）。"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import screen_watch
from model_api_runtime.v2 import serve_worker

_NOW = 10_000.0


def _wire(monkeypatch, *, frames, last_frame_id, chat_msgs, should_wake):
    """把 _tick_screen_watch_for_user 的四个明文输入 + oracle + 落库副作用全部替身化。
    返回 (enqueue_calls, notify_calls, upsert_calls)。"""
    monkeypatch.setattr(serve_worker.time, "time", lambda: _NOW)
    monkeypatch.setattr(
        serve_worker.hosted_config_store,
        "hosted_runtime_v2_enabled_strict",
        lambda store: True,
    )
    monkeypatch.setattr(serve_worker.db, "frame_list_meta", lambda uid: list(frames))
    monkeypatch.setattr(
        jobs_store, "get_wake_schedule",
        lambda uid: {"last_screen_watch_frame_id": last_frame_id})
    monkeypatch.setattr(
        serve_worker.core_store, "get_store",
        lambda uid: types.SimpleNamespace(chat_messages=list(chat_msgs)))
    monkeypatch.setattr(
        serve_worker, "_wake_decision_for_user",
        lambda uid: {"should_wake": should_wake, "wake_interval_sec": 7200, "block_reason": ""})

    enqueue_calls, notify_calls, upsert_calls = [], [], []
    monkeypatch.setattr(
        jobs_store, "enqueue_job",
        lambda u, lane, **kw: enqueue_calls.append((u, lane, kw)) or ("j1", False))
    monkeypatch.setattr(
        serve_worker.core_wake_bus, "notify",
        lambda ch, uid: notify_calls.append((ch, uid)))
    monkeypatch.setattr(
        jobs_store, "upsert_wake_schedule",
        lambda uid, **kw: upsert_calls.append((uid, kw)))
    return enqueue_calls, notify_calls, upsert_calls


def test_wakes_and_persists_the_frame_id(monkeypatch):
    """fresh + changed + not-chatting + oracle=yes -> enqueue screen_watch, notify, and
    persist last_screen_watch_frame_id=<latest> alongside the advanced next_at."""
    enqueue, notify, upsert = _wire(
        monkeypatch,
        frames=[{"filename": "frameNEW.env.json", "ts": _NOW, "app": None}],
        last_frame_id="frameOLD",
        chat_msgs=[{"role": "user", "ts": _NOW - 1000}],  # 1000s ago > 180 -> not chatting
        should_wake=True,
    )
    assert serve_worker._tick_screen_watch_for_user("u_wake") == 1
    assert enqueue == [("u_wake", "screen_watch", {"reason": "screen_watch"})]
    assert notify == [("v2_jobs", "u_wake")]
    # exactly one upsert, carrying BOTH the advanced next_at and the consumed frame id
    assert len(upsert) == 1
    uid, kw = upsert[0]
    assert uid == "u_wake"
    assert kw["next_screen_watch_at"] == _NOW + screen_watch.INTERVAL_SEC
    assert kw["last_screen_watch_frame_id"] == "frameNEW"


def test_chat_suppression_does_not_consume_the_frame_id(monkeypatch):
    """The deliberate fix vs the resident: a frame suppressed by active chat must remain
    'new' so it can still be seen once the user stops typing. should_watch -> (False,
    "chatting"); enqueue NOT called AND upsert called WITHOUT last_screen_watch_frame_id."""
    enqueue, notify, upsert = _wire(
        monkeypatch,
        frames=[{"filename": "frameNEW.env.json", "ts": _NOW, "app": None}],
        last_frame_id="frameOLD",
        chat_msgs=[{"role": "user", "ts": _NOW - 10}],  # 10s ago < 180 -> chatting
        should_wake=True,  # oracle would say yes, but gate suppresses first
    )
    assert serve_worker._tick_screen_watch_for_user("u_chat") == 0
    assert enqueue == []
    assert notify == []
    # next_at still advanced, but the frame id is NOT consumed (kwarg absent entirely)
    assert len(upsert) == 1
    uid, kw = upsert[0]
    assert kw["next_screen_watch_at"] == _NOW + screen_watch.INTERVAL_SEC
    assert "last_screen_watch_frame_id" not in kw


def test_blocked_proactive_gate_never_enqueues(monkeypatch):
    """Zero pre-activation burn: should_watch says yes, oracle says no -> no job, and the
    frame id is NOT consumed (only the next_at advances)."""
    enqueue, notify, upsert = _wire(
        monkeypatch,
        frames=[{"filename": "frameNEW.env.json", "ts": _NOW, "app": None}],
        last_frame_id="frameOLD",
        chat_msgs=[{"role": "user", "ts": _NOW - 1000}],  # not chatting
        should_wake=False,  # un-activated / Ambient-off / do-not-disturb
    )
    assert serve_worker._tick_screen_watch_for_user("u_blocked") == 0
    assert enqueue == []
    assert notify == []
    assert len(upsert) == 1
    uid, kw = upsert[0]
    assert kw["next_screen_watch_at"] == _NOW + screen_watch.INTERVAL_SEC
    assert "last_screen_watch_frame_id" not in kw


def test_next_screen_watch_at_always_advances(monkeypatch):
    """No frames at all -> gate returns (False, "no_frames"); we still advance next_at so a
    blocked user is not reconsidered on every single 30s scheduler tick."""
    enqueue, notify, upsert = _wire(
        monkeypatch,
        frames=[],  # no frames -> should_watch False, and oracle never consulted
        last_frame_id="",
        chat_msgs=[],
        should_wake=True,
    )
    assert serve_worker._tick_screen_watch_for_user("u_noframes") == 0
    assert enqueue == []
    assert len(upsert) == 1
    uid, kw = upsert[0]
    assert kw["next_screen_watch_at"] == _NOW + screen_watch.INTERVAL_SEC
    assert "last_screen_watch_frame_id" not in kw
