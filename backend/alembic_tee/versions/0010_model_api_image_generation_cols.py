"""Keep the TEE model-route snapshot schema aligned with RDS 0073.

Revision ID: 0010_image_generation_cols
Revises: 0009_provider_latency
"""

from alembic import op


revision = "0010_image_generation_cols"
down_revision = "0009_provider_latency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE model_api_routes
            ADD COLUMN IF NOT EXISTS is_image_generation BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS image_generation_test_status TEXT NOT NULL DEFAULT 'untested',
            ADD COLUMN IF NOT EXISTS last_image_generation_test_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS last_image_generation_test_error TEXT NOT NULL DEFAULT '';

        CREATE UNIQUE INDEX IF NOT EXISTS model_api_routes_one_image_generation
            ON model_api_routes (user_id) WHERE is_image_generation;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS model_api_routes_one_image_generation;
        ALTER TABLE model_api_routes
            DROP COLUMN IF EXISTS last_image_generation_test_error,
            DROP COLUMN IF EXISTS last_image_generation_test_at,
            DROP COLUMN IF EXISTS image_generation_test_status,
            DROP COLUMN IF EXISTS is_image_generation;
        """
    )
