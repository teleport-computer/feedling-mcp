"""Partial index for the ops-overview app-session window scan.

Revision ID: 0078_admin_dashboard_indexes
Revises: 0077_perception_signal_state_v2

Serves the ``sessions`` CTE in ``db.recent_admin_product_kpis`` (admin
运营总览): a pure ``ts`` range scan over ``user_logs`` restricted to
``stream='tracking_events' AND doc->>'type'='app_session_end'``.  Every
existing ``user_logs`` index leads with ``user_id``, so that CTE had to
scan the whole table each dashboard load; this partial index holds exactly
the matching rows keyed by ``ts``.
"""

from alembic import op


revision = "0078_admin_dashboard_indexes"
down_revision = "0077_perception_signal_state_v2"
branch_labels = None
depends_on = None


_INDEXES = {
    "ix_user_logs_app_session_end_ts": (
        "CREATE INDEX CONCURRENTLY ix_user_logs_app_session_end_ts "
        "ON user_logs (ts) "
        "WHERE stream='tracking_events' "
        "AND doc->>'type'='app_session_end'"
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
