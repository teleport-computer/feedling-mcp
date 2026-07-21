"""Reconciler: allowlist desired → per-user fence convergence."""
import uuid

import pytest

import db
from conftest import configure_model_api_route
from hosted import runtime_reconciler


def _seed_user() -> str:
    """Insert a minimal ``users`` row + an active anthropic route, mirroring
    ``tests/test_dual_runtime_db.py``'s local ``fresh_user`` helper (no shared
    conftest fixture exists yet). The route is required because flipping to
    ``v2`` goes through ``config_store.set_hosted_runtime_mode``, which raises
    unless the user already has a model_api config (``require_active_hosted_route``)."""
    uid = f"u_reconciler_{uuid.uuid4().hex[:12]}"
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )
    configure_model_api_route(uid, provider="anthropic", model="claude-3-5-sonnet-latest")
    return uid


@pytest.fixture()
def fresh_user():
    return _seed_user()


@pytest.fixture()
def second_user():
    return _seed_user()


def test_desired_for_defaults_resident(monkeypatch):
    monkeypatch.delenv("FEEDLING_RUNTIME_DEFAULT_DESIRED", raising=False)
    assert runtime_reconciler.desired_for("usr_x", {}) == "resident"
    assert runtime_reconciler.desired_for("usr_x", {"usr_x": "v2"}) == "v2"
    monkeypatch.setenv("FEEDLING_RUNTIME_DEFAULT_DESIRED", "v2")
    assert runtime_reconciler.desired_for("usr_x", {}) == "v2"          # P6 全量默认
    assert runtime_reconciler.desired_for("usr_x", {"usr_x": "resident"}) == "resident"  # 显式 pin 胜默认


def test_reconcile_once_converges_both_directions(fresh_user):
    uid = fresh_user
    db.upsert_runtime_allowlist(uid, "v2")
    stats = runtime_reconciler.reconcile_once()
    assert stats["flipped"] >= 1
    from hosted import config_store
    from core import store as core_store
    mode, state, _ = config_store.get_hosted_runtime_control_strict(core_store.get_store(uid))
    assert (mode, state) == ("db_action_v2", "v2")
    # 反向
    db.upsert_runtime_allowlist(uid, "resident")
    runtime_reconciler.reconcile_once()
    mode, state, _ = config_store.get_hosted_runtime_control_strict(core_store.get_store(uid))
    assert (mode, state) == ("resident_cli", "resident")


def test_reconcile_converged_user_is_noop(fresh_user):
    uid = fresh_user
    db.upsert_runtime_allowlist(uid, "v2")
    runtime_reconciler.reconcile_once()
    stats = runtime_reconciler.reconcile_once()   # 已收敛
    assert stats["flipped"] == 0


def test_one_bad_user_does_not_wedge_the_loop(fresh_user, second_user, monkeypatch):
    db.upsert_runtime_allowlist(fresh_user, "v2")
    db.upsert_runtime_allowlist(second_user, "v2")
    orig = runtime_reconciler._flip_user
    def boom_first(uid, desired):
        if uid == fresh_user:
            raise RuntimeError("simulated flip failure")
        return orig(uid, desired)
    monkeypatch.setattr(runtime_reconciler, "_flip_user", boom_first)
    stats = runtime_reconciler.reconcile_once()
    assert stats["failed"] == 1 and stats["flipped"] == 1   # 坏用户不挡好用户


def test_failed_user_backs_off(fresh_user, monkeypatch):
    db.upsert_runtime_allowlist(fresh_user, "v2")
    monkeypatch.setattr(runtime_reconciler, "_flip_user",
                        lambda uid, d: (_ for _ in ()).throw(RuntimeError("x")))
    runtime_reconciler.reconcile_once()
    stats = runtime_reconciler.reconcile_once()   # 退避窗口内
    assert stats["skipped_backoff"] >= 1


def test_removed_v2_user_reverts_to_resident(fresh_user, monkeypatch):
    """spec §4a/§9: deleting the allowlist row IS the documented rollback
    path ("不在表里 = desired resident，表即真相" / emergency runbook "把 v2
    用户移出名单回 V1"). Under the default-resident canary phase, dropping the
    row must not strand the user off-scope at (db_action_v2, v2) forever —
    ``reconcile_once`` has to keep unioning
    ``list_hosted_runtime_nonresident_controls`` so a fenced-v2 user with no
    allowlist row is still visited and flipped back."""
    monkeypatch.delenv("FEEDLING_RUNTIME_DEFAULT_DESIRED", raising=False)
    uid = fresh_user
    from core import store as core_store
    from hosted import config_store

    db.upsert_runtime_allowlist(uid, "v2")
    stats = runtime_reconciler.reconcile_once()
    assert stats["flipped"] >= 1
    mode, state, _ = config_store.get_hosted_runtime_control_strict(core_store.get_store(uid))
    assert (mode, state) == ("db_action_v2", "v2")

    db.delete_runtime_allowlist(uid)
    assert uid not in db.get_runtime_allowlist_map()

    stats = runtime_reconciler.reconcile_once()
    assert stats["flipped"] >= 1
    mode, state, _ = config_store.get_hosted_runtime_control_strict(core_store.get_store(uid))
    assert (mode, state) == ("resident_cli", "resident")
