"""Merge screen-chat and pre perception TEE heads.

Revision ID: 0016_merge_screen_pre_perception
Revises: 0014_screen_chat_frames, 0015_merge_pre_perception
"""

from alembic import op


revision = "0016_merge_screen_pre_perception"
down_revision = (
    "0014_screen_chat_frames",
    "0015_merge_pre_perception",
)
branch_labels = None
depends_on = None


_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0016_merge_screen_pre_perception"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
