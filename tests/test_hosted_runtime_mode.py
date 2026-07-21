"""Hosted model-API ownership fence: bidirectional under the "dual" policy,
fenced to db_action_v2 under the emergency "v2_only" override."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from conftest import configure_model_api_route, seed_user  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import config_store  # noqa: E402


def test_persisted_resident_value_is_only_a_dormant_compatibility_state():
    assert config_store.effective_hosted_runtime_mode("resident_cli") == "resident_cli"
    assert config_store.effective_hosted_runtime_mode(None) == "resident_cli"


def test_dual_policy_is_default(monkeypatch):
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    assert config_store.hosted_runtime_policy() == "dual"
    assert config_store.forced_hosted_runtime_mode() is None


def test_v2_only_policy_rejects_resident_selection(monkeypatch):
    monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only")
    with pytest.raises(ValueError, match="requires 'db_action_v2'"):
        config_store.set_hosted_runtime_mode(
            SimpleNamespace(user_id="unused"),
            config_store.HOSTED_RUNTIME_MODE_RESIDENT,
        )


def test_v2_only_policy_forces_db_action_v2(monkeypatch):
    monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only")
    assert config_store.hosted_runtime_policy() == "v2_only"
    assert (
        config_store.forced_hosted_runtime_mode()
        == config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    )
    for retired in ("per_user", "resident_only"):
        monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, retired)
        with pytest.raises(RuntimeError, match="FEEDLING_HOSTED_RUNTIME_POLICY"):
            config_store.hosted_runtime_policy()


def test_v2_mode_persists_with_generation_fence(backend_env, monkeypatch):
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    uid = f"runtime_v2_only_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    configure_model_api_route(
        uid, provider="anthropic", model="claude-test", test_status="ok"
    )
    store = core_store.get_store(uid)

    assert config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    ) == config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    assert config_store.get_hosted_runtime_control_strict(store)[:2] == (
        "db_action_v2",
        "v2",
    )


def test_dual_policy_allows_resident_selection(backend_env, monkeypatch):
    # Restored bidirectional coverage (2b294a1f had made resident unselectable
    # in any policy): under the default "dual" policy the per-user fence is
    # the truth and flips both ways.
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    uid = f"runtime_dual_resident_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    configure_model_api_route(
        uid, provider="anthropic", model="claude-test", test_status="ok"
    )
    store = core_store.get_store(uid)
    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    )

    assert config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_RESIDENT
    ) == config_store.HOSTED_RUNTIME_MODE_RESIDENT
    assert config_store.get_hosted_runtime_control_strict(store)[:2] == (
        "resident_cli",
        "resident",
    )


def test_route_delete_uses_dormant_fence_without_enabling_resident(
    backend_env, monkeypatch
):
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    uid = f"runtime_delete_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    configure_model_api_route(
        uid, provider="anthropic", model="claude-test", test_status="ok"
    )
    store = core_store.get_store(uid)
    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    )
    before = db.get_runtime_generation(uid)

    config_store.prepare_model_api_delete(store)

    assert db.get_hosted_runtime_control_strict(uid) == (
        "resident_cli",
        "resident",
        before + 2,
    )
    # The emergency v2_only override still fails closed here: it is a
    # fleet-wide forcing lane, independent of whether the delete already put
    # this particular user on the dormant fence.
    monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only")
    with pytest.raises(ValueError, match="requires 'db_action_v2'"):
        config_store.set_hosted_runtime_mode(
            store, config_store.HOSTED_RUNTIME_MODE_RESIDENT
        )


# ------------------------------------------------------------------
# Restored from the pre-retirement file (git show 2b294a1f -- this path).
# These specifically exercise `set_hosted_runtime_mode` flipping to
# `resident_cli`, which 2b294a1f made universally rejected; they are back in
# scope now that the "dual" default routes bidirectionally by the per-user
# fence. Adapted to the current seed_user/configure_model_api_route fixtures
# and unique per-test user ids (the original file used static ids like
# "u_mode_2" against a session-scoped DB).
# ------------------------------------------------------------------

def test_mode_flip_advances_generation_atomically_and_idempotently(
    backend_env, monkeypatch
):
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    uid = f"runtime_generation_flip_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    configure_model_api_route(uid, provider="anthropic", model="m")
    store = core_store.get_store(uid)

    config_store.set_hosted_runtime_mode(store, "db_action_v2")
    generation_after_first_v2 = db.get_runtime_generation(uid)
    # Re-applying the same desired mode is a no-op for the fence generation.
    config_store.set_hosted_runtime_mode(store, "db_action_v2")
    assert db.get_runtime_generation(uid) == generation_after_first_v2

    config_store.set_hosted_runtime_mode(store, "resident_cli")
    assert db.get_runtime_generation(uid) > generation_after_first_v2
    with db.get_pool().connection() as conn:
        state = conn.execute(
            "SELECT hosted_runtime_state FROM v2_runtime_state WHERE user_id=%s",
            (uid,),
        ).fetchone()[0]
    assert state == "resident"


def test_rollback_and_reenable_invalidates_old_v2_effect_generation(
    backend_env, monkeypatch
):
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    uid = f"runtime_generation_aba_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    configure_model_api_route(uid, provider="anthropic", model="m")
    store = core_store.get_store(uid)
    config_store.set_hosted_runtime_mode(store, "db_action_v2")
    old_generation = db.get_runtime_generation(uid)

    db.effect_enqueue(
        "mode-aba-old-effect", uid, 777, "status", old_generation,
        {"kind": "old"},
    )
    config_store.set_hosted_runtime_mode(store, "resident_cli")
    config_store.set_hosted_runtime_mode(store, "db_action_v2")
    assert db.get_runtime_generation(uid) > old_generation

    from model_api_runtime.v2 import effect_outbox

    seen = []
    result = effect_outbox.apply_pending_effects(
        uid, dispatch=lambda effect_type, payload: seen.append((effect_type, payload)))
    assert result == {"applied": 0, "discarded": 1}
    assert seen == []


def test_resident_v2_resident_v2_roundtrip_bridges_answered_boundaries(
    backend_env, monkeypatch
):
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    uid = f"runtime_bidirectional_cursor_bridge_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    configure_model_api_route(uid, provider="anthropic", model="m")
    store = core_store.get_store(uid)

    # Resident answers the first turn. An older unanswered row is superseded by
    # that newer answer and must be consumed by the same cutover boundary.
    for mid, ts in (("resident-old", 1.0), ("resident-answered", 2.0)):
        db.chat_append(uid, mid, ts, {"id": mid, "role": "user", "body_ct": mid}, 0)
    resident_answered_seq = db.chat_seq_for_msg_id(uid, "resident-answered")
    db.chat_update_metadata(
        uid,
        "resident-answered",
        {"reply_status": "replied", "reply_message_id": "resident-reply-1"},
    )

    config_store.set_hosted_runtime_mode(store, "db_action_v2")
    assert db.get_blob_strict(uid, config_store.MODEL_API_RUNTIME_BLOB)[
        "v2_reply_cursor_seq"
    ] == resident_answered_seq

    # V2 answers the next turn and atomically marks its parent for a rollback.
    db.chat_append(
        uid,
        "v2-user",
        3.0,
        {"id": "v2-user", "role": "user", "body_ct": "v2"},
        0,
    )
    v2_user_seq = db.chat_seq_for_msg_id(uid, "v2-user")
    assert v2_user_seq is not None
    db.chat_append_effect_with_cursor(
        uid,
        "v2-reply",
        4.0,
        {"id": "v2-reply", "role": "openclaw", "source": "model_api", "body_ct": "reply"},
        0,
        v2_user_seq,
    )
    config_store.set_hosted_runtime_mode(store, "resident_cli")
    assert db.chat_try_claim_reply(
        uid,
        "v2-user",
        "resident-consumer",
        10.0,
        {"reply_claimed_by": "resident-consumer", "reply_claim_expires_at": "20"},
        redelivery=True,
    ) is None

    # Resident answers once more; re-enabling V2 advances through that row and
    # leaves only truly new input after the cursor.
    db.chat_append(
        uid,
        "resident-user-2",
        5.0,
        {"id": "resident-user-2", "role": "user", "body_ct": "resident-2"},
        0,
    )
    resident_second_seq = db.chat_seq_for_msg_id(uid, "resident-user-2")
    db.chat_update_metadata(
        uid,
        "resident-user-2",
        {"reply_status": "replied", "reply_message_id": "resident-reply-2"},
    )
    config_store.set_hosted_runtime_mode(store, "db_action_v2")
    profile = db.get_blob_strict(uid, config_store.MODEL_API_RUNTIME_BLOB)
    assert profile["v2_reply_cursor_seq"] == resident_second_seq

    db.chat_append(
        uid,
        "brand-new",
        6.0,
        {"id": "brand-new", "role": "user", "body_ct": "new"},
        0,
    )
    after = db.chat_messages_after_seq(uid, profile["v2_reply_cursor_seq"])
    assert [row["id"] for row in after if row.get("role") == "user"] == ["brand-new"]


@pytest.mark.parametrize(
    ("blob_mode", "control_state", "requested_mode", "target_state"),
    [
        ("resident_cli", "v2", "db_action_v2", "v2"),
        ("db_action_v2", "resident", "resident_cli", "resident"),
    ],
)
def test_mode_set_repairs_split_brain_and_invalidates_generation(
    backend_env, blob_mode, control_state, requested_mode, target_state,
):
    uid = f"runtime_split_{control_state}_{target_state}_{blob_mode}_{uuid.uuid4().hex[:8]}"
    seed_user(uid)
    configure_model_api_route(uid, provider="anthropic", model="m")
    db.set_blob_strict(uid, config_store.MODEL_API_RUNTIME_BLOB, {
        "hosted_runtime_mode": blob_mode,
    })
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,%s,11)",
            (uid, control_state),
        )

    config_store.set_hosted_runtime_mode(core_store.get_store(uid), requested_mode)

    with db.get_pool().connection() as conn:
        state, generation = conn.execute(
            "SELECT hosted_runtime_state,runtime_generation "
            "FROM v2_runtime_state WHERE user_id=%s",
            (uid,),
        ).fetchone()
    assert (state, generation) == (target_state, 13)
    assert db.get_blob_strict(
        uid, config_store.MODEL_API_RUNTIME_BLOB
    )["hosted_runtime_mode"] == requested_mode


def test_mode_write_propagates_persistence_failure(backend_env, monkeypatch):
    uid = f"runtime_strict_write_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    configure_model_api_route(uid, provider="anthropic", model="m")
    store = core_store.get_store(uid)
    config_store.set_hosted_runtime_mode(store, "resident_cli")
    monkeypatch.setattr(
        db, "patch_blob_strict",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        config_store.set_hosted_runtime_mode(store, "db_action_v2")


def test_concurrent_mode_flip_and_error_patch_preserve_both_fields(backend_env):
    import concurrent.futures
    import threading

    uid = f"runtime_concurrent_patch_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    configure_model_api_route(uid, provider="anthropic", model="m")
    store = core_store.get_store(uid)
    config_store.set_hosted_runtime_mode(store, "resident_cli")
    barrier = threading.Barrier(2)

    def _flip():
        barrier.wait()
        return config_store.set_hosted_runtime_mode(store, "db_action_v2")

    def _error():
        barrier.wait()
        config_store.set_last_runtime_error(store, "turn_failed:provider_unknown")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_flip), pool.submit(_error)]
        for future in futures:
            future.result(timeout=5)

    profile = db.get_blob_strict(uid, config_store.MODEL_API_RUNTIME_BLOB)
    assert profile["hosted_runtime_mode"] == "db_action_v2"
    assert profile["last_runtime_error"] == "turn_failed:provider_unknown"


def test_set_rejects_unknown_mode(backend_env):
    uid = f"runtime_unknown_mode_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    store = core_store.get_store(uid)
    with pytest.raises(ValueError):
        config_store.set_hosted_runtime_mode(store, "bogus")
