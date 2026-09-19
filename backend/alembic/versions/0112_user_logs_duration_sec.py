"""user_logs.duration_sec + covering partial index for the admin app-usage read.

Revision ID: 0112_user_logs_duration_sec
Revises: 0111_memory_vectors

T653 (2026-09-19): ``/v1/admin/data-track/users`` timed out on prod (5.8 s of
SQL for ~1000 users, 2.6 s of it the fleet-wide ``app_usage`` aggregate). The
old query summed ``doc->'payload'->>'duration_sec'`` for every
``app_session_end`` row, and because the value lives inside ``doc`` PostgreSQL
had to fetch 85k heap tuples spread over 50k blocks even though a partial
index already found them (index-only scans cannot serve ``f(doc)`` — see the
PostgreSQL manual, "Index-Only Scans and Covering Indexes"). Putting ``doc``
itself into an INCLUDE index is not an option either: real
``app_session_end`` payloads can exceed the 8191-byte btree row limit and the
INSERT would fail.

So the duration gets its own nullable bigint column, filled by every
``user_logs`` writer (``db._user_log_duration_sec``) and backfilled here with
the exact rule the old SQL used (``^[0-9]{1,10}$`` → bigint, else NULL). The
partial covering index ``(user_id, ts) INCLUDE (duration_sec)`` then serves
the aggregate as an index-only scan. Semantics are unchanged: NULL rows
contribute 0 to ``foreground_sec`` and still count as sessions, exactly like
the old ``CASE … ELSE 0``.

ADD COLUMN (nullable, no default) is metadata-only, but its ACCESS EXCLUSIVE
lock is only released when the statement's transaction commits — so every
step here runs in its own autocommit statement rather than in the chain's
single migration transaction (otherwise the backfill UPDATE would hold that
lock and stall every user_logs reader and writer for its whole duration).
The backfill touches only the ~85k matching rows (row locks); the index is
built CONCURRENTLY; and the table is VACUUMed afterwards because the backfill
rewrote those rows' pages and cleared their all-visible bits — without the
VACUUM the new index-only scan would fetch all ~85k heap tuples until
autovacuum happened to run, i.e. the very cost this migration removes.
"""

from alembic import op


revision = "0112_user_logs_duration_sec"
down_revision = "0111_memory_vectors"
branch_labels = None
depends_on = None


INDEX_NAME = "ix_user_logs_app_session_end_usage"

# Keep the predicate byte-identical to the read SQL in db.admin_data_track_snapshot
# and to the sibling ix_user_logs_app_session_end_ts (0078).
CREATE_INDEX_SQL = (
    f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
    "ON user_logs (user_id, ts) INCLUDE (duration_sec) "
    "WHERE stream = 'tracking_events' "
    "AND (doc->>'type') = 'app_session_end'"
)

ADD_COLUMN_SQL = "ALTER TABLE user_logs ADD COLUMN IF NOT EXISTS duration_sec BIGINT"

# Rebuild the visibility map the backfill just dirtied, so the covering index
# is index-only from the first admin read. Re-run after any later BACKFILL_SQL.
VACUUM_SQL = "VACUUM (ANALYZE) user_logs"

# Idempotent: only rows that still have no value. Re-runnable after deploy
# to close the window in which old writers appended rows without the column.
BACKFILL_SQL = (
    "UPDATE user_logs "
    "SET duration_sec = (doc->'payload'->>'duration_sec')::bigint "
    "WHERE stream = 'tracking_events' "
    "AND (doc->>'type') = 'app_session_end' "
    "AND duration_sec IS NULL "
    "AND doc->'payload'->>'duration_sec' ~ '^[0-9]{1,10}$'"
)


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
    validity = _index_validity(INDEX_NAME)
    with op.get_context().autocommit_block():
        op.execute(ADD_COLUMN_SQL)
        op.execute(BACKFILL_SQL)
        # IF NOT EXISTS accepts an invalid shell left by a canceled
        # concurrent build, so remove it before retrying.
        if validity is False:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
        if validity is not True:
            op.execute(CREATE_INDEX_SQL)
        op.execute(VACUUM_SQL)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
    op.execute("ALTER TABLE user_logs DROP COLUMN IF EXISTS duration_sec")
