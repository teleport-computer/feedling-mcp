"""Dual-runtime DB surface: restored V1 supervisor functions + allowlist CRUD."""
import json
import uuid

import pytest

import db
from conftest import configure_model_api_route


def _force_fence(user_id: str, *, mode: str, state: str) -> None:
    """Write the hosted-runtime fence directly, bypassing the guarded
    resident<->draining<->v2 transition machinery (``db.patch_blob_strict``'s
    ``runtime_state_target`` commits ``draining`` and the target state in the
    SAME transaction, so it's never externally observable — see
    ``db.get_hosted_runtime_control_strict``/``db.advance_runtime_state`` for
    the real carrier this mirrors: the ``model_api_runtime`` blob's
    ``hosted_runtime_mode`` key + the ``v2_runtime_state.hosted_runtime_state``
    row). Test-only; production reaches these states through the atomic
    control-plane writers.
    """
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO user_blobs (user_id, kind, doc) "
            "VALUES (%s, 'model_api_runtime', %s::jsonb) "
            "ON CONFLICT (user_id, kind) DO UPDATE SET "
            "doc = user_blobs.doc || EXCLUDED.doc",
            (user_id, json.dumps({"hosted_runtime_mode": mode})),
        )
        conn.execute(
            "INSERT INTO v2_runtime_state (user_id, hosted_runtime_state) "
            "VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "hosted_runtime_state = EXCLUDED.hosted_runtime_state, "
            "runtime_generation = v2_runtime_state.runtime_generation + 1, "
            "updated_at = now()",
            (user_id, state),
        )


@pytest.fixture()
def fresh_user():
    """Insert a minimal row into ``users`` so allowlist rows (and any future
    FK'd writes) aren't rejected. Mirrors the ``_seed`` helper in
    ``test_chat_send_v2_enqueue.py`` — no conftest ``fresh_user`` fixture
    exists yet, so this is a local one-off."""
    uid = f"u_dualruntime_{uuid.uuid4().hex[:12]}"
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )
    return uid


def test_v1_supervisor_db_surface_restored():
    # 反转 retirement 断言：这些必须重新存在（Task 3 的 agent_runtime 包要用）
    for name in (
        "set_supervisor_heartbeat",
        "read_supervisor_heartbeat",
        "set_supervisor_instance_heartbeat",
        "list_supervisor_instance_heartbeats",
        "prune_supervisor_instance_heartbeats",
        "list_agent_runtime_enabled_users",
    ):
        assert hasattr(db, name), name


def test_allowlist_crud_roundtrip(fresh_user):
    uid = fresh_user  # 若 conftest 无此 fixture，参照本文件同目录其它 DB 测试建用户的方式
    assert db.get_runtime_allowlist_map() == {} or uid not in db.get_runtime_allowlist_map()
    db.upsert_runtime_allowlist(uid, "v2", updated_by="test", note="canary")
    assert db.get_runtime_allowlist_map()[uid] == "v2"
    rows = db.list_runtime_allowlist()
    row = next(r for r in rows if r["user_id"] == uid)
    assert row["desired"] == "v2" and row["note"] == "canary"
    db.upsert_runtime_allowlist(uid, "resident")   # upsert 覆盖
    assert db.get_runtime_allowlist_map()[uid] == "resident"
    assert db.delete_runtime_allowlist(uid) is True
    assert uid not in db.get_runtime_allowlist_map()
    assert db.delete_runtime_allowlist(uid) is False  # 幂等


def test_allowlist_rejects_bad_desired(fresh_user):
    with pytest.raises(Exception):  # CHECK 约束或应用层校验
        db.upsert_runtime_allowlist(fresh_user, "bogus")


@pytest.fixture()
def three_users_with_routes():
    """Three users, each with an active ``test_status='ok'`` anthropic route —
    the roster's baseline eligibility bar. Route seeding mirrors
    ``tests/test_model_api_chat_send_routing.py``'s ``configure_model_api_route``
    usage (that file's own ``client`` fixture pins policy env to ``v2_only``,
    which is irrelevant here since this is a pure DB-level test with no HTTP
    client/policy involved — so its fixtures aren't imported, only the seeding
    helper)."""
    uids = []
    for _ in range(3):
        uid = f"u_dualruntime_{uuid.uuid4().hex[:12]}"
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO users (user_id, created_at, doc) "
                "VALUES (%s, '', '{}'::jsonb) ON CONFLICT (user_id) DO NOTHING",
                (uid,),
            )
        configure_model_api_route(uid, provider="anthropic", model="claude-3-5-sonnet-latest")
        db.set_onboarding_route_strict(
            uid,
            {"route": "model_api", "selected_at": "2026-07-27T00:00:00Z"},
        )
        uids.append(uid)
    return uids


def test_v1_roster_excludes_v2_and_draining_users(three_users_with_routes):
    u_resident, u_v2, u_draining = three_users_with_routes
    _force_fence(u_v2, mode="db_action_v2", state="v2")
    _force_fence(u_draining, mode="db_action_v2", state="draining")
    # u_resident 不动（默认 resident，无 fence 行）
    roster_ids = {r["user_id"] for r in db.list_agent_runtime_enabled_users()}
    assert u_resident in roster_ids
    assert u_v2 not in roster_ids
    assert u_draining not in roster_ids


def test_route_control_plane_selects_at_most_one_responder_class(fresh_user):
    """The primary responder invariant is ownership eligibility, not claim CAS.

    At each supported control state exactly one of Hosted V1, Hosted V2, or the
    route-declared independent resident is eligible. An already-running process
    is reaped asynchronously; the positive roster gate prevents it from being
    selected again while the resident route owns the account.
    """
    uid = fresh_user
    configure_model_api_route(
        uid,
        provider="anthropic",
        model="claude-3-5-sonnet-latest",
    )

    def eligibility() -> dict[str, bool]:
        route_doc = db.get_blob(uid, "onboarding_route")
        route = (
            str(route_doc.get("route") or "")
            if isinstance(route_doc, dict)
            else ""
        )
        _mode, runtime_state, _generation = db.get_hosted_runtime_control_strict(uid)
        roster = {row["user_id"] for row in db.list_agent_runtime_enabled_users()}
        return {
            "hosted_v1": uid in roster,
            "hosted_v2": runtime_state in {"v2", "draining"},
            "resident": route == "resident",
        }

    no_route_blob = eligibility()
    assert no_route_blob == {
        "hosted_v1": False,
        "hosted_v2": False,
        "resident": False,
    }
    assert sum(no_route_blob.values()) == 0

    db.set_onboarding_route_strict(
        uid,
        {"route": "model_api", "selected_at": "2026-07-27T00:00:00Z"},
    )
    model_api_v1 = eligibility()
    assert model_api_v1 == {
        "hosted_v1": True,
        "hosted_v2": False,
        "resident": False,
    }
    assert sum(model_api_v1.values()) == 1

    _force_fence(uid, mode="db_action_v2", state="v2")
    model_api_v2 = eligibility()
    assert model_api_v2 == {
        "hosted_v1": False,
        "hosted_v2": True,
        "resident": False,
    }
    assert sum(model_api_v2.values()) == 1

    _force_fence(uid, mode="resident_cli", state="resident")
    db.set_onboarding_route_strict(
        uid,
        {"route": "resident", "selected_at": "2026-07-27T00:01:00Z"},
    )
    resident = eligibility()
    assert resident == {
        "hosted_v1": False,
        "hosted_v2": False,
        "resident": True,
    }
    assert sum(resident.values()) == 1


def test_perception_flag_follows_fence_after_flip(fresh_user):
    # Task 8's runtime_reconciler now exists — flip the fence via the
    # allowlist + reconciler (the real production path) rather than calling
    # admin_core.set_runtime_mode directly. Needs an active route: v2 mode
    # requires it (require_active_hosted_route in db.patch_blob_strict).
    from hosted import runtime_reconciler
    from perception import service

    configure_model_api_route(fresh_user, provider="anthropic", model="claude-3-5-sonnet-latest")

    assert service.perception_ingress_runtime_v2_enabled(fresh_user) is False

    db.upsert_runtime_allowlist(fresh_user, "v2")
    stats = runtime_reconciler.reconcile_once()
    assert stats["flipped"] >= 1
    assert service.perception_ingress_runtime_v2_enabled(fresh_user) is True

    db.upsert_runtime_allowlist(fresh_user, "resident")
    stats = runtime_reconciler.reconcile_once()
    assert stats["flipped"] >= 1
    assert service.perception_ingress_runtime_v2_enabled(fresh_user) is False
