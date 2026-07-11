"""account_reset (POST /v1/account/reset) 必须清掉该用户在所有按 user_id 存储的表里的行。

回归测试：baseline(0001) 之后新增的 perception / agent_runtime / genesis 表
曾被 db.delete_user_data 的硬编码清单漏掉，导致"删账号"后这些数据永久残留。
此测试给一个用户在每张表里塞行，走真实 reset 路由，断言全部清零。
"""

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


_pk_counter = itertools.count(1)


def _register(client) -> tuple[str, str]:
    import base64
    raw = next(_pk_counter).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(raw).decode(), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _seed_all_per_user_tables(user_id: str) -> None:
    """给该用户在每张按 user_id 存储的表里插一行。"""
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO perception_items (user_id, kind, item_id, ts, doc) "
            "VALUES (%s, 'photo', 'i1', 1.0, '{}'::jsonb)",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO perception_daily (user_id, date, signal, doc, updated_at) "
            "VALUES (%s, '2026-06-28', 'steps', '{}'::jsonb, 1.0)",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO agent_runtime_instances (user_id, driver, status, runtime_home) "
            "VALUES (%s, 'claude', 'idle', '/tmp/rt')",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO genesis_import_jobs (user_id, job_id, status) "
            "VALUES (%s, 'job1', 'done')",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO genesis_import_chunks "
            "(user_id, job_id, seq, ciphertext_sha256, encrypted_body) "
            "VALUES (%s, 'job1', 0, 'abc', %s)",
            (user_id, b"x"),
        )
        conn.execute(
            "INSERT INTO genesis_import_outputs (user_id, job_id, output_type) "
            "VALUES (%s, 'job1', 'persona')",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO world_book_entries (user_id, entry_id, updated_at, doc) "
            "VALUES (%s, 'wb1', '2026-07-03T00:00:00', '{}'::jsonb)",
            (user_id,),
        )
    cid = db.model_api_credential_create(
        user_id, provider="anthropic", base_url="", label="k",
        api_key_envelope={"v": 1, "body_ct": "ct", "nonce": "n"},
        api_key_hint="sk-x...000", supports_responses=False,
    )
    db.model_api_route_upsert(user_id, cid, "claude-sonnet-4-5", None)


_PER_USER_TABLES = (
    "perception_items",
    "perception_daily",
    "agent_runtime_instances",
    "genesis_import_jobs",
    "genesis_import_chunks",
    "genesis_import_outputs",
    "world_book_entries",
    "model_api_credentials",
    "model_api_routes",
)


def _remaining_rows(user_id: str) -> dict[str, int]:
    counts = {}
    with db.get_pool().connection() as conn:
        for table in _PER_USER_TABLES:
            row = conn.execute(
                f"SELECT count(*) FROM {table} WHERE user_id = %s", (user_id,)
            ).fetchone()
            counts[table] = row[0]
    return counts


def test_reset_purges_every_per_user_table(client):
    uid, api_key = _register(client)
    _seed_all_per_user_tables(uid)

    # sanity: 行确实插进去了
    assert all(v > 0 for v in _remaining_rows(uid).values())

    res = client.post(
        "/v1/account/reset",
        json={"confirm": "delete-all-data"},
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200, res.get_data(as_text=True)

    leftover = {t: n for t, n in _remaining_rows(uid).items() if n > 0}
    assert leftover == {}, f"删账号后这些表仍有残留行: {leftover}"


@pytest.fixture()
def seeded_user(client) -> dict:
    """已注册 + 各 per-user 表有行的用户（供 R2-failure 测试沿用既有 fixture 风格）。"""
    uid, api_key = _register(client)
    _seed_all_per_user_tables(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, 'model_api', %s)",
            (uid, db.Jsonb({"provider": "anthropic"})),
        )
        conn.execute(
            "INSERT INTO user_logs (user_id, stream, ts, item_key, doc) "
            "VALUES (%s, 'chat', 1.0, 'k1', '{}'::jsonb)",
            (uid,),
        )
        conn.execute(
            "INSERT INTO memory_moments (user_id, moment_id, occurred_at, doc) "
            "VALUES (%s, 'm1', 1.0, '{}'::jsonb)",
            (uid,),
        )
    return {"user_id": uid, "api_key": api_key}


def test_reset_besteffort_downstream_failure_does_not_abort_or_half_delete(client, seeded_user, monkeypatch):
    """DB 删除后的 best-effort 下游清理（R2 frames / DB 兜底）失败不应让 reset
    503 abort、更不应半删——DB 是原子权威源（0011 CASCADE）。
    （明文 onboarding 归档是例外：它在删账号之前清理、失败会 abort，见
    test_onboarding_archive_reset.py::test_reset_aborts_when_archive_cleanup_fails_persistently。）"""
    uid = seeded_user["user_id"]
    from onboarding_archive import storage as arch
    monkeypatch.setattr(arch, "enabled", lambda: False)  # 归档不启用 → 不影响本用例

    def _boom(_uid):
        raise RuntimeError("R2 frames down")

    monkeypatch.setattr(db, "delete_user_frames", _boom)  # best-effort 步骤抖动

    resp = client.post(
        "/v1/account/reset",
        headers={"X-API-Key": seeded_user["api_key"]},
        json={"confirm": "delete-all-data"},
    )
    assert resp.status_code == 200  # best-effort 下游失败不 abort
    assert resp.get_json().get("deleted") is True

    with db.get_pool().connection() as conn:
        for t in ("users", "user_blobs", "user_logs", "memory_moments"):
            col = "user_id"
            n = conn.execute(
                f"SELECT count(*) FROM {t} WHERE {col} = %s", (uid,)
            ).fetchone()[0]
            assert n == 0, f"{t} not purged despite best-effort downstream failure ({n} rows)"


def test_reset_stops_hosted_agent(client):
    """删账号后托管 agent 必须停下来：用户既不再被托管发现（→ supervisor 下个
    tick 把子进程 kill + 释放 lease），其 agent_runtime_instances lease 行也清掉
    （renew 命中 0 行 → supervisor 判定丢 lease 同样会 kill）。"""
    import db as _db
    uid, api_key = _register(client)

    # 该用户配了一个能 fit 的 provider 且 test_status=ok → 会被托管发现
    # (roster 数据源已从 user_blobs(kind='model_api') 改为
    # model_api_routes JOIN model_api_credentials，见 Task 3)
    cid = _db.model_api_credential_create(
        uid, provider="anthropic", base_url="", label="k",
        api_key_envelope={"v": 1, "body_ct": "ct", "nonce": "n"},
        api_key_hint="sk-x...000", supports_responses=False,
    )
    rid = _db.model_api_route_upsert(uid, cid, "claude-x", None)
    _db.model_api_route_mark_test(uid, rid, status="ok")
    _db.model_api_route_activate(uid, rid)
    with _db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_runtime_instances (user_id, driver, status, runtime_home) "
            "VALUES (%s, 'claude', 'running', '/tmp/rt')",
            (uid,),
        )

    discovered = {u["user_id"] for u in _db.list_agent_runtime_enabled_users()}
    assert uid in discovered, "前置条件：用户应被托管发现"

    res = client.post(
        "/v1/account/reset",
        json={"confirm": "delete-all-data"},
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200, res.get_data(as_text=True)

    discovered_after = {u["user_id"] for u in _db.list_agent_runtime_enabled_users()}
    assert uid not in discovered_after, "删账号后用户仍被托管发现 → supervisor 会重新拉起 agent"

    from agent_runtime import leases
    assert leases.get(uid) is None, "删账号后 lease 行仍在"
