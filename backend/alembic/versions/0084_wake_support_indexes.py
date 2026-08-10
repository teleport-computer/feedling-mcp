"""Index per-user Runtime V2 wake activity for the admin support view.

Revision ID: 0084_wake_support_indexes
Revises: 0083_screen_chat_frames
"""

from alembic import op


revision = "0084_wake_support_indexes"
down_revision = "0083_screen_chat_frames"
branch_labels = None
depends_on = None


# Migration predicates must remain self-contained historical facts. A contract
# test compares this tuple with worker._WAKE_LANES so a new runtime wake lane
# cannot silently fall back to the broad time-window indexes.
_WAKE_LANES = ("heartbeat", "scheduled", "manual_wake", "screen_watch")
_WAKE_LANES_SQL = ",".join(f"'{lane}'" for lane in _WAKE_LANES)


# agent_jobs is append-only. Without these user-first paths, a 72-hour support
# lookup scans every job in the global time window and filters almost all of
# them out. A 300k-row/60-day plan removed 15,247 and 13,726 rows respectively;
# these partial indexes reduced both plans to three buffers.
_INDEXES = {
    "ix_agent_jobs_user_wake_created": (
        "CREATE INDEX CONCURRENTLY ix_agent_jobs_user_wake_created "
        "ON agent_jobs (user_id, created_at DESC) "
        f"WHERE lane IN ({_WAKE_LANES_SQL})"
    ),
    "ix_agent_jobs_user_wake_failure": (
        "CREATE INDEX CONCURRENTLY ix_agent_jobs_user_wake_failure "
        "ON agent_jobs (user_id, finished_at DESC, id DESC) "
        f"WHERE lane IN ({_WAKE_LANES_SQL}) "
        "AND status IN ('failed','expired')"
    ),
}


def _index_validity(name: str) -> bool | None:
    """Return None for missing indexes and False for invalid index shells."""
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
            # Interrupted CONCURRENTLY builds leave an invalid same-name shell;
            # IF NOT EXISTS would otherwise mistake that shell for a usable index.
            if validity[name] is False:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            if validity[name] is not True:
                op.execute(create_sql)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
