"""Dual-runtime DB surface: restored V1 supervisor functions + allowlist CRUD."""
import uuid

import pytest

import db


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
