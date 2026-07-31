"""Index the admin runtime-health window scans (full-window, all lanes).

Revision ID: 0071_runtime_health_idx
Revises: 0070_tee_sync_prune
"""

from alembic import op


# id 必须 ≤32 字符（alembic version 表列宽），且必须是上一条的 revision 值本身
# ——不是它的文件名（0070 的文件叫 ..._prune_metrics.py，revision 是 ..._prune）。
revision = "0071_runtime_health_idx"
down_revision = "0070_tee_sync_prune"
branch_labels = None
depends_on = None


# recent_runtime_health 去掉了每条子查询的 LIMIT 1000（采样上界让 168h/720h 档
# 静默少报），扫描量因此从"最近 1000 行"变成"窗口内全量"。这三条索引是那次改动
# 的承重墙。
#
# 为什么不能复用已有索引：
#   * ix_v2_turn_metrics_lane_created_at 是 (lane, created_at DESC)。本页的查询
#     只有 created_at 范围谓词 + GROUP BY lane，没有 lane 等值谓词；PG 16 没有
#     B-tree skip scan，前导列缺等值谓词时拿不到范围收窄。本机 PG 16 实测
#     （30 万行 / 摊 60 天 / 5 个 lane，ANALYZE 后）：
#       24h 档  无本迁移 → Seq Scan，shared 4226 buffer
#               有本迁移 → Index Scan using ix_v2_turn_metrics_created_at，1454 buffer
#       720h 档 规划器会去用那条复合索引，但退化成**全索引扫描**（21k+ buffer）
#               ——它任何时候都没给出范围收窄，只是宽窗口下顺带满足了排序。
#     所以准确的说法不是"复合索引用不上"，而是"它对非前导列的范围谓词只能全扫"。
#   * ix_agent_jobs_chat_terminal_finished 带 WHERE lane='chat'，本页是全 lane。
#   * ix_agent_jobs_hb_history 带 WHERE lane='heartbeat'，同理。
#     agent_jobs 侧 24h 实测：Index Scan using ix_agent_jobs_terminal_finished_at，
#     1663 buffer / 21ms。
#
# 两条 agent_jobs 索引做成 partial / 全表的分工：终态那条能用 status 谓词收窄，
# capture 那条的查询要数在飞回合（open），不能按 status 过滤。
_INDEXES = {
    "ix_agent_jobs_terminal_finished_at": (
        "CREATE INDEX CONCURRENTLY ix_agent_jobs_terminal_finished_at "
        "ON agent_jobs (finished_at DESC) "
        "WHERE status IN ('completed','failed','expired','superseded')"
    ),
    "ix_agent_jobs_created_at": (
        "CREATE INDEX CONCURRENTLY ix_agent_jobs_created_at "
        "ON agent_jobs (created_at DESC)"
    ),
    "ix_v2_turn_metrics_created_at": (
        "CREATE INDEX CONCURRENTLY ix_v2_turn_metrics_created_at "
        "ON v2_turn_metrics (created_at DESC)"
    ),
}


def _index_validity(name: str) -> bool | None:
    """None = 不存在；False = 存在但 invalid（上次 CONCURRENTLY 被中断的空壳）。"""
    row = op.get_bind().exec_driver_sql(
        "SELECT idx.indisvalid FROM pg_class AS cls "
        "JOIN pg_index AS idx ON idx.indexrelid=cls.oid "
        f"WHERE cls.relkind='i' AND cls.relname='{name}' "
        "AND pg_table_is_visible(cls.oid)"
    ).fetchone()
    return None if row is None else bool(row[0])


def upgrade() -> None:
    # 两张表都是 append-heavy 的热表：并发建索引，不阻塞 job 写入与 metric flush。
    # 先在事务内探测有效性，再进 autocommit 块——CREATE INDEX CONCURRENTLY 不能
    # 跑在事务里。
    validity = {name: _index_validity(name) for name in _INDEXES}
    with op.get_context().autocommit_block():
        for name, create_sql in _INDEXES.items():
            # 被取消的 CONCURRENTLY 会留下同名 invalid 空壳，IF NOT EXISTS 会把它
            # 当成"已建好"静默放过（照 0048 的处置）。
            if validity[name] is False:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            if validity[name] is not True:
                op.execute(create_sql)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
