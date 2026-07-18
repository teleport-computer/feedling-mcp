"""Hosted chat/send always enters the Runtime V2 durable job path."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from core import enclave as core_enclave
from core import store as core_store
from hosted import chat_send_core, config_store as hosted_config_store
from hosted import agent_runtime_cutover
from model_api_runtime.v2 import jobs_store
from core import wake_bus as core_wake_bus

from conftest import configure_model_api_route


def _seed(uid):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )
    configure_model_api_route(
        uid, provider="anthropic", model="m", test_status="ok",
        envelope={"body_ct": "x", "nonce": "n", "K_user": "k"})


def test_db_action_v2_enqueues_job_and_skips_resident(monkeypatch):
    _seed("u_send_v2")
    store = core_store.get_store("u_send_v2")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")

    # 让信封构建/驱动解析通过、不打真 enclave/provider。
    monkeypatch.setattr(
        chat_send_core.core_envelope, "_build_shared_envelope_for_store",
        lambda s, pt, **kw: ({"id": "u-msg-1", "body_ct": "c", "nonce": "n", "K_user": "k"}, ""),
    )
    # V2 must claim/enqueue before provider BYOK resolution. The worker owns the
    # decrypt and will surface a durable terminal error if it fails there.
    monkeypatch.setattr(
        hosted_config_store,
        "_load_runtime_provider_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("V2 send must not decrypt provider config")
        ),
    )
    monkeypatch.setattr(chat_send_core.agent_runtime_cutover, "resolve_driver", lambda cfg: "claude")
    # append_chat 走真 store 会尝试 DB；用真的即可（已 seed user）。返回其真实 row。
    # Task 2 v2 liveness guard: stub a live worker pool so this test keeps
    # exercising enqueue behavior, not the (separately tested) dead-pool guard.
    monkeypatch.setattr(chat_send_core.jobs_store, "workers_alive", lambda **kw: True)

    notified = {}
    monkeypatch.setattr(
        chat_send_core.core_wake_bus, "notify",
        lambda channel, user_id="": notified.update(channel=channel, user_id=user_id),
    )
    body, status = chat_send_core.model_api_chat_send_core(
        store, api_key="key", runtime_tok="", payload={"message": "hi"},
    )

    assert status == 202
    assert body["status"] == "processing"
    assert notified["channel"] == "v2_jobs" and notified["user_id"] == "u_send_v2"
    # A7: the enqueue no longer rides a separate jobs_store.enqueue_job call —
    # it happens atomically inside store.append_chat via db.chat_append_and_enqueue.
    # Verify the real effect: exactly one pending chat job row for this user,
    # carrying the send's reason and the envelope id as trace_id.
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT lane, status, reason, trace_id FROM agent_jobs WHERE user_id=%s",
            ("u_send_v2",),
        ).fetchall()
    assert len(rows) == 1
    lane, job_status, reason, trace_id = rows[0]
    assert lane == "chat"
    assert job_status == "pending"
    assert reason == "chat_send"
    assert trace_id == "u-msg-1"


def test_runtime_mode_read_failure_refuses_before_persistence(monkeypatch):
    _seed("u_send_mode_db_failure")
    store = core_store.get_store("u_send_mode_db_failure")
    monkeypatch.setattr(
        hosted_config_store,
        "get_hosted_runtime_control_strict",
        lambda _store: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(
        store,
        "append_chat",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not persist")),
    )

    body, status = chat_send_core.model_api_chat_send_core(
        store, api_key="key", runtime_tok="", payload={"message": "hi"},
    )

    assert status == 503
    assert body == {"error": "runtime_control_unavailable"}


def test_v2_only_policy_mismatch_never_repairs_or_routes_to_resident(monkeypatch):
    uid = "u_send_v2_policy_mismatch"
    _seed(uid)
    store = core_store.get_store(uid)
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "v2_only")
    monkeypatch.setattr(
        hosted_config_store,
        "apply_hosted_runtime_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("request path must not mutate runtime ownership")
        ),
    )
    monkeypatch.setattr(
        store,
        "append_chat",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("policy mismatch must refuse before persistence")
        ),
    )
    body, status = chat_send_core.model_api_chat_send_core(
        store, api_key="key", runtime_tok="", payload={"message": "hi"},
    )

    assert status == 503
    assert body == {"error": "runtime_policy_not_ready"}


def test_v2_blob_without_authoritative_v2_state_fails_closed(monkeypatch):
    uid = "u_send_split_runtime_control"
    _seed(uid)
    store = core_store.get_store(uid)
    # Reproduce the deployed pre-0034 split: blob opted into V2 while the new
    # authoritative row is absent/default resident.
    db.patch_blob_strict(
        uid,
        hosted_config_store.MODEL_API_RUNTIME_BLOB,
        {"hosted_runtime_mode": "db_action_v2"},
    )
    monkeypatch.setattr(
        store,
        "append_chat",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("split ownership must not persist")),
    )

    body, status = chat_send_core.model_api_chat_send_core(
        store, api_key="key", runtime_tok="", payload={"message": "hi"},
    )

    assert status == 503
    assert body == {"error": "runtime_policy_not_ready"}


def test_db_action_v2_input_write_failure_never_enqueues(monkeypatch):
    _seed("u_send_v2_strict_failure")
    store = core_store.get_store("u_send_v2_strict_failure")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    monkeypatch.setattr(
        chat_send_core.core_envelope, "_build_shared_envelope_for_store",
        lambda s, pt, **kw: ({"id": "u-msg-strict", "body_ct": "c", "nonce": "n", "K_user": "k"}, ""),
    )
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose, **kw: b"sk-or-test",
    )
    monkeypatch.setattr(chat_send_core.agent_runtime_cutover, "resolve_driver", lambda cfg: "claude")
    monkeypatch.setattr(chat_send_core.jobs_store, "workers_alive", lambda **kw: True)
    monkeypatch.setattr(chat_send_core.jobs_store, "live_worker_capacity", lambda **kw: 1)
    monkeypatch.setattr(chat_send_core.jobs_store, "inflight_job_count", lambda: 0)
    monkeypatch.setattr(chat_send_core.jobs_store, "recent_mean_service_sec", lambda **kw: None)

    def _fail_append(*args, **kwargs):
        assert kwargs.get("strict") is True
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "append_chat", _fail_append)
    monkeypatch.setattr(
        chat_send_core.jobs_store, "enqueue_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not enqueue")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        chat_send_core.model_api_chat_send_core(
            store, api_key="key", runtime_tok="", payload={"message": "hi"},
        )


def test_db_action_v2_with_no_live_workers_refuses_before_persist(monkeypatch):
    """Task 2: db_action_v2 skips the resident wedge guard entirely (it only
    means something for the CLI-consumer world), but had no replacement — if
    every serve_worker process is dead, chat/send would enqueue a job that
    never gets claimed and the user waits forever with no error. When
    jobs_store.workers_alive() is False, the send must refuse with a distinct
    503 (workers_unavailable, NOT hosting_runtime_unavailable/
    supervisor_unavailable — the two failure modes must stay distinguishable)
    and must persist NOTHING: no chat message, no agent_jobs row."""
    _seed("u_send_v2_dead_pool")
    store = core_store.get_store("u_send_v2_dead_pool")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")

    monkeypatch.setattr(
        chat_send_core.core_envelope, "_build_shared_envelope_for_store",
        lambda s, pt, **kw: ({"id": "u-msg-1", "body_ct": "c", "nonce": "n", "K_user": "k"}, ""),
    )
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose, **kw: b"sk-or-test",
    )
    monkeypatch.setattr(chat_send_core.agent_runtime_cutover, "resolve_driver", lambda cfg: "claude")
    monkeypatch.setattr(chat_send_core.jobs_store, "workers_alive", lambda **kw: False)

    enqueue_called = {"n": 0}
    monkeypatch.setattr(
        chat_send_core.jobs_store, "enqueue_job",
        lambda *a, **k: enqueue_called.update(n=enqueue_called["n"] + 1) or (0, False),
    )

    with db.get_pool().connection() as conn:
        before = conn.execute(
            "SELECT count(*) FROM chat_messages WHERE user_id=%s", ("u_send_v2_dead_pool",)
        ).fetchone()[0]

    body, status = chat_send_core.model_api_chat_send_core(
        store, api_key="key", runtime_tok="", payload={"message": "hi"},
    )

    assert status == 503
    assert body["error"] == "workers_unavailable"
    assert body["error"] not in ("hosting_runtime_unavailable", "supervisor_unavailable")
    assert enqueue_called["n"] == 0

    with db.get_pool().connection() as conn:
        after = conn.execute(
            "SELECT count(*) FROM chat_messages WHERE user_id=%s", ("u_send_v2_dead_pool",)
        ).fetchone()[0]
        jobs_after = conn.execute(
            "SELECT count(*) FROM agent_jobs WHERE user_id=%s", ("u_send_v2_dead_pool",)
        ).fetchone()[0]
    assert after == before  # nothing persisted on refusal
    assert jobs_after == 0


def test_db_action_v2_over_sla_admission_rejects_before_persist(monkeypatch):
    """§6 admission ceiling: the resident wedge guard is skipped for db_action_v2
    (Task 9) and the dead-pool liveness guard above only catches a fully dead
    worker pool (Task 2) — it says nothing about a *live but overloaded* pool.
    If estimated queue wait exceeds the SLA, send must refuse with a distinct
    "busy"/"queue_over_sla" 503 BEFORE persisting anything (same
    persist-nothing-on-refusal principle as the two guards above)."""
    _seed("u_send_v2_over_sla")
    store = core_store.get_store("u_send_v2_over_sla")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")

    monkeypatch.setattr(
        chat_send_core.core_envelope, "_build_shared_envelope_for_store",
        lambda s, pt, **kw: ({"id": "u-msg-1", "body_ct": "c", "nonce": "n", "K_user": "k"}, ""),
    )
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose, **kw: b"sk-or-test",
    )
    monkeypatch.setattr(chat_send_core.agent_runtime_cutover, "resolve_driver", lambda cfg: "claude")
    monkeypatch.setattr(chat_send_core.jobs_store, "workers_alive", lambda **kw: True)
    # 1 worker, 999 in-flight, no history (falls back to the 20s default
    # service time) → est_wait = ceil(999/1)*20 = 19980s, far over the 60s SLA.
    monkeypatch.setattr(chat_send_core.jobs_store, "live_worker_capacity", lambda **kw: 1)
    monkeypatch.setattr(chat_send_core.jobs_store, "inflight_job_count", lambda: 999)
    monkeypatch.setattr(chat_send_core.jobs_store, "recent_mean_service_sec", lambda **kw: None)

    enqueue_called = {"n": 0}
    monkeypatch.setattr(
        chat_send_core.jobs_store, "enqueue_job",
        lambda *a, **k: enqueue_called.update(n=enqueue_called["n"] + 1) or (0, False),
    )
    append_chat_calls = {"n": 0}

    def _append_chat_spy(*a, **k):
        append_chat_calls["n"] += 1
        raise AssertionError("append_chat must not be called on admission refusal")

    monkeypatch.setattr(store, "append_chat", _append_chat_spy)

    body, status = chat_send_core.model_api_chat_send_core(
        store, api_key="key", runtime_tok="", payload={"message": "hi"},
    )

    assert status == 503
    assert body["error"] == "busy"
    assert body["reason"] == "queue_over_sla"
    assert body["est_wait_sec"] == 19980
    assert append_chat_calls["n"] == 0
    assert enqueue_called["n"] == 0


def test_db_action_v2_admission_check_fails_open_on_exception(monkeypatch):
    """§6 admission ceiling must never itself become a failure source: if the
    est-wait computation blows up (DB hiccup, unexpected None, etc.), send must
    still proceed normally instead of the user silently losing their turn."""
    _seed("u_send_v2_admission_failopen")
    store = core_store.get_store("u_send_v2_admission_failopen")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")

    monkeypatch.setattr(
        chat_send_core.core_envelope, "_build_shared_envelope_for_store",
        lambda s, pt, **kw: ({"id": "u-msg-1", "body_ct": "c", "nonce": "n", "K_user": "k"}, ""),
    )
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose, **kw: b"sk-or-test",
    )
    monkeypatch.setattr(chat_send_core.agent_runtime_cutover, "resolve_driver", lambda cfg: "claude")
    monkeypatch.setattr(chat_send_core.jobs_store, "workers_alive", lambda **kw: True)
    monkeypatch.setattr(chat_send_core.jobs_store, "live_worker_capacity", lambda **kw: 1)

    def _boom():
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(chat_send_core.jobs_store, "inflight_job_count", _boom)

    notified = {}
    monkeypatch.setattr(
        chat_send_core.core_wake_bus, "notify",
        lambda channel, user_id="": notified.update(channel=channel, user_id=user_id),
    )

    body, status = chat_send_core.model_api_chat_send_core(
        store, api_key="key", runtime_tok="", payload={"message": "hi"},
    )

    assert status == 202
    assert body["status"] == "processing"
    assert notified["channel"] == "v2_jobs"
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT lane, status, reason FROM agent_jobs WHERE user_id=%s",
            ("u_send_v2_admission_failopen",),
        ).fetchall()
    assert len(rows) == 1
    lane, job_status, reason = rows[0]
    assert (lane, job_status, reason) == ("chat", "pending", "chat_send")


def test_db_action_v2_admission_admits_under_sla(monkeypatch):
    """Normal case: live worker pool with no queue backlog admits as usual —
    the admission ceiling is a no-op when the estimated wait is under SLA."""
    _seed("u_send_v2_admission_ok")
    store = core_store.get_store("u_send_v2_admission_ok")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")

    monkeypatch.setattr(
        chat_send_core.core_envelope, "_build_shared_envelope_for_store",
        lambda s, pt, **kw: ({"id": "u-msg-1", "body_ct": "c", "nonce": "n", "K_user": "k"}, ""),
    )
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose, **kw: b"sk-or-test",
    )
    monkeypatch.setattr(chat_send_core.agent_runtime_cutover, "resolve_driver", lambda cfg: "claude")
    monkeypatch.setattr(chat_send_core.jobs_store, "workers_alive", lambda **kw: True)
    monkeypatch.setattr(chat_send_core.jobs_store, "live_worker_capacity", lambda **kw: 1)
    monkeypatch.setattr(chat_send_core.jobs_store, "inflight_job_count", lambda: 0)
    monkeypatch.setattr(chat_send_core.jobs_store, "recent_mean_service_sec", lambda **kw: None)

    notified = {}
    monkeypatch.setattr(
        chat_send_core.core_wake_bus, "notify",
        lambda channel, user_id="": notified.update(channel=channel, user_id=user_id),
    )

    body, status = chat_send_core.model_api_chat_send_core(
        store, api_key="key", runtime_tok="", payload={"message": "hi"},
    )

    assert status == 202
    assert body["status"] == "processing"
    assert notified["channel"] == "v2_jobs"
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT lane, status, reason FROM agent_jobs WHERE user_id=%s",
            ("u_send_v2_admission_ok",),
        ).fetchall()
    assert len(rows) == 1
    lane, job_status, reason = rows[0]
    assert (lane, job_status, reason) == ("chat", "pending", "chat_send")
