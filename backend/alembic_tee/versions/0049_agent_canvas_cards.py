"""Index agent Canvas cards without indexing or copying message bodies.

T674: both Canvas readers use the exact partial predicate below. The index
keeps the scan on matching cards; no doc INCLUDE (large envelopes exceed the
btree tuple size limit). No data backfill or V2 writer change is necessary.
"""
from alembic import op

revision = "0049_agent_canvas_cards"
down_revision = "0048_v2_wake_circuit"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_chat_messages_agent_canvas_cards"
CREATE_INDEX_SQL = (
    f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
    "ON chat_messages (user_id, ts DESC) "
    "WHERE (doc->>'role') IN ('agent','openclaw') "
    "AND (doc->>'content_type') = 'file' "
    "AND lower(doc->>'file_name') LIKE '%.io.html'"
)


_UPDATE_PREPARED_HEAD = """UPDATE server_config SET value=convert_to(
        jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
        '["0049_agent_canvas_cards"]'::jsonb)::text, 'UTF8')
        WHERE key='phase4_primary_prepared'
        AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared','false')='true'"""


def _index_validity() -> bool | None:
    row = op.get_bind().exec_driver_sql(
        "SELECT idx.indisvalid FROM pg_class AS cls "
        "JOIN pg_index AS idx ON idx.indexrelid=cls.oid "
        f"WHERE cls.relkind='i' AND cls.relname='{INDEX_NAME}' "
        "AND pg_table_is_visible(cls.oid)"
    ).fetchone()
    return None if row is None else bool(row[0])


def upgrade() -> None:
    validity = _index_validity()
    with op.get_context().autocommit_block():
        # A canceled concurrent build leaves an invalid shell; IF NOT EXISTS
        # alone would silently retain it on retry.
        if validity is False:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
        if validity is not True:
            op.execute(CREATE_INDEX_SQL)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
