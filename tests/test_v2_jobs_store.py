"""jobs_store：single-flight coalesce、SKIP LOCKED 独占 claim、job 生命周期。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from core import wake_bus
from model_api_runtime.v2 import jobs_store

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 jobs_store tests require the PostgreSQL test fixture",
)


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    """claim_next_job() is a GLOBAL work-queue claim (by design it doesn't filter
    by user_id — any worker can pick up any user's pending job). That means a
    pending job left behind by one test (e.g. an enqueue test that never drains
    it) pollutes `ORDER BY priority DESC, created_at` for every later test in
    this module and gets claimed instead of the row the test just created.
    Truncate the whole table before each test so claim tests only ever see
    the row(s) they set up themselves."""
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def test_enqueue_returns_job_id_and_not_coalesced_first_time():
    seed_user("u_js_1"); _reset("u_js_1")
    job_id, coalesced = jobs_store.enqueue_job("u_js_1", "chat", reason="hi")
    assert isinstance(job_id, int) and job_id > 0
    assert coalesced is False


def test_enqueue_same_user_lane_coalesces_to_existing_pending():
    seed_user("u_js_2"); _reset("u_js_2")
    first_id, first_c = jobs_store.enqueue_job("u_js_2", "chat")
    second_id, second_c = jobs_store.enqueue_job("u_js_2", "chat")
    assert second_id == first_id
    assert first_c is False and second_c is True


def test_enqueue_rejects_unknown_lane():
    seed_user("u_js_2b"); _reset("u_js_2b")
    with pytest.raises(ValueError):
        jobs_store.enqueue_job("u_js_2b", "not_a_lane")


def test_claim_moves_pending_to_claimed_and_returns_row():
    seed_user("u_js_3"); _reset("u_js_3")
    job_id, _ = jobs_store.enqueue_job("u_js_3", "chat", trace_id="t1")
    row = jobs_store.claim_next_job("worker-A")
    assert row is not None
    assert row["id"] == job_id
    assert row["status"] == "claimed"
    assert row["claimed_by"] == "worker-A"
    assert row["trace_id"] == "t1"


def test_claim_sets_fallback_deadline_when_job_has_no_deadline():
    """Robustness fix: claim_next_job's UPDATE must stamp a fallback deadline_at
    (COALESCE, same TTL idiom as mark_running) so a job stuck in 'claimed' status
    (e.g. an exception between claim and mark_running) is still reapable —
    reap_stuck_jobs only ever looks at rows where deadline_at IS NOT NULL."""
    seed_user("u_js_3b"); _reset("u_js_3b")
    job_id, _ = jobs_store.enqueue_job("u_js_3b", "chat")  # no explicit deadline_at
    row = jobs_store.claim_next_job("worker-B")
    assert row is not None
    assert row["id"] == job_id
    assert row["status"] == "claimed"
    assert row["deadline_at"] is not None


def test_claim_is_exclusive_second_claim_skips():
    # single-flight means at most one active job per (user, lane); after one claim
    # of the only pending job, a second claim finds nothing.
    seed_user("u_js_4"); _reset("u_js_4")
    jobs_store.enqueue_job("u_js_4", "chat")
    first = jobs_store.claim_next_job("w1")
    second = jobs_store.claim_next_job("w2")
    assert first is not None
    assert second is None


def test_lifecycle_running_completed_frees_singleflight_slot():
    seed_user("u_js_5"); _reset("u_js_5")
    job_id, _ = jobs_store.enqueue_job("u_js_5", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id)
    jobs_store.mark_completed(job_id)
    # completed is terminal → the partial unique index no longer covers it →
    # a new job can be enqueued fresh (not coalesced).
    new_id, coalesced = jobs_store.enqueue_job("u_js_5", "chat")
    assert new_id != job_id
    assert coalesced is False


def test_mark_failed_increments_attempt_count():
    seed_user("u_js_6"); _reset("u_js_6")
    job_id, _ = jobs_store.enqueue_job("u_js_6", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_failed(job_id, "boom")
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, attempt_count, last_error FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()
    assert row[0] == "failed"
    assert row[1] == 1
    assert row[2] == "boom"


def test_enqueue_after_failed_job_also_coalesces_free(monkeypatch=None):
    """Partial-index crux: a job in a TERMINAL status ('failed') must not block
    a fresh enqueue for the same (user, lane) — only 'pending'/'claimed'/'running'
    rows are covered by ux_agent_jobs_singleflight. A full (non-partial) unique
    index would wrongly reject/coalesce this new INSERT.
    """
    seed_user("u_js_7"); _reset("u_js_7")
    job_id, _ = jobs_store.enqueue_job("u_js_7", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_failed(job_id, "boom")
    new_id, coalesced = jobs_store.enqueue_job("u_js_7", "chat")
    assert new_id != job_id
    assert coalesced is False


def test_reap_expires_stuck_claimed_job_by_deadline():
    seed_user("u_js_7b"); _reset("u_js_7b")
    job_id, _ = jobs_store.enqueue_job("u_js_7b", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id)  # stamps deadline_at = now + RUNNING_TTL_SEC
    # reap with a "now" far in the future → deadline is in the past relative to it.
    import time
    reaped = jobs_store.reap_stuck_jobs(now=time.time() + jobs_store.RUNNING_TTL_SEC + 10)
    assert reaped == 1
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT status FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    assert row[0] == "expired"


def test_reap_leaves_fresh_running_job_alone():
    seed_user("u_js_8"); _reset("u_js_8")
    job_id, _ = jobs_store.enqueue_job("u_js_8", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id)
    reaped = jobs_store.reap_stuck_jobs()  # now=None → now(); deadline is in the future
    assert reaped == 0


def test_status_events_append_and_list_by_cursor():
    seed_user("u_js_9")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_status_events WHERE user_id='u_js_9'")
    id1 = jobs_store.append_status_event("u_js_9", "processing", label="starting")
    id2 = jobs_store.append_status_event(
        "u_js_9", "reading_memory", label="读取上下文", detail={"count": 3}
    )
    assert id2 > id1
    events = jobs_store.list_status_events("u_js_9", after_id=id1)
    assert [e["kind"] for e in events] == ["reading_memory"]
    assert events[0]["label"] == "读取上下文"
    assert events[0]["detail_json"] == {"count": 3}
    assert events[0]["id"] == id2


def test_append_status_event_fires_cross_process_chat_wake(monkeypatch):
    """FIX 2 (§9): the V2 worker writes status events from a separate process than
    the web tier holding the parked chat long-poll. append_status_event must fire
    a cross-process wake on the "chat" channel after the INSERT commits, so the
    parked poll sees intermediate status progressively instead of only at
    turn-end / on its ~30s timeout."""
    seed_user("u_js_9d")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_status_events WHERE user_id='u_js_9d'")
    calls = []
    monkeypatch.setattr(wake_bus, "notify", lambda channel, user_id="": calls.append((channel, user_id)))
    event_id = jobs_store.append_status_event("u_js_9d", "processing", label="starting")
    assert calls == [("chat", "u_js_9d")]
    # The notify is additive/best-effort — the status row itself must still land.
    events = jobs_store.list_status_events("u_js_9d", after_id=0)
    assert [e["id"] for e in events] == [event_id]
    assert events[0]["kind"] == "processing"


def test_list_status_events_delegates_to_db_primitive(monkeypatch):
    """Cross-plan amendment: jobs_store.list_status_events must not run its own SQL —
    it delegates to db.list_agent_status_events so Plan C's long-poll reads the same
    single source of truth."""
    seed_user("u_js_9b")
    calls = []

    def _fake(user_id, *, after_id=0, limit=50):
        calls.append((user_id, after_id, limit))
        return ["sentinel"]

    monkeypatch.setattr(db, "list_agent_status_events", _fake)
    result = jobs_store.list_status_events("u_js_9b", after_id=5, limit=10)
    assert result == ["sentinel"]
    assert calls == [("u_js_9b", 5, 10)]


def test_db_list_agent_status_events_primitive_reads_raw_rows():
    seed_user("u_js_9c")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_status_events WHERE user_id='u_js_9c'")
    id1 = jobs_store.append_status_event("u_js_9c", "processing", label="starting")
    id2 = jobs_store.append_status_event(
        "u_js_9c", "reading_memory", label="读取上下文", detail={"count": 3}
    )
    all_events = db.list_agent_status_events("u_js_9c")
    assert [e["id"] for e in all_events] == [id1, id2]
    after = db.list_agent_status_events("u_js_9c", after_id=id1)
    assert len(after) == 1
    assert after[0]["id"] == id2
    assert after[0]["kind"] == "reading_memory"
    assert after[0]["detail_json"] == {"count": 3}
    assert isinstance(after[0]["created_at"], float)
    limited = db.list_agent_status_events("u_js_9c", after_id=0, limit=1)
    assert [e["id"] for e in limited] == [id1]


def test_runtime_state_upsert_merges_patch():
    seed_user("u_js_10")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM runtime_state WHERE user_id='u_js_10'")
    assert jobs_store.get_runtime_state("u_js_10") == {}
    jobs_store.upsert_runtime_state("u_js_10", {"a": 1})
    merged = jobs_store.upsert_runtime_state("u_js_10", {"b": 2})
    assert merged == {"a": 1, "b": 2}
    assert jobs_store.get_runtime_state("u_js_10") == {"a": 1, "b": 2}


def test_action_queue_add_and_next_pending_in_seq_order():
    seed_user("u_aq_1"); _reset("u_aq_1")
    job_id, _ = jobs_store.enqueue_job("u_aq_1", "chat")
    ids = jobs_store.add_actions(job_id, "u_aq_1", [
        {"type": "memory_fetch", "payload": {"ids": ["m1"]}},
        {"type": "final_response", "visible": True, "requires_model_authorship": True},
    ])
    assert len(ids) == 2
    nxt = jobs_store.next_pending_action(job_id)
    assert nxt["type"] == "memory_fetch"
    assert nxt["seq"] == 0
    assert nxt["payload_json"] == {"ids": ["m1"]}


def test_action_lifecycle_done_advances_to_next():
    seed_user("u_aq_2"); _reset("u_aq_2")
    job_id, _ = jobs_store.enqueue_job("u_aq_2", "chat")
    a1, a2 = jobs_store.add_actions(job_id, "u_aq_2", [
        {"type": "memory_fetch"},
        {"type": "final_response"},
    ])
    jobs_store.mark_action_running(a1)
    jobs_store.mark_action_done(a1, {"cards": 3})
    nxt = jobs_store.next_pending_action(job_id)
    assert nxt["id"] == a2
    assert nxt["type"] == "final_response"
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, result_json FROM agent_action_queue WHERE id=%s", (a1,)
        ).fetchone()
    assert row[0] == "completed"
    assert row[1] == {"cards": 3}


def test_action_failed_and_skipped_are_terminal():
    seed_user("u_aq_3"); _reset("u_aq_3")
    job_id, _ = jobs_store.enqueue_job("u_aq_3", "chat")
    a1, a2 = jobs_store.add_actions(job_id, "u_aq_3", [{"type": "x"}, {"type": "y"}])
    jobs_store.mark_action_failed(a1, "nope")
    jobs_store.mark_action_skipped(a2)
    assert jobs_store.next_pending_action(job_id) is None
    with db.get_pool().connection() as conn:
        rows = dict(conn.execute(
            "SELECT status, count(*) FROM agent_action_queue WHERE job_id=%s GROUP BY status",
            (job_id,),
        ).fetchall())
    assert rows == {"failed": 1, "skipped": 1}


def test_invalidate_pending_actions_marks_them_and_stamps_job():
    seed_user("u_aq_4"); _reset("u_aq_4")
    job_id, _ = jobs_store.enqueue_job("u_aq_4", "chat")
    a1, a2 = jobs_store.add_actions(job_id, "u_aq_4", [{"type": "x"}, {"type": "y"}])
    jobs_store.mark_action_running(a1)
    jobs_store.mark_action_done(a1, {})
    n = jobs_store.invalidate_pending_actions(job_id, by_job_id=999)
    assert n == 1  # only the still-pending a2
    with db.get_pool().connection() as conn:
        st = conn.execute("SELECT status FROM agent_action_queue WHERE id=%s", (a2,)).fetchone()[0]
        job = conn.execute(
            "SELECT invalidated_by_job_id FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()[0]
    assert st == "invalidated"
    assert job == 999


# --- §6 admission ceiling: 三个纯读查询 (live_worker_count / inflight_job_count /
# recent_mean_service_sec) ---------------------------------------------------


def test_live_worker_count_counts_only_recent():
    jobs_store.record_worker_heartbeat("w-fresh-1")
    jobs_store.record_worker_heartbeat("w-fresh-2")
    # 塞一个陈旧心跳（beat_at 在窗口外）
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_worker_heartbeats (worker_id, beat_at) "
            "VALUES (%s, now() - make_interval(secs => %s)) "
            "ON CONFLICT (worker_id) DO UPDATE SET beat_at = EXCLUDED.beat_at",
            ("w-stale", 120),
        )
    assert jobs_store.live_worker_count(within_sec=30) >= 2
    # 陈旧的不计入
    n_wide = jobs_store.live_worker_count(within_sec=300)
    n_narrow = jobs_store.live_worker_count(within_sec=30)
    assert n_wide > n_narrow


def test_inflight_job_count_counts_active_states():
    seed_user("u_js_11"); _reset("u_js_11")
    before = jobs_store.inflight_job_count()
    jobs_store.enqueue_job("u_js_11", "chat", reason="t")
    assert jobs_store.inflight_job_count() == before + 1


def test_recent_mean_service_sec_none_without_history():
    # 全新 lane，无 completed job
    assert jobs_store.recent_mean_service_sec(lane="no-such-lane") is None


def test_recent_mean_service_sec_averages_completed():
    seed_user("u_js_12")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, started_at, finished_at) "
            "VALUES (%s,'svc-test','completed', now() - make_interval(secs=>10), now())",
            ("u_js_12",),
        )
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, started_at, finished_at) "
            "VALUES (%s,'svc-test','completed', now() - make_interval(secs=>20), now())",
            ("u_js_12",),
        )
    mean = jobs_store.recent_mean_service_sec(lane="svc-test", limit=50)
    assert mean is not None
    assert 14.0 <= mean <= 16.0  # (10+20)/2 = 15


# --- kind discriminator: genesis heartbeats must be invisible to the chat/send
# admission gate (workers_alive / live_worker_count read only kind='turn') ---


def _clear_heartbeats():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_worker_heartbeats")


def test_genesis_heartbeat_does_not_inflate_turn_worker_liveness():
    """A genesis heartbeat row must be invisible to the chat/send admission gate.

    live_worker_count() feeds admission.estimate_wait_sec(workers=...); counting a
    genesis row as a turn worker would halve the estimated queue wait for a
    single-process pool and over-admit onto turn slots that do not exist.
    """
    _clear_heartbeats()
    jobs_store.record_worker_heartbeat("w1")                      # default kind='turn'
    jobs_store.record_worker_heartbeat("w1:genesis", kind="genesis")

    assert jobs_store.live_worker_count() == 1
    assert jobs_store.workers_alive() is True
    assert jobs_store.genesis_worker_alive() is True


def test_genesis_heartbeat_alone_does_not_open_the_send_gate():
    """Genesis alive but every turn worker dead => send must still 503."""
    _clear_heartbeats()
    jobs_store.record_worker_heartbeat("only:genesis", kind="genesis")

    assert jobs_store.workers_alive() is False
    assert jobs_store.live_worker_count() == 0
    assert jobs_store.genesis_worker_alive() is True


def test_genesis_worker_alive_false_when_nothing_beats():
    _clear_heartbeats()
    assert jobs_store.genesis_worker_alive() is False
