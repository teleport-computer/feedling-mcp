"""Erase per-user Runtime V2 metrics atomically with account deletion.

Revision ID: 0048_v2_turn_metrics_user_fk
Revises: 0047_model_route_context_window
"""

from alembic import op


revision = "0048_v2_turn_metrics_user_fk"
down_revision = "0047_model_route_context_window"
branch_labels = None
depends_on = None


_UP = """
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'fk_v2_turn_metrics_user'
      AND conrelid = 'v2_turn_metrics'::regclass
  ) THEN
    ALTER TABLE v2_turn_metrics
      ADD CONSTRAINT fk_v2_turn_metrics_user
      FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
      NOT VALID;
  END IF;
END
$$;

-- A NOT VALID FK starts enforcing new writes immediately. Install it before
-- cleaning historical rows so a concurrent terminal metric flush cannot create
-- a fresh orphan between cleanup and validation.
DELETE FROM v2_turn_metrics AS metric
WHERE metric.user_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM users WHERE users.user_id = metric.user_id
  );

ALTER TABLE v2_turn_metrics
  VALIDATE CONSTRAINT fk_v2_turn_metrics_user;
"""


_DOWN = """
ALTER TABLE v2_turn_metrics
  DROP CONSTRAINT IF EXISTS fk_v2_turn_metrics_user;
"""


_INDEX_UP = """
CREATE INDEX CONCURRENTLY ix_v2_turn_metrics_user_id
  ON v2_turn_metrics (user_id);
"""


_INDEX_DOWN = """
DROP INDEX CONCURRENTLY IF EXISTS ix_v2_turn_metrics_user_id;
"""


def _user_index_validity() -> bool | None:
    row = op.get_bind().exec_driver_sql(
        "SELECT idx.indisvalid FROM pg_class AS cls "
        "JOIN pg_index AS idx ON idx.indexrelid=cls.oid "
        "WHERE cls.relkind='i' "
        "AND cls.relname='ix_v2_turn_metrics_user_id' "
        "AND pg_table_is_visible(cls.oid)"
    ).fetchone()
    return None if row is None else bool(row[0])


def upgrade() -> None:
    # The table is append-heavy and can be large. Build the child-key index
    # without blocking metric writers, then install/validate the erase FK.
    index_validity = _user_index_validity()
    with op.get_context().autocommit_block():
        # A canceled CREATE INDEX CONCURRENTLY can leave an invalid same-name
        # shell. Drop it before retry; IF NOT EXISTS would silently accept it.
        if index_validity is False:
            op.execute(_INDEX_DOWN)
        if index_validity is not True:
            op.execute(_INDEX_UP)
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
    with op.get_context().autocommit_block():
        op.execute(_INDEX_DOWN)
