"""Merge plaintext-shadow gates with trace write-health migrations.

Revision ID: 0029_plaintext_shadow_merge
Revises: 0028_trace_write_stats_health, 0027_plaintext_shadow_gates

Restore evidence signed for either parent head cannot authorize the merged
schema.  Invalidate it and advance the prepared-primary pin atomically.
"""

from alembic import op


revision = "0029_plaintext_shadow_merge"
down_revision = (
    "0028_trace_write_stats_health",
    "0027_plaintext_shadow_gates",
)
branch_labels = None
depends_on = None


_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0029_plaintext_shadow_merge"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute("TRUNCATE TABLE plaintext_shadow_restore_evidence")
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
