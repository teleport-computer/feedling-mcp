"""Fleet-wide Runtime V2-only ownership and deployment policy."""

from __future__ import annotations

import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from conftest import configure_model_api_route, seed_user  # noqa: E402
from chat import chat_core  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import config_store  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402


ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _legacy_exact_job_shapes_profile_off(monkeypatch):
    """This suite's exact queue assertions describe the profile-off contract."""
    monkeypatch.setenv("FEEDLING_V2_PROFILE_ENABLED", "0")


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _seed_runnable(prefix: str, *, test_status: str = "ok") -> str:
    user_id = _uid(prefix)
    seed_user(user_id)
    configure_model_api_route(
        user_id,
        provider="anthropic",
        model="claude-test",
        test_status=test_status,
    )
    return user_id


def _clear_control(user_id: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (user_id,))
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (user_id,))
        conn.execute("DELETE FROM v2_runtime_state WHERE user_id=%s", (user_id,))
        conn.execute(
            "DELETE FROM user_blobs WHERE user_id=%s AND kind=%s",
            (user_id, config_store.MODEL_API_RUNTIME_BLOB),
        )


@pytest.fixture()
def fresh_user_store():
    """A user_id with an active model_api route and a clean runtime-control
    slate, wrapped in a UserStore (no ``conftest.fresh_user_store`` fixture
    existed prior to this task — this local fixture follows the same
    seed+clear pattern as ``_seed_runnable``/``_clear_control`` above)."""
    user_id = _seed_runnable("policy_fresh")
    _clear_control(user_id)
    return core_store.get_store(user_id)


def test_policy_parser_defaults_dual_and_rejects_retired_selectors(monkeypatch):
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    assert config_store.hosted_runtime_policy() == "dual"
    assert config_store.forced_hosted_runtime_mode() is None

    monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only")
    assert config_store.hosted_runtime_policy() == "v2_only"
    assert config_store.forced_hosted_runtime_mode() == "db_action_v2"

    for invalid in ("per_user", "resident_only", "v2-onley"):
        monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, invalid)
        with pytest.raises(RuntimeError, match="FEEDLING_HOSTED_RUNTIME_POLICY"):
            config_store.hosted_runtime_policy()


def test_policy_dual_is_default_and_valid(monkeypatch):
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    assert config_store.hosted_runtime_policy() == "dual"
    monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only")
    assert config_store.hosted_runtime_policy() == "v2_only"
    monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, "bogus")
    with pytest.raises(RuntimeError):
        config_store.hosted_runtime_policy()


def test_set_runtime_mode_accepts_resident_again(fresh_user_store):
    # 双向：v2 → resident → v2，fence generation 单调递增
    config_store.set_hosted_runtime_mode(fresh_user_store, "db_action_v2")
    _, state1, gen1 = config_store.get_hosted_runtime_control_strict(fresh_user_store)
    config_store.set_hosted_runtime_mode(fresh_user_store, "resident_cli")
    mode2, state2, gen2 = config_store.get_hosted_runtime_control_strict(fresh_user_store)
    assert mode2 == "resident_cli" and state2 == "resident" and gen2 > gen1


def test_v2_only_reconciles_every_runnable_shape_idempotently(monkeypatch):
    missing = _seed_runnable("policy_missing")
    resident = _seed_runnable("policy_resident")
    already_v2 = _seed_runnable("policy_v2")
    split = _seed_runnable("policy_split")
    failed = _seed_runnable("policy_failed", test_status="failed")
    for user_id in (missing, resident, already_v2, split, failed):
        _clear_control(user_id)

    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    # Restored (2b294a1f had removed this): establishes an explicit resident
    # control row — distinct from ``missing`` (no control row at all) — so
    # reconcile's v2_only forcing is exercised against an established resident
    # state too. Bidirectional under the new "dual" default policy.
    config_store.set_hosted_runtime_mode(
        core_store.get_store(resident), config_store.HOSTED_RUNTIME_MODE_RESIDENT
    )
    config_store.set_hosted_runtime_mode(
        core_store.get_store(already_v2),
        config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
    )
    db.patch_blob_strict(
        split,
        config_store.MODEL_API_RUNTIME_BLOB,
        {"hosted_runtime_mode": config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2},
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,'resident',11)",
            (split,),
        )

    monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only")
    monkeypatch.setattr(
        config_store,
        "UserStore",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fleet reconciliation must not hydrate UserStore")
        ),
    )
    result = config_store.reconcile_hosted_runtime_policy()

    assert result["policy"] == "v2_only"
    for user_id in (missing, resident, already_v2, split):
        mode, state, _generation = db.get_hosted_runtime_control_strict(user_id)
        assert (mode, state) == ("db_action_v2", "v2")
        assert jobs_store.get_wake_schedule(user_id) is not None
    assert db.get_hosted_runtime_control_strict(split)[2] == 13
    assert db.get_hosted_runtime_control_strict(failed)[:2] == (
        "resident_cli",
        "resident",
    )

    generations = {
        user_id: db.get_hosted_runtime_control_strict(user_id)[2]
        for user_id in (missing, resident, already_v2, split)
    }
    config_store.reconcile_hosted_runtime_policy()
    assert {
        user_id: db.get_hosted_runtime_control_strict(user_id)[2]
        for user_id in generations
    } == generations

    status = config_store.hosted_runtime_policy_status()
    assert status["policy"] == "v2_only"
    assert status["eligible_count"] == status["ready_count"]
    assert status["inconsistent_count"] == 0


def test_v2_schedule_seed_failure_never_flips_ownership(monkeypatch):
    user_id = _seed_runnable("policy_seed_failure")
    _clear_control(user_id)
    monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only")
    monkeypatch.setattr(jobs_store, "get_wake_schedule", lambda _uid: None)
    monkeypatch.setattr(
        jobs_store,
        "upsert_wake_schedule",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("schedule down")),
    )

    with pytest.raises(RuntimeError, match="schedule down"):
        config_store.apply_hosted_runtime_policy(core_store.get_store(user_id))

    assert db.get_hosted_runtime_control_strict(user_id)[:2] == (
        "resident_cli",
        "resident",
    )


def test_config_mutation_lock_serializes_runtime_transition(monkeypatch):
    user_id = _seed_runnable("policy_config_lock")
    _clear_control(user_id)
    store = core_store.get_store(user_id)
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    lock_held = threading.Event()
    transition_started = threading.Event()
    release_lock = threading.Event()
    transition_done = threading.Event()
    errors: list[BaseException] = []

    def _holder():
        try:
            with db.hosted_runtime_config_mutation_lock(user_id):
                lock_held.set()
                release_lock.wait(timeout=3)
        except BaseException as exc:  # surface thread failures in the test
            errors.append(exc)

    def _transition():
        try:
            assert lock_held.wait(timeout=3)
            transition_started.set()
            config_store.set_hosted_runtime_mode(
                store, config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
            )
            transition_done.set()
        except BaseException as exc:  # surface thread failures in the test
            errors.append(exc)

    holder = threading.Thread(target=_holder, daemon=True)
    transition = threading.Thread(target=_transition, daemon=True)
    holder.start()
    transition.start()
    try:
        assert transition_started.wait(timeout=3)
        assert not transition_done.wait(timeout=0.15)
    finally:
        release_lock.set()
    holder.join(timeout=3)
    transition.join(timeout=3)

    assert not holder.is_alive()
    assert not transition.is_alive()
    assert errors == []
    assert transition_done.is_set()
    assert db.get_hosted_runtime_control_strict(user_id)[:2] == (
        "db_action_v2",
        "v2",
    )


def test_config_lock_waiters_do_not_open_one_db_session_each(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(db, "_hosted_runtime_config_connection_slots", slots)
    monkeypatch.setattr(db, "_HOSTED_RUNTIME_CONFIG_LOCK_WAIT_SEC", 2.0)
    opened = []
    holding = threading.Event()
    release = threading.Event()
    waiter_errors: list[BaseException] = []

    class FakeResult:
        def fetchone(self):
            return (True,)

    class FakeConnection:
        def execute(self, *_args, **_kwargs):
            return FakeResult()

        def close(self):
            return None

    def fake_listen_connection():
        opened.append(object())
        return FakeConnection()

    monkeypatch.setattr(db, "listen_connection", fake_listen_connection)

    def holder():
        with db.hosted_runtime_config_mutation_lock("u_config_holder"):
            holding.set()
            release.wait(timeout=2)

    def waiter(index: int):
        try:
            assert holding.wait(timeout=2)
            with db.hosted_runtime_config_mutation_lock(f"u_waiter_{index}"):
                pass
        except BaseException as exc:
            waiter_errors.append(exc)

    holder_thread = threading.Thread(target=holder, daemon=True)
    holder_thread.start()
    assert holding.wait(timeout=2)
    waiters = [
        threading.Thread(target=waiter, args=(index,), daemon=True)
        for index in range(12)
    ]
    for thread in waiters:
        thread.start()
    time.sleep(0.15)
    assert len(opened) == 1
    release.set()
    holder_thread.join(timeout=2)
    for thread in waiters:
        thread.join(timeout=2)
    assert not holder_thread.is_alive()
    assert all(not thread.is_alive() for thread in waiters)
    assert waiter_errors == []


def test_config_lock_timeout_is_visible_503(monkeypatch):
    from hosted import setup_core

    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(db, "_hosted_runtime_config_connection_slots", slots)
    monkeypatch.setattr(db, "_HOSTED_RUNTIME_CONFIG_LOCK_WAIT_SEC", 0.01)
    store = core_store.get_store(_seed_runnable("policy_config_busy"))
    try:
        body, status = setup_core.model_api_test(store, api_key="unused")
    finally:
        slots.release()
    assert status == 503
    assert body == {"error": "model_api_config_busy"}


def test_v2_cutover_immediately_enqueues_resident_unanswered_message(
    monkeypatch,
):
    user_id = _seed_runnable("policy_cutover_chat_recovery")
    _clear_control(user_id)
    store = core_store.get_store(user_id)
    db.chat_append(
        user_id,
        "resident-answered",
        1.0,
        {"id": "resident-answered", "role": "user", "body_ct": "a"},
        0,
    )
    db.chat_update_metadata(
        user_id,
        "resident-answered",
        {"reply_status": "replied", "reply_message_id": "resident-reply"},
    )
    # This user row arrived while the resident turn was running. Its assistant
    # row can therefore be newer even though the message itself is unanswered.
    db.chat_append(
        user_id,
        "resident-pending",
        2.0,
        {"id": "resident-pending", "role": "user", "body_ct": "b"},
        0,
    )
    db.chat_append(
        user_id,
        "resident-late-reply",
        3.0,
        {"id": "resident-late-reply", "role": "openclaw", "body_ct": "r"},
        0,
    )
    monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only")

    config_store.apply_hosted_runtime_policy(store)

    generation = db.get_runtime_generation(user_id)
    with db.get_pool().connection() as conn:
        jobs = conn.execute(
            "SELECT lane,status,reason,trace_id,expected_runtime_generation "
            "FROM agent_jobs WHERE user_id=%s",
            (user_id,),
        ).fetchall()
    assert jobs == [(
        "chat",
        "pending",
        "runtime_cutover_recovery",
        "resident-pending",
        generation,
    )]


def test_v2_recutover_replaces_stale_generation_recovery_job(monkeypatch):
    # Restored (2b294a1f had removed this): bidirectional under "dual".
    user_id = _seed_runnable("policy_recutover_chat_recovery")
    _clear_control(user_id)
    store = core_store.get_store(user_id)
    db.chat_append(
        user_id,
        "resident-pending",
        1.0,
        {"id": "resident-pending", "role": "user", "body_ct": "b"},
        0,
    )
    db.chat_append(
        user_id,
        "resident-late-reply",
        2.0,
        {"id": "resident-late-reply", "role": "openclaw", "body_ct": "r"},
        0,
    )
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)

    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    )
    first_generation = db.get_runtime_generation(user_id)
    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_RESIDENT
    )
    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    )
    current_generation = db.get_runtime_generation(user_id)

    with db.get_pool().connection() as conn:
        jobs = conn.execute(
            "SELECT status,trace_id,expected_runtime_generation "
            "FROM agent_jobs WHERE user_id=%s ORDER BY id",
            (user_id,),
        ).fetchall()
    assert jobs == [
        ("superseded", "resident-pending", first_generation),
        ("pending", "resident-pending", current_generation),
    ]


def test_v2_recutover_does_not_fall_back_behind_newest_terminal_marker(
    monkeypatch,
):
    # Restored (2b294a1f had removed this): bidirectional under "dual".
    user_id = _seed_runnable("policy_recutover_terminal_latest")
    _clear_control(user_id)
    store = core_store.get_store(user_id)
    db.chat_append(
        user_id,
        "older-unanswered",
        1.0,
        {"id": "older-unanswered", "role": "user", "body_ct": "a"},
        0,
    )
    db.chat_append(
        user_id,
        "newest-terminal",
        2.0,
        {"id": "newest-terminal", "role": "user", "body_ct": "b"},
        0,
    )
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='failed',finished_at=now(),"
            "last_error='terminal test marker' "
            "WHERE user_id=%s AND trace_id='newest-terminal'",
            (user_id,),
        )

    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_RESIDENT
    )
    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    )

    with db.get_pool().connection() as conn:
        jobs = conn.execute(
            "SELECT status,trace_id FROM agent_jobs WHERE user_id=%s ORDER BY id",
            (user_id,),
        ).fetchall()
    assert jobs == [("failed", "newest-terminal")]


def test_v2_verify_loop_uses_worker_liveness_without_resident_ping(monkeypatch):
    user_id = _seed_runnable("policy_verify_loop")
    _clear_control(user_id)
    monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only")
    store = core_store.get_store(user_id)
    config_store.apply_hosted_runtime_policy(store)
    monkeypatch.setattr(jobs_store, "workers_alive", lambda **kwargs: True)

    body, status = chat_core.verify_loop(store, {"timeout_sec": 0})

    assert status == 200
    assert body["passing"] is True
    assert body["loop_alive"] is True
    with store.chat_lock:
        assert not any(
            message.get("source") == "verify_ping"
            for message in store.chat_messages
        )
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_jobs WHERE user_id=%s", (user_id,)
        ).fetchone()[0] == 0


def test_failed_active_model_test_fences_current_v2_generation(monkeypatch):
    user_id = _seed_runnable("policy_failed_active_test")
    _clear_control(user_id)
    store = core_store.get_store(user_id)
    monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only")
    config_store.apply_hosted_runtime_policy(store)
    v2_generation = db.get_runtime_generation(user_id)
    monkeypatch.setattr(
        "hosted.setup_core._test_active_route",
        lambda *_args, **_kwargs: ({"error": "provider_test_failed"}, 400),
    )

    from hosted import setup_core

    body, status = setup_core.model_api_test(store, api_key="test-key")

    assert status == 400
    assert body == {"error": "provider_test_failed"}
    assert db.get_hosted_runtime_control_strict(user_id) == (
        "resident_cli",
        "resident",
        v2_generation + 2,
    )


def test_all_main_composes_force_literal_dual_policy():
    # Dual-runtime coexistence (2026-07-21 design, Task 11): the three main CVM
    # composes each carry both backend AND a pooled serve-worker container, and
    # both services force the same literal measured-compose policy values —
    # "dual" (not the old "v2_only") plus a "resident" default fence, so a
    # fresh deploy's day-1 behavior is identical to pre-dual-runtime.
    for name in (
        "docker-compose.phala.yaml",
        "docker-compose.phala.test.yaml",
        "docker-compose.phala.pre.yaml",
    ):
        compose = yaml.safe_load((ROOT / "deploy" / name).read_text())
        for service in ("backend", "serve-worker"):
            env = compose["services"][service]["environment"]
            assert env["FEEDLING_HOSTED_RUNTIME_POLICY"] == "dual"
            assert env["FEEDLING_RUNTIME_DEFAULT_DESIRED"] == "resident"


def test_every_main_compose_turns_the_runtime_v2_baseline_on(monkeypatch):
    """Guard: "prod is OFF by default" is FALSE, and code comments must not say it.

    ``core.util.runtime_v2_default_on`` is the baseline for the env-gated V2
    rollout flags (perception ingress before it moved to the chat fence, screen
    captioning, the resident runtime flags). Its docstring — and screen_flag_v2's
    — used to read as "OFF in prod", but all three main composes inject
    FEEDLING_RUNTIME_V2_DEFAULT_ON=true, so prod has been ON the whole time.
    That mismatch is precisely why PR #107's "follow the chat fence" change
    silently flipped every prod user off the decrypting perception ingress
    (2026-07-24 null-perception incident). Pin compose and function together so
    neither side can drift back into the misleading state.
    """
    from core import util as core_util

    for name in (
        "docker-compose.phala.yaml",
        "docker-compose.phala.test.yaml",
        "docker-compose.phala.pre.yaml",
    ):
        compose = yaml.safe_load((ROOT / "deploy" / name).read_text())
        value = compose["services"]["backend"]["environment"]["FEEDLING_RUNTIME_V2_DEFAULT_ON"]
        assert str(value).lower() == "true", name
        monkeypatch.setenv("FEEDLING_RUNTIME_V2_DEFAULT_ON", str(value))
        assert core_util.runtime_v2_default_on() is True, name

    monkeypatch.delenv("FEEDLING_RUNTIME_V2_DEFAULT_ON", raising=False)
    assert core_util.runtime_v2_default_on() is False  # only an env-less process is OFF


def test_main_compose_serve_worker_shares_the_backend_image_and_stays_internal():
    # The serve-worker container is a second copy of the SAME image as backend
    # (so deploy/pin-runtime-release.sh's existing backend-image regex retags
    # it automatically) and reaches enclave/backend over compose-internal
    # URLs, never the public gateway passthrough the standalone runner CVMs
    # use.
    for name in (
        "docker-compose.phala.yaml",
        "docker-compose.phala.test.yaml",
        "docker-compose.phala.pre.yaml",
    ):
        compose = yaml.safe_load((ROOT / "deploy" / name).read_text())
        services = compose["services"]
        worker = services["serve-worker"]
        assert worker["image"] == services["backend"]["image"]
        assert worker["command"] == [
            "python",
            "-u",
            "backend/model_api_runtime/v2/serve_worker.py",
        ]
        env = worker["environment"]
        assert env["FEEDLING_API_URL"] == "http://backend:5001"
        assert env["FEEDLING_ENCLAVE_URL"] == "https://enclave:5003"


def test_all_three_standalone_runner_composes_are_v1_agent_runner_only():
    # Dual-runtime coexistence (Task 11, post-review correction): all three
    # environments are topologically identical — main CVM is `dual` (backend
    # + serve-worker), independent runner CVM is V1 agent-runner. Test is NOT
    # a gap: deploy/docker-compose.phala.runner.yaml has ALWAYS been V1
    # agent-runner form on origin/test (deploy-test-runner-cvm ships it to
    # the real feedling-io-agents-test CVM today — the #97 host-all fix was
    # verified there); this repo's file had drifted to a serve-worker-only
    # shape during an earlier, never-fully-deployed Runtime-V2-only migration
    # pass on this branch. Restored byte-for-byte from origin/test alongside
    # prod's. pre's is adapted from that same origin/test V1 runner template
    # (env var names unchanged, only naming — app/container/volume — is
    # pre-specific). prod's is restored byte-for-byte from origin/test too
    # (= prod's actual live topology).
    for name in (
        "docker-compose.phala.runner.yaml",
        "docker-compose.phala.pre.runner.yaml",
        "docker-compose.phala.prod.runner.yaml",
    ):
        path = ROOT / "deploy" / name
        source = path.read_text()
        compose = yaml.safe_load(source)
        assert set(compose["services"]) == {"agent-runner"}
        runner = compose["services"]["agent-runner"]
        assert runner["command"] == [
            "python",
            "-u",
            "backend/agent_runtime/supervisor.py",
        ]
        assert "model_api_runtime/v2/serve_worker.py" not in source
        assert "AGENT_RUNTIME_USERS" in source
        assert "volumes" in compose
        assert set(compose["volumes"]) == set(
            runner["volumes"][0].split(":")[0:1]
        )
