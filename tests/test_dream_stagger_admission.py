"""Nightly Dream burst protection: per-user stagger + fleet admission ceiling.

Prod 09-10 / 09-13: every user's Dream became due at the same second (window
start) and the burst of whole-garden card reads coincided with enclave decrypt
timeouts. These tests pin the two guards in the shared Dream gate, which both
the resident V1 path (``/v1/capture/tick`` → legacy ``proactive_jobs``) and the
Runtime V2 scheduler (``serve_worker._tick_dream_for_user`` → ``agent_jobs``)
go through.
"""
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from model_api_runtime.v2 import jobs_store, serve_worker  # noqa: E402
from proactive import capture_jobs  # noqa: E402
from proactive import dream_scheduler  # noqa: E402

# 2026-09-10 02:00:00 UTC — the window start of one of the incident nights.
WINDOW_START = datetime(2026, 9, 10, 2, 0, 0, tzinfo=timezone.utc).timestamp()


def _memory(user_id: str, memory_id: str) -> dict:
    at = "2026-06-20T00:00:00Z"
    return {
        "v": 1,
        "id": memory_id,
        "type": "fact",
        "owner_user_id": user_id,
        "visibility": "shared",
        "body_ct": f"ct_{memory_id}",
        "nonce": f"nonce_{memory_id}",
        "K_user": f"ku_{memory_id}",
        "K_enclave": f"ke_{memory_id}",
        "occurred_at": at,
        "created_at": at,
        "updated_at": at,
        "status": "active",
        "importance": 0.6,
        "pulse": 0.3,
    }


def _dream_jobs(store) -> list[dict]:
    return [
        row for row in store.list_proactive_jobs(since_epoch=0, limit=0)
        if row.get("job_kind") == "memory_dream"
    ]


@pytest.fixture()
def dream_env(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_DREAM_TIMEZONE", "UTC")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_NEW_CARDS", "1")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_INTERVAL_SEC", "0")
    for name in (
        "FEEDLING_DREAM_NIGHT_ONLY",
        "FEEDLING_DREAM_NIGHT_START_HOUR",
        "FEEDLING_DREAM_NIGHT_END_HOUR",
        "FEEDLING_DREAM_STAGGER",
        "FEEDLING_DREAM_MAX_CONCURRENT",
    ):
        monkeypatch.delenv(name, raising=False)
    core_store._stores.clear()
    yield


def _user_with_cards(user_id: str):
    seed_user(user_id)
    db.memory_replace_all(user_id, [_memory(user_id, f"mem_{user_id}_{i}") for i in range(3)])
    store = core_store.UserStore(user_id)
    # The product default zone is not UTC; pin it so WINDOW_START is 02:00 local.
    store.save_proactive_settings({"timezone": "UTC"})
    return store


# ---------------------------------------------------------------------------
# Stagger: pure, deterministic, well spread, margin kept
# ---------------------------------------------------------------------------


def test_stagger_offset_is_a_stable_function_of_user_and_night(monkeypatch, dream_env):
    first = dream_scheduler.dream_stagger_offset_sec("usr_stable", "2026-09-10")
    # The clock must not matter (not a per-tick draw)...
    monkeypatch.setattr(dream_scheduler.time, "time", lambda: 9_999_999_999.0)
    assert dream_scheduler.dream_stagger_offset_sec("usr_stable", "2026-09-10") == first
    # ...and the value is pinned, so a restart / another process agrees.
    assert first == 5095
    assert dream_scheduler.dream_stagger_offset_sec("usr_other", "2026-09-10") != first


def test_stagger_slot_is_fixed_within_a_night_and_rotates_across_nights(dream_env):
    store = type("S", (), {"user_id": "usr_rotate", "load_proactive_settings": lambda self: {}})()
    ticks = [WINDOW_START + delta for delta in (0, 1800, 3 * 3600 - 1)]
    assert {dream_scheduler._night_key(store, now=t) for t in ticks} == {"2026-09-10"}
    assert dream_scheduler._night_key(store, now=WINDOW_START + 86400) == "2026-09-11"

    # Fairness under saturation: a user in the latest quarter of the span one
    # night is not pinned there. Over 30 nights 200 users each land in the late
    # quarter about a quarter of the time — nobody loses every night.
    span = dream_scheduler.dream_stagger_span_sec()
    nights = [f"2026-09-{day:02d}" for day in range(1, 31)]
    late_share = []
    for i in range(200):
        late = sum(
            dream_scheduler.dream_stagger_offset_sec(f"usr_fair_{i}", night) >= span * 3 // 4
            for night in nights
        )
        late_share.append(late / len(nights))
    assert max(late_share) < 0.6, max(late_share)
    assert abs(sum(late_share) / len(late_share) - 0.25) < 0.05


def test_night_key_keeps_one_night_across_midnight_for_wrapping_windows(monkeypatch, dream_env):
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_START_HOUR", "23")
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_END_HOUR", "2")
    store = type("S", (), {"user_id": "usr_wrap_key", "load_proactive_settings": lambda self: {}})()
    before = datetime(2026, 9, 9, 23, 30, 0, tzinfo=timezone.utc).timestamp()
    after = datetime(2026, 9, 10, 1, 30, 0, tzinfo=timezone.utc).timestamp()
    assert dream_scheduler._night_key(store, now=before) == "2026-09-09"
    assert dream_scheduler._night_key(store, now=after) == "2026-09-09"


def test_stagger_spreads_users_over_the_window_and_keeps_the_retry_tail(dream_env):
    # Default window 02:00-05:00: first attempts spread over the first 90 min,
    # the last 90 min stay free for failure-backoff retries (10+20+40 min).
    span = dream_scheduler.dream_stagger_span_sec()
    assert span == 3 * 3600 - 5400

    offsets = [dream_scheduler.dream_stagger_offset_sec(f"usr_{i:05d}", "2026-09-10") for i in range(3000)]
    assert all(0 <= offset < span for offset in offsets)
    buckets = Counter(offset * 6 // span for offset in offsets)  # six 15-min buckets
    assert sorted(buckets) == list(range(6))
    expected = len(offsets) / 6
    assert all(abs(count - expected) < expected * 0.15 for count in buckets.values()), buckets


def test_stagger_span_follows_custom_and_wrapping_windows(monkeypatch, dream_env):
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_START_HOUR", "2")
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_END_HOUR", "3")
    assert dream_scheduler.dream_stagger_span_sec() == 1800  # margin capped at half

    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_START_HOUR", "23")
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_END_HOUR", "2")
    assert dream_scheduler.dream_stagger_span_sec() == 3 * 3600 - 5400

    store = type("S", (), {"user_id": "usr_wrap", "load_proactive_settings": lambda self: {}})()
    after_midnight = datetime(2026, 9, 10, 0, 30, 0, tzinfo=timezone.utc).timestamp()
    assert dream_scheduler._seconds_into_night_window(store, now=after_midnight) == 5400


# ---------------------------------------------------------------------------
# Stagger gate on the real resident V1 path (Postgres, legacy proactive_jobs)
# ---------------------------------------------------------------------------


def test_user_whose_offset_has_not_arrived_is_not_enqueued(dream_env):
    user_id = "usr_dream_stagger_gate"
    store = _user_with_cards(user_id)
    offset = dream_scheduler.dream_stagger_offset_sec(user_id, "2026-09-10")
    assert offset > 120, "pick a user id whose offset leaves room before it"

    early = dream_scheduler.tick_memory_dream(store, now=WINDOW_START + offset - 60)
    assert early["enqueued"] is False
    assert early["reason"] == "dream_stagger_not_due"
    assert _dream_jobs(store) == []

    due = dream_scheduler.tick_memory_dream(store, now=WINDOW_START + offset + 1)
    assert due["enqueued"] is True, due["reason"]
    assert len(_dream_jobs(store)) == 1


def test_stagger_kill_switch_restores_window_start_behaviour(monkeypatch, dream_env):
    monkeypatch.setenv("FEEDLING_DREAM_STAGGER", "0")
    store = _user_with_cards("usr_dream_stagger_off")
    out = dream_scheduler.tick_memory_dream(store, now=WINDOW_START)
    assert out["enqueued"] is True, out["reason"]


def test_force_bypasses_stagger(dream_env):
    store = _user_with_cards("usr_dream_stagger_force")
    assert dream_scheduler.dream_stagger_offset_sec("usr_dream_stagger_force", "2026-09-10") > 0
    out = dream_scheduler.tick_memory_dream(store, now=WINDOW_START, force=True)
    assert out["enqueued"] is True, out["reason"]


def test_outside_the_window_the_reason_is_still_night_not_due(dream_env):
    store = _user_with_cards("usr_dream_stagger_day")
    out = dream_scheduler.tick_memory_dream(store, now=WINDOW_START + 12 * 3600)
    assert out["reason"] == "night_not_due"


# ---------------------------------------------------------------------------
# Admission ceiling with real jobs in Postgres (V1 legacy rows + V2 agent_jobs)
# ---------------------------------------------------------------------------


@pytest.fixture()
def no_v2_jobs():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")


def _active_count() -> int:
    return dream_scheduler.active_dream_job_count()


def test_active_count_sees_v1_and_v2_dreams_but_not_orphans_or_terminal(dream_env, no_v2_jobs):
    baseline = _active_count()
    now = time.time()

    live_v1 = _user_with_cards("usr_dream_cap_live_v1")
    capture_jobs.enqueue_memory_dream_job(live_v1, trigger="nightly_dream", dream_key="k1", now=now)
    assert _active_count() == baseline + 1

    orphan = _user_with_cards("usr_dream_cap_orphan_v1")
    capture_jobs.enqueue_memory_dream_job(
        orphan, trigger="nightly_dream", dream_key="k2",
        now=now - dream_scheduler.DREAM_ADMISSION_LEGACY_HORIZON_SEC - 60,
    )
    assert _active_count() == baseline + 1, "an hour-old active V1 row is an orphan"

    done = _user_with_cards("usr_dream_cap_done_v1")
    job, _, _ = capture_jobs.enqueue_memory_dream_job(done, trigger="nightly_dream", dream_key="k3", now=now)
    done.update_proactive_job(job["job_id"], {"status": "completed"})
    assert _active_count() == baseline + 1

    seed_user("usr_dream_cap_v2")
    jobs_store.enqueue_job("usr_dream_cap_v2", "dream", reason="nightly_dream")
    seed_user("usr_dream_cap_v2_capture")
    jobs_store.enqueue_job("usr_dream_cap_v2_capture", "capture", reason="quiet_timeout")
    # A fresh pending V2 dream is claimed as soon as the pool has room: it is
    # load already on its way (Codex review 2026-09-15). Other lanes never count.
    assert _active_count() == baseline + 2
    _set_v2_status("usr_dream_cap_v2", "pending",
                   age_sec=dream_scheduler.DREAM_ADMISSION_LEGACY_HORIZON_SEC + 60)
    assert _active_count() == baseline + 1, "a pending row past the horizon is a stalled queue"
    _set_v2_status("usr_dream_cap_v2", "running")
    _set_v2_status("usr_dream_cap_v2_capture", "running")
    assert _active_count() == baseline + 2
    _set_v2_status("usr_dream_cap_v2", "claimed")
    assert _active_count() == baseline + 2


def _set_v2_status(user_id: str, status: str, *, age_sec: float = 0.0) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status=%s, "
            "created_at = now() - make_interval(secs => %s) WHERE user_id=%s",
            (status, float(age_sec), user_id),
        )


def test_stale_pending_v2_dreams_do_not_block_v1_dream(monkeypatch, dream_env, no_v2_jobs):
    """Review repro: a drained V2 queue left four 10h-old pending dream jobs and
    every V1 tick answered dream_concurrency_cap, fleet-wide, forever."""
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    baseline = _active_count()
    for i in range(4):
        user_id = f"usr_dream_stale_v2_{i}"
        seed_user(user_id)
        jobs_store.enqueue_job(user_id, "dream", reason="nightly_dream")
        _set_v2_status(user_id, "pending", age_sec=10 * 3600)
    monkeypatch.setenv("FEEDLING_DREAM_MAX_CONCURRENT", str(baseline + 4))

    v1_user = _user_with_cards("usr_dream_stale_v2_v1_waiting")
    out = dream_scheduler.tick_memory_dream(v1_user, now=time.time())
    assert out["enqueued"] is True, out["reason"]


def test_client_supplied_now_cannot_pin_or_hide_a_v1_slot(monkeypatch, dream_env, no_v2_jobs):
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    baseline = _active_count()
    stale_client = _user_with_cards("usr_dream_cap_client_past")
    out = dream_scheduler.tick_memory_dream(stale_client, now=time.time() - 3 * 3600)
    assert out["enqueued"] is True, out["reason"]
    assert _active_count() == baseline + 1, "a past client clock must not hide a live job"
    future_client = _user_with_cards("usr_dream_cap_client_future")
    out = dream_scheduler.tick_memory_dream(future_client, now=time.time() + 30 * 86400)
    assert out["enqueued"] is True, out["reason"]
    job = _dream_jobs(future_client)[0]
    assert float(job["ts"]) <= time.time() + 1, "a future client clock must not pin a slot"


def test_ceiling_blocks_v1_enqueue_until_a_slot_frees(monkeypatch, dream_env, no_v2_jobs):
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    baseline = _active_count()
    now = time.time()
    holders = []
    for i in range(2):
        holder = _user_with_cards(f"usr_dream_cap_holder_{i}")
        job, enqueued, _ = capture_jobs.enqueue_memory_dream_job(
            holder, trigger="nightly_dream", dream_key=f"hold{i}", now=now,
        )
        assert enqueued
        holders.append((holder, job))
    monkeypatch.setenv("FEEDLING_DREAM_MAX_CONCURRENT", str(baseline + 2))

    waiting = _user_with_cards("usr_dream_cap_waiting")
    capped = dream_scheduler.tick_memory_dream(waiting, now=now)
    assert capped["enqueued"] is False
    assert capped["reason"] == "dream_concurrency_cap"
    assert _dream_jobs(waiting) == []
    assert dream_scheduler.load_dream_state(waiting)["pending_dream_key"] == ""

    holder, job = holders[0]
    holder.update_proactive_job(job["job_id"], {"status": "failed"})
    admitted = dream_scheduler.tick_memory_dream(waiting, now=now + 1)
    assert admitted["enqueued"] is True, admitted["reason"]
    assert len(_dream_jobs(waiting)) == 1


def test_ceiling_zero_is_a_kill_switch_and_force_bypasses(monkeypatch, dream_env, no_v2_jobs):
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    seed_user("usr_dream_cap_ks_v2")
    jobs_store.enqueue_job("usr_dream_cap_ks_v2", "dream", reason="nightly_dream")
    _set_v2_status("usr_dream_cap_ks_v2", "running")
    monkeypatch.setenv("FEEDLING_DREAM_MAX_CONCURRENT", str(_active_count()))

    forced = _user_with_cards("usr_dream_cap_forced")
    assert dream_scheduler.tick_memory_dream(forced, now=time.time())["reason"] == "dream_concurrency_cap"
    assert dream_scheduler.tick_memory_dream(forced, now=time.time(), force=True)["enqueued"] is True

    monkeypatch.setenv("FEEDLING_DREAM_MAX_CONCURRENT", "0")
    unlimited = _user_with_cards("usr_dream_cap_unlimited")
    assert dream_scheduler.tick_memory_dream(unlimited, now=time.time())["enqueued"] is True


def test_ceiling_count_failure_admits(monkeypatch, dream_env):
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")

    def _boom(**_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(dream_scheduler.db, "memory_dream_active_job_count", _boom)
    store = _user_with_cards("usr_dream_cap_count_fails")
    assert dream_scheduler.tick_memory_dream(store, now=time.time())["enqueued"] is True


def test_admission_held_elsewhere_answers_busy_and_lock_failure_admits(monkeypatch, dream_env, no_v2_jobs):
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    store = _user_with_cards("usr_dream_admission_busy")
    with db.memory_dream_admission_lock() as held:
        assert held is True
        busy = dream_scheduler.tick_memory_dream(store, now=time.time())
    assert busy["enqueued"] is False and busy["reason"] == "dream_admission_busy"
    assert _dream_jobs(store) == []
    assert dream_scheduler.tick_memory_dream(store, now=time.time())["enqueued"] is True

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(dream_scheduler.db, "memory_dream_admission_lock", _boom)
    other = _user_with_cards("usr_dream_admission_lock_fails")
    assert dream_scheduler.tick_memory_dream(other, now=time.time())["enqueued"] is True


def test_ceiling_blocks_the_v2_scheduler_producer(monkeypatch, dream_env, no_v2_jobs):
    """Runtime V2: the serve_worker Dream producer goes through the same gate,
    and V2 dream jobs a worker holds (claimed/running) count toward the ceiling."""
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    serve_worker.wire_assembly()
    for i in range(2):
        seed_user(f"usr_dream_v2_busy_{i}")
        jobs_store.enqueue_job(f"usr_dream_v2_busy_{i}", "dream", reason="nightly_dream")
        _set_v2_status(f"usr_dream_v2_busy_{i}", "running")
    baseline_v1 = _active_count() - 2
    monkeypatch.setenv("FEEDLING_DREAM_MAX_CONCURRENT", str(baseline_v1 + 2))

    user_id = "usr_dream_v2_waiting"
    _user_with_cards(user_id)

    def _v2_dream_rows():
        with db.get_pool().connection() as conn:
            return conn.execute(
                "SELECT count(*) FROM agent_jobs WHERE user_id=%s AND lane='dream'", (user_id,),
            ).fetchone()[0]

    assert serve_worker._tick_dream_for_user(user_id) == 0
    assert _v2_dream_rows() == 0

    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='completed', finished_at=now() "
            "WHERE user_id='usr_dream_v2_busy_0'"
        )
    assert serve_worker._tick_dream_for_user(user_id) == 1
    assert _v2_dream_rows() == 1


def test_pending_v2_flood_cannot_be_claimed_past_the_ceiling(monkeypatch, dream_env, no_v2_jobs):
    """Codex review 2026-09-15 (I1).

    Before: only claimed/running V2 Dreams held a slot, so while nothing was
    claimed yet every due user was admitted as ``pending``; the heavy pool then
    claimed all of them at once, far past FEEDLING_DREAM_MAX_CONCURRENT.
    After: fresh pending rows hold a slot, so admission stops at the ceiling and
    claiming everything that was admitted stays within it.
    """
    from conftest import set_v2_runtime_owner

    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    serve_worker.wire_assembly()
    baseline = _active_count()
    slots = 2
    monkeypatch.setenv("FEEDLING_DREAM_MAX_CONCURRENT", str(baseline + slots))

    users = [f"usr_dream_flood_v2_{i}" for i in range(6)]
    admitted = 0
    for user_id in users:
        _user_with_cards(user_id)
        set_v2_runtime_owner(user_id)
        admitted += serve_worker._tick_dream_for_user(user_id)
    assert admitted == slots

    claimed = []
    while True:
        job = jobs_store.claim_next_job("dream-flood-worker", lanes={"dream"})
        if job is None:
            break
        claimed.append(job)
    assert len(claimed) == slots
    assert _active_count() == baseline + slots


def test_concurrent_producers_cannot_share_one_free_slot(monkeypatch, dream_env, no_v2_jobs):
    """Codex review 2026-09-15 (I1): V1 ticks (every backend worker) and the V2
    scheduler run at the same time. Before: each read ``active < cap`` and then
    enqueued separately, so producers that counted together all got in.
    After: count + enqueue is one decision under a fleet-wide lock.

    The barrier holds every producer that reaches the count until all of them
    have (or a short timeout passes), which is exactly the interleaving that
    used to overshoot.
    """
    import threading

    from conftest import set_v2_runtime_owner

    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    serve_worker.wire_assembly()
    baseline = _active_count()
    monkeypatch.setenv("FEEDLING_DREAM_MAX_CONCURRENT", str(baseline + 1))

    v1_users = [_user_with_cards(f"usr_dream_race_v1_{i}") for i in range(3)]
    v2_users = []
    for i in range(3):
        user_id = f"usr_dream_race_v2_{i}"
        _user_with_cards(user_id)
        set_v2_runtime_owner(user_id)
        v2_users.append(user_id)

    producers = len(v1_users) + len(v2_users)
    barrier = threading.Barrier(producers, timeout=2.0)
    real_count = dream_scheduler.active_dream_job_count

    def _count_then_wait():
        active = real_count()
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return active

    monkeypatch.setattr(dream_scheduler, "active_dream_job_count", _count_then_wait)
    results: list = []
    lock = threading.Lock()

    def _run(fn):
        out = fn()
        with lock:
            results.append(out)

    threads = [
        threading.Thread(target=_run, args=(lambda s=s: dream_scheduler.tick_memory_dream(
            s, now=time.time())["enqueued"],))
        for s in v1_users
    ] + [
        threading.Thread(target=_run, args=(lambda u=u: bool(serve_worker._tick_dream_for_user(u)),))
        for u in v2_users
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == producers
    monkeypatch.setattr(dream_scheduler, "active_dream_job_count", real_count)
    assert sum(bool(r) for r in results) <= 1
    assert _active_count() <= baseline + 1


# ---------------------------------------------------------------------------
# Observability: a due user still capped when the window closes
# ---------------------------------------------------------------------------


def _window_missed_events(monkeypatch):
    events = []
    real = dream_scheduler.debug_trace.trace_event

    def _capture(store, **kwargs):
        if kwargs.get("type") == "memory.dream.window_missed":
            events.append(kwargs)
            return None
        return real(store, **kwargs)

    monkeypatch.setattr(dream_scheduler.debug_trace, "trace_event", _capture)
    return events


def test_capped_until_window_end_emits_one_content_free_event(monkeypatch, dream_env, no_v2_jobs):
    monkeypatch.setenv("FEEDLING_DREAM_STAGGER", "0")
    events = _window_missed_events(monkeypatch)
    store = _user_with_cards("usr_dream_window_missed")
    monkeypatch.setattr(dream_scheduler, "_admission_ceiling_reached", lambda: True)

    capped = dream_scheduler.tick_memory_dream(store, now=WINDOW_START + 3 * 3600 - 30)
    assert capped["reason"] == "dream_concurrency_cap"
    assert events == []

    closed = dream_scheduler.tick_memory_dream(store, now=WINDOW_START + 3 * 3600 + 30)
    assert closed["reason"] == "night_not_due"
    assert len(events) == 1
    assert events[0]["status"] == "warning"
    assert events[0]["detail"] == {
        "reason": "dream_concurrency_cap_at_window_end",
        "max_concurrent": dream_scheduler.dream_max_concurrent(),
    }

    dream_scheduler.tick_memory_dream(store, now=WINDOW_START + 3 * 3600 + 90)
    assert len(events) == 1, "once per capped-to-closed transition"


def test_admission_busy_until_window_end_also_emits_window_missed(
    monkeypatch, dream_env, no_v2_jobs
):
    """之前：窗口里最后一次判定是 dream_admission_busy（准入锁被别的 tick 占着）的
    用户，窗口关了也不发 memory.dream.window_missed —— 今晚没做梦查不到原因。
    之后：和并发上限同样发一次，理由码写明是 busy。"""
    monkeypatch.setenv("FEEDLING_DREAM_STAGGER", "0")
    events = _window_missed_events(monkeypatch)
    store = _user_with_cards("usr_dream_window_missed_busy")

    with db.memory_dream_admission_lock() as held:
        assert held is True
        busy = dream_scheduler.tick_memory_dream(store, now=WINDOW_START + 3 * 3600 - 30)
    assert busy["reason"] == "dream_admission_busy"
    assert events == []

    closed = dream_scheduler.tick_memory_dream(store, now=WINDOW_START + 3 * 3600 + 30)
    assert closed["reason"] == "night_not_due"
    assert len(events) == 1
    assert events[0]["detail"] == {
        "reason": "dream_admission_busy_at_window_end",
        "max_concurrent": dream_scheduler.dream_max_concurrent(),
    }


def test_no_window_missed_event_when_the_user_was_not_capped(monkeypatch, dream_env, no_v2_jobs):
    monkeypatch.setenv("FEEDLING_DREAM_STAGGER", "0")
    events = _window_missed_events(monkeypatch)
    store = _user_with_cards("usr_dream_window_not_missed")
    monkeypatch.setattr(dream_scheduler, "_admission_ceiling_reached", lambda: True)
    dream_scheduler.tick_memory_dream(store, now=WINDOW_START + 12 * 3600)
    monkeypatch.setattr(dream_scheduler, "_admission_ceiling_reached", lambda: False)
    assert dream_scheduler.tick_memory_dream(store, now=WINDOW_START + 3600)["enqueued"] is True
    dream_scheduler.tick_memory_dream(store, now=WINDOW_START + 3 * 3600 + 30)
    assert events == []
