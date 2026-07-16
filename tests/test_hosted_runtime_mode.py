"""hosted_runtime_mode 灰度开关：默认 resident_cli，可切 db_action_v2，非法值拒绝。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from core import store as core_store
from hosted import config_store as hosted_config_store
from hosted import setup_core

from conftest import configure_model_api_route


def _seed_model_api_user(uid):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )
    # 需要一个 model_api 配置，_patch_model_api_runtime_profile 才能建 runtime profile。
    # provider config 现在落在 model_api_routes/credentials（model-api-multi-profile）。
    configure_model_api_route(uid, provider="anthropic", model="m")


def _reset_runtime_control(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_runtime_state WHERE user_id=%s", (uid,))
        conn.execute(
            "DELETE FROM user_blobs WHERE user_id=%s AND kind=%s",
            (uid, hosted_config_store.MODEL_API_RUNTIME_BLOB),
        )


def test_default_mode_is_resident_cli():
    _seed_model_api_user("u_mode_1")
    store = core_store.get_store("u_mode_1")
    assert hosted_config_store.get_hosted_runtime_mode(store) == "resident_cli"


def test_invalid_persisted_mode_falls_back_to_resident_cli():
    uid = "u_mode_invalid_persisted"
    _seed_model_api_user(uid)
    db.set_blob(uid, hosted_config_store.MODEL_API_RUNTIME_BLOB, {
        "hosted_runtime_mode": "not-a-mode",
    })
    store = core_store.get_store(uid)

    assert hosted_config_store.get_hosted_runtime_mode(store) == "resident_cli"
    assert hosted_config_store.get_hosted_runtime_mode_strict(store) == "resident_cli"


def test_set_and_get_db_action_v2():
    uid = "u_mode_2"
    _seed_model_api_user(uid)
    _reset_runtime_control(uid)
    store = core_store.get_store(uid)
    out = hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    assert out == "db_action_v2"
    assert hosted_config_store.get_hosted_runtime_mode(store) == "db_action_v2"
    with db.get_pool().connection() as conn:
        state, generation = conn.execute(
            "SELECT hosted_runtime_state, runtime_generation "
            "FROM v2_runtime_state WHERE user_id=%s",
            (uid,),
        ).fetchone()
    assert (state, generation) == ("v2", 3)


def test_mode_flip_advances_generation_atomically_and_idempotently():
    uid = "u_mode_generation_flip"
    _seed_model_api_user(uid)
    _reset_runtime_control(uid)
    store = core_store.get_store(uid)

    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    assert db.get_runtime_generation(uid) == 3
    # Re-applying the same desired mode is a no-op for the fence generation.
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    assert db.get_runtime_generation(uid) == 3

    hosted_config_store.set_hosted_runtime_mode(store, "resident_cli")
    assert db.get_runtime_generation(uid) == 5
    with db.get_pool().connection() as conn:
        state = conn.execute(
            "SELECT hosted_runtime_state FROM v2_runtime_state WHERE user_id=%s",
            (uid,),
        ).fetchone()[0]
    assert state == "resident"


def test_rollback_and_reenable_invalidates_old_v2_effect_generation():
    uid = "u_mode_generation_aba"
    _seed_model_api_user(uid)
    _reset_runtime_control(uid)
    store = core_store.get_store(uid)
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    old_generation = db.get_runtime_generation(uid)
    assert old_generation == 3

    db.effect_enqueue(
        "mode-aba-old-effect", uid, 777, "status", old_generation,
        {"kind": "old"},
    )
    hosted_config_store.set_hosted_runtime_mode(store, "resident_cli")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    assert db.get_runtime_generation(uid) == 7

    from model_api_runtime.v2 import effect_outbox

    seen = []
    result = effect_outbox.apply_pending_effects(
        uid, dispatch=lambda effect_type, payload: seen.append((effect_type, payload)))
    assert result == {"applied": 0, "discarded": 1}
    assert seen == []


def test_resident_v2_resident_v2_roundtrip_bridges_answered_boundaries():
    uid = "u_mode_bidirectional_cursor_bridge"
    _seed_model_api_user(uid)
    _reset_runtime_control(uid)
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

    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    assert db.get_blob_strict(uid, hosted_config_store.MODEL_API_RUNTIME_BLOB)[
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
    hosted_config_store.set_hosted_runtime_mode(store, "resident_cli")
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
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    profile = db.get_blob_strict(uid, hosted_config_store.MODEL_API_RUNTIME_BLOB)
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


def test_model_api_delete_fences_effects_and_preserves_reply_cursor():
    uid = "u_mode_delete_fence"
    _seed_model_api_user(uid)
    _reset_runtime_control(uid)
    store = core_store.get_store(uid)
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    old_generation = db.get_runtime_generation(uid)
    db.patch_blob_strict(
        uid,
        hosted_config_store.MODEL_API_RUNTIME_BLOB,
        {
            "v2_reply_cursor_seq": 42,
            "provider": "anthropic",
            "model": "m",
            "memory_quality_warning": "stale-provider-warning",
        },
    )
    db.effect_enqueue(
        "delete-fence-old-effect",
        uid,
        778,
        "status",
        old_generation,
        {"kind": "old"},
    )

    body, status = setup_core.model_api_delete(store)

    assert status == 200 and body == {"deleted": True}
    profile = db.get_blob_strict(
        uid, hosted_config_store.MODEL_API_RUNTIME_BLOB)
    assert profile["hosted_runtime_mode"] == "resident_cli"
    assert profile["v2_reply_cursor_seq"] == 42
    assert "provider" not in profile
    assert "model" not in profile
    assert "memory_quality_warning" not in profile
    assert db.get_runtime_generation(uid) > old_generation
    assert db.model_api_credentials_list(uid) == []

    from model_api_runtime.v2 import effect_outbox

    seen = []
    result = effect_outbox.apply_pending_effects(
        uid,
        dispatch=lambda effect_type, payload: seen.append((effect_type, payload)),
    )
    assert result == {"applied": 0, "discarded": 1}
    assert seen == []


def test_model_api_delete_refences_concurrent_v2_resurrection(monkeypatch):
    uid = "u_mode_delete_resurrection"
    _seed_model_api_user(uid)
    _reset_runtime_control(uid)
    store = core_store.get_store(uid)
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    monkeypatch.setenv(
        hosted_config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only"
    )
    real_delete = db.model_api_config_delete_strict
    resurrected_generation = []

    def delete_after_interposed_policy_flip(user_id):
        # Reproduce a startup/setup transition that observed the route between
        # the endpoint's first resident fence and credential deletion.
        hosted_config_store.apply_hosted_runtime_policy(store)
        resurrected_generation.append(db.get_runtime_generation(user_id))
        return real_delete(user_id)

    monkeypatch.setattr(
        db, "model_api_config_delete_strict", delete_after_interposed_policy_flip
    )

    body, status = setup_core.model_api_delete(store)

    assert status == 200 and body == {"deleted": True}
    assert resurrected_generation
    mode, state, generation = db.get_hosted_runtime_control_strict(uid)
    assert (mode, state) == ("resident_cli", "resident")
    assert generation > resurrected_generation[0]

    # Even a caller holding a stale pre-delete config projection cannot flip
    # ownership after credentials are gone: eligibility is rechecked inside the
    # generation-fenced DB transaction.
    monkeypatch.setattr(
        hosted_config_store,
        "_load_model_api_config",
        lambda _store: {"provider": "anthropic", "model": "m"},
    )
    with pytest.raises(ValueError, match="active tested route"):
        hosted_config_store.apply_hosted_runtime_policy(store)
    assert db.get_hosted_runtime_control_strict(uid) == (
        "resident_cli",
        "resident",
        generation,
    )


def test_model_api_delete_fails_closed_before_removing_credentials(monkeypatch):
    uid = "u_mode_delete_fence_failure"
    _seed_model_api_user(uid)
    store = core_store.get_store(uid)
    credentials_before = db.model_api_credentials_list(uid)
    assert credentials_before
    monkeypatch.setattr(
        hosted_config_store,
        "prepare_model_api_delete",
        lambda _store: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    body, status = setup_core.model_api_delete(store)

    assert status == 500
    assert body == {"error": "model_api_runtime_disable_failed"}
    assert db.model_api_credentials_list(uid) == credentials_before


def test_model_api_delete_never_false_succeeds_when_config_delete_fails(monkeypatch):
    uid = "u_mode_delete_config_failure"
    _seed_model_api_user(uid)
    store = core_store.get_store(uid)
    credentials_before = db.model_api_credentials_list(uid)
    assert credentials_before
    monkeypatch.setattr(
        db,
        "model_api_config_delete_strict",
        lambda _uid: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    body, status = setup_core.model_api_delete(store)

    assert status == 500
    assert body == {"error": "model_api_config_delete_failed"}
    assert db.model_api_credentials_list(uid) == credentials_before


@pytest.mark.parametrize(
    ("blob_mode", "control_state", "requested_mode", "target_state"),
    [
        ("resident_cli", "v2", "db_action_v2", "v2"),
        ("db_action_v2", "resident", "resident_cli", "resident"),
    ],
)
def test_mode_set_repairs_split_brain_and_invalidates_generation(
    blob_mode, control_state, requested_mode, target_state
):
    uid = f"u_mode_split_{control_state}_{target_state}_{blob_mode}"
    _seed_model_api_user(uid)
    _reset_runtime_control(uid)
    db.set_blob_strict(uid, hosted_config_store.MODEL_API_RUNTIME_BLOB, {
        "hosted_runtime_mode": blob_mode,
    })
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,%s,11)",
            (uid, control_state),
        )

    hosted_config_store.set_hosted_runtime_mode(
        core_store.get_store(uid), requested_mode)

    with db.get_pool().connection() as conn:
        state, generation = conn.execute(
            "SELECT hosted_runtime_state,runtime_generation "
            "FROM v2_runtime_state WHERE user_id=%s",
            (uid,),
        ).fetchone()
    assert (state, generation) == (target_state, 13)
    assert db.get_blob_strict(
        uid, hosted_config_store.MODEL_API_RUNTIME_BLOB
    )["hosted_runtime_mode"] == requested_mode


def test_strict_mode_read_propagates_control_plane_failure(monkeypatch):
    _seed_model_api_user("u_mode_strict_read")
    store = core_store.get_store("u_mode_strict_read")
    monkeypatch.setattr(
        db, "get_blob_strict",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        hosted_config_store.get_hosted_runtime_mode_strict(store)


def test_mode_write_propagates_persistence_failure(monkeypatch):
    _seed_model_api_user("u_mode_strict_write")
    store = core_store.get_store("u_mode_strict_write")
    hosted_config_store.set_hosted_runtime_mode(store, "resident_cli")
    monkeypatch.setattr(
        db, "patch_blob_strict",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")


def test_concurrent_mode_flip_and_error_patch_preserve_both_fields():
    import concurrent.futures
    import threading

    uid = "u_mode_concurrent_patch"
    _seed_model_api_user(uid)
    store = core_store.get_store(uid)
    hosted_config_store.set_hosted_runtime_mode(store, "resident_cli")
    barrier = threading.Barrier(2)

    def _flip():
        barrier.wait()
        return hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")

    def _error():
        barrier.wait()
        hosted_config_store.set_last_runtime_error(store, "turn_failed:provider_unknown")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_flip), pool.submit(_error)]
        for future in futures:
            future.result(timeout=5)

    profile = db.get_blob_strict(uid, hosted_config_store.MODEL_API_RUNTIME_BLOB)
    assert profile["hosted_runtime_mode"] == "db_action_v2"
    assert profile["last_runtime_error"] == "turn_failed:provider_unknown"


def test_profile_normalization_cannot_regress_concurrently_advanced_v2_cursor(
    monkeypatch,
):
    """A generic normalizer starts from a stale full profile. Pause it at the
    persistence boundary, advance the dedicated cursor concurrently, then let
    the stale writer continue: it must not include/overwrite the cursor key."""
    import concurrent.futures
    import threading

    uid = "u_mode_normalize_cursor_race"
    _seed_model_api_user(uid)
    store = core_store.get_store(uid)
    db.patch_blob_strict(
        uid,
        hosted_config_store.MODEL_API_RUNTIME_BLOB,
        {"v2_reply_cursor_seq": 7, "hosted_runtime_mode": "resident_cli"},
    )
    stale = dict(db.get_blob_strict(
        uid, hosted_config_store.MODEL_API_RUNTIME_BLOB))

    entered = threading.Event()
    release = threading.Event()
    original_patch = db.patch_blob_strict

    def _paused_patch(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_patch(*args, **kwargs)

    monkeypatch.setattr(db, "patch_blob_strict", _paused_patch)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            hosted_config_store._save_model_api_runtime_profile,
            store,
            stale,
            strict=True,
        )
        assert entered.wait(timeout=5)
        db.advance_blob_int_strict(
            uid,
            hosted_config_store.MODEL_API_RUNTIME_BLOB,
            "v2_reply_cursor_seq",
            42,
        )
        release.set()
        future.result(timeout=5)

    profile = db.get_blob_strict(
        uid, hosted_config_store.MODEL_API_RUNTIME_BLOB)
    assert profile["v2_reply_cursor_seq"] == 42

    hosted_config_store._patch_model_api_runtime_profile(
        store, {"v2_reply_cursor_seq": 1, "last_runtime_error": "x"})
    profile = db.get_blob_strict(
        uid, hosted_config_store.MODEL_API_RUNTIME_BLOB)
    assert profile["v2_reply_cursor_seq"] == 42
    assert profile["last_runtime_error"] == "x"


def test_set_rejects_unknown_mode():
    _seed_model_api_user("u_mode_3")
    store = core_store.get_store("u_mode_3")
    with pytest.raises(ValueError):
        hosted_config_store.set_hosted_runtime_mode(store, "bogus")


def _seed_bare_user(uid):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )


def test_set_without_model_api_config_raises_and_stays_default():
    # 用户没有 model_api config → set 无法落地，必须抛错（不能返回假成功），
    # 且 get 仍回退默认 resident_cli（什么都没写进去）。
    _seed_bare_user("u_mode_4")
    store = core_store.get_store("u_mode_4")
    with pytest.raises(ValueError):
        hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    assert hosted_config_store.get_hosted_runtime_mode(store) == "resident_cli"


def test_set_rejects_stale_runtime_blob_without_active_model_route():
    uid = "u_mode_stale_blob"
    _seed_bare_user(uid)
    db.set_blob_strict(uid, hosted_config_store.MODEL_API_RUNTIME_BLOB, {
        "hosted_runtime_mode": "resident_cli",
    })
    store = core_store.get_store(uid)

    with pytest.raises(ValueError, match="no model_api config"):
        hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")


# ------------------------------------------------------------------
# set_last_runtime_error (Task 3: v2 worker terminal-failure error surface).
# Public direct lever wrapping _patch_model_api_runtime_profile — the v2 worker
# has no `store` binding in its early-failure path, only user_id, so
# serve_worker's injected callback re-fetches the store itself and calls this.
# ------------------------------------------------------------------

def test_set_last_runtime_error_writes_profile_field():
    _seed_model_api_user("u_mode_5")
    store = core_store.get_store("u_mode_5")
    hosted_config_store.set_last_runtime_error(store, "boom")
    profile = hosted_config_store._load_model_api_runtime_profile(store)
    assert profile.get("last_runtime_error") == "boom"
    with db.get_pool().connection() as conn:
        route_error = conn.execute(
            "SELECT last_runtime_error FROM model_api_routes "
            "WHERE user_id=%s AND is_active",
            (store.user_id,),
        ).fetchone()[0]
    assert route_error == "boom"


def test_set_last_runtime_error_truncates_at_300_chars():
    _seed_model_api_user("u_mode_6")
    store = core_store.get_store("u_mode_6")
    long_message = "x" * 500
    hosted_config_store.set_last_runtime_error(store, long_message)
    profile = hosted_config_store._load_model_api_runtime_profile(store)
    assert profile.get("last_runtime_error") == "x" * 300
    assert len(profile.get("last_runtime_error")) == 300
    with db.get_pool().connection() as conn:
        route_error = conn.execute(
            "SELECT last_runtime_error FROM model_api_routes "
            "WHERE user_id=%s AND is_active",
            (store.user_id,),
        ).fetchone()[0]
    assert route_error == "x" * 300
