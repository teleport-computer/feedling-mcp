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
from core import envelope as core_envelope
from core import store as core_store
from memory import service as memory_service
from model_api_runtime.v2 import jobs_store, serve_worker, trajectory, worker
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


def _plaintext_envelope(user_id: str, memory_id: str) -> dict:
    return {
        "id": memory_id,
        "owner_user_id": user_id,
        "visibility": "shared",
        "body": '{"summary":"plain","content":"memory","bucket":"","threads":[]}',
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


def test_capture_commit_accepts_plaintext_envelope_for_plaintext_user(monkeypatch):
    uid = "u_capture_plaintext"
    _seed(uid)
    monkeypatch.setattr(
        core_envelope,
        "resolve_content_encryption",
        lambda user_id: "off" if user_id == uid else "on",
    )
    job_id, _job = _running(uid)
    action = {
        "type": "memory.add",
        "envelope": _plaintext_envelope(uid, "mom-plaintext"),
    }

    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[action],
    )
    result = jobs_store.commit_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        batch_id=batch["id"],
    )

    assert result["committed"] is True
    with db.get_pool().connection() as conn:
        moment = conn.execute(
            "SELECT doc FROM memory_moments WHERE user_id=%s AND moment_id=%s",
            (uid, "mom-plaintext"),
        ).fetchone()[0]
    assert moment["body"].startswith("{\"summary\":")
    assert "body_ct" not in moment


def test_capture_rejects_plaintext_envelope_for_encrypted_user(monkeypatch):
    uid = "u_capture_encrypted_plaintext_rejected"
    _seed(uid)
    monkeypatch.setattr(core_envelope, "resolve_content_encryption", lambda _uid: "on")
    job_id, _job = _running(uid)

    with pytest.raises(ValueError, match="plaintext envelope not enabled"):
        jobs_store.prepare_capture_batch(
            job_id=job_id,
            user_id=uid,
            claimed_by="capture-worker",
            window=_window(),
            actions=[
                {
                    "type": "memory.add",
                    "envelope": _plaintext_envelope(uid, "mom-rejected"),
                }
            ],
        )


def test_capture_rejects_mixed_plaintext_and_ciphertext_envelope(monkeypatch):
    uid = "u_capture_mixed_shape_rejected"
    _seed(uid)
    monkeypatch.setattr(core_envelope, "resolve_content_encryption", lambda _uid: "off")
    job_id, _job = _running(uid)
    envelope = _envelope(uid, "mom-mixed")
    envelope["body"] = "plaintext must not coexist"

    with pytest.raises(ValueError, match="content shape invalid"):
        jobs_store.prepare_capture_batch(
            job_id=job_id,
            user_id=uid,
            claimed_by="capture-worker",
            window=_window(),
            actions=[{"type": "memory.add", "envelope": envelope}],
        )


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


def _prepare_supersede(uid: str, actions: list[dict]) -> tuple[int, dict]:
    job_id, _job = _running(uid)
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=actions,
    )
    assert batch is not None
    return job_id, batch


@pytest.mark.parametrize(
    "retired_fields",
    [
        {"status": "superseded", "superseded_by": "user-correction"},
        {"is_archived": True, "archived_at": "2026-07-21T00:00:00Z",
         "archive_reason": "repair"},
    ],
    ids=["superseded", "archived"],
)
def test_capture_commit_rejects_supersede_of_target_retired_after_prepare(retired_fields):
    """之前：prepare 后用户改/归档了目标卡，commit 仍然再退休一次 → 两张 active 后继。
    之后：commit 在行锁下重查目标，已不 active 就整批按语义拒绝（和目标缺失同一分支）。"""
    uid = "u_capture_target_retired"
    _seed(uid)
    assert db.memory_upsert(uid, "target", "2026-07-20", _manual_card(uid, "target"))
    job_id, batch = _prepare_supersede(uid, [{
        "type": "memory.supersede",
        "supersedes": ["target"],
        "envelope": _envelope(uid, "replacement"),
    }])
    retired = {**_manual_card(uid, "target"), **retired_fields}
    assert db.memory_upsert(uid, "target", "2026-07-20", retired)

    result = jobs_store.commit_capture_batch(
        job_id=job_id, user_id=uid, claimed_by="capture-worker", batch_id=batch["id"],
    )

    assert result == {
        "committed": False,
        "reason": "capture_supersede_target_inactive",
        "rejected": True,
    }
    moments = {m["id"]: m for m in db.memory_load(uid)}
    assert set(moments) == {"target"}
    assert moments["target"] == retired
    state = _capture_state(uid)
    assert int(state.get("last_captured_until_seq") or 0) == 0
    # The window travels with the rejection, so the escape valve can count it.
    assert int(state["capture_fail_streak"]) == 1
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0


def test_capture_commit_rejects_two_supersedes_of_one_target_in_one_batch():
    uid = "u_capture_target_twice"
    _seed(uid)
    assert db.memory_upsert(uid, "target", "2026-07-20", _manual_card(uid, "target"))
    job_id, batch = _prepare_supersede(uid, [
        {"type": "memory.supersede", "supersedes": ["target"],
         "envelope": _envelope(uid, "successor-a", body="a")},
        {"type": "memory.supersede", "supersedes": ["target"],
         "envelope": _envelope(uid, "successor-b", body="b")},
    ])

    result = jobs_store.commit_capture_batch(
        job_id=job_id, user_id=uid, claimed_by="capture-worker", batch_id=batch["id"],
    )

    assert result["reason"] == "capture_supersede_target_inactive"
    assert {m["id"] for m in db.memory_load(uid)} == {"target"}
    assert db.memory_load(uid)[0]["status"] == "active"


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


def test_bootstrap_events_are_written_without_a_ts():
    """Guards the premise of the data-track bootstrap_events pushdown.

    admin_data_track_snapshot no longer reads bootstrap_events fleet-wide
    (db._PAGED_LOG_STREAMS). That is an identity transform for the fleet-wide
    last_activity_at only because bootstrap_events.ts is structurally always
    NULL — neither write site passes one. That is a property of the writers,
    not a schema constraint: the moment someone stamps a ts, the fleet-wide
    _latest_epoch behind active_1d/3d silently stops seeing this stream while
    the detail page still sees it. This must go red before that ships.

    Anchored on behaviour — write a row through each path, read its ts column
    back — not on line numbers or source text.
    """
    from bootstrap import gates as boot_gates

    uid = "u_bootstrap_ts_guard"
    _seed(uid)

    class _Store:
        user_id = uid

    boot_gates._log_bootstrap_event(_Store(), "guard_probe", success=True)

    job_id, _job = _running(uid)
    batch = jobs_store.prepare_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        window=_window(),
        actions=[_add(uid, "mom-bootstrap-ts")],
    )
    assert batch is not None
    assert jobs_store.commit_capture_batch(
        job_id=job_id,
        user_id=uid,
        claimed_by="capture-worker",
        batch_id=batch["id"],
    )["committed"] is True

    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT doc->>'event_type', ts FROM user_logs "
            "WHERE user_id=%s AND stream='bootstrap_events' ORDER BY seq",
            (uid,),
        ).fetchall()

    written = {row[0] for row in rows}
    assert "guard_probe" in written, "the bootstrap/gates write path never ran"
    assert "memory_action_added_envelope_v1" in written, (
        "the V2 capture-commit write path never ran"
    )
    assert [row[1] for row in rows] == [None] * len(rows), (
        "a bootstrap_events row now carries a ts; the data-track fleet query no "
        "longer reads this stream, so last_activity_at (and active_1d/3d) will "
        "silently ignore it — revisit db._PAGED_LOG_STREAMS before landing this"
    )


# ── 落卡毒窗口逃生阀（V2 真路径）──────────────────────────────────────────
#
# 下面两条走真的 worker + 真的 jobs_store + 真的 Postgres，验「逃生阀在 V2
# 故障现场会不会真的触发」。只调纯函数的测试（test_v2_capture_poison_window_escape）
# 验不出调用点的问题 —— 这次 Codex review 抓到的三处全是那类：V2 失败原因带
# ``extraction_failed:`` 前缀、新用户起点只有 seq 0、prepared 重试时窗口是空壳。


def _poison_deps(uid: str, *, messages: list[dict], **overrides) -> "worker.TurnDeps":
    def tail(_uid, after_seq, *_args, **_kwargs):
        return [m for m in messages if int(m["seq"]) > int(after_seq)]

    kwargs = dict(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        read_memory_context=lambda _uid: {},
        # 生产装配（serve_worker._read_capture_state）：读状态要经过 _state_doc 归一化。
        # 用原始 blob 当替身会漏掉「写了字段、归一化白名单却没有」这类 bug（Codex 第 7 轮）。
        read_capture_state=serve_worker._read_capture_state,
        read_compaction_tail_after_seq=tail,
        build_memory_envelope=lambda *_args: {},
        get_prepared_capture_batch=jobs_store.get_prepared_capture_batch,
        prepare_capture_batch=jobs_store.prepare_capture_batch,
        authorize_capture_provider_call=jobs_store.authorize_capture_provider_call,
        commit_capture_batch=jobs_store.commit_capture_batch,
        fail_capture_job=jobs_store.fail_capture_job,
        cancel_capture_job=jobs_store.cancel_capture_job,
    )
    kwargs.update(overrides)
    return worker.TurnDeps(**kwargs)


def _run_capture(uid: str, job: dict, deps, owner: str) -> str:
    return asyncio.run(
        worker._run_extraction(
            job["id"], uid, "capture", deps, object(), asyncio.Semaphore(1),
            claimed_by=owner,
        )
    )


def _capture_state(uid: str) -> dict:
    return dict(db.get_blob_strict(uid, "capture_state") or {})


def test_v2_first_window_parse_failure_is_skipped_after_three_real_runs(monkeypatch):
    """🔴 新用户第一批就吐坏 JSON：连续 3 次后游标推过去，而不是永久卡死。

    同时覆盖两个只有真路径才暴露的点：
    - V2 报的是 ``extraction_failed:json_decode_error``（带前缀），要落进 3 次档
    - 新用户起点没有消息 id、seq 是 0；每次失败之间新消息让终点前移，
      身份键不能跟着终点变
    """
    from memory import capture_failure
    from model_api_runtime.v2 import extraction

    uid = "u_capture_poison_first_window"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})

    async def bad_json(**_kwargs):
        return [], "json_decode_error:JSONDecodeError"

    monkeypatch.setattr(extraction, "extract", bad_json)
    messages: list[dict] = []

    def add_message(seq: int) -> None:
        messages.append({
            "id": f"m{seq}", "seq": seq, "ts": float(seq), "role": "user",
            "raw_role": "user", "source": "chat", "capture_eligible": True,
            "content": f"message {seq}",
        })

    add_message(1)
    add_message(2)
    for attempt in range(1, capture_failure.CAPTURE_POISON_SKIP_AFTER + 1):
        monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: len(messages))
        owner = f"capture-worker-{attempt}"
        _job_id, job = _running(uid, owner=owner)
        assert _run_capture(uid, job, _poison_deps(uid, messages=messages), owner) == "failed"
        state = _capture_state(uid)
        if attempt < capture_failure.CAPTURE_POISON_SKIP_AFTER:
            assert int(state.get("last_captured_until_seq") or 0) == 0, f"第 {attempt} 次就跳了"
            assert int(state["capture_fail_streak"]) == attempt, "身份键跟着终点变了，streak 被重置"
            # 字段名和 V1 的状态归一化（capture_scheduler._state_doc）必须是同一个，
            # 否则 V1 那边读回来就丢了（我拆模块时批量改名误伤过一次）。
            assert state["capture_fail_window_key"] == "after_seq:0"
            add_message(len(messages) + 1)  # 故障期间用户还在聊天，终点前移

    assert int(state["last_captured_until_seq"]) == len(messages)
    assert state["last_captured_until_message_id"] == f"m{len(messages)}"
    assert int(state["capture_skipped_windows"]) == 1
    assert int(state["capture_fail_streak"]) == 0


@pytest.mark.parametrize("entry", ["run_turn", "run_extraction"])
def test_prepared_batch_whose_commit_keeps_raising_is_eventually_skipped(monkeypatch, entry):
    """🔴 prepared 批次每次提交都抛异常（比如写入时拿不到密钥）：到上限后跳过。

    以前 prepared 重试时窗口是个 until_message_id="" 的空壳，逃生阀认为
    「说不清推到哪」而永远不跳 —— 那个批次就成了新的队头阻塞。

    prepared 批次在 worker 里有两处恢复：
    - run_turn：生产入口 _run_turn_body 在进 _run_extraction **之前**就恢复（真实流量走这里）
    - run_extraction：_run_extraction 内部那段兜底
    两处各漏过一次窗口（第一轮修了后者，第三轮 review 才发现前者），所以两条都测。
    """
    from memory import capture_failure

    uid = "u_capture_poison_prepared_retry"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    first_id, _job = _running(uid, owner="first-owner")
    assert jobs_store.prepare_capture_batch(
        job_id=first_id, user_id=uid, claimed_by="first-owner",
        window=_window(after=0, through=4), actions=[_add(uid, "mom-stuck")],
    ) is not None
    assert jobs_store.fail_capture_job(
        job_id=first_id, user_id=uid, claimed_by="first-owner",
        error="worker_crashed_after_prepare",
    )

    commit_calls = []

    def commit_raises(**kwargs):
        commit_calls.append(kwargs["batch_id"])
        raise RuntimeError("capture_memory_write_failed")

    deps = _poison_deps(uid, messages=[], commit_capture_batch=commit_raises,
                        capture_enabled=lambda _uid: True)
    limit = capture_failure.CAPTURE_TRANSIENT_SKIP_AFTER
    for attempt in range(1, limit + 1):
        owner = f"retry-owner-{attempt}"
        if entry == "run_turn":
            _job_id, job = _running(uid, owner=owner, start=False)
            assert asyncio.run(worker._run_turn(job, deps)) == "failed"
        else:
            _job_id, job = _running(uid, owner=owner, start=False)
            assert jobs_store.mark_running(job["id"], claimed_by=owner)
            assert _run_capture(uid, job, deps, owner) == "failed"
        state = _capture_state(uid)
        if attempt < limit:
            assert int(state.get("last_captured_until_seq") or 0) == 0, f"第 {attempt} 次就跳了"

    assert len(commit_calls) == limit, "每次都应该先去提交那个 prepared 批次"
    if entry == "run_turn":
        # 生产出口 _run_turn 挂着 V2 落卡提示：连续失败 ≥3 次后用户能看到「记忆整理受阻」。
        # 以前 V2 完全没有这条提示。
        from notices import core as notices_core

        rows = {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}
        assert "memory_backoff:capture" in rows, "V2 落卡连续失败，用户侧没有任何提示"
        # 第 limit 次触发了跳过：旧的「受阻、修好后补记」提示要被清掉（V2 按两个时间字段推断跳过）
        assert rows["memory_backoff:capture"]["resolved"] is True, "跳过后旧提示还挂着"
    assert int(state["last_captured_until_seq"]) == 4
    assert state["last_captured_until_message_id"] == "m4"
    assert int(state["capture_skipped_windows"]) == 1

    # 🔴 跳过的同一事务里就要删掉那个 prepared 批次 —— 生产上跳过后用户可能
    # 不再发消息、不会有下一个任务来顺带清理（Codex 第四轮）。
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0, "跳过后加密的批次内容还留在库里"

    # 游标推过去之后，那个旧批次不能再被捡起来重放。
    next_id, _job = _running(uid, owner="after-skip")
    assert jobs_store.get_prepared_capture_batch(
        job_id=next_id, user_id=uid, claimed_by="after-skip", after_seq=4,
    ) is None
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_capture_batches WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0


def test_commit_rejected_on_the_same_window_is_eventually_skipped():
    """🔴 同一窗口每次提交都被语义拒绝（例如 supersede 的目标已不存在）：到上限后跳过。

    拒绝分支会删批次、标失败，worker 看到 rejected 直接返回，**不会**再走带窗口的
    fail_capture_job。以前这里不带窗口 → streak 无限涨、永不跳过。
    """
    from memory import capture_failure

    uid = "u_capture_poison_commit_rejected"
    _seed(uid)
    limit = capture_failure.CAPTURE_TRANSIENT_SKIP_AFTER
    for attempt in range(1, limit + 1):
        owner = f"reject-owner-{attempt}"
        job_id, _job = _running(uid, owner=owner)
        batch = jobs_store.prepare_capture_batch(
            job_id=job_id, user_id=uid, claimed_by=owner,
            window=_window(after=0, through=3),
            actions=[{
                "type": "memory.supersede",
                "supersedes": "deleted-target",
                "envelope": _envelope(uid, f"mom-reject-{attempt}"),
            }],
        )
        assert batch is not None
        result = jobs_store.commit_capture_batch(
            job_id=job_id, user_id=uid, claimed_by=owner, batch_id=batch["id"],
        )
        assert result["rejected"] is True
        state = _capture_state(uid)
        if attempt < limit:
            assert int(state.get("last_captured_until_seq") or 0) == 0, f"第 {attempt} 次就跳了"
            assert int(state["capture_fail_streak"]) == attempt

    assert int(state["last_captured_until_seq"]) == 3
    assert state["last_captured_until_message_id"] == "m3"
    assert state["capture_seq_initialized"] is True
    assert int(state["capture_skipped_windows"]) == 1


# ── 进度两份记法不一致时（prod 上旧逃生阀留下的状态）──────────────────────


def _seed_chat_rows(uid: str, ids: list[str]) -> dict[str, int]:
    for i, mid in enumerate(ids, start=1):
        db.chat_append_strict(
            uid, mid, float(i),
            {"id": mid, "role": "user", "source": "chat", "ts": float(i)}, 5000,
        )
    return {mid: int(db.chat_seq_for_msg_id(uid, mid)) for mid in ids}


def _write_capture_state(uid: str, doc: dict) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO user_blobs (user_id,kind,doc) VALUES (%s,'capture_state',%s) "
            "ON CONFLICT (user_id,kind) DO UPDATE SET doc=EXCLUDED.doc",
            (uid, Jsonb(doc)),
        )


#: prod 上 V1 旧逃生阀跳过后留下的形状：数字被写成 0 且标了已初始化，id 记着真实位置。
def _corrupted_state(message_id: str) -> dict:
    return {
        "last_captured_until_message_id": message_id,
        "last_captured_until_seq": 0,
        "capture_seq_initialized": True,
    }


def test_worker_and_commit_agree_on_the_frontier_when_seq_was_zeroed(monkeypatch):
    """🔴 数字是 0、id 记着第 3 条：worker 从第 3 条之后开始，提交也认第 3 条。

    以前 worker 信数字（从 0 开始 → 把记过的历史重整一遍，记忆重复）；
    而只改 worker 不改提交的话，提交读到 0、批次起点是 3 → frontier_changed → 永远被拒。
    两边必须走同一个 frontier_seq。
    """
    from model_api_runtime.v2 import extraction

    uid = "u_capture_frontier_zeroed"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    seqs = _seed_chat_rows(uid, ["m1", "m2", "m3", "m4", "m5"])
    _write_capture_state(uid, _corrupted_state("m3"))

    # ① worker 算出来的起点
    async def bad_json(**_kwargs):
        return [], "json_decode_error:JSONDecodeError"

    monkeypatch.setattr(extraction, "extract", bad_json)
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: seqs["m5"])
    seen_windows: list[dict] = []

    def record_fail(**kwargs):
        seen_windows.append(dict(kwargs.get("window") or {}))
        return jobs_store.fail_capture_job(**kwargs)

    messages = [
        {"id": mid, "seq": seq, "ts": float(i), "role": "user", "raw_role": "user",
         "source": "chat", "capture_eligible": True, "content": mid}
        for i, (mid, seq) in enumerate(seqs.items(), start=1)
    ]
    _job_id, job = _running(uid, owner="frontier-worker")
    deps = _poison_deps(uid, messages=messages, fail_capture_job=record_fail)
    assert _run_capture(uid, job, deps, "frontier-worker") == "failed"
    assert seen_windows and seen_windows[0]["after_seq"] == seqs["m3"], (
        "worker 信了被写成 0 的数字，会把已经记过的历史重新整理一遍")

    # ② 同一个起点上提交必须成功，不能被当成「游标被别人推进了」。
    # 用「数字落后于 id」的形状（V1 完成只更新 id、数字停在旧值）：旧提交路径只要数字
    # 非 0 就直接信它 → 算出第 1 条，批次起点是第 3 条 → frontier_changed。
    # （数字恰好是 0 时旧提交路径会回落查 id，反而是旧 worker 信了 0 —— 两边照样对不上。）
    _write_capture_state(uid, {"last_captured_until_message_id": "m3",
                               "last_captured_until_seq": seqs["m1"],
                               "capture_seq_initialized": True})
    commit_id, _job = _running(uid, owner="frontier-commit")
    batch = jobs_store.prepare_capture_batch(
        job_id=commit_id, user_id=uid, claimed_by="frontier-commit",
        window={"after_seq": seqs["m3"], "through_seq": seqs["m5"],
                "after_message_id": "m3", "until_message_id": "m5", "until_ts": 5.0},
        actions=[_add(uid, "mom-frontier")],
    )
    assert batch is not None
    result = jobs_store.commit_capture_batch(
        job_id=commit_id, user_id=uid, claimed_by="frontier-commit", batch_id=batch["id"],
    )
    assert result.get("committed") is True, result
    state = _capture_state(uid)
    assert int(state["last_captured_until_seq"]) == seqs["m5"]
    assert state["capture_seq_initialized"] is True


@pytest.mark.parametrize("reason", [
    "extraction_failed:quota_insufficient",
    "extraction_failed:auth_invalid",
    "extraction_failed:upstream_unavailable",
])
def test_v2_account_failures_do_not_skip_by_count(reason):
    """🔴 账号/服务坏了（余额不足、密钥失效、上游不可用）：不按次数跳（7 天内），等修好后补上。

    7 天上限见 test_account_failures_skip_only_after_persisting_seven_days 和本文件的 V2 7 天边界测试；这里只测「次数再多也不跳」。
    """
    uid = f"u_capture_account_{reason.split(':')[1]}"
    _seed(uid)
    for attempt in range(1, 13):
        owner = f"account-owner-{attempt}"
        job_id, _job = _running(uid, owner=owner)
        assert jobs_store.fail_capture_job(
            job_id=job_id, user_id=uid, claimed_by=owner, error=reason,
            window=_window(after=0, through=3),
        )
    state = _capture_state(uid)
    assert int(state.get("last_captured_until_seq") or 0) == 0
    assert int(state.get("capture_skipped_windows") or 0) == 0
    assert int(state["capture_fail_streak"]) == 12, "退避/告警用的总连续失败数照常累加"


def test_disabling_capture_does_not_refresh_the_retrying_notice():
    """🔴 用户关掉落卡：任务被取消（不累计失败）却也返回 failed。

    提示钩子不能拿共享状态里的旧失败次数，再发一条「正在自动重试 / 修好后会补记」——
    落卡都关了，根本不会重试（Codex 第 6 轮）。走生产入口 _run_turn。
    """
    from notices import core as notices_core

    uid = "u_capture_disabled_notice"
    _seed(uid)
    _write_capture_state(uid, {
        "capture_fail_streak": 5,
        "last_capture_failed_at": 100.0,
        "last_capture_failed_job_id": "some-earlier-job",
        "capture_account_error_code": "quota_insufficient",
    })
    _job_id, job = _running(uid, start=False)
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        read_capture_state=serve_worker._read_capture_state,
        cancel_capture_job=jobs_store.cancel_capture_job,
        fail_capture_job=jobs_store.fail_capture_job,
        capture_enabled=lambda _uid: False,
    )
    assert asyncio.run(worker._run_turn(job, deps)) == "failed"
    keys = {r["dedupe_key"] for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}
    assert "memory_backoff:capture" not in keys



def test_v2_notice_is_raised_on_real_failures_and_cleared_by_real_commit():
    """V2 落卡：经生产入口 _run_turn 连续失败 3 次 → 提示出现；之后真实提交成功 → 提示清掉。

    读状态用生产装配（经 _state_doc 归一化），失败走真实 fail_capture_job，成功走真实
    commit_capture_batch（它负责把失败次数清零）。
    """
    from notices import core as notices_core

    uid = "u_capture_v2_notice_lifecycle"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    first_id, _job = _running(uid, owner="lifecycle-first")
    assert jobs_store.prepare_capture_batch(
        job_id=first_id, user_id=uid, claimed_by="lifecycle-first",
        window=_window(after=0, through=4), actions=[_add(uid, "mom-lifecycle")],
    ) is not None
    assert jobs_store.fail_capture_job(
        job_id=first_id, user_id=uid, claimed_by="lifecycle-first",
        error="worker_crashed_after_prepare",
    )

    def commit_raises(**_kwargs):
        raise RuntimeError("provider_http_503: upstream unavailable")

    deps = _poison_deps(uid, messages=[], commit_capture_batch=commit_raises,
                        capture_enabled=lambda _uid: True)
    for attempt in range(3):
        _job_id, job = _running(uid, owner=f"lifecycle-{attempt}", start=False)
        assert asyncio.run(worker._run_turn(job, deps)) == "failed"

    def _notice():
        rows = {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}
        return rows.get("memory_backoff:capture")

    raised = _notice()
    assert raised is not None and raised["resolved"] is False

    ok_deps = _poison_deps(uid, messages=[], commit_capture_batch=jobs_store.commit_capture_batch,
                           capture_enabled=lambda _uid: True)
    _job_id, job = _running(uid, owner="lifecycle-ok", start=False)
    assert asyncio.run(worker._run_turn(job, ok_deps)) == "completed"
    assert int(_capture_state(uid).get("capture_fail_streak") or 0) == 0
    assert _notice()["resolved"] is True, "真实提交成功后提示没有清掉"


def test_v2_account_failure_skips_after_seven_days_through_the_store(monkeypatch):
    """V2 持久化路径的 7 天边界：未满 7 天不跳，满 7 天跳过（时间来自 _capture_fail_on_cursor）。"""
    from memory import capture_failure

    uid = "u_capture_account_seven_days"
    _seed(uid)
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(jobs_store.time, "time", lambda: clock["now"])
    limit = capture_failure.CAPTURE_ACCOUNT_SKIP_AFTER_SEC

    def fail(owner):
        job_id, _job = _running(uid, owner=owner)
        assert jobs_store.fail_capture_job(
            job_id=job_id, user_id=uid, claimed_by=owner,
            error="extraction_failed:quota_insufficient", window=_window(after=0, through=3))

    # 正常退避下每 6 小时重试一次（中断超过 24 小时会重新计时）
    start = clock["now"]
    attempt = 0
    while clock["now"] < start + limit - 60:
        fail(f"seven-{attempt}")
        attempt += 1
        clock["now"] = min(clock["now"] + 6 * 3600, start + limit - 60)
        if clock["now"] == start + limit - 60:
            fail(f"seven-{attempt}")
            attempt += 1
            break
    assert int(_capture_state(uid).get("capture_skipped_windows") or 0) == 0, "未满 7 天就跳了"
    clock["now"] = start + limit + 60
    fail("seven-last")
    state = _capture_state(uid)
    assert int(state["capture_skipped_windows"]) == 1
    assert int(state["last_captured_until_seq"]) == 3


def test_success_clears_stale_account_cause_before_later_serverside_failures():
    """余额不足失败 → 成功提交 → 3 次服务端「批次丢失」：提示不能说「额度不足」（Codex 第 8 轮）。"""
    from notices import core as notices_core

    uid = "u_capture_stale_account_cause"
    _seed(uid)
    job_id, _job = _running(uid, owner="stale-0")
    assert jobs_store.fail_capture_job(
        job_id=job_id, user_id=uid, claimed_by="stale-0",
        error="extraction_failed:quota_insufficient", window=_window(after=0, through=3))
    assert _capture_state(uid)["capture_account_error_code"] == "quota_insufficient"

    ok_id, _job = _running(uid, owner="stale-ok")
    batch = jobs_store.prepare_capture_batch(
        job_id=ok_id, user_id=uid, claimed_by="stale-ok",
        window=_window(after=0, through=3), actions=[_add(uid, "mom-stale-ok")])
    assert jobs_store.commit_capture_batch(
        job_id=ok_id, user_id=uid, claimed_by="stale-ok", batch_id=batch["id"])["committed"]
    state = _capture_state(uid)
    assert state.get("capture_account_error_code", "") == ""
    assert int(state.get("capture_account_fail_since") or 0) == 0

    last = None
    for attempt in range(3):
        fid, _job = _running(uid, owner=f"stale-f{attempt}")
        assert jobs_store.fail_capture_job(
            job_id=fid, user_id=uid, claimed_by=f"stale-f{attempt}",
            error="capture_batch_unavailable")
        last = fid
    assert _capture_state(uid).get("capture_account_error_code", "") == ""
    deps = worker.TurnDeps(
        read_messages=lambda _u: [], resolve_provider=lambda _u: (object(), {}),
        mint_enclave_token=lambda _u: "rt", read_capture_state=serve_worker._read_capture_state)
    asyncio.run(worker._notify_capture_backoff(
        deps, {"lane": "capture", "user_id": uid, "id": last}, "failed"))
    rows = {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}
    assert "额度" not in rows["memory_backoff:capture"]["user_text"]


def test_provider_resolution_failure_uses_the_capture_failure_path():
    """V2 落卡在解析 provider 之前就失败（未配置/信封缺失）：也要累计退避、记任务 id、发提示；不跳过。"""
    from notices import core as notices_core

    uid = "u_capture_provider_unresolved"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    deps = _poison_deps(uid, messages=[], capture_enabled=lambda _uid: True,
                        resolve_provider=lambda _uid: (None, {"error": "model_api_not_configured"}))
    for attempt in range(3):
        _job_id, job = _running(uid, owner=f"unresolved-{attempt}", start=False)
        assert asyncio.run(worker._run_turn(job, deps)) == "failed"
    state = _capture_state(uid)
    assert int(state["capture_fail_streak"]) == 3, "provider 前置失败没进落卡失败框架，退避不会生效"
    assert int(state.get("capture_skipped_windows") or 0) == 0
    keys = {r["dedupe_key"] for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}
    assert "memory_backoff:capture" in keys


def test_missing_call_transcript_batch_is_eventually_skipped(monkeypatch):
    """🔴 批次里有一张通话卡、它的转写取不到：以前窗口没有终点，逃生阀永远不跳 → 永久卡死。

    窗口终点现在在读到这批之后立刻定下，先于取转写。独立审查复现过 12 次失败 0 次跳过。
    """
    from memory import capture_failure

    uid = "u_capture_voice_transcript_missing"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    messages = [
        {"id": "m1", "seq": 1, "ts": 1.0, "role": "user", "raw_role": "user",
         "source": "voice_call_transcript", "voice_call_id": "call_x",
         "capture_eligible": True, "content": "preview"},
        {"id": "m2", "seq": 2, "ts": 2.0, "role": "user", "raw_role": "user",
         "source": "chat", "capture_eligible": True, "content": "hi"},
    ]
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: 2)

    def missing(_uid, _call):
        raise RuntimeError("voice_transcript_not_found")

    deps = _poison_deps(uid, messages=messages, read_voice_transcript=missing)
    limit = capture_failure.CAPTURE_TRANSIENT_SKIP_AFTER
    for attempt in range(limit):
        _jid, job = _running(uid, owner=f"voice-{attempt}")
        assert _run_capture(uid, job, deps, f"voice-{attempt}") == "failed"
    state = _capture_state(uid)
    assert int(state.get("capture_skipped_windows") or 0) == 1
    assert int(state["last_captured_until_seq"]) == 2


def test_outer_turn_failure_on_capture_arms_backoff(monkeypatch):
    """落卡在 _run_turn_body 外层就抛异常（例如 provider 解析抛错）：也要累计退避、发得出提示。"""
    uid = "u_capture_outer_turn_failure"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})

    def boom(_uid):
        raise RuntimeError("enclave down")

    deps = _poison_deps(uid, messages=[], capture_enabled=lambda _uid: True, resolve_provider=boom)
    for attempt in range(3):
        _job_id, job = _running(uid, owner=f"outer-{attempt}", start=False)
        assert asyncio.run(worker._run_turn(job, deps)) == "failed"
    state = _capture_state(uid)
    assert int(state["capture_fail_streak"]) == 3
    assert int(state.get("capture_skipped_windows") or 0) == 0


@pytest.mark.parametrize("resolver_error,user_fix", [
    ("model_api_not_tested", True),
    ("model_api_not_configured", True),
    ("model_api_key_decrypt_failed", False),
    ("runtime_token_mint_failed", False),
])
def test_provider_setup_failures_tell_the_user_to_fix_settings(resolver_error, user_fix):
    """用户自己的模型配置问题要提示去设置里修；解密/签发失败是我们的问题，不能甩给用户。"""
    from notices import core as notices_core

    uid = f"u_capture_provider_setup_{resolver_error}"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    deps = _poison_deps(uid, messages=[], capture_enabled=lambda _uid: True,
                        resolve_provider=lambda _uid: (None, {"error": resolver_error}))
    for attempt in range(3):
        _job_id, job = _running(uid, owner=f"setup-{attempt}", start=False)
        assert asyncio.run(worker._run_turn(job, deps)) == "failed"
    rows = {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}
    notice = rows["memory_backoff:capture"]
    if user_fix:
        assert notice["blame"] == "user_provider" and "设置" in notice["user_text"]
    else:
        assert notice["blame"] == "system" and "设置" not in notice["user_text"]


def test_provider_setup_codes_are_registered_public_failure_codes():
    """provider_setup:<slug> 必须在产生方公开词表里：否则 admin 时间线遮蔽、rollup 当未知码。"""
    from admin import data_track
    from memory import capture_failure

    visible = data_track._load_worker_failure_codes()
    for slug in capture_failure.PROVIDER_SETUP_USER_ERRORS:
        assert f"provider_setup:{slug}" in visible
    assert "provider_unavailable" in visible


# ---------------------------------------------------------------------------
# 崩溃/卡死的落卡任务：平台回收（租约回收器 / watchdog）也要进落卡失败框架
# ---------------------------------------------------------------------------
#
# 以前这两条回收路径只改任务行、不碰 capture_state：一批消息只要每次都把 worker
# 弄崩/卡死，调度器就不停重建任务 —— 没有退避、没有提示。

import time as _time


def _reap_future() -> list[dict]:
    """租约回收器一轮，时间拨到所有租约都已过期之后（DB 里的租约不用手改）。"""
    return jobs_store.reap_stuck_job_rows(
        now=_time.time() + jobs_store.RUNNING_TTL_SEC + 10
    )


def _job_row(job_id: int):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT status,attempt_count,last_error,claimed_by,"
            "finished_at IS NOT NULL,lease_expires_at IS NOT NULL "
            "FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()


def _backoff_notice(uid: str):
    from notices import core as notices_core

    rows = {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}
    return rows.get("memory_backoff:capture")


def test_reaped_capture_crash_counts_once_per_expiry_and_notices_without_skipping():
    """同一批每次都把 worker 弄崩（租约过期被回收）：每次回收记 1 次失败，第 3 次出提示，游标不动。"""
    uid = "u_capture_reaped_crash"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    _write_capture_state(uid, {
        "last_captured_until_message_id": "m2",
        "last_captured_until_seq": 2,
        "capture_seq_initialized": True,
    })

    for attempt in range(1, 4):
        job_id, _job = _running(uid, owner=f"crash-{attempt}")
        reaped = _reap_future()
        assert [(r["id"], r["lane"], r["last_error"]) for r in reaped] == [
            (job_id, "capture", "lease_timeout")
        ]
        # 同一轮之后再扫一次：任务已经终结，不能再记一次。
        assert _reap_future() == []
        assert _job_row(job_id)[:3] == ("expired", 1, "lease_timeout")

        state = _capture_state(uid)
        assert int(state["capture_fail_streak"]) == attempt
        assert state["last_capture_failed_job_id"] == str(job_id)
        assert float(state["last_capture_failed_at"]) > 0
        # worker 崩溃是平台问题，不能提示成「你的模型服务不可用」。
        assert state["capture_account_error_code"] == ""
        notice = _backoff_notice(uid)
        if attempt < 3:
            assert notice is None
        else:
            assert notice is not None and notice["resolved"] is False
            assert notice["blame"] == "system"

    state = _capture_state(uid)
    assert int(state["last_captured_until_seq"]) == 2
    assert state["last_captured_until_message_id"] == "m2"
    assert int(state.get("capture_skipped_windows") or 0) == 0


def test_watchdog_capture_requeues_do_not_count_and_exhaustion_counts_once():
    """watchdog 杀进程：重投不记（同一任务还会再跑），重投预算用完终结时记 1 次。"""
    uid = "u_capture_watchdog_budget"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    job_id, _job = _running(uid, owner="slot-0:g1")

    outcomes = []
    for kill in range(1, jobs_store.GENERAL_LEASE_REQUEUE_MAX_ATTEMPTS + 2):
        recovered = jobs_store.recover_killed_job(
            job_id=job_id, claimed_by="slot-0:g1", reason="slot_watchdog_timeout",
        )
        assert recovered is not None
        assert recovered == {
            "job_id": job_id, "user_id": uid, "lane": "capture",
            "recovery": recovered["recovery"],
        }
        outcomes.append(recovered["recovery"])
        streak = int(_capture_state(uid).get("capture_fail_streak") or 0)
        if recovered["recovery"] == "requeued":
            assert _job_row(job_id)[:3] == ("pending", kill, "slot_watchdog_timeout")
            assert streak == 0, "重投也记了失败 —— 一次卡死会被数成好几次"
            claimed = jobs_store.claim_next_job("slot-0:g1", lanes={"capture"})
            assert claimed is not None and int(claimed["id"]) == job_id
            assert jobs_store.mark_running(job_id, claimed_by="slot-0:g1")
        else:
            assert _job_row(job_id)[:4] == (
                "expired", kill, jobs_store.GENERAL_WATCHDOG_REQUEUE_EXHAUSTED, None,
            )
            assert streak == 1

    assert outcomes == ["requeued"] * jobs_store.GENERAL_LEASE_REQUEUE_MAX_ATTEMPTS + [
        "terminal"
    ]
    with db.get_pool().connection() as conn:
        events = conn.execute(
            "SELECT job_attempt_count,recovery,reason FROM v2_job_recovery_events "
            "WHERE job_id=%s ORDER BY job_attempt_count",
            (job_id,),
        ).fetchall()
    assert [e[1] for e in events] == outcomes
    assert [e[0] for e in events] == list(range(1, len(outcomes) + 1))

    # 重复的 watchdog 回收请求 / 之后的租约回收器都不能再记一次。
    assert jobs_store.recover_killed_job(job_id=job_id, claimed_by="slot-0:g1") is None
    assert _reap_future() == []
    state = _capture_state(uid)
    assert int(state["capture_fail_streak"]) == 1
    assert state["last_capture_failed_job_id"] == str(job_id)
    assert state["capture_account_error_code"] == ""


def test_stale_capture_expiry_after_a_newer_capture_succeeded_records_nothing(monkeypatch):
    """回收器挑中候选之后、加锁之前，新的落卡任务已经成功（游标推进、失败清零）：旧回收什么都不记。

    走生产路径制造竞态：候选任务租约真实过期 → 调度器入队时把它收掉（记一次失败）→ 再入队建新任务 →
    新任务真实 prepare + commit 成功 → 回收器这才处理那个旧候选。
    """
    uid = "u_capture_stale_expiry"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    seqs = _seed_chat_rows(uid, ["m1", "m2", "m3"])
    _write_capture_state(uid, {"capture_fail_streak": 2, "last_capture_failed_at": 1.0})
    old_id, _job = _running(uid, owner="old-worker")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (old_id,),
        )

    original = jobs_store._recover_capture_claim
    newer: dict = {}

    def newer_capture_wins_first(**kwargs):
        if not newer:
            # 调度器先撞见过期的旧任务：终结 + 记一次失败，这一轮不建新任务（第 12 轮 C1）。
            assert jobs_store.enqueue_job(uid, "capture") == (old_id, True)
            newer["streak_after_enqueue"] = int(_capture_state(uid)["capture_fail_streak"])
            new_id, _new_job = _running(uid, owner="new-worker")
            assert new_id != old_id
            batch = jobs_store.prepare_capture_batch(
                job_id=new_id, user_id=uid, claimed_by="new-worker",
                window=_window(after=0, through=seqs["m3"]),
                actions=[_add(uid, "mom-newer")],
            )
            assert jobs_store.commit_capture_batch(
                job_id=new_id, user_id=uid, claimed_by="new-worker", batch_id=batch["id"],
            )["committed"] is True
            newer["id"] = new_id
        return original(**kwargs)

    monkeypatch.setattr(jobs_store, "_recover_capture_claim", newer_capture_wins_first)
    assert jobs_store.reap_stuck_job_rows() == []

    state = _capture_state(uid)
    assert newer["streak_after_enqueue"] == 3
    assert int(state["capture_fail_streak"]) == 0
    assert int(state["last_captured_until_seq"]) == seqs["m3"]
    assert _job_row(old_id)[:3] == ("expired", 1, "lease_timeout")
    assert _job_row(newer["id"])[0] == "completed"


def test_capture_expiry_does_not_count_when_a_newer_capture_already_ran():
    """防御性守卫：同一用户已有**更新的**落卡任务跑过，旧任务仍卡在 claimed，回收它时只终结不记账。

    正常的单飞保证下两者不会同时存在（这里直接写库构造）；守卫防的是这种状态一旦出现，
    旧回收把失败叠加到新任务已经清零的状态上。
    """
    uid = "u_capture_newer_job_guard"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    old_id, _job = _running(uid, owner="old-worker")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id,lane,status,finished_at,"
            "expected_runtime_generation) VALUES (%s,'capture','completed',now(),1)",
            (uid,),
        )
    _write_capture_state(uid, {"capture_fail_streak": 0, "last_captured_until_seq": 7,
                               "capture_seq_initialized": True})

    assert [r["id"] for r in _reap_future()] == [old_id]

    assert _job_row(old_id)[:3] == ("expired", 1, "lease_timeout")
    state = _capture_state(uid)
    assert int(state["capture_fail_streak"]) == 0
    assert "last_capture_failed_job_id" not in state


def test_capture_expiry_does_not_count_when_capture_was_disabled():
    """用户已关掉落卡：和 worker 的取消路径一致，崩溃回收不累计、不发「正在重试」提示。"""
    uid = "u_capture_crash_disabled"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    for attempt in range(3):
        job_id, _job = _running(uid, owner=f"disabled-{attempt}")
        if attempt == 0:
            core_store.UserStore(uid).save_proactive_settings({"capture_enabled": False})
        assert [r["id"] for r in _reap_future()] == [job_id]
    assert int(_capture_state(uid).get("capture_fail_streak") or 0) == 0
    assert _backoff_notice(uid) is None


@pytest.mark.parametrize("lane", ["dream", "profile", "chat"])
def test_reaper_still_expires_non_capture_claims_exactly_as_before(lane):
    """别的 lane 仍走原来的通用回收：同样的终态、同样的列，也不碰 capture_state。"""
    uid = f"u_capture_reaper_other_{lane}"
    _seed(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    claimed = jobs_store.claim_next_job(f"{lane}-owner", lanes={lane})
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=f"{lane}-owner")

    reaped = _reap_future()

    assert reaped == [{
        "id": job_id, "user_id": uid, "lane": lane,
        "last_error": "lease_timeout", "claimed_by": f"{lane}-owner",
    }]
    # 终结 CTE 不清 claimed_by / lease：保持原样。
    assert _job_row(job_id) == ("expired", 1, "lease_timeout", f"{lane}-owner", True, True)
    assert db.get_blob_strict(uid, "capture_state") is None


def test_capture_lease_expiry_moves_the_job_row_like_the_generic_reaper(monkeypatch):
    """落卡摘出来单独回收后，任务行的变化必须和通用路径逐列一致（两份 SQL 不许漂）。"""
    # 打开失败复核：通用路径会给终结的任务建复核请求，落卡这条也必须建。
    monkeypatch.setenv(jobs_store._TRAJECTORY_REVIEW_ENABLED_ENV, "1")
    capture_uid = "u_capture_reaper_parity_capture"
    other_uid = "u_capture_reaper_parity_profile"
    _seed(capture_uid)
    _seed(other_uid)
    capture_id, _ = _running(capture_uid, owner="parity-owner")
    other_id, _ = jobs_store.enqueue_job(other_uid, "profile")
    claimed = jobs_store.claim_next_job("parity-owner", lanes={"profile"})
    assert claimed is not None and int(claimed["id"]) == other_id
    assert jobs_store.mark_running(other_id, claimed_by="parity-owner")

    reaped = {r["id"]: r for r in _reap_future()}

    assert set(reaped) == {capture_id, other_id}
    assert {k: v for k, v in reaped[capture_id].items() if k not in {"id", "user_id", "lane"}} == {
        k: v for k, v in reaped[other_id].items() if k not in {"id", "user_id", "lane"}
    }
    assert _job_row(capture_id) == _job_row(other_id)
    with db.get_pool().connection() as conn:
        reviews = conn.execute(
            "SELECT source_job_id FROM v2_trajectory_reviews WHERE source_job_id = ANY(%s)",
            ([capture_id, other_id],),
        ).fetchall()
    assert sorted(r[0] for r in reviews) == sorted([capture_id, other_id]), (
        "失败复核只给了其中一个 lane")


@pytest.mark.parametrize("first,second", [
    ("worker_fail", "reaper"),
    ("reaper", "worker_fail"),
    ("reaper", "chat_clear"),
])
def test_capture_recovery_serializes_with_capture_lock_holders_without_deadlock(
    monkeypatch, first, second
):
    """回收器和其他握落卡锁的事务同时动同一个用户：只会排队、不会死锁，失败只记一次。

    - worker 亲手报失败（fail_capture_job：chat fence → runtime → job → capture_state）
    - Chat Clear（独占 chat fence → runtime → 改 job 行）：如果回收器先锁 job 行、后拿 fence，
      就会和它形成环 —— 回收器握 job 等 fence，Clear 握 fence 等 job。

    先动手的一方拿齐锁后停住，另一方在另一个连接上开始并且必须真的在等锁；放行后两边都
    必须正常完成（chat_clear 出错会返回 None，回收器单个任务出错会被吞掉返回 []，都在断言里）。
    """
    uid = f"u_capture_recovery_race_{first}_{second}"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    job_id, _job = _running(uid, owner="race-worker")
    holding = threading.Event()
    release = threading.Event()

    if first == "worker_fail":
        original_state = jobs_store._capture_failed_state

        def hold_after_state_lock(*args, **kwargs):
            if threading.current_thread().name == "race-first":
                holding.set()
                assert release.wait(10)
            return original_state(*args, **kwargs)

        monkeypatch.setattr(jobs_store, "_capture_failed_state", hold_after_state_lock)
    else:
        original_record = jobs_store._record_capture_crash_failure_on_cursor

        def hold_after_terminal(cur, **kwargs):
            failed = original_record(cur, **kwargs)
            if threading.current_thread().name == "race-first":
                holding.set()
                assert release.wait(10)
            return failed

        monkeypatch.setattr(
            jobs_store, "_record_capture_crash_failure_on_cursor", hold_after_terminal
        )

    results: dict = {}
    errors: list[BaseException] = []

    def run(name, fn):
        try:
            results[name] = fn()
        except BaseException as exc:  # noqa: BLE001 — 线程里的失败要带回主线程断言
            errors.append(exc)

    def worker_fail():
        return jobs_store.fail_capture_job(
            job_id=job_id, user_id=uid, claimed_by="race-worker",
            error="capture_agent_call_failed: boom",
        )

    calls = {
        "worker_fail": worker_fail,
        "reaper": _reap_future,
        "chat_clear": lambda: db.chat_clear(uid),
    }
    t1 = threading.Thread(target=run, args=(first, calls[first]), name="race-first")
    t1.start()
    assert holding.wait(10)
    t2 = threading.Thread(target=run, args=(second, calls[second]), name="race-second")
    t2.start()
    t2.join(1.0)
    assert t2.is_alive(), "第二个事务没有等锁 —— 两边并没有真正争同一组锁"
    release.set()
    t1.join(15)
    t2.join(15)
    assert not t1.is_alive() and not t2.is_alive()
    assert errors == []

    if first == "worker_fail":
        assert results["worker_fail"] is True and results["reaper"] == []
        assert _job_row(job_id)[0] == "failed"
    else:
        assert [r["id"] for r in results["reaper"]] == [job_id]
        assert _job_row(job_id)[0] == "expired"
    if second == "chat_clear":
        assert results["chat_clear"] is not None, "Chat Clear 事务失败（死锁牺牲品？）"
        return
    if second == "worker_fail":
        assert results["worker_fail"] is False
    state = _capture_state(uid)
    assert int(state["capture_fail_streak"]) == 1
    assert state["last_capture_failed_job_id"] == str(job_id)




def _expire_lease(job_id: int) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (job_id,),
        )


def _active_capture_jobs(uid: str) -> list:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT id,status FROM agent_jobs WHERE user_id=%s AND lane='capture' "
            "AND status IN ('pending','claimed','running') ORDER BY id",
            (uid,),
        ).fetchall()


def test_crash_expiry_notice_blames_the_system_even_after_a_quota_failure():
    """额度不足失败之后又碰上 worker 崩溃：最近一次失败是平台问题，提示不能还让用户去充值（第 12 轮 I1）。"""
    uid = "u_capture_crash_blames_system"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    for attempt in range(2):
        job_id, _job = _running(uid, owner=f"quota-{attempt}")
        assert jobs_store.fail_capture_job(
            job_id=job_id, user_id=uid, claimed_by=f"quota-{attempt}",
            error="extraction_failed:quota_insufficient", window=_window(after=0, through=3))
    assert _capture_state(uid)["capture_account_error_code"] == "quota_insufficient"

    crash_id, _job = _running(uid, owner="crash")
    assert [r["id"] for r in _reap_future()] == [crash_id]

    state = _capture_state(uid)
    assert int(state["capture_fail_streak"]) == 3
    assert state["capture_account_error_code"] == ""
    notice = _backoff_notice(uid)
    assert notice is not None and notice["resolved"] is False
    assert notice["blame"] == "system"
    assert "充值" not in notice["user_text"] and "设置" not in notice["user_text"]


def test_crash_between_parse_failures_breaks_the_consecutive_parse_count():
    """解析 ×2 → worker 崩溃被回收 → 解析 ×1：不是「连续 3 次解析失败」，不能跳过这一批（第 12 轮 C2）。"""
    uid = "u_capture_parse_crash_parse"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    _write_capture_state(uid, {
        "last_captured_until_message_id": "m2",
        "last_captured_until_seq": 2,
        "capture_seq_initialized": True,
    })
    window = _window(after=2, through=5)

    def parse_failure(owner: str) -> None:
        job_id, _job = _running(uid, owner=owner)
        assert jobs_store.fail_capture_job(
            job_id=job_id, user_id=uid, claimed_by=owner,
            error="extraction_failed:json_decode_error:JSONDecodeError", window=window)

    parse_failure("parse-1")
    parse_failure("parse-2")
    assert int(_capture_state(uid)["capture_parse_fail_streak"]) == 2

    crash_id, _job = _running(uid, owner="crash")
    assert [r["id"] for r in _reap_future()] == [crash_id]
    state = _capture_state(uid)
    assert int(state["capture_parse_fail_streak"]) == 0
    assert int(state["capture_fail_streak"]) == 3

    parse_failure("parse-3")
    state = _capture_state(uid)
    assert int(state.get("capture_skipped_windows") or 0) == 0, "崩溃夹在中间仍被当成连续解析失败跳过了"
    assert state["last_captured_until_message_id"] == "m2"
    assert int(state["last_captured_until_seq"]) == 2
    assert int(state["capture_parse_fail_streak"]) == 1
    assert int(state["capture_fail_streak"]) == 4


def test_scheduler_enqueue_counts_a_crashed_capture_and_does_not_rebuild_in_the_same_tick():
    """调度器入队撞见租约已过期的落卡任务：终结 + 记一次失败，这一轮不建新任务（第 12 轮 C1）。

    以前通用入队就地把旧任务改成 expired 并立刻建新任务、不记账；调度器和回收器都 30 秒一轮，
    调度器一直抢先的话「崩溃 → 重建 → 崩溃」永远没有退避、没有提示。
    """
    uid = "u_capture_enqueue_crash"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    _write_capture_state(uid, {
        "last_captured_until_message_id": "m2",
        "last_captured_until_seq": 2,
        "capture_seq_initialized": True,
    })
    for attempt in range(1, 4):
        job_id, _job = _running(uid, owner=f"enqueue-crash-{attempt}")
        # 仍在租约内：照旧合并进正在跑的任务。
        assert jobs_store.enqueue_job(uid, "capture") == (job_id, True)
        assert _job_row(job_id)[0] == "running"
        _expire_lease(job_id)

        assert jobs_store.enqueue_job(uid, "capture") == (job_id, True)

        assert _job_row(job_id)[:3] == ("expired", 1, "lease_timeout")
        assert _active_capture_jobs(uid) == [], "同一轮里又建了新任务"
        state = _capture_state(uid)
        assert int(state["capture_fail_streak"]) == attempt
        assert state["last_capture_failed_job_id"] == str(job_id)
        assert state["capture_account_error_code"] == ""
        notice = _backoff_notice(uid)
        assert (notice is not None and notice["blame"] == "system") if attempt == 3 else notice is None
        # 回收器随后再扫也不会再记一次。
        assert _reap_future() == []
        assert int(_capture_state(uid)["capture_fail_streak"]) == attempt

    assert int(_capture_state(uid)["last_captured_until_seq"]) == 2
    # 下一轮（退避由调度器自己判断）再入队才建新任务。
    new_id, coalesced = jobs_store.enqueue_job(uid, "capture")
    assert coalesced is False and new_id != job_id


def test_scheduler_enqueue_of_a_crashed_capture_from_an_old_generation_just_supersedes():
    """Chat Clear / 切换过（generation 变了）的旧任务：照通用路径 supersede + 建新任务，不记账。"""
    uid = "u_capture_enqueue_old_generation"
    _seed(uid)
    job_id, _job = _running(uid, owner="old-gen")
    _expire_lease(job_id)
    conftest.set_v2_runtime_owner(uid, generation=2)

    new_id, coalesced = jobs_store.enqueue_job(uid, "capture")

    assert coalesced is False and new_id != job_id
    assert _job_row(job_id)[:3] == ("superseded", 0, "stale_runtime_generation")
    assert db.get_blob_strict(uid, "capture_state") is None


@pytest.mark.parametrize("lane", ["dream", "profile"])
def test_non_capture_enqueue_still_expires_and_rebuilds_in_one_step(lane):
    """别的 lane 入队撞见过期任务：仍是通用行为（expired + 立刻建新任务），不碰 capture_state。"""
    uid = f"u_capture_enqueue_other_{lane}"
    _seed(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    claimed = jobs_store.claim_next_job(f"{lane}-owner", lanes={lane})
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=f"{lane}-owner")
    _expire_lease(job_id)

    new_id, coalesced = jobs_store.enqueue_job(uid, lane)

    assert coalesced is False and new_id != job_id
    assert _job_row(job_id)[:3] == ("expired", 1, "lease_timeout")
    assert db.get_blob_strict(uid, "capture_state") is None


def _chat_send(uid: str, msg_id: str):
    return db.chat_append_and_enqueue(
        uid, msg_id, _time.time(),
        {"id": msg_id, "role": "user", "source": "model_api", "ts": _time.time(),
         "body_ct": "c", "nonce": "n", "K_user": "k", "content_type": "text"},
        5000, "chat", reason="chat_send", trace_id=msg_id,
        expected_generation=db.get_runtime_generation(uid),
    )


def test_chat_preempt_counts_a_crashed_capture_instead_of_requeueing_it():
    """活跃聊天的用户：发消息抢占时撞见租约已过期的落卡任务，终结 + 记账，不重投（第 12 轮 C1）。

    重投（回到 pending、不加次数、不记账）会替崩溃打掩护：每次在回收器之前发一条消息，
    失败次数就永远涨不起来。租约仍有效的落卡任务照旧重投（只是让位给聊天）。
    """
    uid = "u_capture_chat_preempt_crash"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})

    live_id, _job = _running(uid, owner="live-capture")
    _chat_send(uid, "chat-live")
    assert _job_row(live_id)[:3] == ("pending", 0, "foreground_chat_preempted")
    assert int(_capture_state(uid).get("capture_fail_streak") or 0) == 0
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE agent_jobs SET status='completed',finished_at=now() "
                     "WHERE user_id=%s AND status IN ('pending','claimed','running')", (uid,))

    crashed_id, _job = _running(uid, owner="crashed-capture")
    _expire_lease(crashed_id)
    _seq, chat_id = _chat_send(uid, "chat-after-crash")

    assert chat_id is not None
    assert _job_row(crashed_id)[:3] == ("expired", 1, "lease_timeout")
    state = _capture_state(uid)
    assert int(state["capture_fail_streak"]) == 1
    assert state["last_capture_failed_job_id"] == str(crashed_id)
    assert all(r["lane"] != "capture" for r in _reap_future())
    assert int(_capture_state(uid)["capture_fail_streak"]) == 1


def test_crash_accounting_waits_for_an_in_flight_opt_out_and_then_records_nothing():
    """关闭落卡先拿到设置行锁、还没提交：回收器必须等它，提交后读到已关闭 → 不记账、不提示（第 12 轮 I2）。"""
    uid = "u_capture_crash_optout_first"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    _write_capture_state(uid, {"capture_fail_streak": 2, "last_capture_failed_at": 1.0})
    job_id, _job = _running(uid, owner="optout-first")

    results: dict = {}
    with db.get_pool().connection() as optout:
        with optout.transaction():
            with optout.cursor() as cur:
                # 与 patch_proactive_settings_strict 相同的锁：fence → consent → 设置行 FOR UPDATE。
                db._lock_chat_user_fence_on_cursor(cur, uid)
                db._lock_capture_consent_on_cursor(cur, uid)
                cur.execute(
                    "SELECT doc FROM user_blobs WHERE user_id=%s "
                    "AND kind='proactive_settings' FOR UPDATE", (uid,))
                cur.execute(
                    "UPDATE user_blobs SET doc=doc || '{\"capture_enabled\": false}'::jsonb "
                    "WHERE user_id=%s AND kind='proactive_settings'", (uid,))
                reaper = threading.Thread(
                    target=lambda: results.setdefault("reaped", _reap_future()))
                reaper.start()
                reaper.join(1.0)
                assert reaper.is_alive(), "回收器没有等正在提交的关闭 —— 读的是关闭之前的快照"
    reaper.join(15)
    assert not reaper.is_alive()

    assert [r["id"] for r in results["reaped"]] == [job_id]
    assert _job_row(job_id)[0] == "expired"
    assert int(_capture_state(uid)["capture_fail_streak"]) == 2
    assert _backoff_notice(uid) is None


def test_opt_out_waits_for_crash_accounting_and_suppresses_its_notice(monkeypatch):
    """回收器先读到开着并锁住设置行：关闭必须排在它后面；记账提交后、发提示前再查一次开关 → 不提示（第 12 轮 I2）。"""
    uid = "u_capture_crash_accounting_first"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    _write_capture_state(uid, {"capture_fail_streak": 2, "last_capture_failed_at": 1.0})
    job_id, _job = _running(uid, owner="accounting-first")
    holding = threading.Event()
    release = threading.Event()
    original = jobs_store._capture_allowed_for_crash_accounting_on_cursor

    def hold_after_decision(cur, user_id):
        allowed = original(cur, user_id)
        if threading.current_thread().name == "crash-reaper":
            holding.set()
            assert release.wait(10)
        return allowed

    monkeypatch.setattr(
        jobs_store, "_capture_allowed_for_crash_accounting_on_cursor", hold_after_decision)
    results: dict = {}
    errors: list[BaseException] = []

    def run(name, fn):
        try:
            results[name] = fn()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    reaper = threading.Thread(target=run, args=("reaped", _reap_future), name="crash-reaper")
    reaper.start()
    assert holding.wait(10)
    optout = threading.Thread(target=run, args=(
        "optout",
        lambda: db.patch_proactive_settings_strict(uid, {"capture_enabled": False})))
    optout.start()
    optout.join(1.0)
    assert optout.is_alive(), "关闭没有等回收器的记账决定 —— 两边之间可以插进别的顺序"
    release.set()
    reaper.join(15)
    optout.join(15)
    assert not reaper.is_alive() and not optout.is_alive()
    assert errors == []

    assert [r["id"] for r in results["reaped"]] == [job_id]
    assert results["optout"]["capture_enabled"] is False
    # 记账在关闭之前线性化：这一次算数。
    state = _capture_state(uid)
    assert int(state["capture_fail_streak"]) == 3
    assert state["last_capture_failed_job_id"] == str(job_id)
    # 但提交后发提示时落卡已经关了：不再告诉用户「正在重试」。
    assert _backoff_notice(uid) is None


# --- 整个 worker 容器死了、租约还没到期（#9 余项） ------------------------------ #

_DEAD_WORKER = "v2-worker-deadtest-1-0a1b2c3d-abc1234"


def _fleet_owner(worker_id: str = _DEAD_WORKER, pool: str = "heavy") -> str:
    return f"{worker_id}:{pool}-0:{'0' * 31}1"


def _beat(worker_id: str, *, age_sec: float) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_worker_heartbeats (worker_id, beat_at, kind, capacity, pool) "
            "VALUES (%s, clock_timestamp() - make_interval(secs => %s), %s, 1, %s) "
            "ON CONFLICT (worker_id) DO UPDATE SET beat_at=EXCLUDED.beat_at",
            (
                worker_id,
                float(age_sec),
                "genesis" if worker_id.endswith(":genesis") else "turn",
                worker_id.rsplit(":", 1)[-1],
            ),
        )


def _age_claim(job_id: int, *, age_sec: float) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET claimed_at=clock_timestamp() - make_interval(secs => %s) "
            "WHERE id=%s",
            (float(age_sec), job_id),
        )


@pytest.fixture()
def _dead_worker_heartbeats():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_worker_heartbeats WHERE worker_id LIKE 'v2-worker-deadtest-%'")
    yield
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_worker_heartbeats WHERE worker_id LIKE 'v2-worker-deadtest-%'")


def _whole_worker_went_silent(worker_id: str = _DEAD_WORKER) -> None:
    stale = jobs_store.CAPTURE_OWNER_DEAD_SEC + 60
    for name in ("foreground", "wake", "heavy", "genesis"):
        _beat(f"{worker_id}:{name}", age_sec=stale)


def test_fleet_claim_owner_parses_only_the_production_slot_shape():
    assert jobs_store.fleet_claim_owner(_fleet_owner()) == (_DEAD_WORKER, "heavy")
    assert jobs_store.fleet_claim_owner(
        f"a:b:wake-12:{'f' * 32}"
    ) == ("a:b", "wake")
    for unknown in (
        "live-capture",
        f"{_DEAD_WORKER}#0",
        f"{_DEAD_WORKER}:heavy-0:g0",
        f"{_DEAD_WORKER}:genesis",
        f"{_DEAD_WORKER}:other-0:{'0' * 32}",
        None,
    ):
        assert jobs_store.fleet_claim_owner(unknown) is None


def test_chat_preempt_counts_a_capture_whose_whole_worker_died_before_its_lease_expired(
    _dead_worker_heartbeats,
):
    """容器整个死掉时租约还有几分钟：活跃聊天每次抢占都重投，崩溃就永远记不上账。

    主人的全部心跳（三个池 + genesis）都停了超过窗口、claim 也早于窗口 → 按崩溃终结 + 记一次。
    """
    uid = "u_capture_chat_preempt_dead_worker"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    owner = _fleet_owner()
    job_id, _job = _running(uid, owner=owner)
    _age_claim(job_id, age_sec=jobs_store.CAPTURE_OWNER_DEAD_SEC + 60)
    _whole_worker_went_silent()
    assert _job_row(job_id)[5] is True  # lease still set and not expired

    _seq, chat_id = _chat_send(uid, "chat-after-worker-death")

    assert chat_id is not None
    assert _job_row(job_id)[:4] == ("expired", 1, "lease_timeout", owner)
    state = _capture_state(uid)
    assert int(state["capture_fail_streak"]) == 1
    assert state["last_capture_failed_job_id"] == str(job_id)
    # The lease reaper later sees a terminal row and must not count it again.
    assert all(r["lane"] != "capture" for r in _reap_future())
    assert int(_capture_state(uid)["capture_fail_streak"]) == 1


def test_dead_owner_check_rides_on_the_job_row_select(_dead_worker_heartbeats):
    """The chat send transaction pays no extra round trip for the dead-owner
    check: it is a column of the job-row SELECT (review 09-15)."""
    from psycopg.rows import dict_row

    uid = "u_capture_dead_owner_one_statement"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    owner = _fleet_owner()
    job_id, _job = _running(uid, owner=owner)
    _age_claim(job_id, age_sec=jobs_store.CAPTURE_OWNER_DEAD_SEC + 60)
    _whole_worker_went_silent()
    statements: list[str] = []

    class _Spy:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, params=None):
            statements.append(str(sql))
            return self._cur.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._cur, name)

    with db.get_pool().connection() as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                preempted = jobs_store._expire_overdue_capture_for_chat_on_cursor(
                    _Spy(cur),
                    {"id": job_id, "user_id": uid, "status": "running",
                     "claimed_by": owner},
                )
        finally:
            conn.rollback()
    assert preempted is not None and preempted.recovery == "terminal"
    first_runtime_read = next(
        i for i, sql in enumerate(statements) if "v2_runtime_state" in sql
    )
    assert len(statements[:first_runtime_read]) == 1, statements[:first_runtime_read]
    assert "v2_worker_heartbeats" in statements[0] and "agent_jobs" in statements[0]


@pytest.mark.parametrize(
    "case",
    [
        "home_pool_beating",
        "genesis_thread_still_beating",
        "other_pool_still_beating",
        "owner_never_registered",
        "claim_younger_than_window",
        "unknown_claim_shape",
    ],
)
def test_chat_preempt_still_requeues_a_capture_whose_owner_is_not_provably_dead(
    _dead_worker_heartbeats, case
):
    uid = f"u_capture_chat_preempt_alive_{case}"[:60]
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    owner = "live-capture" if case == "unknown_claim_shape" else _fleet_owner()
    job_id, _job = _running(uid, owner=owner)
    if case != "claim_younger_than_window":
        _age_claim(job_id, age_sec=jobs_store.CAPTURE_OWNER_DEAD_SEC + 60)
    if case != "owner_never_registered":
        _whole_worker_went_silent()
    fresh = {
        "home_pool_beating": "heavy",
        "genesis_thread_still_beating": "genesis",
        "other_pool_still_beating": "foreground",
    }.get(case)
    if fresh:
        _beat(f"{_DEAD_WORKER}:{fresh}", age_sec=0)

    _chat_send(uid, f"chat-{case}")

    assert _job_row(job_id)[:3] == ("pending", 0, "foreground_chat_preempted")
    assert int(_capture_state(uid).get("capture_fail_streak") or 0) == 0


def test_chat_preempt_of_a_dead_workers_non_capture_job_is_unchanged(
    _dead_worker_heartbeats,
):
    """死主人判据只接在落卡分支上：别的 lane 照旧让位（superseded），不记落卡失败。"""
    uid = "u_dead_worker_non_capture"
    _seed(uid)
    owner = _fleet_owner(pool="wake")
    job_id, coalesced = jobs_store.enqueue_job(uid, "heartbeat")
    assert not coalesced
    job = jobs_store.claim_next_job(owner, lanes={"heartbeat"})
    assert job is not None and int(job["id"]) == job_id
    _age_claim(job_id, age_sec=jobs_store.CAPTURE_OWNER_DEAD_SEC + 60)
    _whole_worker_went_silent()

    _chat_send(uid, "chat-dead-heartbeat")

    assert _job_row(job_id)[:3] == ("superseded", 0, "foreground_chat_preempted")
    assert int(_capture_state(uid).get("capture_fail_streak") or 0) == 0


# --- Codex 第 13 轮 ---------------------------------------------------------- #


def _race(first_name: str, first_fn, second_fn, holding: threading.Event,
          release: threading.Event) -> tuple[dict, list]:
    """先动手的一方拿齐锁后停住，第二方必须真的在等锁；放行后两边都跑完。"""
    results: dict = {}
    errors: list[BaseException] = []

    def run(name, fn):
        try:
            results[name] = fn()
        except BaseException as exc:  # noqa: BLE001 — 线程里的失败要带回主线程断言
            errors.append(exc)

    t1 = threading.Thread(target=run, args=("first", first_fn), name=first_name)
    t1.start()
    assert holding.wait(10)
    t2 = threading.Thread(target=run, args=("second", second_fn), name="race-second")
    t2.start()
    t2.join(1.0)
    assert t2.is_alive(), "第二方没有等锁 —— 两边并没有真正争同一组锁"
    release.set()
    t1.join(15)
    t2.join(15)
    assert not t1.is_alive() and not t2.is_alive()
    return results, errors


@pytest.mark.parametrize("first", ["enqueue", "reaper"])
def test_capture_enqueue_waiting_behind_a_crash_expiry_honours_the_backoff_it_armed(
    monkeypatch, first
):
    """A 锁住过期的落卡任务、终结 + 记账；B 在等同一行锁。B 醒来后看到「没有活跃任务」，
    不能立刻建新任务绕过 A 刚记下的退避（第 13 轮 I1）。A 是另一次入队或回收器都一样。

    两次入队的调度器都是在 A 记账**之前**判的退避（同一个 admitted_at）。
    """
    uid = f"u_capture_enqueue_backoff_race_{first}"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    _write_capture_state(uid, {
        "last_captured_until_message_id": "m2",
        "last_captured_until_seq": 2,
        "capture_seq_initialized": True,
    })
    job_id, _job = _running(uid, owner="race-crashed")
    _expire_lease(job_id)
    holding = threading.Event()
    release = threading.Event()
    original = jobs_store._record_capture_crash_failure_on_cursor

    def hold_after_accounting(cur, **kwargs):
        failed = original(cur, **kwargs)
        if threading.current_thread().name == "race-first":
            holding.set()
            assert release.wait(10)
        return failed

    monkeypatch.setattr(
        jobs_store, "_record_capture_crash_failure_on_cursor", hold_after_accounting)
    admitted_at = _time.time()

    def enqueue():
        return jobs_store.enqueue_capture(
            uid, reason="quiet_timeout", backoff_now=admitted_at)

    results, errors = _race(
        "race-first", enqueue if first == "enqueue" else _reap_future, enqueue,
        holding, release)

    assert errors == []
    if first == "enqueue":
        assert results["first"] == jobs_store.CaptureEnqueueResult(job_id, "expired_deferred")
    else:
        assert [r["id"] for r in results["first"]] == [job_id]
    assert results["second"] == jobs_store.CaptureEnqueueResult(None, "backoff_deferred")
    assert _active_capture_jobs(uid) == [], "等锁的那次入队绕过刚记下的退避建了新任务"
    assert _job_row(job_id)[:3] == ("expired", 1, "lease_timeout")
    assert int(_capture_state(uid)["capture_fail_streak"]) == 1


def test_capture_enqueue_backoff_recheck_matches_the_scheduler_when_nothing_changed():
    """重判用调度器的同一个 now：退避已过 / 从没失败时照常建任务；已知仍在退避里也不建。"""
    uid = "u_capture_enqueue_backoff_recheck"
    _seed(uid)
    now = _time.time()
    assert jobs_store.enqueue_capture(uid, backoff_now=now).disposition == "created"
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE agent_jobs SET status='completed',finished_at=now() "
                     "WHERE user_id=%s", (uid,))
    _write_capture_state(uid, {"capture_fail_streak": 1, "last_capture_failed_at": now - 601})
    assert jobs_store.enqueue_capture(uid, backoff_now=now).disposition == "created"
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE agent_jobs SET status='completed',finished_at=now() "
                     "WHERE user_id=%s", (uid,))
    _write_capture_state(uid, {"capture_fail_streak": 1, "last_capture_failed_at": now - 10})
    assert jobs_store.enqueue_capture(uid, backoff_now=now) == jobs_store.CaptureEnqueueResult(
        None, "backoff_deferred")
    # 不传 backoff_now（手动 force / 通用 enqueue_job）不重判。
    assert jobs_store.enqueue_capture(uid).disposition == "created"
    assert jobs_store.enqueue_capture(uid, backoff_now=now).disposition == "coalesced_active"


def test_v2_capture_submit_reports_deferred_rounds_without_a_fake_pending_job(monkeypatch):
    """旧任务过期被终结 / 被刚记的失败压在退避里：submit 不能回一个假的 pending 任务、
    也不能报成 v2_coalesced（第 13 轮 M1）。手动 force 不受退避限制。"""
    uid = "u_capture_submit_deferred"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    monkeypatch.setenv("FEEDLING_V2_CAPTURE_ENABLED", "1")
    notified: list = []
    monkeypatch.setattr(proactive_core.core_wake_bus, "notify",
                        lambda *args: notified.append(args))

    class Store:
        user_id = uid

    def submit(trigger):
        return proactive_core._submit_v2_capture(
            Store(), trigger=trigger, now=_time.time(),
            window=_window(), capture_key="capture:k")

    crashed_id, _job = _running(uid, owner="submit-crashed")
    _expire_lease(crashed_id)

    assert submit("quiet_timeout") == {
        "enqueued": False, "reason": "v2_expired_deferred", "job": None}
    assert _job_row(crashed_id)[0] == "expired"
    assert submit("quiet_timeout") == {
        "enqueued": False, "reason": "failure_backoff", "job": None}
    assert _active_capture_jobs(uid) == [] and notified == []

    forced = submit("manual_force")
    assert forced["enqueued"] is True and forced["reason"] == "v2"
    assert [r[0] for r in _active_capture_jobs(uid)] == [forced["job"]["id"]]
    coalesced = submit("quiet_timeout")
    assert coalesced["enqueued"] is False and coalesced["reason"] == "v2_coalesced"
    assert coalesced["job"]["id"] == forced["job"]["id"]
    assert len(notified) == 1


def test_first_time_opt_out_racing_crash_accounting_leaves_no_retrying_notice(monkeypatch):
    """设置行还不存在、首次关闭落卡正握着 consent 锁没提交：回收器照记账（卡死 worker 也常握着
    这把锁，不记会让默认用户的卡死永远进不了退避），但开关说不准 → 不发「正在重试」提示；
    关闭提交后也不能留下提示（第 13 轮 I2）。"""
    uid = "u_capture_first_opt_out_race"
    _seed(uid)
    assert db.get_blob_strict(uid, "proactive_settings") is None
    _write_capture_state(uid, {"capture_fail_streak": 2, "last_capture_failed_at": 1.0})
    job_id, _job = _running(uid, owner="first-opt-out-crash")
    holding = threading.Event()
    release = threading.Event()
    original = db._lock_capture_consent_on_cursor

    def hold_consent(cur, user_id):
        original(cur, user_id)
        if threading.current_thread().name == "first-opt-out":
            holding.set()
            assert release.wait(15)

    monkeypatch.setattr(db, "_lock_capture_consent_on_cursor", hold_consent)
    results: dict = {}
    errors: list[BaseException] = []

    def run(name, fn):
        try:
            results[name] = fn()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    optout = threading.Thread(target=run, args=(
        "optout", lambda: core_store.UserStore(uid).save_proactive_settings(
            {"capture_enabled": False})), name="first-opt-out")
    optout.start()
    assert holding.wait(10)
    try:
        reaper = threading.Thread(target=run, args=("reaped", _reap_future), name="reaper")
        reaper.start()
        reaper.join(10)
        assert not reaper.is_alive(), "回收器被正在提交的首次关闭卡住了"
        assert [r["id"] for r in results["reaped"]] == [job_id]
        assert int(_capture_state(uid)["capture_fail_streak"]) == 3
        assert _backoff_notice(uid) is None, "开关说不准时发了「正在重试」提示"
    finally:
        release.set()
        optout.join(15)
    assert not optout.is_alive() and errors == []
    assert db.get_blob_strict(uid, "proactive_settings")["capture_enabled"] is False
    assert _backoff_notice(uid) is None


def test_opt_out_waits_for_an_in_flight_crash_notice_and_then_clears_it(monkeypatch):
    """回收器拿到 consent 锁、读到开着、正在发提示：关闭必须排在它后面，并在提交后清掉这条提示（第 13 轮 I2）。"""
    from proactive import capture_jobs

    uid = "u_capture_notice_then_opt_out"
    _seed(uid)
    _write_capture_state(uid, {"capture_fail_streak": 2, "last_capture_failed_at": 1.0})
    job_id, _job = _running(uid, owner="notice-crash")
    holding = threading.Event()
    release = threading.Event()
    original = capture_jobs.notify_backoff

    first_seen: dict = {}

    def hold_after_notice(*args, **kwargs):
        original(*args, **kwargs)
        if threading.current_thread().name == "race-first":
            first_seen["notice"] = _backoff_notice(uid)
            holding.set()
            assert release.wait(10)

    monkeypatch.setattr(capture_jobs, "notify_backoff", hold_after_notice)

    def opt_out():
        return core_store.UserStore(uid).save_proactive_settings({"capture_enabled": False})

    results, errors = _race("race-first", _reap_future, opt_out, holding, release)

    assert errors == []
    assert [r["id"] for r in results["first"]] == [job_id]
    assert first_seen["notice"] is not None and first_seen["notice"]["resolved"] is False
    assert results["second"]["capture_enabled"] is False
    notice = _backoff_notice(uid)
    assert notice is not None and notice["resolved"] is True, "关闭提交后没有清掉「正在重试」提示"


def test_opt_out_clears_an_existing_retrying_notice():
    uid = "u_capture_opt_out_clears_notice"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    for attempt in range(3):
        job_id, _job = _running(uid, owner=f"clear-{attempt}")
        assert [r["id"] for r in _reap_future()] == [job_id]
    assert _backoff_notice(uid)["resolved"] is False
    core_store.UserStore(uid).save_proactive_settings({"dnd": True})
    assert _backoff_notice(uid)["resolved"] is False, "无关的设置写入不该清提示"
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": False})
    assert _backoff_notice(uid)["resolved"] is True


def test_chat_send_does_not_wait_for_crash_accounting_postcommit(monkeypatch):
    """聊天抢占终结了崩溃的落卡任务：镜像 + 提示放到后台做，发送不等它（第 13 轮 M2）。"""
    uid = "u_capture_chat_postcommit_async"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    crashed_id, _job = _running(uid, owner="postcommit-crashed")
    _expire_lease(crashed_id)
    started = threading.Event()
    release = threading.Event()
    ran: list = []

    def slow_postcommit(user_id, job_id, *, source, failed_state):
        started.set()
        assert release.wait(10)
        ran.append((user_id, job_id, source, int(failed_state["capture_fail_streak"])))

    monkeypatch.setattr(jobs_store, "after_capture_crash_recorded", slow_postcommit)
    try:
        t0 = _time.monotonic()
        _seq, chat_id = _chat_send(uid, "chat-slow-postcommit")
        elapsed = _time.monotonic() - t0
        assert chat_id is not None
        assert started.wait(10), "收尾根本没跑"
        assert ran == [] and elapsed < 5, "聊天发送等了崩溃记账的收尾"
    finally:
        release.set()
    assert jobs_store.wait_capture_postcommit_idle(10)
    assert ran == [(uid, crashed_id, "chat_preempt", 1)]


def test_chat_send_crash_postcommit_still_notices_and_survives_a_failing_hook(monkeypatch):
    uid = "u_capture_chat_postcommit_real"
    _seed(uid)
    core_store.UserStore(uid).save_proactive_settings({"capture_enabled": True})
    calls: list = []
    real = jobs_store.after_capture_crash_recorded

    def flaky(user_id, job_id, **kwargs):
        calls.append(job_id)
        if len(calls) == 1:
            raise RuntimeError("mirror down")
        return real(user_id, job_id, **kwargs)

    monkeypatch.setattr(jobs_store, "after_capture_crash_recorded", flaky)
    assert jobs_store.after_capture_crash_recorded_in_background(
        uid, 0, source="test", failed_state={"capture_fail_streak": 0})
    _write_capture_state(uid, {"capture_fail_streak": 2, "last_capture_failed_at": 1.0})
    crashed_id, _job = _running(uid, owner="postcommit-real")
    _expire_lease(crashed_id)
    _chat_send(uid, "chat-real-postcommit")
    assert jobs_store.wait_capture_postcommit_idle(10)
    assert calls == [0, crashed_id], "一次收尾失败把后台线程带死了"
    notice = _backoff_notice(uid)
    assert notice is not None and notice["resolved"] is False and notice["blame"] == "system"
