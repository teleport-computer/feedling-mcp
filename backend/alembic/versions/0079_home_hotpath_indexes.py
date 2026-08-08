"""chat_messages(ts) + user_logs proactive 部分索引 —— 首页/产品健康 prod 热路径。

Revision ID: 0079_home_hotpath_indexes
Revises: 0078_admin_dashboard_indexes

两个索引都是 0078 落地时"等 prod EXPLAIN 证据再说"的存量 follow-up，
证据在 IA v2 上 prod 后到齐（2026-08-06 实测）：

- ``ix_chat_messages_ts``：admin_home_queue（72h 有去无回）、admin_home_feed
  （48h 首次真回复）、admin_product_health_proactive_reply_rate（10 周右删失）
  此前全部顺扫 chat_messages——首页冷构 ~8s 的主因之一。chat_messages 既有
  索引都以 user_id 打头，给不出 ts 范围收窄。
- ``ix_user_logs_proactive_jobs_ts``：admin_product_health_power_users 的 V1
  分支按 stream='proactive_jobs' 扫 16 周，user_logs 索引同样全部 user_id
  打头——产品健康页在 prod 顶到 30s builder deadline 的主因。附带加速
  view=proactive 的既有日报查询。
"""

from alembic import op


revision = "0079_home_hotpath_indexes"
down_revision = "0078_admin_dashboard_indexes"
branch_labels = None
depends_on = None


_INDEXES = {
    "ix_chat_messages_ts": (
        "CREATE INDEX CONCURRENTLY ix_chat_messages_ts ON chat_messages (ts)"
    ),
    "ix_user_logs_proactive_jobs_ts": (
        "CREATE INDEX CONCURRENTLY ix_user_logs_proactive_jobs_ts "
        "ON user_logs (ts) WHERE stream='proactive_jobs'"
    ),
}


def _index_validity(name: str) -> bool | None:
    """Return None for absent, False for an interrupted invalid index."""
    row = op.get_bind().exec_driver_sql(
        "SELECT idx.indisvalid FROM pg_class AS cls "
        "JOIN pg_index AS idx ON idx.indexrelid=cls.oid "
        f"WHERE cls.relkind='i' AND cls.relname='{name}' "
        "AND pg_table_is_visible(cls.oid)"
    ).fetchone()
    return None if row is None else bool(row[0])


def upgrade() -> None:
    validity = {name: _index_validity(name) for name in _INDEXES}
    with op.get_context().autocommit_block():
        for name, create_sql in _INDEXES.items():
            # IF NOT EXISTS accepts an invalid shell left by a canceled
            # concurrent build, so remove it before retrying.
            if validity[name] is False:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            if validity[name] is not True:
                op.execute(create_sql)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
