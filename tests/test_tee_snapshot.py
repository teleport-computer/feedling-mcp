"""SNAPSHOT lane：临时表 COPY + 主键精确合并。

为什么是全量替换而不是增量：这批表数据量极小（prod 实测 25 张合计 1340 行 /
约 2 MB），但有大量 UPDATE/DELETE（队列 status 流转、心跳、allowlist）。现有的
append-only 游标模型处理可变行要靠 requeue 补偿 + prune，成本线性于表数且永久；
全量替换天然正确。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from tee_shadow import mirror, snapshot


@pytest.fixture
def sample_table():
    """在两侧建一张同形的测试表，避免依赖真实业务表的 schema 演进。"""
    ddl = ("CREATE TABLE IF NOT EXISTS _snap_probe ("
           "  k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    with db.get_pool().connection() as c:
        c.execute(ddl)
        c.execute("TRUNCATE _snap_probe")
    with mirror.get_tee_pool().connection() as c:
        c.execute(ddl)
        c.execute("TRUNCATE _snap_probe")
    yield "_snap_probe"
    with db.get_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS _snap_probe")
    with mirror.get_tee_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS _snap_probe")


@pytest.fixture
def unique_key_table():
    table = "_snap_unique_probe"
    ddl = (
        f"CREATE TABLE {table} ("
        "id TEXT PRIMARY KEY, business_key TEXT NOT NULL UNIQUE, v TEXT NOT NULL)"
    )
    for pool in (db.get_pool(), mirror.get_tee_pool()):
        with pool.connection() as c:
            c.execute(f"DROP TABLE IF EXISTS {table}")
            c.execute(ddl)
    yield table
    for pool in (db.get_pool(), mirror.get_tee_pool()):
        with pool.connection() as c:
            c.execute(f"DROP TABLE IF EXISTS {table}")


def _tee_rows(table: str) -> list[tuple]:
    with mirror.get_tee_pool().connection() as c:
        return c.execute(f"SELECT k, v FROM {table} ORDER BY k").fetchall()


@pytest.fixture
def fk_tables():
    """A SNAPSHOT parent with a child owned by another replication lane.

    This mirrors the live TEE relationship where CIPHERTEXT-lane
    v2_trajectory_reviews references SNAPSHOT-lane agent_jobs.  Refreshing the
    parent must not require truncating or rebuilding the external child.
    """
    parent = "_snap_fk_parent"
    child = "_snap_fk_external_child"
    parent_ddl = (
        f"CREATE TABLE {parent} (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
    )
    child_ddl = (
        f"CREATE TABLE {child} ("
        f"  id TEXT PRIMARY KEY, parent_k TEXT NOT NULL REFERENCES {parent}(k) "
        f"  ON DELETE CASCADE, v TEXT NOT NULL)"
    )
    for pool in (db.get_pool(), mirror.get_tee_pool()):
        with pool.connection() as c:
            c.execute(f"DROP TABLE IF EXISTS {child}")
            c.execute(f"DROP TABLE IF EXISTS {parent}")
            c.execute(parent_ddl)
            c.execute(child_ddl)
    yield parent, child
    for pool in (db.get_pool(), mirror.get_tee_pool()):
        with pool.connection() as c:
            c.execute(f"DROP TABLE IF EXISTS {child}")
            c.execute(f"DROP TABLE IF EXISTS {parent}")


def test_snapshot_copies_rows(sample_table):
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO _snap_probe (k, v) VALUES ('a','1'), ('b','2')")
    rep = snapshot.snapshot_table(sample_table)
    assert rep["ok"] is True
    assert rep["rows"] == 2
    assert _tee_rows(sample_table) == [("a", "1"), ("b", "2")]


def test_snapshot_is_idempotent(sample_table):
    """连跑两趟结果相同——全量替换不该产生重复行或 PK 冲突。"""
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO _snap_probe (k, v) VALUES ('a','1')")
    snapshot.snapshot_table(sample_table)
    snapshot.snapshot_table(sample_table)
    assert _tee_rows(sample_table) == [("a", "1")]


def test_snapshot_propagates_update_and_delete(sample_table):
    """这是 SNAPSHOT lane 存在的理由：游标模型做不到的两件事。"""
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO _snap_probe (k, v) VALUES ('a','1'), ('b','2')")
    snapshot.snapshot_table(sample_table)

    with db.get_pool().connection() as c:
        c.execute("UPDATE _snap_probe SET v='changed' WHERE k='a'")
        c.execute("DELETE FROM _snap_probe WHERE k='b'")
    snapshot.snapshot_table(sample_table)

    assert _tee_rows(sample_table) == [("a", "changed")]


def test_snapshot_releases_stale_secondary_unique_key_before_upsert(unique_key_table):
    with db.get_pool().connection() as c:
        c.execute(
            f"INSERT INTO {unique_key_table} (id, business_key, v) "
            "VALUES ('source-id', 'same-key', 'source')"
        )
    with mirror.get_tee_pool().connection() as c:
        c.execute(
            f"INSERT INTO {unique_key_table} (id, business_key, v) "
            "VALUES ('stale-id', 'same-key', 'target')"
        )

    rep = snapshot.snapshot_table(unique_key_table)

    assert rep["ok"] is True
    with mirror.get_tee_pool().connection() as c:
        assert c.execute(
            f"SELECT id, business_key, v FROM {unique_key_table}"
        ).fetchall() == [("source-id", "same-key", "source")]


def test_snapshot_parent_update_preserves_external_child(fk_tables):
    """Refreshing a retained parent must update in place, not TRUNCATE it.

    The old implementation fails before COPY because PostgreSQL refuses to
    truncate a table referenced by an external lane's foreign key.
    """
    parent, child = fk_tables
    with db.get_pool().connection() as c:
        c.execute(f"INSERT INTO {parent} (k, v) VALUES ('a', 'new')")
    with mirror.get_tee_pool().connection() as c:
        c.execute(f"INSERT INTO {parent} (k, v) VALUES ('a', 'old')")
        c.execute(
            f"INSERT INTO {child} (id, parent_k, v) VALUES ('c1', 'a', 'keep')"
        )

    rep = snapshot.snapshot_table(parent)

    assert rep["ok"] is True
    with mirror.get_tee_pool().connection() as c:
        assert c.execute(f"SELECT k, v FROM {parent}").fetchall() == [("a", "new")]
        assert c.execute(
            f"SELECT id, parent_k, v FROM {child}"
        ).fetchall() == [("c1", "a", "keep")]


def test_snapshot_parent_delete_uses_normal_fk_semantics(fk_tables):
    """Only a source-absent parent is deleted, so only its child cascades."""
    parent, child = fk_tables
    with db.get_pool().connection() as c:
        c.execute(f"INSERT INTO {parent} (k, v) VALUES ('keep', 'new')")
    with mirror.get_tee_pool().connection() as c:
        c.execute(
            f"INSERT INTO {parent} (k, v) VALUES ('keep', 'old'), ('stale', 'old')"
        )
        c.execute(
            f"INSERT INTO {child} (id, parent_k, v) VALUES "
            f"('c-keep', 'keep', 'retained'), ('c-stale', 'stale', 'removed')"
        )

    rep = snapshot.snapshot_table(parent)

    assert rep["ok"] is True
    with mirror.get_tee_pool().connection() as c:
        assert c.execute(
            f"SELECT k, v FROM {parent} ORDER BY k"
        ).fetchall() == [("keep", "new")]
        assert c.execute(
            f"SELECT id, parent_k, v FROM {child} ORDER BY id"
        ).fetchall() == [("c-keep", "keep", "retained")]


def test_failed_snapshot_leaves_old_data_intact(sample_table, monkeypatch):
    """目标 merge 必须在同一事务：中途失败时 TEE 侧保留旧的完整快照。

    失败必须真实发生在 dst 侧执行 `COPY ... FROM STDIN` 的过程中——此时
    临时表已在事务里创建。如果只在源库读取阶段（`_stream_rows`）注入异常，
    目标事务根本不会开始，测试就守不到目标侧的回滚边界；所以返回一段
    PostgreSQL 无法解析的损坏 COPY BINARY 载荷，让失败落在 dst 端。
    """
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO _snap_probe (k, v) VALUES ('a','1')")
    snapshot.snapshot_table(sample_table)
    assert _tee_rows(sample_table) == [("a", "1")]

    with db.get_pool().connection() as c:
        c.execute("UPDATE _snap_probe SET v='2' WHERE k='a'")

    def corrupted(*a, **kw):
        return b"not a valid postgresql binary copy payload at all"

    monkeypatch.setattr(snapshot, "_stream_rows", corrupted)
    rep = snapshot.snapshot_table(sample_table)
    assert rep["ok"] is False
    assert rep["error"]
    # 旧快照原样还在，证明 stage COPY 与后续目标 merge 共享一个回滚边界。
    assert _tee_rows(sample_table) == [("a", "1")]


def test_failure_after_prune_rolls_back_stale_delete(sample_table, monkeypatch):
    """A failure after stale-row prune must roll the completed delete back."""
    with db.get_pool().connection() as c:
        c.execute(
            "INSERT INTO _snap_probe (k, v) VALUES "
            "('a', 'old'), ('stale', 'old')"
        )
    snapshot.snapshot_table(sample_table)
    with db.get_pool().connection() as c:
        c.execute("UPDATE _snap_probe SET v='new' WHERE k='a'")
        c.execute("DELETE FROM _snap_probe WHERE k='stale'")

    real_prune = snapshot._prune_target

    def fail_after_prune(*args, **kwargs):
        real_prune(*args, **kwargs)
        raise RuntimeError("injected failure after prune")

    monkeypatch.setattr(snapshot, "_prune_target", fail_after_prune)
    rep = snapshot.snapshot_table(sample_table)

    assert rep["ok"] is False
    assert "injected failure after prune" in (rep["error"] or "")
    assert _tee_rows(sample_table) == [("a", "old"), ("stale", "old")]


def test_snapshot_refuses_when_table_exceeds_max_rows(sample_table, monkeypatch):
    """超限必须失败，不能静默截断——否则 TEE 会悄悄缺数据而无人察觉。
    早退分支不该碰 TEE 侧：旧快照必须原样保留，证明没有执行目标合并。
    """
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO _snap_probe (k, v) VALUES ('a','1')")
    snapshot.snapshot_table(sample_table)
    assert _tee_rows(sample_table) == [("a", "1")]

    with db.get_pool().connection() as c:
        c.execute("UPDATE _snap_probe SET v='2' WHERE k='a'")

    monkeypatch.setattr(snapshot, "MAX_ROWS", 0)
    rep = snapshot.snapshot_table(sample_table)
    assert rep["ok"] is False
    assert "MAX_ROWS" in (rep["error"] or "")
    # 早退分支没碰 TEE 侧：旧快照原样还在。
    assert _tee_rows(sample_table) == [("a", "1")]


def test_snapshot_refuses_table_without_primary_key_before_mutation():
    table = "_snap_no_pk"
    ddl = f"CREATE TABLE {table} (k TEXT, v TEXT NOT NULL)"
    for pool in (db.get_pool(), mirror.get_tee_pool()):
        with pool.connection() as c:
            c.execute(f"DROP TABLE IF EXISTS {table}")
            c.execute(ddl)
    try:
        with db.get_pool().connection() as c:
            c.execute(f"INSERT INTO {table} (k, v) VALUES ('source', 'new')")
        with mirror.get_tee_pool().connection() as c:
            c.execute(f"INSERT INTO {table} (k, v) VALUES ('target', 'keep')")

        rep = snapshot.snapshot_table(table)

        assert rep["ok"] is False
        assert "primary key" in (rep["error"] or "").lower()
        with mirror.get_tee_pool().connection() as c:
            assert c.execute(f"SELECT k, v FROM {table}").fetchall() == [
                ("target", "keep")
            ]
    finally:
        for pool in (db.get_pool(), mirror.get_tee_pool()):
            with pool.connection() as c:
                c.execute(f"DROP TABLE IF EXISTS {table}")


def test_snapshot_all_continues_past_a_failing_table(monkeypatch):
    """影子期铁律：单表失败不能中断其余表，也不能上抛污染主路径。"""
    from tee_shadow import table_registry as reg

    calls = []

    def fake(table):
        calls.append(table)
        if table == "provider_health":
            return {"table": table, "rows": 0, "ok": False, "error": "boom"}
        return {"table": table, "rows": 1, "ok": True, "error": None}

    monkeypatch.setattr(snapshot, "snapshot_table", fake)
    rep = snapshot.snapshot_all()
    assert rep["failures"] == 1
    assert len(calls) == len(reg.tables_in_lane(reg.SNAPSHOT))


@pytest.fixture
def drift_table_rds_extra():
    """RDS 侧比 TEE 侧多一列 extra_col。真实来源：滚动部署窗口（新列先落 RDS
    后落 TEE）+ 环境历史残留列（test RDS 的 model_api_routes.thinking_fallback
    全仓零命中）。"""
    with db.get_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS t_drift")
        c.execute("CREATE TABLE t_drift (k TEXT PRIMARY KEY, v TEXT NOT NULL, extra_col TEXT)")
    with mirror.get_tee_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS t_drift")
        c.execute("CREATE TABLE t_drift (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    yield "t_drift"
    with db.get_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS t_drift")
    with mirror.get_tee_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS t_drift")


@pytest.fixture
def drift_table_tee_extra():
    """TEE 侧比 RDS 侧多一列 tee_only_col。真实来源：alembic_tee 先于 RDS 迁移
    落地的窗口。"""
    with db.get_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS t_drift2")
        c.execute("CREATE TABLE t_drift2 (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    with mirror.get_tee_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS t_drift2")
        c.execute(
            "CREATE TABLE t_drift2 (k TEXT PRIMARY KEY, v TEXT NOT NULL, tee_only_col TEXT)")
    yield "t_drift2"
    with db.get_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS t_drift2")
    with mirror.get_tee_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS t_drift2")


def test_snapshot_survives_rds_only_column(drift_table_rds_extra):
    """RDS 多一列时，该表必须仍然同步（只是那列同步不过去并被报出来），
    而不是整张表永久失败。真实来源：滚动部署窗口 + 环境历史残留列
    （test RDS 的 model_api_routes.thinking_fallback 全仓零命中）。"""
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO t_drift (k, v, extra_col) VALUES ('a','1','x'), ('b','2','y')")
    rep = snapshot.snapshot_table(drift_table_rds_extra)
    assert rep["ok"] is True
    assert rep["rows"] == 2
    assert rep["missing_in_tee"] == ["extra_col"]
    with mirror.get_tee_pool().connection() as c:
        rows = c.execute("SELECT k, v FROM t_drift ORDER BY k").fetchall()
    assert rows == [("a", "1"), ("b", "2")]


def test_snapshot_survives_tee_only_column(drift_table_tee_extra):
    """TEE 多一列时同样要能同步（该列留默认值/NULL），并报出来。
    来源：alembic_tee 先于 RDS 迁移落地的窗口。"""
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO t_drift2 (k, v) VALUES ('a','1')")
    rep = snapshot.snapshot_table(drift_table_tee_extra)
    assert rep["ok"] is True
    assert rep["missing_in_rds"] == ["tee_only_col"]
    with mirror.get_tee_pool().connection() as c:
        rows = c.execute("SELECT k, v FROM t_drift2 ORDER BY k").fetchall()
    assert rows == [("a", "1")]

    # Retained rows keep target-only values while common columns converge.
    with db.get_pool().connection() as c:
        c.execute("UPDATE t_drift2 SET v='2' WHERE k='a'")
    with mirror.get_tee_pool().connection() as c:
        c.execute("UPDATE t_drift2 SET tee_only_col='tee-local' WHERE k='a'")
    rep = snapshot.snapshot_table(drift_table_tee_extra)
    assert rep["ok"] is True
    with mirror.get_tee_pool().connection() as c:
        rows = c.execute("SELECT k, v, tee_only_col FROM t_drift2").fetchall()
    assert rows == [("a", "2", "tee-local")]


@pytest.fixture
def drift_table_disjoint():
    """两侧列集完全不相交——整表被重建成了另一个东西（改名/重设计），或者
    某次迁移只落到了一侧。"""
    with db.get_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS t_drift3")
        c.execute("CREATE TABLE t_drift3 (rds_only TEXT PRIMARY KEY)")
    with mirror.get_tee_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS t_drift3")
        c.execute("CREATE TABLE t_drift3 (tee_only TEXT PRIMARY KEY)")
    yield "t_drift3"
    with db.get_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS t_drift3")
    with mirror.get_tee_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS t_drift3")


def test_snapshot_refuses_merge_when_no_common_columns(drift_table_disjoint):
    """列集完全不相交时必须拒绝执行，而不是清空后再写不回任何东西。

    没有这条护栏，交集逻辑会把"两侧完全对不上"降级成"共同列为空的正常快照"：
    TEE 那张表被清空、一行都写不回去，而报告还是 ok=True——比原先的整表失败
    更糟（原先只是不同步，现在是把已有的影子数据也弄没了）。TEE 侧存量必须
    原样还在。
    """
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO t_drift3 (rds_only) VALUES ('r')")
    with mirror.get_tee_pool().connection() as c:
        c.execute("INSERT INTO t_drift3 (tee_only) VALUES ('t')")

    rep = snapshot.snapshot_table(drift_table_disjoint)

    assert rep["ok"] is False
    assert "无公共列" in (rep["error"] or "")
    with mirror.get_tee_pool().connection() as c:
        rows = c.execute("SELECT tee_only FROM t_drift3").fetchall()
    assert rows == [("t",)], "TEE 侧存量被清空了——护栏没生效"


def test_users_is_snapshotted_before_per_user_tables():
    """FK 顺序：TEE 侧这些表带指向 users 的 CASCADE FK。users 归 MIRROR lane
    （由 reconcile 灌），所以 SNAPSHOT lane 自己不含 users——但顺序断言仍然要有，
    防止将来有人把 users 挪进 SNAPSHOT 后忘了排序。"""
    from tee_shadow import table_registry as reg

    order = snapshot.snapshot_order()
    snap = set(reg.tables_in_lane(reg.SNAPSHOT))
    assert set(order) == snap
    if "users" in snap:
        assert order.index("users") == 0


def test_snapshot_parents_are_ordered_before_known_children():
    order = snapshot.snapshot_order()
    agent_job_children = (
        "agent_action_queue",
        "v2_capture_batches",
        "v2_mcp_mutation_attempts",
        "v2_terminal_failure_outbox",
        "v2_trajectory_streams",
    )
    for child in agent_job_children:
        assert order.index("agent_jobs") < order.index(child)


def test_every_registered_snapshot_table_has_a_primary_key():
    """Exact replacement needs a stable key for upsert and source-absence prune."""
    from tee_shadow import table_registry as reg

    with mirror.get_tee_pool().connection() as c:
        missing = [
            table for table in reg.tables_in_lane(reg.SNAPSHOT)
            if not snapshot._primary_key_columns(c, table)
        ]
    assert missing == []
