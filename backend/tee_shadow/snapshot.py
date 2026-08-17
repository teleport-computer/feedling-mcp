"""SNAPSHOT lane：明文小表的主键精确合并（RDS → TEE）。

为什么不做增量：这批表数据量极小（2026-07-27 prod 实测 25 张合计 1340 行 /
约 2 MB），但有大量 UPDATE/DELETE——队列 status 流转、心跳、allowlist、路由配置。
现有的 append-only 游标模型（tee_replicator.worker）处理可变行要靠
tee_pending_device_migration 的 requeue lane 补偿 + reconciler prune，每张表都得
配一套，成本线性于表数且永久。整表替换天然正确，且实现只有一个循环。

原子性：临时表 COPY + UPSERT + prune 必须在同一事务里。不能用 TRUNCATE：实际
schema 里 SNAPSHOT 表既会互相引用，也会被 CIPHERTEXT 表引用（例如
v2_trajectory_reviews → agent_jobs）。中途失败时 TEE 侧保留旧的完整快照。

影子期铁律：任何失败只 log + 计入报告，绝不上抛污染主路径。
"""
from __future__ import annotations

import io
import logging

from psycopg import sql

import db
from tee_shadow import mirror
from tee_shadow import table_registry as reg

log = logging.getLogger("feedling.tee_shadow")

# 单表行数上限。SNAPSHOT lane 的前提是"表很小"；一旦某张表长过这个数，全量刷
# 的代价就不再可忽略，应该把它改判到 CIPHERTEXT（游标增量）或 LOGICAL（逻辑
# 复制）lane。超限不静默截断——那会让 TEE 悄悄缺数据；直接失败并要求人来判。
MAX_ROWS = 200_000

# Keep this pure-data ordering explicit: snapshot_order() is also consumed by
# the admin dry-run path, which must not open a TEE connection merely to plan.
_PARENT_FIRST = ("users", "agent_jobs")


def snapshot_order() -> tuple[str, ...]:
    """SNAPSHOT lane 的执行顺序。

    父表必须先 UPSERT，子表的新行才能通过 FK；当前字典序会让 agent_jobs 位于
    v2_* 子表之前。users 归 MIRROR lane（由 reconcile 灌），本 lane 通常不含
    它；但若将来有人把 users 挪进来，它必须排第一。
    """
    tables = set(reg.tables_in_lane(reg.SNAPSHOT))
    parents = [table for table in _PARENT_FIRST if table in tables]
    return tuple(parents + sorted(tables - set(parents)))


def _row_count(conn, table: str) -> int:
    return conn.execute(
        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
    ).fetchone()[0]


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


def _primary_key_columns(conn, table: str) -> list[str]:
    """Return target primary-key columns in declared key order."""
    rows = conn.execute(
        "SELECT kcu.column_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON kcu.constraint_catalog = tc.constraint_catalog "
        " AND kcu.constraint_schema = tc.constraint_schema "
        " AND kcu.constraint_name = tc.constraint_name "
        "WHERE tc.table_schema='public' AND tc.table_name=%s "
        "  AND tc.constraint_type='PRIMARY KEY' "
        "ORDER BY kcu.ordinal_position",
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def _stream_rows(src_conn, table: str, cols: list[str]) -> bytes:
    """把整表（限定列集）读成 COPY 的二进制载荷。

    单独抽成函数是为了让测试能注入"读到一半炸掉"——事务原子性是这个模块最
    关键的性质，必须有测试守着。
    """
    buf = io.BytesIO()
    copy_stmt = sql.SQL("COPY {} ({}) TO STDOUT (FORMAT BINARY)").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, cols)),
    )
    with src_conn.cursor().copy(copy_stmt) as cp:
        for chunk in cp:
            buf.write(chunk)
    return buf.getvalue()


def _prune_target(dst, table_ident, stage_ident, pk_idents) -> None:
    """Delete target rows absent from the staged source snapshot."""
    key_match = sql.SQL(" AND ").join(
        sql.SQL("target.{} IS NOT DISTINCT FROM staged.{}").format(pk, pk)
        for pk in pk_idents
    )
    dst.execute(sql.SQL(
        "DELETE FROM {} AS target WHERE NOT EXISTS "
        "(SELECT 1 FROM {} AS staged WHERE {})"
    ).format(table_ident, stage_ident, key_match))


def _merge_payload(
    dst, table: str, cols: list[str], pk_cols: list[str], payload: bytes,
) -> None:
    """Make one target table exactly match the staged source rows.

    The temporary table deliberately contains only common columns. TEE-only
    columns therefore keep their values on retained rows and use target defaults
    for new rows, preserving the existing column-drift contract.
    """
    stage = "_tee_snapshot_stage"
    table_ident = sql.Identifier(table)
    stage_ident = sql.Identifier(stage)
    col_idents = list(map(sql.Identifier, cols))
    pk_idents = list(map(sql.Identifier, pk_cols))

    dst.execute(sql.SQL("CREATE TEMP TABLE {} ON COMMIT DROP AS "
                        "SELECT {} FROM {} WITH NO DATA").format(
        stage_ident, sql.SQL(", ").join(col_idents), table_ident))
    with dst.cursor().copy(sql.SQL("COPY {} ({}) FROM STDIN (FORMAT BINARY)").format(
            stage_ident, sql.SQL(", ").join(col_idents))) as cp:
        cp.write(payload)

    mutable_cols = [c for c in cols if c not in set(pk_cols)]
    if mutable_cols:
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
            for c in mutable_cols
        )
        changed = sql.SQL(" OR ").join(
            sql.SQL("target.{} IS DISTINCT FROM EXCLUDED.{}").format(
                sql.Identifier(c), sql.Identifier(c),
            )
            for c in mutable_cols
        )
        conflict_action = sql.SQL("DO UPDATE SET {} WHERE {}").format(
            assignments, changed,
        )
    else:
        conflict_action = sql.SQL("DO NOTHING")
    dst.execute(sql.SQL(
        "INSERT INTO {} AS target ({}) OVERRIDING SYSTEM VALUE "
        "SELECT {} FROM {} ON CONFLICT ({}) {}"
    ).format(
        table_ident,
        sql.SQL(", ").join(col_idents),
        sql.SQL(", ").join(col_idents),
        stage_ident,
        sql.SQL(", ").join(pk_idents),
        conflict_action,
    ))

    _prune_target(dst, table_ident, stage_ident, pk_idents)


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
                pk_cols = _primary_key_columns(tee_probe, table)

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
            if not pk_cols:
                rep["error"] = "target table has no primary key; refusing exact merge"
                log.warning("[tee-snapshot] %s: %s", table, rep["error"])
                return rep
            missing_pk = [c for c in pk_cols if c not in set(common)]
            if missing_pk:
                rep["error"] = f"primary key columns absent from common columns: {missing_pk}"
                log.warning("[tee-snapshot] %s: %s", table, rep["error"])
                return rep
            if rep["missing_in_tee"]:
                log.warning(
                    "[tee-snapshot] %s: TEE 落后于 RDS，以下列的数据没有同步: %s",
                    table, rep["missing_in_tee"])

            payload = _stream_rows(src, table, common)

        with mirror.get_tee_pool().connection() as dst:
            # 显式事务：stage COPY、UPSERT 与 prune 要么一起生效、要么一起回滚。
            with dst.transaction():
                _merge_payload(dst, table, common, pk_cols, payload)
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
