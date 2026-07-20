"""Bound encrypted Runtime V2 trajectory retention.

Revision ID: 0049_v2_trajectory_retention
Revises: 0048_v2_turn_metrics_user_fk
"""

from alembic import op


revision = "0049_v2_trajectory_retention"
down_revision = "0048_v2_turn_metrics_user_fk"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE agent_jobs
  ADD COLUMN IF NOT EXISTS trajectory_purged_at TIMESTAMPTZ;
"""

_INDEX_UP = """
-- The reaper advances this content-lifecycle tombstone before deleting any
-- ciphertext.  Appenders check the same column, so a delayed recorder cannot
-- recreate a stream after its retention boundary has passed.
CREATE INDEX CONCURRENTLY ix_agent_jobs_trajectory_retention
  ON agent_jobs (finished_at, id)
  WHERE status IN ('completed','failed','expired','superseded')
    AND trajectory_purged_at IS NULL;
"""


_DOWN = """
ALTER TABLE agent_jobs DROP COLUMN IF EXISTS trajectory_purged_at;
"""


_INDEX_DOWN = """
DROP INDEX CONCURRENTLY IF EXISTS ix_agent_jobs_trajectory_retention;
"""


def _index_validity() -> bool | None:
    row = op.get_bind().exec_driver_sql(
        "SELECT idx.indisvalid FROM pg_class AS cls "
        "JOIN pg_index AS idx ON idx.indexrelid=cls.oid "
        "WHERE cls.relkind='i' "
        "AND cls.relname='ix_agent_jobs_trajectory_retention' "
        "AND pg_table_is_visible(cls.oid)"
    ).fetchone()
    return None if row is None else bool(row[0])


def upgrade() -> None:
    op.execute(_UP)
    index_validity = _index_validity()
    with op.get_context().autocommit_block():
        if index_validity is False:
            op.execute(_INDEX_DOWN)
        if index_validity is not True:
            op.execute(_INDEX_UP)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(_INDEX_DOWN)
    op.execute(_DOWN)
