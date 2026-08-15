from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
import sys
import threading
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
from core import store as core_store
from memory import service as memory_service
from model_api_runtime.v2 import jobs_store, trajectory, worker
from proactive import proactive_core


@pytest.fixture(autouse=True)
def _clean_capture_protocol():
    worker._shutdown_capture_provider_guard_executor(wait=True)
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE v2_capture_batches,agent_jobs,users CASCADE"
        )
        conn.execute(
            "UPDATE v2_runtime_control SET turns_halted=false,updated_at=now() "
            "WHERE id=1"
        )
    yield
    worker._shutdown_capture_provider_guard_executor(wait=True)
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_control SET turns_halted=false,updated_at=now() "
            "WHERE id=1"
        )


def _seed(user_id: str) -> None:
    conftest.seed_user(user_id)
    conftest.set_v2_runtime_owner(user_id, generation=1)


def _running(
    user_id: str,
    owner: str = "capture-worker",
    *,
    start: bool = True,
) -> tuple[int, dict]:
    job_id, coalesced = jobs_store.enqueue_job(user_id, "capture")
    assert not coalesced
    job = jobs_store.claim_next_job(owner, lanes={"capture"})
    assert job is not None and int(job["id"]) == job_id
    if start:
        assert jobs_store.mark_running(job_id, claimed_by=owner)
    return job_id, job


def _envelope(user_id: str, memory_id: str, *, body: str = "ciphertext") -> dict:
    return {
        "id": memory_id,
        "owner_user_id": user_id,
        "visibility": "shared",
        "body_ct": body,
        "nonce": "nonce",
        "K_user": "wrapped-user",
        "K_enclave": "wrapped-enclave",
        "enclave_pk_fpr": "fpr",
        "type": "fact",
        "occurred_at": "2026-07-20T12:00:00Z",
        "source": "memory_capture",
        "importance": 0.8,
        "pulse": 0.4,
        "last_referenced_at": "2026-07-20T12:00:00Z",
    }


def _window(*, after: int = 0, through: int = 1) -> dict:
    return {
        "after_seq": after,
        "through_seq": through,
        "after_message_id": "" if after == 0 else f"m{after}",
        "until_message_id": f"m{through}",
        "until_ts": float(through),
    }


def _add(user_id: str, memory_id: str) -> dict:
    return {
        "type": "memory.add",
        "envelope": _envelope(user_id, memory_id),
        "reason": "provider scratch must not persist",
        "plaintext_draft": "DO_NOT_STORE",
    }


def _manual_card(user_id: str, memory_id: str) -> dict:
    return {
        **_envelope(user_id, memory_id, body=f"manual-{memory_id}"),
        "created_at": "2026-07-20T12:00:00Z",
        "updated_at": "2026-07-20T12:00:00Z",
        "status": "active",
    }


def test_capture_action_validation_normalizes_occurred_at_and_rejects_garbage():
    action = _add("u_capture_timestamp", "mom-timestamp")
    action["envelope"]["occurred_at"] = "2026-07-20T20:00:00+08:00"

    normalized = jobs_store._validate_capture_actions(
        "u_capture_timestamp", [action]
    )

    assert normalized[0]["envelope"]["occurred_at"] == "2026-07-20T12:00:00Z"

    action["envelope"]["occurred_at"] = "not-a-date"
    with pytest.raises(ValueError, match="invalid occurred_at"):
        jobs_store._validate_capture_actions("u_capture_timestamp", [action])


def test_capture_commit_is_atomic_strips_plaintext_and_keeps_canonical_logs():
    uid = "u_capture_atomic"
    _seed(uid)
    job_id, _job = _running(uid)
    action = _add(uid, "mom-atomic")
    action["envelope"]["occurred_at"] = "2026-07-20T20:00:00+08:00"
    action["envelope"]["_inner"] = {"content": "DO_NOT_STORE"}
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[action],
    )
    assert batch is not None

    with db.get_pool().connection() as conn:
        raw = conn.execute(
            "SELECT actions_json::text FROM v2_capture_batches WHERE id=%s",
            (batch["id"],),
        ).fetchone()[0]
    assert "DO_NOT_STORE" not in raw
    assert "plaintext_draft" not in raw

    result = jobs_store.commit_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        batch_id=batch["id"],
    )
    assert result["committed"] is True
    with db.get_pool().connection() as conn:
        status = conn.execute(
            "SELECT status FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()[0]
        state = conn.execute(
            "SELECT doc FROM user_blobs WHERE user_id=%s AND kind='capture_state'",
            (uid,),
        ).fetchone()[0]
        moment = conn.execute(
            "SELECT doc FROM memory_moments WHERE user_id=%s AND moment_id='mom-atomic'",
            (uid,),
        ).fetchone()[0]
        batch_count = conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE user_id=%s", (uid,)
        ).fetchone()[0]
        streams = {
            row[0]
            for row in conn.execute(
                "SELECT stream FROM user_logs WHERE user_id=%s", (uid,)
            ).fetchall()
        }
    assert status == "completed"
    assert state["last_captured_until_seq"] == 1
    assert state["capture_seq_initialized"] is True
    assert moment["occurred_at"] == "2026-07-20T12:00:00Z"
    assert moment["created_at"].endswith("Z")
    assert "." not in moment["created_at"]
    assert batch_count == 0
    assert {"memory_changes", "bootstrap_events"}.issubset(streams)


def test_v2_capture_banner_accumulates_across_midnight_and_ignores_noop(monkeypatch):
    uid = "u_capture_daily_v2"
    _seed(uid)
    db.set_blob(uid, "proactive_settings", {
        "capture_enabled": True,
    })

    def commit(*, after: int, through: int, memory_id: str | None, now: float):
        monkeypatch.setattr(jobs_store.time, "time", lambda: now)
        job_id, _job = _running(uid)
        actions = [_add(uid, memory_id)] if memory_id else []
        batch = jobs_store.prepare_capture_batch(
            job_id=job_id,
            user_id=uid,
            claimed_by="capture-worker",
            window=_window(after=after, through=through),
            actions=actions,
        )
        assert batch is not None
        return jobs_store.commit_capture_batch(
            job_id=job_id,
            user_id=uid,
            claimed_by="capture-worker",
            batch_id=batch["id"],
        )

    first_at = datetime(2026, 8, 1, 10, tzinfo=timezone.utc).timestamp()
    second_at = datetime(2026, 8, 1, 22, tzinfo=timezone.utc).timestamp()
    noop_at = datetime(2026, 8, 2, 8, tzinfo=timezone.utc).timestamp()
    next_positive_at = datetime(2026, 8, 2, 9, tzinfo=timezone.utc).timestamp()

    assert commit(after=0, through=1, memory_id="mom-daily-1", now=first_at)[
        "cards_added"
    ] == 1
    assert commit(after=1, through=2, memory_id="mom-daily-2", now=second_at)[
        "cards_added"
    ] == 1
    state = db.get_blob_strict(uid, "capture_state")
    assert state["last_capture_cards_added"] == 2
    assert state["last_capture_cards_added_at"] == second_at

    assert commit(after=2, through=3, memory_id=None, now=noop_at)[
        "cards_added"
    ] == 0
    state = db.get_blob_strict(uid, "capture_state")
    assert state["last_capture_cards_added"] == 2
    assert state["last_capture_cards_added_at"] == second_at
    assert state["last_capture_completed_at"] == noop_at

    assert commit(
        after=3,
        through=4,
        memory_id="mom-daily-3",
        now=next_positive_at,
    )["cards_added"] == 1
    state = db.get_blob_strict(uid, "capture_state")
    assert state["last_capture_cards_added"] == 3
    assert state["last_capture_cards_added_at"] == next_positive_at


def test_capture_waits_for_cross_process_whole_garden_mutation(monkeypatch):
    """A snapshot loaded before Capture cannot delete its card after commit.

    Two distinct ``UserStore`` instances model separate backend processes: the
    Python locks are unrelated, so only PostgreSQL can serialize them.
    """
    uid = "u_capture_memory_fence_insert"
    _seed(uid)
    assert db.memory_upsert(uid, "seed", "2026-07-20", _manual_card(uid, "seed"))
    job_id, _job = _running(uid)
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[_add(uid, "captured")],
    )
    assert batch is not None

    writer_store = core_store.UserStore(uid)
    writer_loaded = threading.Event()
    release_writer = threading.Event()
    capture_at_fence = threading.Event()
    role = threading.local()
    real_lock = db._lock_memory_user_mutation_on_cursor

    def observed_lock(cur, user_id):
        if getattr(role, "value", "") == "capture":
            capture_at_fence.set()
        return real_lock(cur, user_id)

    monkeypatch.setattr(db, "_lock_memory_user_mutation_on_cursor", observed_lock)

    def stale_writer():
        role.value = "writer"
        with memory_service.mutation_lock(writer_store):
            snapshot = memory_service._load_moments(writer_store)
            writer_loaded.set()
            assert release_writer.wait(timeout=3)
            snapshot.append(_manual_card(uid, "manual"))
            memory_service._save_moments(writer_store, snapshot)

    def capture_commit():
        role.value = "capture"
        return jobs_store.commit_capture_batch(
            job_id=job_id,
            user_id=uid,
            claimed_by="capture-worker",
            batch_id=batch["id"],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer_future = executor.submit(stale_writer)
        assert writer_loaded.wait(timeout=3)
        capture_future = executor.submit(capture_commit)
        assert capture_at_fence.wait(timeout=3)
        with pytest.raises(FutureTimeoutError):
            capture_future.result(timeout=0.1)
        release_writer.set()
        writer_future.result(timeout=3)
        result = capture_future.result(timeout=3)

    assert result["committed"] is True
    assert {m["id"] for m in db.memory_load(uid)} == {"seed", "manual", "captured"}
    state = db.get_blob_strict(uid, "capture_state")
    assert state["last_captured_until_seq"] == 1


def test_whole_garden_writer_cannot_restore_capture_supersede(monkeypatch):
    """A writer queued behind Capture re-reads the superseded target."""
    uid = "u_capture_memory_fence_supersede"
    _seed(uid)
    assert db.memory_upsert(uid, "target", "2026-07-20", _manual_card(uid, "target"))
    job_id, _job = _running(uid)
    action = {
        "type": "memory.supersede",
        "envelope": _envelope(uid, "replacement"),
        "supersedes": ["target"],
    }
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[action],
    )
    assert batch is not None

    writer_store = core_store.UserStore(uid)
    capture_has_fence = threading.Event()
    release_capture = threading.Event()
    writer_at_fence = threading.Event()
    role = threading.local()
    real_lock = db._lock_memory_user_mutation_on_cursor

    def observed_lock(cur, user_id):
        name = getattr(role, "value", "")
        if name == "capture":
            real_lock(cur, user_id)
            capture_has_fence.set()
            assert release_capture.wait(timeout=3)
            return None
        if name == "writer":
            writer_at_fence.set()
        return real_lock(cur, user_id)

    monkeypatch.setattr(db, "_lock_memory_user_mutation_on_cursor", observed_lock)

    def capture_commit():
        role.value = "capture"
        return jobs_store.commit_capture_batch(
            job_id=job_id,
            user_id=uid,
            claimed_by="capture-worker",
            batch_id=batch["id"],
        )

    def whole_garden_writer():
        role.value = "writer"
        with memory_service.mutation_lock(writer_store):
            snapshot = memory_service._load_moments(writer_store)
            snapshot.append(_manual_card(uid, "manual-after"))
            memory_service._save_moments(writer_store, snapshot)

    with ThreadPoolExecutor(max_workers=2) as executor:
        capture_future = executor.submit(capture_commit)
        assert capture_has_fence.wait(timeout=3)
        writer_future = executor.submit(whole_garden_writer)
        assert writer_at_fence.wait(timeout=3)
        with pytest.raises(FutureTimeoutError):
            writer_future.result(timeout=0.1)
        release_capture.set()
        assert capture_future.result(timeout=3)["committed"] is True
        writer_future.result(timeout=3)

    moments = {m["id"]: m for m in db.memory_load(uid)}
    assert set(moments) == {"target", "replacement", "manual-after"}
    assert moments["target"]["status"] == "superseded"
    assert moments["target"]["superseded_by"] == "replacement"
    assert db.get_blob_strict(uid, "capture_state")["last_captured_until_seq"] == 1


def test_halted_capture_boundaries_cancel_without_backoff():
    uid = "u_capture_d4_authorize"
    _seed(uid)
    job_id, _job = _running(uid)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE v2_runtime_control SET turns_halted=true WHERE id=1")
    result = jobs_store.authorize_capture_provider_call(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
    )
    assert result == {
        "authorized": False,
        "reason": "turns_halted",
        "rejected": True,
    }

    uid = "u_capture_d4_prepare"
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE v2_runtime_control SET turns_halted=false WHERE id=1")
    _seed(uid)
    job_id, _job = _running(uid)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE v2_runtime_control SET turns_halted=true WHERE id=1")
    prepared = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[_add(uid, "must-not-prepare")],
    )
    assert prepared == {"rejected": True, "reason": "turns_halted"}

    uid = "u_capture_d4_commit"
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE v2_runtime_control SET turns_halted=false WHERE id=1")
    _seed(uid)
    job_id, _job = _running(uid)
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[_add(uid, "must-not-commit")],
    )
    assert batch is not None
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE v2_runtime_control SET turns_halted=true WHERE id=1")
    committed = jobs_store.commit_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        batch_id=batch["id"],
    )
    assert committed == {
        "committed": False,
        "reason": "turns_halted",
        "rejected": True,
    }
    with db.get_pool().connection() as conn:
        job = conn.execute(
            "SELECT status,last_error FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()
        batch_count = conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE user_id=%s", (uid,)
        ).fetchone()[0]
    assert job == ("failed", "turns_halted")
    assert batch_count == 0
    assert all(m["id"] != "must-not-commit" for m in db.memory_load(uid))
    assert db.get_blob_strict(uid, "capture_state").get("capture_fail_streak", 0) == 0


def test_capture_provider_disclosure_blocks_halt_until_callback_returns():
    uid = "u_capture_provider_halt_fence"
    _seed(uid)
    job_id, _job = _running(uid)
    provider_started = threading.Event()
    release_provider = threading.Event()

    def provider_call():
        assert uid in db._chat_outer_fence_users.get()
        provider_started.set()
        assert release_provider.wait(timeout=3)
        return ([{"captured": True}], None)

    def disclose():
        return jobs_store.authorize_capture_provider_call(
            job_id=job_id,
            user_id=uid,
            claimed_by="capture-worker",
            provider_call=provider_call,
        )

    def halt():
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE v2_runtime_control SET turns_halted=true,updated_at=now() "
                "WHERE id=1"
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        disclosure_future = executor.submit(disclose)
        assert provider_started.wait(timeout=3)
        halt_future = executor.submit(halt)
        with pytest.raises(FutureTimeoutError):
            halt_future.result(timeout=0.1)
        release_provider.set()
        result = disclosure_future.result(timeout=3)
        halt_future.result(timeout=3)

    assert result == {
        "authorized": True,
        "provider_call_completed": True,
        "provider_result": ([{"captured": True}], None),
    }
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT turns_halted FROM v2_runtime_control WHERE id=1"
        ).fetchone()[0] is True


def test_capture_provider_disclosure_blocks_opt_out_until_callback_returns():
    uid = "u_capture_provider_consent_fence"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    job_id, _job = _running(uid)
    provider_started = threading.Event()
    release_provider = threading.Event()

    def provider_call():
        assert uid in db._chat_outer_fence_users.get()
        provider_started.set()
        assert release_provider.wait(timeout=3)
        return ([], None)

    def disclose():
        return jobs_store.authorize_capture_provider_call(
            job_id=job_id,
            user_id=uid,
            claimed_by="capture-worker",
            provider_call=provider_call,
        )

    def opt_out():
        return core_store.UserStore(uid).save_proactive_settings(
            {"capture_enabled": False}
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        disclosure_future = executor.submit(disclose)
        assert provider_started.wait(timeout=3)
        opt_out_future = executor.submit(opt_out)
        with pytest.raises(FutureTimeoutError):
            opt_out_future.result(timeout=0.1)
        release_provider.set()
        result = disclosure_future.result(timeout=3)
        settings = opt_out_future.result(timeout=3)

    assert result["authorized"] is True
    assert result["provider_call_completed"] is True
    assert result["provider_result"] == ([], None)
    assert settings["capture_enabled"] is False


def test_capture_provider_disclosure_allows_owned_lease_renewal():
    uid = "u_capture_provider_lease_renewal"
    _seed(uid)
    job_id, _job = _running(uid)
    provider_started = threading.Event()
    release_provider = threading.Event()

    def provider_call():
        provider_started.set()
        assert release_provider.wait(timeout=3)
        return ([], None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        disclosure_future = executor.submit(
            jobs_store.authorize_capture_provider_call,
            job_id=job_id,
            user_id=uid,
            claimed_by="capture-worker",
            provider_call=provider_call,
        )
        assert provider_started.wait(timeout=3)
        renewal_future = executor.submit(
            jobs_store.renew_job_lease,
            job_id,
            "capture-worker",
            ttl_sec=jobs_store.RUNNING_TTL_SEC,
        )
        assert renewal_future.result(timeout=1) is True
        release_provider.set()
        result = disclosure_future.result(timeout=3)

    assert result["authorized"] is True
    assert result["provider_call_completed"] is True


def test_capture_opt_out_wins_provider_callback_is_never_invoked():
    uid = "u_capture_provider_consent_wins"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": False})
    job_id, _job = _running(uid)
    provider_called = threading.Event()

    result = jobs_store.authorize_capture_provider_call(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        provider_call=lambda: provider_called.set(),
    )

    assert result == {
        "authorized": False,
        "reason": "capture_disabled",
        "rejected": True,
    }
    assert not provider_called.is_set()


def test_worker_keeps_opt_out_fenced_for_complete_async_provider_call(monkeypatch):
    uid = "u_capture_worker_provider_consent_fence"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    _job_id, job = _running(uid)
    provider_started = threading.Event()
    release_provider = threading.Event()
    trajectory_events: list[str] = []

    from model_api_runtime.v2 import extraction

    class Recorder:
        async def record(self, kind, _payload):
            assert uid in db._chat_outer_fence_users.get()
            trajectory_events.append(str(kind))

    async def provider_call(**kwargs):
        # run_coroutine_threadsafe must copy jobs_store's outer-fence context
        # onto this original event loop before any durable trajectory callback.
        assert uid in db._chat_outer_fence_users.get()
        await kwargs["trajectory_out"](
            "provider_request", {"messages": [{"role": "user"}]}
        )
        provider_started.set()
        assert await asyncio.to_thread(release_provider.wait, 3)
        return [], None

    monkeypatch.setattr(extraction, "extract", provider_call)
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: 1)
    monkeypatch.setattr(jobs_store, "_CAPTURE_PROVIDER_DB_KEEPALIVE_SEC", 0.02)
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        read_memory_context=lambda _uid: {},
        read_capture_state=lambda _uid: {
            "last_captured_until_seq": 0,
            "capture_seq_initialized": True,
        },
        read_compaction_tail_after_seq=lambda *_args, **_kwargs: [
            {
                "id": "m1",
                "seq": 1,
                "ts": 1.0,
                "role": "user",
                "raw_role": "user",
                "source": "chat",
                "capture_eligible": True,
                "content": "remember this",
            }
        ],
        build_memory_envelope=lambda *_args: {},
        get_prepared_capture_batch=jobs_store.get_prepared_capture_batch,
        prepare_capture_batch=jobs_store.prepare_capture_batch,
        authorize_capture_provider_call=jobs_store.authorize_capture_provider_call,
        commit_capture_batch=jobs_store.commit_capture_batch,
        fail_capture_job=jobs_store.fail_capture_job,
        cancel_capture_job=jobs_store.cancel_capture_job,
    )

    def run_worker():
        return asyncio.run(
            worker._run_extraction(
                job["id"],
                uid,
                "capture",
                deps,
                object(),
                asyncio.Semaphore(1),
                claimed_by="capture-worker",
                trajectory_recorder=Recorder(),
            )
        )

    def opt_out():
        return core_store.UserStore(uid).save_proactive_settings(
            {"capture_enabled": False}
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        worker_future = executor.submit(run_worker)
        assert provider_started.wait(timeout=3)
        opt_out_future = executor.submit(opt_out)
        with pytest.raises(FutureTimeoutError):
            opt_out_future.result(timeout=0.1)
        release_provider.set()
        worker_result = worker_future.result(timeout=5)
        settings = opt_out_future.result(timeout=5)

    # The opt-out was already queued on the consent lock when disclosure ended,
    # so it wins before the worker's later prepare boundary and discards the
    # provider result without persisting a Capture batch.
    assert worker_result == "failed"
    assert settings["capture_enabled"] is False
    assert trajectory_events[0] == "provider_request"
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0


def test_capture_guard_does_not_occupy_saturated_default_executor(monkeypatch):
    uid = "u_capture_dedicated_guard_executor"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    _job_id, job = _running(uid)
    ordinary_to_thread_progress = threading.Event()
    trajectory_to_thread_progress = threading.Event()
    provider_task_started = threading.Event()
    watchdog_fired = threading.Event()
    provider_task: list[asyncio.Task] = []
    owner_loop: list[asyncio.AbstractEventLoop] = []
    progress_stages: list[str] = []

    from model_api_runtime.v2 import extraction

    def seal(user_id, _plaintext, item_id):
        assert user_id == uid
        assert uid in db._chat_outer_fence_users.get()
        return {
            "v": 1,
            "id": item_id,
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_ct": "sealed",
            "nonce": "nonce",
            "K_user": "wrapped-user",
            "K_enclave": "wrapped-enclave",
        }

    def append_batch(_job_id, user_id, *, events):
        assert user_id == uid
        assert uid in db._chat_outer_fence_users.get()
        assert [event["event_kind"] for event in events] == ["provider_request"]
        trajectory_to_thread_progress.set()
        return [1]

    recorder = trajectory.TrajectoryRecorder(
        job_id=job["id"],
        user_id=uid,
        seal=seal,
        append=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("single provider event must use append_batch")
        ),
        append_batch=append_batch,
    )

    async def provider_call(**kwargs):
        assert uid in db._chat_outer_fence_users.get()
        provider_task.append(asyncio.current_task())
        provider_task_started.set()
        ordinary = asyncio.create_task(
            asyncio.to_thread(ordinary_to_thread_progress.set)
        )
        await kwargs["trajectory_out"](
            "provider_request", {"messages": [{"role": "user"}]}
        )
        await ordinary
        return [], None

    monkeypatch.setattr(extraction, "extract", provider_call)
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: 1)
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        read_memory_context=lambda _uid: {},
        read_capture_state=lambda _uid: {
            "last_captured_until_seq": 0,
            "capture_seq_initialized": True,
        },
        read_compaction_tail_after_seq=lambda *_args, **_kwargs: [
            {
                "id": "m1",
                "seq": 1,
                "ts": 1.0,
                "role": "user",
                "raw_role": "user",
                "source": "chat",
                "capture_eligible": True,
                "content": "remember this",
            }
        ],
        build_memory_envelope=lambda *_args: {},
        get_prepared_capture_batch=jobs_store.get_prepared_capture_batch,
        prepare_capture_batch=jobs_store.prepare_capture_batch,
        authorize_capture_provider_call=jobs_store.authorize_capture_provider_call,
        commit_capture_batch=jobs_store.commit_capture_batch,
        fail_capture_job=jobs_store.fail_capture_job,
        cancel_capture_job=jobs_store.cancel_capture_job,
    )

    async def scenario():
        loop = asyncio.get_running_loop()
        owner_loop.append(loop)
        loop.set_default_executor(
            ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="test-saturated-default",
            )
        )
        progress_token = worker._TURN_PROGRESS_CB.set(progress_stages.append)
        try:
            return await asyncio.wait_for(
                worker._run_extraction(
                    job["id"],
                    uid,
                    "capture",
                    deps,
                    object(),
                    asyncio.Semaphore(1),
                    claimed_by="capture-worker",
                    trajectory_recorder=recorder,
                ),
                timeout=3,
            )
        finally:
            worker._TURN_PROGRESS_CB.reset(progress_token)

    # Keep the regression bounded even if somebody moves the guard back onto
    # the sole default thread: cancelling the provider Task breaks that exact
    # cycle, after which the failed result/assertion is observable instead of
    # hanging the whole suite forever.
    def break_regression_deadlock():
        watchdog_fired.set()
        if provider_task_started.wait(timeout=1) and provider_task and owner_loop:
            owner_loop[0].call_soon_threadsafe(provider_task[0].cancel)

    watchdog = threading.Timer(4, break_regression_deadlock)
    watchdog.start()
    try:
        assert asyncio.run(scenario()) == "completed"
    finally:
        watchdog.cancel()
        watchdog.join(timeout=1)
    assert not watchdog_fired.is_set()
    assert ordinary_to_thread_progress.is_set()
    assert trajectory_to_thread_progress.is_set()
    assert "extraction_provider_start" in progress_stages
    assert "extraction_provider_complete" in progress_stages


def test_capture_keepalive_failure_cancels_and_drains_before_opt_out(
    monkeypatch,
):
    uid = "u_capture_keepalive_failure_drain"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    _job_id, job = _running(uid)
    provider_started = threading.Event()
    provider_done = threading.Event()
    keepalive_entered = threading.Event()
    release_keepalive = threading.Event()
    opt_out_returned = threading.Event()
    opt_out_observed_provider_done: list[bool] = []

    from model_api_runtime.v2 import extraction

    async def blocked_provider(**_kwargs):
        provider_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            provider_done.set()

    def failing_keepalive(_cur):
        keepalive_entered.set()
        assert release_keepalive.wait(timeout=3)
        raise RuntimeError("forced_capture_keepalive_failure")

    def fail_after_opt_out(**kwargs):
        assert opt_out_returned.wait(timeout=3)
        return jobs_store.fail_capture_job(**kwargs)

    monkeypatch.setattr(extraction, "extract", blocked_provider)
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: 1)
    monkeypatch.setattr(jobs_store, "_CAPTURE_PROVIDER_DB_KEEPALIVE_SEC", 0.02)
    monkeypatch.setattr(jobs_store, "_capture_provider_db_keepalive", failing_keepalive)
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        read_memory_context=lambda _uid: {},
        read_capture_state=lambda _uid: {
            "last_captured_until_seq": 0,
            "capture_seq_initialized": True,
        },
        read_compaction_tail_after_seq=lambda *_args, **_kwargs: [
            {
                "id": "m1",
                "seq": 1,
                "ts": 1.0,
                "role": "user",
                "raw_role": "user",
                "source": "chat",
                "capture_eligible": True,
                "content": "remember this",
            }
        ],
        build_memory_envelope=lambda *_args: {},
        get_prepared_capture_batch=jobs_store.get_prepared_capture_batch,
        prepare_capture_batch=jobs_store.prepare_capture_batch,
        authorize_capture_provider_call=jobs_store.authorize_capture_provider_call,
        commit_capture_batch=jobs_store.commit_capture_batch,
        fail_capture_job=fail_after_opt_out,
        cancel_capture_job=jobs_store.cancel_capture_job,
    )

    def run_worker():
        return asyncio.run(
            worker._run_extraction(
                job["id"],
                uid,
                "capture",
                deps,
                object(),
                asyncio.Semaphore(1),
                claimed_by="capture-worker",
            )
        )

    def opt_out():
        settings = core_store.UserStore(uid).save_proactive_settings(
            {"capture_enabled": False}
        )
        opt_out_observed_provider_done.append(provider_done.is_set())
        opt_out_returned.set()
        return settings

    with ThreadPoolExecutor(max_workers=2) as executor:
        worker_future = executor.submit(run_worker)
        assert provider_started.wait(timeout=3)
        assert keepalive_entered.wait(timeout=3)
        opt_out_future = executor.submit(opt_out)
        with pytest.raises(FutureTimeoutError):
            opt_out_future.result(timeout=0.1)
        release_keepalive.set()
        settings = opt_out_future.result(timeout=5)
        worker_result = worker_future.result(timeout=5)

    assert worker_result == "failed"
    assert settings["capture_enabled"] is False
    assert provider_done.is_set()
    assert opt_out_observed_provider_done == [True]


def test_capture_guard_pool_is_one_thread_per_slot_process(monkeypatch):
    worker._shutdown_capture_provider_guard_executor(wait=True)
    monkeypatch.setenv("FEEDLING_V2_MAX_WORKERS", "99")
    executor = worker._capture_provider_guard_thread_pool()
    try:
        assert executor._max_workers == 1
        assert executor is worker._capture_provider_guard_thread_pool()
    finally:
        worker._shutdown_capture_provider_guard_executor(wait=True)


def test_capture_commit_linearizes_before_halt_update(monkeypatch):
    uid = "u_capture_d4_linearized"
    _seed(uid)
    job_id, _job = _running(uid)
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[_add(uid, "lands-before-halt")],
    )
    assert batch is not None

    control_locked = threading.Event()
    release_commit = threading.Event()
    real_check = jobs_store._capture_turns_halted_on_cursor

    def observed_check(cur):
        halted = real_check(cur)
        control_locked.set()
        assert release_commit.wait(timeout=3)
        return halted

    monkeypatch.setattr(jobs_store, "_capture_turns_halted_on_cursor", observed_check)

    def commit():
        return jobs_store.commit_capture_batch(
            job_id=job_id,
            user_id=uid,
            claimed_by="capture-worker",
            batch_id=batch["id"],
        )

    def halt():
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE v2_runtime_control SET turns_halted=true,updated_at=now() "
                "WHERE id=1"
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        commit_future = executor.submit(commit)
        assert control_locked.wait(timeout=3)
        halt_future = executor.submit(halt)
        with pytest.raises(FutureTimeoutError):
            halt_future.result(timeout=0.1)
        release_commit.set()
        assert commit_future.result(timeout=3)["committed"] is True
        halt_future.result(timeout=3)

    assert {m["id"] for m in db.memory_load(uid)} == {"lands-before-halt"}
    assert db.get_blob_strict(uid, "capture_state")["last_captured_until_seq"] == 1
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT turns_halted FROM v2_runtime_control WHERE id=1"
        ).fetchone()[0] is True


def test_failed_capture_opt_out_is_not_acknowledged(monkeypatch):
    uid = "u_capture_consent_failure"
    _seed(uid)
    store = core_store.UserStore(uid)
    store.save_proactive_settings({"capture_enabled": True})

    def _fail_patch(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "patch_proactive_settings_strict", _fail_patch)
    with pytest.raises(RuntimeError, match="database unavailable"):
        store.save_proactive_settings({"capture_enabled": False})

    assert db.get_blob_strict(uid, "proactive_settings")["capture_enabled"] is True


def test_stale_unrelated_settings_patch_cannot_restore_capture_consent(monkeypatch):
    uid = "u_capture_consent_stale_patch"
    _seed(uid)
    initial = core_store.UserStore(uid)
    initial.save_proactive_settings(
        {"capture_enabled": True, "timezone": "UTC"}
    )

    stale_writer = core_store.UserStore(uid)
    stale_read_complete = threading.Event()
    original_load = stale_writer.load_proactive_settings

    def _load_stale_snapshot():
        snapshot = original_load()
        assert snapshot["capture_enabled"] is True
        stale_read_complete.set()
        return snapshot

    monkeypatch.setattr(stale_writer, "load_proactive_settings", _load_stale_snapshot)
    executor = ThreadPoolExecutor(max_workers=1)
    future = None
    try:
        with db.get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    db._lock_capture_consent_on_cursor(cur, uid)
                    future = executor.submit(
                        stale_writer.save_proactive_settings,
                        {"timezone": "Europe/Paris"},
                    )
                    assert stale_read_complete.wait(timeout=2.0)
                    # The writer began with capture_enabled=true but cannot
                    # enter its atomic read/patch section until this opt-out
                    # transaction commits.
                    with pytest.raises(FutureTimeoutError):
                        future.result(timeout=0.1)
                    cur.execute(
                        "UPDATE user_blobs SET doc=doc || %s "
                        "WHERE user_id=%s AND kind='proactive_settings'",
                        (Jsonb({"capture_enabled": False}), uid),
                    )
                    assert cur.rowcount == 1
        assert future is not None
        returned = future.result(timeout=2.0)
    finally:
        executor.shutdown(wait=True)

    persisted = db.get_blob_strict(uid, "proactive_settings")
    assert persisted["capture_enabled"] is False
    assert persisted["timezone"] == "Europe/Paris"
    assert returned["capture_enabled"] is False


def test_capture_opt_out_purges_an_existing_prepared_journal():
    uid = "u_capture_consent_purge_prepared"
    _seed(uid)
    job_id, _job = _running(uid)
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[_add(uid, "mom-purge-on-disable")],
    )
    assert batch is not None

    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": False})
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0


def test_capture_prepare_after_opt_out_writes_nothing_and_cancels_without_backoff():
    uid = "u_capture_consent_disable_wins"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": False})
    job_id, _job = _running(uid)

    rejected = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[_add(uid, "mom-must-not-journal")],
    )
    assert rejected == {"rejected": True, "reason": "capture_disabled"}
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()[0] == "failed"
    state = db.get_blob_strict(uid, "capture_state")
    assert state["capture_fail_streak"] == 0


def test_seq_frontier_discovers_later_live_message_with_older_timestamp():
    uid = "u_capture_seq_discovery"
    _seed(uid)
    db.chat_append_strict(
        uid,
        "import-row",
        100.0,
        {
            "id": "import-row",
            "role": "user",
            "source": "history_import",
            "ts": 100.0,
        },
        5000,
    )
    job_id, _job = _running(uid)
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window={
            **_window(),
            "until_message_id": "import-row",
            "until_ts": 100.0,
        },
        actions=[],
    )
    assert jobs_store.commit_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        batch_id=batch["id"],
    )["committed"]

    db.chat_append_strict(
        uid,
        "late-live-row",
        10.0,
        {
            "id": "late-live-row",
            "role": "user",
            "source": "chat",
            "ts": 10.0,
        },
        5000,
    )
    refreshed = proactive_core.capture_scheduler.refresh_capture_state_from_chat(
        core_store.UserStore(uid), now=101.0
    )
    assert refreshed["last_seen_message_id"] == "late-live-row"
    assert refreshed["message_count"] == 1


def test_live_discovery_filters_before_bounding_synthetic_backlog():
    uid = "u_capture_live_before_synthetic_backlog"
    _seed(uid)
    db.set_blob_strict(
        uid,
        "capture_state",
        {"last_captured_until_seq": 0, "capture_seq_initialized": True},
    )
    db.chat_append_strict(
        uid,
        "live-first",
        1.0,
        {"id": "live-first", "role": "user", "source": "chat", "ts": 1.0},
        5000,
    )
    for index in range(70):
        message_id = f"synthetic-{index}"
        db.chat_append_strict(
            uid,
            message_id,
            float(index + 2),
            {
                "id": message_id,
                "role": "user",
                "source": "verify_ping",
                "ts": float(index + 2),
            },
            5000,
        )

    refreshed = proactive_core.capture_scheduler.refresh_capture_state_from_chat(
        core_store.UserStore(uid), now=100.0
    )
    assert refreshed["last_seen_message_id"] == "live-first"
    assert refreshed["message_count"] == 1


def test_mid_transaction_exception_rolls_back_and_retry_reuses_batch(monkeypatch):
    uid = "u_capture_crash"
    _seed(uid)
    job_id, _job = _running(uid)
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[_add(uid, "mom-crash")],
    )
    assert batch is not None

    real_sha256 = jobs_store.hashlib.sha256
    monkeypatch.setattr(
        jobs_store.hashlib,
        "sha256",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        jobs_store.commit_capture_batch(
            job_id=job_id,
            user_id=uid,
            claimed_by="capture-worker",
            batch_id=batch["id"],
        )
    monkeypatch.setattr(jobs_store.hashlib, "sha256", real_sha256)

    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM memory_moments WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()[0] == "running"
        assert conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE id=%s", (batch["id"],)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM user_blobs WHERE user_id=%s "
            "AND kind='capture_state' AND doc ? 'last_captured_until_seq'",
            (uid,),
        ).fetchone()[0] == 0

    assert jobs_store.commit_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        batch_id=batch["id"],
    )["committed"]


def test_lost_owner_cannot_write_memory_frontier_or_backoff():
    uid = "u_capture_lost_owner"
    _seed(uid)
    job_id, _job = _running(uid, owner="owner-a")
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="owner-a",
        window=_window(),
        actions=[_add(uid, "mom-lost")],
    )
    assert batch is not None
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET claimed_by='owner-b' WHERE id=%s", (job_id,)
        )

    assert not jobs_store.commit_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="owner-a",
        batch_id=batch["id"],
    )["committed"]
    assert not jobs_store.fail_capture_job(
        job_id=job_id,
        user_id=uid,
        claimed_by="owner-a",
        error="provider_failed",
    )
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM memory_moments WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM user_blobs WHERE user_id=%s AND kind='capture_state'",
            (uid,),
        ).fetchone()[0] == 0


def test_poisoned_supersede_batch_is_rejected_and_next_job_can_regenerate():
    uid = "u_capture_poison"
    _seed(uid)
    job_id, _job = _running(uid)
    poison = {
        "type": "memory.supersede",
        "supersedes": "deleted-target",
        "envelope": _envelope(uid, "mom-poison"),
    }
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[poison],
    )
    rejected = jobs_store.commit_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        batch_id=batch["id"],
    )
    assert rejected == {
        "committed": False,
        "reason": "capture_supersede_target_missing",
        "rejected": True,
    }
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM memory_moments WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0

    successor_id, _job = _running(uid, owner="capture-worker-2")
    successor = jobs_store.prepare_capture_batch(
        job_id=successor_id,
        user_id=uid,
        claimed_by="capture-worker-2",
        window=_window(),
        actions=[_add(uid, "mom-regenerated")],
    )
    assert jobs_store.commit_capture_batch(
        job_id=successor_id,
        user_id=uid,
        claimed_by="capture-worker-2",
        batch_id=successor["id"],
    )["committed"]


def test_prepared_retry_commits_before_provider_or_enclave(monkeypatch):
    uid = "u_capture_early_retry"
    _seed(uid)
    first_id, _job = _running(uid, owner="first-owner")
    batch = jobs_store.prepare_capture_batch(
        job_id=first_id,
        user_id=uid,
        claimed_by="first-owner",
        window=_window(),
        actions=[_add(uid, "mom-retry")],
    )
    assert batch is not None
    assert jobs_store.fail_capture_job(
        job_id=first_id,
        user_id=uid,
        claimed_by="first-owner",
        error="worker_crashed_after_prepare",
    )

    second_id, second_job = _running(uid, owner="second-owner", start=False)
    provider_calls = []
    trajectory_events = []

    async def _provider_forbidden(**_kwargs):
        provider_calls.append(True)
        return [], None

    from model_api_runtime.v2 import extraction

    monkeypatch.setattr(extraction, "extract", _provider_forbidden)
    monkeypatch.setattr(worker, "_make_trajectory_recorder", lambda *_args: object())

    async def _record(_recorder, kind, payload, *, best_effort=False):
        trajectory_events.append((kind, dict(payload), best_effort))

    monkeypatch.setattr(worker, "_record_trajectory", _record)
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (_ for _ in ()).throw(
            AssertionError("provider resolution ran before prepared recovery")
        ),
        mint_enclave_token=lambda _uid: (_ for _ in ()).throw(
            AssertionError("token mint ran before prepared recovery")
        ),
        read_capture_state=lambda _uid: db.get_blob_strict(_uid, "capture_state") or {},
        read_compaction_tail_after_seq=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("enclave reader ran before prepared recovery")
        ),
        build_memory_envelope=lambda *_a: (_ for _ in ()).throw(
            AssertionError("envelope build ran before prepared recovery")
        ),
        get_prepared_capture_batch=jobs_store.get_prepared_capture_batch,
        prepare_capture_batch=jobs_store.prepare_capture_batch,
        authorize_capture_provider_call=jobs_store.authorize_capture_provider_call,
        commit_capture_batch=jobs_store.commit_capture_batch,
        fail_capture_job=jobs_store.fail_capture_job,
        cancel_capture_job=jobs_store.cancel_capture_job,
        capture_enabled=lambda _uid: True,
    )
    assert asyncio.run(
        worker._run_turn(second_job, deps)
    ) == "completed"
    assert provider_calls == []
    assert [kind for kind, _payload, _best_effort in trajectory_events] == [
        "turn_started",
        "turn_terminal",
    ]
    assert trajectory_events[0][1]["prepared_batch_recovery"] is True
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT status FROM agent_jobs WHERE id=%s", (second_id,)
        ).fetchone()[0] == "completed"


def test_halted_fleet_cancels_prepared_recovery_before_commit(monkeypatch):
    uid = "u_capture_halted_retry"
    _seed(uid)
    first_id, _job = _running(uid, owner="first-owner")
    batch = jobs_store.prepare_capture_batch(
        job_id=first_id,
        user_id=uid,
        claimed_by="first-owner",
        window=_window(),
        actions=[_add(uid, "mom-halted")],
    )
    assert batch is not None
    assert jobs_store.fail_capture_job(
        job_id=first_id,
        user_id=uid,
        claimed_by="first-owner",
        error="retry",
    )
    second_id, second_job = _running(uid, owner="second-owner", start=False)
    monkeypatch.setattr(worker.kill_switch, "turns_halted", lambda: True)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("halted recovery must not touch provider setup")

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=_forbidden,
        mint_enclave_token=_forbidden,
        read_capture_state=lambda _uid: db.get_blob_strict(_uid, "capture_state") or {},
        get_prepared_capture_batch=jobs_store.get_prepared_capture_batch,
        prepare_capture_batch=jobs_store.prepare_capture_batch,
        authorize_capture_provider_call=jobs_store.authorize_capture_provider_call,
        commit_capture_batch=jobs_store.commit_capture_batch,
        fail_capture_job=jobs_store.fail_capture_job,
        cancel_capture_job=jobs_store.cancel_capture_job,
        capture_enabled=lambda _uid: True,
    )

    assert asyncio.run(worker._run_turn(second_job, deps)) == "failed"
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT status,last_error FROM agent_jobs WHERE id=%s", (second_id,)
        ).fetchone() == ("failed", "turns_halted")
        assert conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM memory_moments WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
    # The earlier synthetic crash armed one retry; the fleet halt must not add
    # another content/provider failure to that existing streak.
    assert db.get_blob_strict(uid, "capture_state")["capture_fail_streak"] == 1


def test_missing_prepared_batch_terminalizes_still_owned_job():
    uid = "u_capture_missing_prepared"
    _seed(uid)
    job_id, _job = _running(uid)
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[_add(uid, "mom-missing")],
    )
    assert batch is not None
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_capture_batches WHERE id=%s", (batch["id"],))

    result = jobs_store.commit_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        batch_id=batch["id"],
    )

    assert result == {
        "committed": False,
        "reason": "batch_unavailable",
        "rejected": True,
    }
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT status,last_error FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone() == ("failed", "capture_batch_unavailable")


def test_stale_prepared_frontier_is_deleted_when_no_longer_adoptable():
    uid = "u_capture_stale"
    _seed(uid)
    first_id, _job = _running(uid, owner="first")
    batch = jobs_store.prepare_capture_batch(
        job_id=first_id,
        user_id=uid,
        claimed_by="first",
        window=_window(),
        actions=[_add(uid, "mom-stale")],
    )
    assert jobs_store.fail_capture_job(
        job_id=first_id,
        user_id=uid,
        claimed_by="first",
        error="retry",
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE user_blobs SET doc=doc || %s WHERE user_id=%s "
            "AND kind='capture_state'",
            (Jsonb({"last_captured_until_seq": 5, "capture_seq_initialized": True}), uid),
        )
    second_id, _job = _running(uid, owner="second")
    assert jobs_store.get_prepared_capture_batch(
        job_id=second_id,
        user_id=uid,
        claimed_by="second",
        after_seq=5,
    ) is None
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE id=%s", (batch["id"],)
        ).fetchone()[0] == 0


def test_first_settings_disable_serializes_before_capture_commit():
    uid = "u_capture_first_disable"
    _seed(uid)
    job_id, _job = _running(uid)
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[_add(uid, "mom-consent")],
    )
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with db.get_pool().connection() as settings_conn:
            with settings_conn.transaction():
                with settings_conn.cursor() as cur:
                    db._lock_capture_consent_on_cursor(cur, uid)
                    cur.execute(
                        "INSERT INTO user_blobs (user_id,kind,doc) "
                        "VALUES (%s,'proactive_settings',%s)",
                        (uid, Jsonb({"capture_enabled": False})),
                    )
                    future = pool.submit(
                        jobs_store.commit_capture_batch,
                        job_id=job_id,
                        user_id=uid,
                        claimed_by="capture-worker",
                        batch_id=batch["id"],
                    )
                    with pytest.raises(FutureTimeoutError):
                        future.result(timeout=0.1)
            result = future.result(timeout=3)
    finally:
        pool.shutdown(wait=True)
    assert result == {
        "committed": False,
        "reason": "capture_disabled",
        "rejected": True,
    }
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM memory_moments WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        state = conn.execute(
            "SELECT doc FROM user_blobs WHERE user_id=%s AND kind='capture_state'",
            (uid,),
        ).fetchone()[0]
    assert state["capture_fail_streak"] == 0


def test_user_disable_before_claim_cancels_prepared_retry_without_provider(
    monkeypatch,
):
    uid = "u_capture_disable_pending"
    _seed(uid)
    first_id, _job = _running(uid, owner="first")
    batch = jobs_store.prepare_capture_batch(
        job_id=first_id,
        user_id=uid,
        claimed_by="first",
        window=_window(),
        actions=[_add(uid, "mom-disabled")],
    )
    assert jobs_store.fail_capture_job(
        job_id=first_id,
        user_id=uid,
        claimed_by="first",
        error="retry",
    )
    second_id, second_job = _running(uid, owner="second", start=False)
    db.set_blob(uid, "proactive_settings", {"capture_enabled": False})

    from model_api_runtime.v2 import extraction

    provider_calls = []

    async def _provider(**_kwargs):
        provider_calls.append(True)
        return [], None

    monkeypatch.setattr(extraction, "extract", _provider)
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        capture_enabled=lambda user_id: bool(
            (db.get_blob_strict(user_id, "proactive_settings") or {}).get(
                "capture_enabled", True
            )
        ),
        cancel_capture_job=jobs_store.cancel_capture_job,
    )
    assert asyncio.run(
        worker.process_job(
            second_job, deps, provider_config=object(), api_key=None, runtime_token="rt"
        )
    ) == "failed"
    assert provider_calls == []
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE id=%s", (batch["id"],)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT status,last_error FROM agent_jobs WHERE id=%s", (second_id,)
        ).fetchone() == ("failed", "capture_disabled")


def test_v2_capture_submit_coalesces_and_notifies_exactly_once(monkeypatch):
    uid = "u_capture_submit"
    _seed(uid)
    monkeypatch.setenv("FEEDLING_V2_CAPTURE_ENABLED", "1")
    notified = []
    monkeypatch.setattr(
        proactive_core.core_wake_bus,
        "notify",
        lambda *args: notified.append(args),
    )

    class Store:
        user_id = uid

    first = proactive_core._submit_v2_capture(
        Store(), trigger="app_background", now=1.0,
        window=_window(), capture_key="capture:k",
    )
    second = proactive_core._submit_v2_capture(
        Store(), trigger="manual_force", now=2.0,
        window=_window(), capture_key="capture:k",
    )
    assert first["enqueued"] is True
    assert second["enqueued"] is False
    assert first["job"]["job_id"] == second["job"]["job_id"]
    assert len(notified) == 1


@pytest.mark.parametrize(
    ("lane", "enabled_field", "error"),
    [
        ("capture", "capture_enabled", "capture_disabled"),
        ("dream", "dream_enabled", "dream_disabled"),
    ],
)
def test_extraction_execution_kill_switches_fail_direct_jobs(
    lane, enabled_field, error
):
    uid = f"u_{lane}_disabled"
    _seed(uid)
    job_id, job = (
        _running(uid, start=False) if lane == "capture" else (None, None)
    )
    if lane == "dream":
        job_id, coalesced = jobs_store.enqueue_job(uid, "dream")
        assert not coalesced
        job = jobs_store.claim_next_job("capture-worker", lanes={"dream"})
    deps_kwargs = {
        "read_messages": lambda _uid: [],
        "resolve_provider": lambda _uid: (object(), {}),
        "mint_enclave_token": lambda _uid: "rt",
        enabled_field: lambda _uid: False,
    }
    deps = worker.TurnDeps(**deps_kwargs)
    assert asyncio.run(
        worker.process_job(
            job, deps, provider_config=object(), api_key=None, runtime_token="rt"
        )
    ) == "failed"
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT status,last_error FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone() == ("failed", error)


def test_delete_user_succeeds_with_applied_capture_batch():
    """Account deletion stays safe with an applied capture batch present.

    The reliably-broken path (before 0055) is an independent ``agent_jobs``
    deletion — see ``test_applied_batch_survives_apply_job_deletion_as_null``.
    Whether the ``DELETE FROM users`` cascade also trips the CHECK depends on the
    order PostgreSQL fires the ``users -> agent_jobs`` (SET NULL) vs
    ``users -> v2_capture_batches`` (delete) cascades, which is version/OID
    dependent. This asserts deletion stays order-independently safe after 0055.
    """
    uid = "u_capture_applied_delete"
    _seed(uid)
    job_id, _job = _running(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_capture_batches "
            "(user_id,runtime_generation,after_seq,through_seq,until_message_id,"
            "actions_json,action_count,status,applied_by_job_id,applied_at) "
            "VALUES (%s,1,0,1,'m1','[]'::jsonb,0,'applied',%s,now())",
            (uid, job_id),
        )
    # Must not raise. Before 0055 this aborted with a ck_v2_capture_batch_applied_shape
    # violation the moment the agent_jobs cascade nulled applied_by_job_id.
    db.delete_user(uid)
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM users WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0


def test_applied_batch_survives_apply_job_deletion_as_null():
    """The apply job can be GC'd independently; the applied row stays valid."""
    uid = "u_capture_applied_job_gc"
    _seed(uid)
    job_id, _job = _running(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_capture_batches "
            "(user_id,runtime_generation,after_seq,through_seq,until_message_id,"
            "actions_json,action_count,status,applied_by_job_id,applied_at) "
            "VALUES (%s,1,0,1,'m1','[]'::jsonb,0,'applied',%s,now())",
            (uid, job_id),
        )
        # Deleting just the apply job must SET NULL without tripping the CHECK.
        conn.execute("DELETE FROM agent_jobs WHERE id=%s", (job_id,))
        row = conn.execute(
            "SELECT status, applied_by_job_id, applied_at IS NOT NULL AS has_ts "
            "FROM v2_capture_batches WHERE user_id=%s",
            (uid,),
        ).fetchone()
    assert row[0] == "applied" and row[1] is None and row[2] is True
