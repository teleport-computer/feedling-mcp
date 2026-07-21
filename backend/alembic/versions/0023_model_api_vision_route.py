"""Add an optional dedicated vision role to saved model routes.

Revision ID: 0023_model_api_vision_route
"""

from alembic import op


revision = "0023_model_api_vision_route"
down_revision = "0022_notify_relay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE model_api_routes
            ADD COLUMN IF NOT EXISTS is_vision BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS vision_test_status TEXT NOT NULL DEFAULT 'untested',
            ADD COLUMN IF NOT EXISTS last_vision_test_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS last_vision_test_error TEXT NOT NULL DEFAULT '';

        CREATE UNIQUE INDEX IF NOT EXISTS model_api_routes_one_vision
            ON model_api_routes (user_id) WHERE is_vision;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS model_api_routes_one_vision;
        ALTER TABLE model_api_routes
            DROP COLUMN IF EXISTS last_vision_test_error,
            DROP COLUMN IF EXISTS last_vision_test_at,
            DROP COLUMN IF EXISTS vision_test_status,
            DROP COLUMN IF EXISTS is_vision;
        """
    )
