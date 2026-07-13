"""Tests for the db-layer CRUD over model_api_credentials / model_api_routes
(Task 2). These replace the single user_blobs(kind='model_api') blob.

Requires a real PostgreSQL — see tests/conftest.py, which provisions a
throwaway DB and runs migrations to head (so both tables already exist)
before any module is collected.
"""

import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import db  # noqa: E402

from conftest import seed_user  # noqa: E402

_ENV = {"v": 1, "body_ct": "ct", "nonce": "n"}


def _uid() -> str:
    return f"usr_{uuid.uuid4().hex[:16]}"


def _cred(uid, provider="anthropic", base_url="", hint="sk-a...451", label=None):
    return db.model_api_credential_create(
        uid, provider=provider, base_url=base_url, label=label or f"{provider} key",
        api_key_envelope=_ENV, api_key_hint=hint, supports_responses=False,
    )


def test_same_provider_can_hold_two_distinct_keys(backend_env):
    """iOS 的 credentialList 让用户在同一 provider 下选不同的凭据
    （个人 key / 团队 key）。credentials 表刻意没有 (user_id,provider,base_url)
    唯一索引，正是为了支持这个。"""
    uid = _uid()
    seed_user(uid)
    a = _cred(uid, hint="sk-a...451", label="Personal")
    b = _cred(uid, hint="sk-a...999", label="Team")
    assert a != b
    creds = db.model_api_credentials_list(uid)
    assert len(creds) == 2
    assert {c["api_key_hint"] for c in creds} == {"sk-a...451", "sk-a...999"}
    assert {c["label"] for c in creds} == {"Personal", "Team"}


def test_one_credential_can_have_many_routes(backend_env):
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    r1 = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", "off")
    assert r1 != r2
    assert len(db.model_api_routes_list(uid)) == 2


def test_route_upsert_is_idempotent_on_credential_model(backend_env):
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    r1 = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    r2 = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", "high")
    assert r1 == r2
    assert len(db.model_api_routes_list(uid)) == 1
    assert db.model_api_route_get(uid, r1)["reasoning_effort"] == "high"


def test_activate_leaves_exactly_one_active(backend_env):
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    r1 = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)

    assert db.model_api_route_activate(uid, r1) is True
    assert db.model_api_route_activate(uid, r2) is True

    actives = [r for r in db.model_api_routes_list(uid) if r["is_active"]]
    assert len(actives) == 1
    assert actives[0]["id"] == r2


def test_active_route_carries_envelope_but_list_does_not(backend_env):
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    r1 = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r1)

    active = db.model_api_active_route(uid)
    assert active["api_key_envelope"] == _ENV
    assert active["provider"] == "anthropic"

    listed = db.model_api_routes_list(uid)
    assert "api_key_envelope" not in listed[0]


def test_route_cannot_reference_another_users_credential(backend_env):
    uid_a = _uid()
    uid_b = _uid()
    seed_user(uid_a)
    seed_user(uid_b)
    cid_a = _cred(uid_a)
    # 复合外键 (user_id, credential_id) 让 DB 拒绝跨用户引用
    assert db.model_api_route_upsert(uid_b, cid_a, "claude-sonnet-4-5", None) is None
    assert db.model_api_routes_list(uid_b) == []


def test_deleting_credential_cascades_its_routes(backend_env):
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)

    assert db.model_api_credential_delete(uid, cid) is True
    assert db.model_api_routes_list(uid) == []


def test_autoselect_active_picks_latest_ok_route(backend_env):
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    r_failed = db.model_api_route_upsert(uid, cid, "bad-model", None)
    r_ok = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    db.model_api_route_mark_test(uid, r_failed, status="failed", error="401")
    db.model_api_route_mark_test(uid, r_ok, status="ok")

    picked = db.model_api_autoselect_active(uid)
    assert picked == r_ok
    assert db.model_api_active_route(uid)["id"] == r_ok


def test_autoselect_returns_none_when_no_ok_route(backend_env):
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "bad-model", None)
    db.model_api_route_mark_test(uid, r, status="failed", error="401")

    assert db.model_api_autoselect_active(uid) is None
    assert db.model_api_active_route(uid) is None


def test_mark_runtime_error_writes_active_route(backend_env):
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)

    assert db.model_api_route_mark_runtime_error(
        uid, error="insufficient balance", error_class="provider_402") is True
    got = db.model_api_route_get(uid, r)
    assert got["last_runtime_error"] == "insufficient balance"
    assert got["last_runtime_error_class"] == "provider_402"


def test_credential_update_changes_only_given_fields(backend_env):
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid, label="Original")

    assert db.model_api_credential_update(uid, cid, label="Renamed") is True
    got = db.model_api_credential_get(uid, cid)
    assert got["label"] == "Renamed"
    assert got["api_key_hint"] == "sk-a...451"  # untouched


def test_credential_update_with_no_fields_returns_false(backend_env):
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    assert db.model_api_credential_update(uid, cid) is False


def test_credential_get_missing_returns_none(backend_env):
    uid = _uid()
    seed_user(uid)
    assert db.model_api_credential_get(uid, str(uuid.uuid4())) is None


def test_route_delete_missing_returns_false(backend_env):
    uid = _uid()
    seed_user(uid)
    assert db.model_api_route_delete(uid, str(uuid.uuid4())) is False


def test_credential_delete_missing_returns_false(backend_env):
    uid = _uid()
    seed_user(uid)
    assert db.model_api_credential_delete(uid, str(uuid.uuid4())) is False


def test_activate_nonexistent_route_does_not_clear_current_active(backend_env):
    """核心保护：activate 一个不存在的 route_id 必须返回 False 且**零副作用**——
    绝不能顺手把用户当前 active route 清掉（否则他从 roster 消失、consumer 被杀）。"""
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    assert db.model_api_route_activate(uid, r) is True

    assert db.model_api_route_activate(uid, str(uuid.uuid4())) is False
    # 原 active route 依旧 active
    assert db.model_api_route_get(uid, r)["is_active"] is True
    assert db.model_api_active_route(uid)["id"] == r


def test_activate_other_users_route_is_noop_for_both(backend_env):
    """activate 属于另一个用户的 route_id → 返回 False，两个用户各自的 active
    route 都不受影响。"""
    uid_a = _uid()
    uid_b = _uid()
    seed_user(uid_a)
    seed_user(uid_b)
    cid_a = _cred(uid_a)
    cid_b = _cred(uid_b)
    ra = db.model_api_route_upsert(uid_a, cid_a, "claude-sonnet-4-5", None)
    rb = db.model_api_route_upsert(uid_b, cid_b, "claude-haiku-4-5", None)
    db.model_api_route_activate(uid_a, ra)
    db.model_api_route_activate(uid_b, rb)

    # B 试图 activate A 的 route
    assert db.model_api_route_activate(uid_b, ra) is False
    assert db.model_api_active_route(uid_a)["id"] == ra
    assert db.model_api_active_route(uid_b)["id"] == rb


def test_activate_malformed_route_id_returns_false_no_side_effect(backend_env):
    """非法 UUID 字面量 → psycopg cast 抛异常被吞 → 返回 False，无副作用。"""
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)

    assert db.model_api_route_activate(uid, "not-a-uuid") is False
    assert db.model_api_active_route(uid)["id"] == r


def test_concurrent_activate_keeps_exactly_one_active(backend_env):
    """并发正确性守卫。这是关于 activate **并发** 行为的核心断言，也是最难测的：
    2 线程 + 单次 Barrier 的朴素形态几乎踩不到竞态——GIL、各线程从池独立取连接、
    各自 BEGIN + 网络往返的时序让两个 activate 约 99.5% 退化成串行（实测 2 线程
    600 轮 0 命中）。那样测试虽不假（不变量断言恒有效），但作为回归守卫几乎无效：
    将来有人改坏落败者的回滚逻辑，它照绿。

    这里用 4 线程 × 100 轮，并预热连接池到 4 条常驻连接以消除连接创建延迟、放大
    临界区重叠。实测（见报告 Fix 2）：每轮约 2 个落败者，100 轮累计约 200 个，
    10 次连跑最小 199、耗时约 0.5s、不变量零违反。

    每轮：重置到「routes[0] 为 active」的已知状态 → 4 线程各 activate 一条不同的
    route，Barrier 同步 → 断言恰一条 active。**跨所有轮次累计**「落败者返回 False」
    总数，末尾断言 >= 1——把「这个测试到底有没有真的踩到并发竞态」本身变成断言：
    若某天时序变得永远串行，测试会明确告诉你它失去了保护力，而不是静默照绿。

    落败者 = 撞 model_api_routes_one_active 唯一索引 → 事务回滚 → 返回 False
    （swallow-and-log，绝不异常逃逸）。若某轮碰巧全部串行化都成功，那也合法；
    正确性断言恒为「不变量恒成立」，并发命中率断言是累计的、有约 200× 的余量，故
    不 flaky。"""
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    n = 4
    routes = [db.model_api_route_upsert(uid, cid, f"model-{i}", None) for i in range(n)]

    # 预热连接池：让 n 条连接常驻，activate 时无需现建连接，放大临界区重叠概率。
    pool = db.get_pool()
    warm = [pool.getconn() for _ in range(n)]
    for c in warm:
        pool.putconn(c)

    total_losers = 0
    for _ in range(100):
        db.model_api_route_activate(uid, routes[0])  # 重置到已知 active 状态
        barrier = threading.Barrier(n)
        results: dict[str, object] = {}

        def _worker(route_id: str):
            barrier.wait()
            try:
                results[route_id] = db.model_api_route_activate(uid, route_id)
            except Exception as e:  # 落败者绝不能异常逃逸
                results[route_id] = e

        threads = [threading.Thread(target=_worker, args=(r,)) for r in routes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 无异常逃逸——每个结果都是 bool。
        assert all(isinstance(v, bool) for v in results.values()), results
        # 至少一个成功。
        assert sum(1 for v in results.values() if v is True) >= 1
        # 不变量：恰好一条 active。
        actives = [r for r in db.model_api_routes_list(uid) if r["is_active"]]
        assert len(actives) == 1
        assert actives[0]["id"] in routes
        total_losers += sum(1 for v in results.values() if v is False)

    # 这个测试确实踩到过并发竞态回滚路径（否则它没在测并发）。
    assert total_losers >= 1, "concurrency test never hit the rollback path — lost its guard value"


def test_roster_only_returns_active_ok_routes(backend_env):
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    r_sonnet = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", "high")
    r_haiku = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    db.model_api_route_mark_test(uid, r_sonnet, status="ok")
    db.model_api_route_mark_test(uid, r_haiku, status="ok")
    db.model_api_route_activate(uid, r_sonnet)

    roster = [e for e in db.list_agent_runtime_enabled_users() if e["user_id"] == uid]
    assert len(roster) == 1
    assert roster[0]["model"] == "claude-sonnet-4-5"
    assert roster[0]["driver"] == "claude"       # anthropic → claude
    assert roster[0]["provider"] == "anthropic"
    assert roster[0]["reasoning_effort"] == "high"

    # 切到 haiku 后 roster 跟着换
    db.model_api_route_activate(uid, r_haiku)
    roster = [e for e in db.list_agent_runtime_enabled_users() if e["user_id"] == uid]
    assert roster[0]["model"] == "claude-haiku-4-5"
    assert roster[0]["reasoning_effort"] == ""


def test_roster_excludes_untested_active_route(backend_env):
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)   # test_status 仍是 untested

    assert [e for e in db.list_agent_runtime_enabled_users() if e["user_id"] == uid] == []


def test_roster_gemini_discovered_as_pi_unconditionally(backend_env):
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid, provider="gemini")
    r = db.model_api_route_upsert(uid, cid, "gemini-2.5-flash", None)
    db.model_api_route_mark_test(uid, r, status="ok")
    db.model_api_route_activate(uid, r)
    rows = [e for e in db.list_agent_runtime_enabled_users() if e["user_id"] == uid]
    assert len(rows) == 1
    assert rows[0]["driver"] == "pi"


def test_account_deletion_clears_credentials_and_routes(backend_env):
    """db.delete_user_data 的冗余兜底清单必须覆盖两张新表——虽然 0014 的
    users FK CASCADE 已经原子级联清净，但这条兜底带仍被 content_core.py 的
    销号路径调用，必须在没有 FK 的库上也正确。"""
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)

    db.delete_user_data(uid)

    assert db.model_api_credentials_list(uid) == []
    assert db.model_api_routes_list(uid) == []


def test_deleting_users_row_cascades(backend_env):
    """0014 迁移给两张新表挂了 users FK + ON DELETE CASCADE 的验收测试。"""
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)

    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE user_id = %s", (uid,))

    assert db.model_api_credentials_list(uid) == []
    assert db.model_api_routes_list(uid) == []


def test_route_columns_timestamps_are_utc_invariant_under_session_timezone(backend_env):
    """Issue 2 regression: _ROUTE_COLUMNS used ``to_char(r.created_at, …'Z')``
    without pinning the session TimeZone GUC first. to_char renders a
    timestamptz in whatever timezone the session happens to be in, so a
    non-UTC session (nothing in this repo pins TimeZone — see db.get_pool())
    would silently mislabel local wall-clock time as UTC.

    The connection pool (min_size=2) means ``SET TIME ZONE`` issued through
    one checked-out connection isn't guaranteed to be the connection
    db.model_api_routes_list()/model_api_route_get() draw next — so this test
    can't reliably observe the bug end-to-end through those functions. Instead
    it runs the exact _ROUTE_COLUMNS SQL fragment directly on one held-open
    connection, toggling TimeZone on *that* connection, which is deterministic.
    """
    uid = _uid()
    seed_user(uid)
    cid = _cred(uid)
    rid = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)

    query = (
        f"SELECT {db._ROUTE_COLUMNS} FROM model_api_routes r "
        "JOIN model_api_credentials c ON c.id = r.credential_id "
        "WHERE r.id = %s"
    )

    with db.get_pool().connection() as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        baseline = conn.execute(query, (rid,)).fetchone()

        conn.execute("SET TIME ZONE 'Asia/Shanghai'")
        shifted = conn.execute(query, (rid,)).fetchone()

        conn.execute("RESET TIME ZONE")

    # _ROUTE_COLUMNS' 0-indexed order: 11=last_test_at, 15=created_at, 16=updated_at
    # (12/13/14 are last_test_error/last_runtime_error/last_runtime_error_class).
    for idx, name in ((11, "last_test_at"), (15, "created_at"), (16, "updated_at")):
        assert baseline[idx] == shifted[idx], (
            f"{name} changed under SET TIME ZONE — to_char is reading the "
            f"session GUC instead of a fixed UTC offset: "
            f"{baseline[idx]!r} != {shifted[idx]!r}"
        )
    # created_at/updated_at are never blank for a freshly-created route.
    assert baseline[15] and baseline[15].endswith("Z")
    assert baseline[16] and baseline[16].endswith("Z")


def test_roster_supports_responses_bool_conversion_from_real_column(backend_env):
    """supports_responses 现在是真 BOOLEAN 列，不是 JSONB 里的字符串
    'true'/'false'。这条断言真正检验 True 这条分支——若代码写成
    ``supports_responses == "true"``（旧 JSONB 时代的写法），True 列值会被
    错误地判定为 False（``True == "true"`` 恒假），导致 openai_compatible
    中转用户被强制走 chat-completions 桥接，记忆/工具静默失效。"""
    uid = _uid()
    seed_user(uid)
    cid = db.model_api_credential_create(
        uid, provider="openai_compatible", base_url="https://relay.example/v1",
        label="relay key", api_key_envelope=_ENV, api_key_hint="sk-r...123",
        supports_responses=True,
    )
    r = db.model_api_route_upsert(uid, cid, "gpt-5", None)
    db.model_api_route_mark_test(uid, r, status="ok")
    db.model_api_route_activate(uid, r)

    rows = [e for e in db.list_agent_runtime_enabled_users() if e["user_id"] == uid]
    assert len(rows) == 1
    assert rows[0]["supports_responses"] is True
