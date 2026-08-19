"""CIPHERTEXT lane prune 的行为测试。

重点守两条不可逆的性质：
  1. 取数顺序必须是「先 TEE 后 RDS」——反了会在复制窗口里误删刚搬进 TEE 的新行，
     而 append-only 游标已越过它，永不恢复。
  2. 安全阈值必须真的拦住大规模删除，且拦住时一行都不删。
"""
from __future__ import annotations

import pytest

from tee_replicator import worker as tee_worker
from tee_shadow import ciphertext_prune


# --------------------------------------------------------------------------- #
# 配置层守卫
# --------------------------------------------------------------------------- #
def test_prune_keys_sql_arity_matches_delete_sql():
    """两条 keys SQL 选出的列数必须与 DELETE 的占位符数一致。

    prune 把差集里的元组**原样**喂给独立的 prune DELETE SQL。列数对不上时
    psycopg 会在真环境里抛参数个数错误，而本仓库的 TEE 侧失败是被吞掉的
    （影子期铁律）——只会计入失败计数，不会有人立刻发现。所以在这里静态卡死。
    """
    for table in ciphertext_prune.prunable_tables():
        cfg = tee_worker._TABLES[table]
        n_delete = ciphertext_prune._PRUNE_DELETE_SQL[table].count("%s")
        for label, sql in (("rds", cfg.prune_rds_keys_sql),
                           ("tee", cfg.prune_tee_keys_sql)):
            n_select = len([c for c in sql.split("FROM")[0]
                            .replace("SELECT", "").split(",") if c.strip()])
            assert n_select == n_delete, (
                f"{table} 的 {label} keys SQL 选了 {n_select} 列，"
                f"但 DELETE 有 {n_delete} 个占位符——差集元组喂不进去")


def test_identity_prune_is_scoped_to_its_kind():
    """identity 的两条 keys SQL 必须带 kind='identity' 辖区限定。

    user_blobs 整表归 MIRROR lane，reconciler 只是把 kind='identity' 让给了
    replicator。不带 WHERE 的话，prune 会把其它 kind 的行（归 reconciler 管）
    当成本 lane 的孤儿删掉。
    """
    cfg = tee_worker._TABLES["identity"]
    assert "kind = 'identity'" in cfg.prune_rds_keys_sql
    assert "kind = 'identity'" in cfg.prune_tee_keys_sql


def test_frame_envelopes_reads_rds_and_tee_from_different_tables():
    """frame_envelopes 在 TEE 侧叫 frames——两条 SQL 不能都指向同一张表。"""
    cfg = tee_worker._TABLES["frame_envelopes"]
    assert "FROM frame_envelopes" in cfg.prune_rds_keys_sql
    assert "FROM frames" in cfg.prune_tee_keys_sql


def test_only_tables_without_a_safe_delete_contract_are_reported_uncovered():
    """没有删除语义的纯 append-only 表不参与 prune，但必须出现在 uncovered 里。

    prune 存在的理由就是"静默的覆盖缺口"，它自己不能再制造一个。
    """
    covered = set(ciphertext_prune.prunable_tables())
    uncovered = set(ciphertext_prune.unprunable_tables())
    assert "v2_trajectory_events" in covered
    for t in ("chat_message_archive", "v2_conversation_summary_segments"):
        assert t not in covered, f"{t} 没有删除语义，不该参与 prune"
        assert t in uncovered, f"{t} 未覆盖但没进报告——静默缺口"
    assert not (covered & uncovered)
    assert covered | uncovered == set(tee_worker._TABLES)


# --------------------------------------------------------------------------- #
# 行为测试（打桩两侧连接，不需要真库）
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, params):
        self._sink.extend(params)


class _FakeConn:
    """把 execute 的返回按 SQL 前缀路由；记录 DELETE 的参数。"""

    def __init__(self, rows, deleted, *, order_log=None, label=""):
        self._rows = rows
        self.deleted = deleted
        self._order_log = order_log
        self._label = label

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self._order_log is not None:
            self._order_log.append(self._label)
        return list(self._rows)

    def cursor(self):
        return _FakeCursor(self.deleted)

    def transaction(self):
        return self

    def connection(self):
        return self


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


def _wire(monkeypatch, *, rds_rows, tee_rows, order_log=None):
    deleted: list = []
    src = _FakeConn(rds_rows, deleted, order_log=order_log, label="rds")
    dst = _FakeConn(tee_rows, deleted, order_log=order_log, label="tee")
    monkeypatch.setattr(ciphertext_prune.db, "get_pool", lambda: _FakePool(src))
    monkeypatch.setattr(ciphertext_prune.mirror, "get_tee_pool", lambda: _FakePool(dst))
    return deleted


def test_reads_tee_before_rds(monkeypatch):
    """顺序铁律：TEE 快照必须早于 RDS 快照。

    反序会制造一个真实的误删窗口——某行在 RDS 快照之后写入、又在 TEE 快照之前
    被 replicator 搬进 TEE，就会被判成残留删掉，而游标已越过它、永不恢复。
    这条断言就是那条铁律的守卫，改动取数顺序时它必须变红。
    """
    order: list[str] = []
    _wire(monkeypatch, rds_rows=[("u1", "m1")], tee_rows=[("u1", "m1")],
          order_log=order)
    ciphertext_prune.prune_table("chat_messages")
    assert order == ["tee", "rds"], f"取数顺序错了：{order}"


def test_deletes_only_rows_missing_from_rds(monkeypatch):
    deleted = _wire(
        monkeypatch,
        rds_rows=[("u1", "keep")],
        tee_rows=[("u1", "keep"), ("u1", "stale"), ("u2", "stale2")],
    )
    rep = ciphertext_prune.prune_table("chat_messages")
    assert rep["stale"] == 2
    assert rep["deleted"] == 2
    assert set(deleted) == {("u1", "stale"), ("u2", "stale2")}
    assert ("u1", "keep") not in deleted


def test_rows_missing_from_tee_are_never_touched(monkeypatch):
    """RDS 有、TEE 没有 = 游标滞后或终态 pending，prune 绝不能管这个方向。"""
    deleted = _wire(
        monkeypatch,
        rds_rows=[("u1", "a"), ("u1", "b"), ("u1", "c")],
        tee_rows=[("u1", "a")],
    )
    rep = ciphertext_prune.prune_table("chat_messages")
    assert rep["stale"] == 0
    assert deleted == []


def test_refuses_and_deletes_nothing_when_over_guard(monkeypatch):
    """超过安全阈值时整表放弃，且**一行都不能删**。"""
    tee = [("u1", f"m{i}") for i in range(5000)]
    deleted = _wire(monkeypatch, rds_rows=[], tee_rows=tee)
    rep = ciphertext_prune.prune_table("chat_messages")
    assert rep["stale"] == 5000
    assert rep["deleted"] == 0
    assert rep["refused"], "超阈值却没有拒绝"
    assert deleted == [], "拒绝时仍然删了行——安全阈值没生效"


def test_small_table_uses_absolute_floor_not_fraction(monkeypatch):
    """小表按比例算出的阈值过小，必须由绝对下限兜住，否则正常的账号删除会被拒。"""
    deleted = _wire(
        monkeypatch,
        rds_rows=[],
        tee_rows=[("u1",), ("u2",), ("u3",)],
    )
    rep = ciphertext_prune.prune_table("v2_conversation_summary")
    assert rep["refused"] is None, "小表的正常删除被安全阈值误拦"
    assert rep["deleted"] == 3
    assert set(deleted) == {("u1",), ("u2",), ("u3",)}


def test_dry_run_reports_without_deleting(monkeypatch):
    deleted = _wire(
        monkeypatch,
        rds_rows=[],
        tee_rows=[("u1", "a"), ("u1", "b")],
    )
    rep = ciphertext_prune.prune_table("chat_messages", dry_run=True)
    assert rep["stale"] == 2
    assert rep["deleted"] == 0
    assert deleted == []


def test_read_failure_is_swallowed_into_report(monkeypatch):
    """影子期铁律：TEE 侧任何失败只落报告，绝不上抛污染主路径。"""

    class _Boom:
        def connection(self):
            raise RuntimeError("connection is lost")

    monkeypatch.setattr(ciphertext_prune.mirror, "get_tee_pool", lambda: _Boom())
    rep = ciphertext_prune.prune_table("chat_messages")
    assert rep["error"]
    assert rep["deleted"] == 0


def test_retries_on_dropped_connection(monkeypatch):
    """连接被网关掐断 → 换新连接重试，整表不能因此失败。

    2026-07-29 prod dry-run 实测：chat_messages 拉 16 万主键期间，池里其它连接
    被 Phala 网关静默掐断，导致**第一张表之后的 8 张表连锁 SSL EOF 全灭**。
    没有这条重试，prune 在真环境里一次只能处理一张表。
    """
    import psycopg

    monkeypatch.setattr(ciphertext_prune.time, "sleep", lambda *_: None)
    calls = {"n": 0}
    deleted: list = []
    good = _FakeConn([("u1", "stale")], deleted)

    class _FlakyPool:
        def connection(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise psycopg.OperationalError("SSL SYSCALL error: EOF detected")
            return good

    monkeypatch.setattr(ciphertext_prune.mirror, "get_tee_pool", lambda: _FlakyPool())
    monkeypatch.setattr(ciphertext_prune.db, "get_pool",
                        lambda: _FakePool(_FakeConn([], deleted)))
    rep = ciphertext_prune.prune_table("chat_messages")
    assert rep["error"] is None, f"重试没生效: {rep['error']}"
    assert rep["stale"] == 1
    assert calls["n"] >= 2, "没有重试"


def test_gives_up_after_bounded_retries(monkeypatch):
    """重试有界——连不上就落报告，不能无限重试卡死整个 tick。"""
    import psycopg

    monkeypatch.setattr(ciphertext_prune.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class _DeadPool:
        def connection(self):
            calls["n"] += 1
            raise psycopg.OperationalError("connection is lost")

    monkeypatch.setattr(ciphertext_prune.mirror, "get_tee_pool", lambda: _DeadPool())
    rep = ciphertext_prune.prune_table("chat_messages")
    assert rep["error"]
    assert rep["deleted"] == 0
    assert calls["n"] == ciphertext_prune._conn_retries()


def test_one_table_failing_does_not_abort_the_rest(monkeypatch):
    """单表连接失败不能带走其余表——正是 prod dry-run 里发生的连锁全灭。"""
    monkeypatch.setattr(ciphertext_prune, "prunable_tables",
                        lambda: ("chat_messages", "world_book_entries"))
    seen: list[str] = []
    real = ciphertext_prune.prune_table

    def fake(table, **kw):
        seen.append(table)
        if table == "chat_messages":
            return {"table": table, "rds_rows": 0, "tee_rows": 0, "stale": 0,
                    "deleted": 0, "error": "SSL SYSCALL error", "refused": None}
        return real(table, **kw)

    _wire(monkeypatch, rds_rows=[], tee_rows=[("u1", "a")])
    monkeypatch.setattr(ciphertext_prune, "prune_table", fake)
    rep = ciphertext_prune.prune_all()
    assert seen == ["chat_messages", "world_book_entries"], "失败表带走了后续表"
    assert rep["errors"] == 1
    assert rep["deleted"] == 1


def test_unknown_and_unconfigured_tables_report_instead_of_raising(monkeypatch):
    assert ciphertext_prune.prune_table("no_such_table")["error"]
    assert ciphertext_prune.prune_table("chat_message_archive")["error"]


def test_prune_all_aggregates_and_lists_uncovered(monkeypatch):
    monkeypatch.setattr(ciphertext_prune, "prunable_tables", lambda: ("chat_messages",))
    _wire(monkeypatch, rds_rows=[], tee_rows=[("u1", "a")])
    rep = ciphertext_prune.prune_all()
    assert rep["stale"] == 1
    assert rep["deleted"] == 1
    assert rep["errors"] == 0
    assert rep["refused"] == []
    assert "chat_message_archive" in rep["uncovered"]


# --------------------------------------------------------------------------- #
# 调度器接线
# --------------------------------------------------------------------------- #
def test_scheduler_records_prune_columns():
    """三个扁平列必须同时出现在 summary 骨架、日志键和 db 的写列清单里。

    少任何一处，指标就只活在 report JSONB 里——而"只活在 JSONB 里就没人看"
    正是 07-29 那次 4 列数据静默失同步的直接原因。
    """
    import db
    from admin import tee_sync_scheduler as sched

    blank = sched._blank_summary(True)
    for col in ("prune_stale", "prune_deleted", "prune_refused"):
        assert col in blank, f"{col} 不在 _blank_summary"
        assert col in sched._LOG_KEYS, f"{col} 不在 _LOG_KEYS"
        assert col in db._TEE_SYNC_RUN_COLS, f"{col} 不在 _TEE_SYNC_RUN_COLS"


@pytest.mark.parametrize("col", ["prune_stale", "prune_deleted", "prune_refused"])
def test_prune_columns_exist_in_migration(col):
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / (
        "backend/alembic/versions/0070_tee_sync_prune_metrics.py")
    assert col in src.read_text(), f"{col} 没有对应的 DDL"
