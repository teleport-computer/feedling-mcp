"""Index runtime-user delivery window and unfinished aggregations.

Revision ID: 0074_runtime_user_delivery_idx
Revises: 0073_merge_tail_anchor_deepseek
"""

from alembic import op


revision = "0074_runtime_user_delivery_idx"
down_revision = "0073_merge_tail_anchor_deepseek"
branch_labels = None
depends_on = None


# Keep the range key first for bounded history scans.  The unfinished indexes
# contain only current obligations and lead with user_id so GROUP BY user_id can
# stream the small partial index instead of walking either append-only table.
_INDEXES = {
    "ix_v2_effect_report_created_at": (
        "CREATE INDEX CONCURRENTLY ix_v2_effect_report_created_at "
        "ON v2_effect_outbox (created_at DESC, user_id) "
        "INCLUDE (status, effect_type)"
    ),
    "ix_v2_effect_report_unfinished": (
        "CREATE INDEX CONCURRENTLY ix_v2_effect_report_unfinished "
        "ON v2_effect_outbox (user_id, status, created_at) "
        "INCLUDE (effect_type) "
        "WHERE status IN "
        "('pending', 'pending_fenced_v1', 'needs_reconciliation')"
    ),
    "ix_v2_terminal_failure_report_created_at": (
        "CREATE INDEX CONCURRENTLY ix_v2_terminal_failure_report_created_at "
        "ON v2_terminal_failure_outbox (created_at DESC, user_id) "
        "INCLUDE (reply_delivered_at, status_delivered_at, "
        "runtime_error_delivered_at)"
    ),
    "ix_v2_terminal_failure_report_unfinished": (
        "CREATE INDEX CONCURRENTLY ix_v2_terminal_failure_report_unfinished "
        "ON v2_terminal_failure_outbox (user_id, created_at) "
        "INCLUDE (reply_delivered_at, status_delivered_at, "
        "runtime_error_delivered_at) "
        "WHERE reply_delivered_at IS NULL "
        "OR status_delivered_at IS NULL "
        "OR runtime_error_delivered_at IS NULL"
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
