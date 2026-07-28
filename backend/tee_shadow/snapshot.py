"""SNAPSHOT lane：明文小表的整表原子替换（RDS → TEE）。

为什么不做增量：这批表数据量极小（2026-07-27 prod 实测 25 张合计 1340 行 /
约 2 MB），但有大量 UPDATE/DELETE——队列 status 流转、心跳、allowlist、路由配置。
现有的 append-only 游标模型（tee_replicator.worker）处理可变行要靠
tee_pending_device_migration 的 requeue lane 补偿 + reconciler prune，每张表都得
配一套，成本线性于表数且永久。整表替换天然正确，且实现只有一个循环。

原子性：TRUNCATE + COPY 必须在同一事务里。中途失败时 TEE 侧保留旧的完整快照，
绝不出现"表已清空、新数据还没写完"的窗口——那个窗口里的读路径会看到用户数据
凭空消失。

影子期铁律：任何失败只 log + 计入报告，绝不上抛污染主路径。
"""
from __future__ import annotations

import io
import logging

import db
from tee_shadow import mirror
from tee_shadow import table_registry as reg

log = logging.getLogger("feedling.tee_shadow")

# 单表行数上限。SNAPSHOT lane 的前提是"表很小"；一旦某张表长过这个数，全量刷
# 的代价就不再可忽略，应该把它改判到 CIPHERTEXT（游标增量）或 LOGICAL（逻辑
# 复制）lane。超限不静默截断——那会让 TEE 悄悄缺数据；直接失败并要求人来判。
MAX_ROWS = 200_000


def snapshot_order() -> tuple[str, ...]:
    """SNAPSHOT lane 的执行顺序。

    TEE 侧这些表带指向 users 的 ON DELETE CASCADE FK，父表必须先在。users 归
    MIRROR lane（由 reconcile 灌），本 lane 通常不含它；但若将来有人把 users
    挪进来，它必须排第一。其余表之间没有互相的 FK，字典序即可（可重现）。
    """
    tables = sorted(reg.tables_in_lane(reg.SNAPSHOT))
    if "users" in tables:
        tables.remove("users")
        tables.insert(0, "users")
    return tuple(tables)


def _row_count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _columns(conn, table: str) -> list[str]:
    """查一张表在给定连接所在库里的实际列集（按 ordinal_position 排序）。

    用于 COPY 前对齐两侧列集——严格位置匹配（裸表名 COPY）只要两侧列集有
    一列之差就会整表永久失败，见 snapshot_table 里交集逻辑的注释。
    """
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
        (table,)).fetchall()
    return [r[0] for r in rows]


def _stream_rows(src_conn, table: str, cols: list[str]) -> bytes:
    """把整表（限定列集）读成 COPY 的二进制载荷。

    单独抽成函数是为了让测试能注入"读到一半炸掉"——事务原子性是这个模块最
    关键的性质，必须有测试守着。
    """
    col_list = ", ".join(cols)
    buf = io.BytesIO()
    with src_conn.cursor().copy(f"COPY {table} ({col_list}) TO STDOUT (FORMAT BINARY)") as cp:
        for chunk in cp:
            buf.write(chunk)
    return buf.getvalue()


def snapshot_table(table: str) -> dict:
    """整表替换一张 SNAPSHOT lane 的表。绝不抛——失败信息落在返回值里。"""
    rep = {
        "table": table, "rows": 0, "ok": False, "error": None,
        "missing_in_tee": [], "missing_in_rds": [],
    }
    try:
        with db.get_pool().connection() as src:
            n = _row_count(src, table)
            if n > MAX_ROWS:
                rep["error"] = (
                    f"row count {n} > MAX_ROWS {MAX_ROWS} — 这张表已经不适合整表"
                    f"快照，改判到 CIPHERTEXT 或 LOGICAL lane")
                log.warning("[tee-snapshot] %s: %s", table, rep["error"])
                return rep
            rds_cols = _columns(src, table)
            with mirror.get_tee_pool().connection() as tee_probe:
                tee_cols = _columns(tee_probe, table)

            # 为什么用交集而不是裸表名：COPY 的 BINARY 格式按列位置严格匹配，
            # 两侧列集差一列就整表失败——而 RDS 侧加列是常态（滚动部署窗口、
            # 环境历史残留列）。严格失败看起来"更安全"，实际后果是那张表永久
            # 不同步，而影子库的价值恰恰在于持续跟进。交集 + 把差异逐列报进
            # missing_in_tee / missing_in_rds，既保住同步、又让漂移可见
            # （scheduler 会把它落进 tee_sync_runs.report）。
            common = [c for c in rds_cols if c in set(tee_cols)]
            rep["missing_in_tee"] = [c for c in rds_cols if c not in set(tee_cols)]
            rep["missing_in_rds"] = [c for c in tee_cols if c not in set(rds_cols)]
            if not common:
                rep["error"] = "两侧无公共列，拒绝整表清空"
                log.warning("[tee-snapshot] %s: %s", table, rep["error"])
                return rep
            if rep["missing_in_tee"]:
                log.warning(
                    "[tee-snapshot] %s: TEE 落后于 RDS，以下列的数据没有同步: %s",
                    table, rep["missing_in_tee"])

            payload = _stream_rows(src, table, common)

        col_list = ", ".join(common)
        with mirror.get_tee_pool().connection() as dst:
            # 显式事务：TRUNCATE 与 COPY 要么一起生效、要么一起回滚。
            with dst.transaction():
                dst.execute(f"TRUNCATE {table}")
                with dst.cursor().copy(
                        f"COPY {table} ({col_list}) FROM STDIN (FORMAT BINARY)") as cp:
                    cp.write(payload)
        rep["rows"] = n
        rep["ok"] = True
    except Exception as exc:  # noqa: BLE001 — 影子期吞掉一切
        rep["error"] = str(exc)[:300]
        log.warning("[tee-snapshot] %s 失败: %s", table, exc)
    return rep


def snapshot_all() -> dict:
    """刷完整条 SNAPSHOT lane。单表失败不中断其余表。"""
    tables = []
    copied = 0
    failures = 0
    for table in snapshot_order():
        rep = snapshot_table(table)
        tables.append(rep)
        if rep["ok"]:
            copied += rep["rows"]
        else:
            failures += 1
    log.info("[tee-snapshot] done: tables=%d copied=%d failures=%d",
             len(tables), copied, failures)
    return {"tables": tables, "copied": copied, "failures": failures}
