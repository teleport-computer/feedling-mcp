"""serve_worker：生产 TurnDeps 装配的可测部分 + /healthz + 独立进程编排（reaper/wake 接线）。
不起真 worker/真 enclave。"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import jobs_store, reaper as v2_reaper, serve_worker, worker


def test_provider_thread_limiter_scales_with_worker_slots(monkeypatch):
    import anyio.to_thread

    async def _run():
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = 40
        monkeypatch.delenv("FEEDLING_V2_PROVIDER_THREAD_LIMIT", raising=False)
        assert serve_worker._configure_provider_thread_limiter(64) == 64

        limiter.total_tokens = 40
        monkeypatch.setenv("FEEDLING_V2_PROVIDER_THREAD_LIMIT", "96")
        assert serve_worker._configure_provider_thread_limiter(64) == 96

    asyncio.run(_run())


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "nope"])
def test_positive_loop_intervals_fail_closed(monkeypatch, raw):
    monkeypatch.setenv("TEST_V2_INTERVAL", raw)
    with pytest.raises(RuntimeError, match="finite and > 0"):
        serve_worker._positive_float_env("TEST_V2_INTERVAL", "1")


def test_default_worker_id_is_unique_across_same_pid_replica_calls(monkeypatch):
    monkeypatch.setattr(serve_worker.socket, "gethostname", lambda: "pod")
    monkeypatch.setattr(serve_worker.os, "getpid", lambda: 1)

    first = serve_worker._default_worker_id()
    second = serve_worker._default_worker_id()

    assert first.startswith("v2-worker-pod-1-")
    assert second.startswith("v2-worker-pod-1-")
    assert first != second


def test_build_production_deps_returns_turndeps():
    deps = serve_worker.build_production_deps()
    assert isinstance(deps, worker.TurnDeps)
    assert callable(deps.read_messages)
    assert callable(deps.resolve_provider)
    assert callable(deps.is_official)
    assert callable(deps.mint_enclave_token)


def test_wire_assembly_injects_envelope_pubkey_getter():
    from core import envelope as core_envelope
    from accounts import registry as accounts_registry

    serve_worker.wire_assembly()
    assert core_envelope.get_user_public_key is accounts_registry._get_user_public_key


def test_health_app_healthz_ok():
    from starlette.testclient import TestClient

    app = serve_worker.build_health_app()
    with TestClient(app) as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_resolve_provider_missing_config_returns_none_and_error():
    """No model_api config for a fresh user -> (None, {"error": ...}), never a
    fabricated ProviderConfig and never a platform/system key fallback."""
    import uuid

    from core import store as core_store

    serve_worker.wire_assembly()
    user_id = f"u_serve_worker_test_{uuid.uuid4().hex[:8]}"
    with __import__("db").get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )
    store = core_store.get_store(user_id)
    deps = serve_worker.build_production_deps()
    provider_config, meta = deps.resolve_provider(user_id)
    assert provider_config is None
    assert meta.get("error")


def test_read_messages_returns_list_for_user_with_no_messages():
    import uuid

    from core import store as core_store

    serve_worker.wire_assembly()
    user_id = f"u_serve_worker_test_{uuid.uuid4().hex[:8]}"
    with __import__("db").get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )
    deps = serve_worker.build_production_deps()
    messages = deps.read_messages(user_id)
    assert messages == []


def test_read_messages_uses_cursor_not_latest_assistant_position(monkeypatch):
    """A user message (B) that arrived before the latest assistant row (R) must
    still be read when the seq cursor sits at A — assistant-position slicing
    would hide it. Boundary is seq (chat_messages_after_seq), never ts."""
    import db
    import conftest

    conftest.seed_user("u_srw_cursor")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", ("u_srw_cursor",))
    for mid, ts, role, ct in [("A", 10.0, "user", "a"), ("B", 20.0, "user", "b"),
                              ("R", 30.0, "openclaw", "r")]:
        db.chat_append("u_srw_cursor", mid, ts,
                       {"id": mid, "ts": ts, "role": role, "content_type": "text",
                        "body_ct": ct, "K_enclave": "k"}, 5000)
    rows = db.chat_messages_after_seq("u_srw_cursor", 0, limit=10)
    a_seq, b_seq = rows[0]["seq"], rows[1]["seq"]
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda uid: "rt")
    monkeypatch.setattr(
        serve_worker.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda row, *args, **kwargs: f"text-{row['id']}".encode(),
    )

    assert serve_worker._read_messages("u_srw_cursor", after_seq=a_seq) == [
        {"id": "B", "ts": 20.0, "seq": b_seq, "role": "user", "content": "text-B"}
    ]


def test_read_messages_propagates_strict_database_read_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(serve_worker.db, "chat_messages_after_seq", _boom)

    with pytest.raises(RuntimeError, match="database unavailable"):
        serve_worker._read_messages("u", after_seq=0)


def test_compaction_reader_is_oldest_first_while_context_reader_is_newest(monkeypatch):
    class _Store:
        chat_messages = [
            {"id": f"m{i}", "ts": float(i), "role": "user", "body_ct": "x", "K_enclave": "k"}
            for i in range(1, 6)
        ]

    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda uid: _Store())
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda uid: "rt")
    monkeypatch.setattr(
        serve_worker.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda row, *args, **kwargs: row["id"].encode(),
    )

    newest = serve_worker._read_tail("u", 0.0, 2)
    oldest = serve_worker._read_compaction_tail("u", 0.0, 2)
    assert [row["id"] for row in newest] == ["m4", "m5"]
    assert [row["id"] for row in oldest] == ["m1", "m2"]


def test_tail_reader_selects_window_before_enclave_decrypt(monkeypatch):
    class _Store:
        chat_messages = [
            {"id": f"m{i}", "ts": float(i), "role": "user", "body_ct": "x", "K_enclave": "k"}
            for i in range(1, 501)
        ]

    calls = []
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda uid: _Store())
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda uid: "rt")
    monkeypatch.setattr(
        serve_worker.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda row, *args, **kwargs: calls.append(row["id"]) or row["id"].encode(),
    )

    out = serve_worker._read_tail("u", 0.0, 2)
    assert [row["id"] for row in out] == ["m499", "m500"]
    assert calls == ["m499", "m500"]


def test_main_module_has_entrypoint_guard():
    import inspect

    src = inspect.getsource(serve_worker)
    assert '__name__ == "__main__"' in src
    assert "def main() -> None" in src or "def main():" in src


def test_is_official_wraps_spawners_and_defaults_true_for_none():
    """Task 8 dependency-direction fix: worker.py must not import
    agent_runtime.spawners (hosted-adjacent) itself, so the official/non-official
    judgment is resolved here and injected via TurnDeps.is_official. Delegates to
    the same ``_is_official_identity`` the hosted prompt-rewrite path uses."""
    import provider_client
    from agent_runtime import spawners as agent_spawners

    official_cfg = provider_client.ProviderConfig(
        provider="anthropic", model="claude-x", api_key="k", base_url="")
    third_party_cfg = provider_client.ProviderConfig(
        provider="deepseek", model="deepseek-chat", api_key="k",
        base_url="https://relay.example.com/v1")

    assert serve_worker._is_official(official_cfg) is agent_spawners._is_official_identity(
        "anthropic", "")
    assert serve_worker._is_official(third_party_cfg) is agent_spawners._is_official_identity(
        "deepseek", "https://relay.example.com/v1")
    assert serve_worker._is_official(third_party_cfg) is False
    # No provider resolved at all (resolve_provider failed) -> default True (matches
    # _is_official_identity's own "missing provider -> official" convention).
    assert serve_worker._is_official(None) is True


def test_resident_mode_is_final_fence_for_scheduled_and_screen_watch(monkeypatch):
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda uid: object())
    monkeypatch.setattr(
        serve_worker.hosted_config_store,
        "get_hosted_runtime_mode_strict",
        lambda store: serve_worker.hosted_config_store.HOSTED_RUNTIME_MODE_RESIDENT,
    )
    monkeypatch.setattr(
        jobs_store,
        "enqueue_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not enqueue")),
    )

    assert serve_worker._fire_scheduled_for_user("resident") == 0
    assert serve_worker._tick_screen_watch_for_user("resident") == 0


def test_read_messages_carries_id_and_ts_and_seq_for_coalesce(client, backend_env, monkeypatch):
    """coalesce_pending dedupes by id and forwards seq (the durable reply-cursor
    key), so _read_messages must carry id/ts/seq — the image shorthand branch
    exercises the id/ts/seq-carrying path without an enclave round trip."""
    import conftest
    import db

    serve_worker.wire_assembly()
    user_id = "u_serve_worker_idts"
    conftest.seed_user(user_id)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (user_id,))
    db.chat_append(user_id, "m_synthetic_1", 12345.0, {
        "id": "m_synthetic_1", "ts": 12345.0, "role": "user", "content_type": "image",
        "body_ct": None, "K_enclave": None,
    }, 5000)
    seq = db.chat_messages_after_seq(user_id, 0, limit=10)[0]["seq"]

    deps = serve_worker.build_production_deps()
    messages = deps.read_messages(user_id)
    # Multimodal round: image rows additionally carry non-sensitive `has_image`/`image_mime`
    # markers so worker._inject_tail_images can find them. `content` stays TEXT here (no
    # caption envelope on this synthetic row -> the "[image]" placeholder); bytes never
    # enter this read path, because compaction shares it.
    assert messages == [{
        "id": "m_synthetic_1", "ts": 12345.0, "seq": seq, "role": "user", "content": "[image]",
        "has_image": True, "image_mime": "image/jpeg",
    }]


# ------------------------------------------------------------------
# FIX 1: the stuck-job reaper loop
# ------------------------------------------------------------------

def test_reaper_loop_calls_reap_stuck_jobs_and_exits_promptly_on_stop_event(monkeypatch):
    """Before this fix, jobs_store.reap_stuck_jobs() was tested but never called
    from anywhere in the running process -> a worker that dies between claim and
    mark_* leaves a permanently wedged claimed/running job (single-flight then
    coalesces all future chat/send for that user into the dead job forever).
    _reaper_loop must call it at least once on a short interval, and must not
    block stop_event from taking effect promptly."""
    calls = {"n": 0}

    def _fake_reap(**kwargs):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(v2_reaper, "reap_once", _fake_reap)
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(serve_worker._reaper_loop(stop_event, interval=0.02))
        # Give it a couple of intervals to tick at least once.
        for _ in range(50):
            if calls["n"] >= 1:
                break
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert calls["n"] >= 1


def test_reaper_loop_transient_db_error_does_not_kill_the_loop(monkeypatch):
    """A transient DB error inside reap_stuck_jobs must be logged and swallowed,
    never propagate out and crash the reaper (which would also crash the worker
    process if gathered with run_worker_loop in _serve)."""
    calls = {"n": 0}

    def _flaky_reap(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db outage")
        return 0

    monkeypatch.setattr(v2_reaper, "reap_once", _flaky_reap)
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(serve_worker._reaper_loop(stop_event, interval=0.02))
        for _ in range(50):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert calls["n"] >= 2  # survived the first raise and ticked again


class _HealthySupervisor:
    """D3 (Task 4): `_heartbeat_loop` now derives capacity from
    `supervisor.poll_liveness()` — a fresh/alive child advertises full capacity."""

    def poll_liveness(self) -> dict:
        return {"alive": True, "last_progress_age_sec": 1.0}


def test_turn_heartbeat_advertises_slot_capacity(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "MAX_WORKERS", 12)
    monkeypatch.setattr(
        jobs_store,
        "record_worker_heartbeat",
        lambda worker_id, **kwargs: calls.append((worker_id, kwargs)),
    )
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(
            serve_worker._heartbeat_loop(
                "worker-a", stop_event, supervisor=_HealthySupervisor(), interval=0.02))
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert calls[0] == ("worker-a", {"capacity": 12, "kind": "turn"})
    assert calls[-1] == ("worker-a", {"capacity": 0, "kind": "turn"})


# ------------------------------------------------------------------
# FIX 3: "v2_jobs" immediate-wake bridge (wake_bus notify -> asyncio.Event)
# ------------------------------------------------------------------

def test_wire_assembly_registers_v2_jobs_handler():
    from core import wake_bus as core_wake_bus

    serve_worker.wire_assembly()
    assert worker.on_v2_job_notify in core_wake_bus._extra_handlers.get("v2_jobs", [])


def test_on_v2_job_notify_bridges_to_event_via_call_soon_threadsafe():
    """set_job_wake_context binds the running loop/event; on_v2_job_notify (the
    wake-bus handler, invoked from a plain OS thread in production) must set
    that event without raising even when called from a different thread."""
    import threading

    async def _driver():
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        worker.set_job_wake_context(loop, event)
        t = threading.Thread(target=worker.on_v2_job_notify, args=("some-user",))
        t.start()
        t.join(timeout=1.0)
        await asyncio.wait_for(event.wait(), timeout=1.0)
        assert event.is_set()

    asyncio.run(_driver())
    # Reset module-level context so other tests don't see a stale loop/event.
    worker.set_job_wake_context(None, None)


def test_on_v2_job_notify_is_a_noop_without_context():
    """Before set_job_wake_context has ever run (or after reset), the handler
    must silently no-op rather than raise (defensive: covers the startup race
    window and direct-call test paths that don't go through serve_worker)."""
    worker.set_job_wake_context(None, None)
    worker.on_v2_job_notify("some-user")  # must not raise


# ------------------------------------------------------------------
# Task 3 (D1): _read_tail — both-roles windowed read (mirrors _read_messages
# but doesn't slice at last-assistant and doesn't skip non-user rows).
# ------------------------------------------------------------------

class _FakeStore:
    def __init__(self, chat_messages):
        self.chat_messages = chat_messages


def _fake_decrypt(envelope, key, *, purpose, runtime_token=""):
    return f"plain-{envelope['id']}".encode()


def _interleaved_rows():
    return [
        {"id": "m1", "ts": 1.0, "role": "user", "content_type": "text",
         "body_ct": "ct1", "K_enclave": "k1"},
        {"id": "m2", "ts": 2.0, "role": "openclaw", "content_type": "text",
         "body_ct": "ct2", "K_enclave": "k2"},
        {"id": "m3", "ts": 3.0, "role": "user", "content_type": "text",
         "body_ct": "ct3", "K_enclave": "k3"},
    ]


def test_read_tail_returns_both_roles_in_order(monkeypatch):
    from core import enclave as core_enclave
    from core import store as core_store

    fake_store = _FakeStore(_interleaved_rows())
    monkeypatch.setattr(core_store, "get_store", lambda uid: fake_store)
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _fake_decrypt)

    out = serve_worker._read_tail("u_tail_test", 0.0, 10)
    assert [r["role"] for r in out] == ["user", "assistant", "user"]
    assert [r["content"] for r in out] == ["plain-m1", "plain-m2", "plain-m3"]
    assert [r["ts"] for r in out] == [1.0, 2.0, 3.0]
    assert [r["id"] for r in out] == ["m1", "m2", "m3"]


def test_read_tail_filters_after_ts(monkeypatch):
    from core import enclave as core_enclave
    from core import store as core_store

    fake_store = _FakeStore(_interleaved_rows())
    monkeypatch.setattr(core_store, "get_store", lambda uid: fake_store)
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _fake_decrypt)

    out = serve_worker._read_tail("u_tail_test", 1.5, 10)
    assert [r["id"] for r in out] == ["m2", "m3"]
    assert [r["role"] for r in out] == ["assistant", "user"]


def test_read_tail_caps_to_limit(monkeypatch):
    from core import enclave as core_enclave
    from core import store as core_store

    rows = [
        {"id": f"m{i}", "ts": float(i), "role": "user", "content_type": "text",
         "body_ct": f"ct{i}", "K_enclave": f"k{i}"}
        for i in range(1, 6)
    ]
    fake_store = _FakeStore(rows)
    monkeypatch.setattr(core_store, "get_store", lambda uid: fake_store)
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _fake_decrypt)

    out = serve_worker._read_tail("u_tail_test", 0.0, 2)
    assert [r["id"] for r in out] == ["m4", "m5"]


# ------------------------------------------------------------------
# Task 4: _read_summary / _write_summary — decrypt-on-read (enclave) +
# encrypt-on-write (local) + CAS against v2_conversation_summary.
# ------------------------------------------------------------------

def test_read_summary_missing_returns_empty(monkeypatch):
    """No row yet for this user (never compressed) -> ("", 0.0, 0), no enclave
    round trip attempted."""
    from model_api_runtime.v2 import jobs_store as v2_jobs_store

    monkeypatch.setattr(v2_jobs_store, "get_summary_row", lambda uid: None)

    out = serve_worker._read_summary("u_summary_test")
    assert out == ("", 0.0, 0)


def test_read_summary_decrypts_present_row(monkeypatch):
    from core import enclave as core_enclave
    from model_api_runtime.v2 import jobs_store as v2_jobs_store

    monkeypatch.setattr(
        v2_jobs_store, "get_summary_row",
        lambda uid: {"summary_envelope": {"body_ct": "x"}, "watermark_ts": 7.0, "version": 3})
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, key, *, purpose, runtime_token="": b"- prior chat")

    out = serve_worker._read_summary("u_summary_test")
    assert out == ("- prior chat", 7.0, 3)


def test_read_summary_empty_envelope(monkeypatch):
    """summary_envelope is None (row exists but nothing compressed into it yet,
    e.g. Task 2's first-write path) -> plaintext "" without touching the
    enclave at all."""
    from core import enclave as core_enclave
    from model_api_runtime.v2 import jobs_store as v2_jobs_store

    monkeypatch.setattr(
        v2_jobs_store, "get_summary_row",
        lambda uid: {"summary_envelope": None, "watermark_ts": 4.0, "version": 1})

    def _boom(*a, **k):
        raise AssertionError("must not decrypt when summary_envelope is empty")

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _boom)

    out = serve_worker._read_summary("u_summary_test")
    assert out == ("", 4.0, 1)


def test_write_summary_builds_envelope_and_cas(monkeypatch):
    from core import envelope as core_envelope
    from core import store as core_store
    from model_api_runtime.v2 import jobs_store as v2_jobs_store

    monkeypatch.setattr(core_store, "get_store", lambda uid: object())
    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store",
        lambda store, plaintext: ({"body_ct": "e"}, ""))

    calls = {}

    def _fake_cas(uid, *, summary_envelope, watermark_ts, expected_version):
        calls["args"] = (uid, summary_envelope, watermark_ts, expected_version)
        return True

    monkeypatch.setattr(v2_jobs_store, "upsert_summary_row_cas", _fake_cas)

    ok = serve_worker._write_summary("u", "- s", 9.0, 2)
    assert ok is True
    assert calls["args"] == ("u", {"body_ct": "e"}, 9.0, 2)


def test_write_summary_envelope_build_failure_returns_false(monkeypatch):
    from core import envelope as core_envelope
    from core import store as core_store
    from model_api_runtime.v2 import jobs_store as v2_jobs_store

    monkeypatch.setattr(core_store, "get_store", lambda uid: object())
    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store",
        lambda store, plaintext: (None, "boom"))

    def _must_not_call(*a, **k):
        raise AssertionError("CAS must not be called when envelope build fails")

    monkeypatch.setattr(v2_jobs_store, "upsert_summary_row_cas", _must_not_call)

    ok = serve_worker._write_summary("u", "- s", 9.0, 2)
    assert ok is False


def test_fire_scheduled_for_user_enqueues_a_scheduled_agent_job(monkeypatch, backend_env):
    """BUG-3: the timer machinery existed but submitted into the dead legacy proactive_jobs
    stream. It must now produce an agent_jobs `scheduled` job."""
    import conftest
    from model_api_runtime.v2 import jobs_store

    serve_worker.wire_assembly()
    uid = "u_sw_fire_scheduled"
    conftest.seed_user(uid)
    monkeypatch.setattr(
        serve_worker.hosted_config_store,
        "get_hosted_runtime_mode_strict",
        lambda _store: serve_worker.hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
    )

    calls = []
    monkeypatch.setattr(jobs_store, "enqueue_job",
                        lambda u, lane, **kw: calls.append((u, lane)) or ("job1", False))

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        def fire_due_timers(self, user_id, *, settings, submit_wake, owner_id):
            submit_wake(object())
            return ()

    monkeypatch.setattr("proactive.scheduled_wake_v2.ScheduledWakeServiceV2", _FakeService)
    assert serve_worker._fire_scheduled_for_user(uid) == 1
    assert calls == [(uid, "scheduled")]
