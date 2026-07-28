"""SNAPSHOT lane：整表 TRUNCATE + COPY 原子替换。

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


def _tee_rows(table: str) -> list[tuple]:
    with mirror.get_tee_pool().connection() as c:
        return c.execute(f"SELECT k, v FROM {table} ORDER BY k").fetchall()


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


def test_failed_snapshot_leaves_old_data_intact(sample_table, monkeypatch):
    """TRUNCATE + COPY 必须在同一事务：中途失败时 TEE 侧保留旧的完整快照，
    绝不出现空表窗口（读路径会读到空表 = 用户数据凭空消失）。

    失败必须真实发生在 dst 侧执行 `COPY ... FROM STDIN` 的过程中——此时
    `TRUNCATE` 已经在同一事务里跑过。如果只在源库读取阶段（`_stream_rows`）
    注入异常，异常永远不会传到 `mirror.get_tee_pool()` 那一侧，`TRUNCATE`
    和 `dst.transaction()` 根本没被执行到，这条测试就测不出"有没有把两条
    语句包在同一事务里"这件事——所以让 `_stream_rows` 正常返回，但返回一段
    postgres 无法解析的损坏 COPY BINARY 载荷，让失败落在 dst 端。
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
    # 旧快照原样还在，没被 TRUNCATE 掉——证明 TRUNCATE 与 COPY 在同一事务里：
    # COPY 因载荷损坏而失败时，先前跑过的 TRUNCATE 也随事务一起回滚了。
    assert _tee_rows(sample_table) == [("a", "1")]


def test_snapshot_refuses_when_table_exceeds_max_rows(sample_table, monkeypatch):
    """超限必须失败，不能静默截断——否则 TEE 会悄悄缺数据而无人察觉。
    早退分支不该碰 TEE 侧：旧快照必须原样保留，证明真的没有执行到 TRUNCATE/COPY。
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
