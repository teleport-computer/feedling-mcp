"""Persist the conservative prompt frontier for each model route.

Revision ID: 0047_model_route_context_window
Revises: 0046_v2_summary_segments

The column is nullable for routes created before this contract existed.  Those
rows remain runnable only when Runtime V2 can resolve an audited family floor
or an operator override.  Every new setup/route-create request persists the
resolved lower bound; an unaudited route without an explicit bound is rejected
before its provider test and can no longer fail for the first time on turn one.
"""

from alembic import op


revision = "0047_model_route_context_window"
down_revision = "0046_v2_summary_segments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE model_api_routes "
        "ADD COLUMN IF NOT EXISTS context_window_tokens BIGINT"
    )
    op.execute(
        "ALTER TABLE model_api_routes "
        "DROP CONSTRAINT IF EXISTS model_api_routes_context_window_tokens_check"
    )
    op.execute(
        "ALTER TABLE model_api_routes "
        "ADD CONSTRAINT model_api_routes_context_window_tokens_check "
        "CHECK (context_window_tokens IS NULL OR "
        "       context_window_tokens BETWEEN 2048 AND 2000000)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE model_api_routes "
        "DROP CONSTRAINT IF EXISTS model_api_routes_context_window_tokens_check"
    )
    op.execute(
        "ALTER TABLE model_api_routes "
        "DROP COLUMN IF EXISTS context_window_tokens"
    )
