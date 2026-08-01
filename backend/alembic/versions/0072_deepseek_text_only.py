"""Correct stale visual status for DeepSeek's text-only API models.

Revision ID: 0072_deepseek_text_only
Revises: 0071_runtime_health_idx
"""

from alembic import op


revision = "0072_deepseek_text_only"
down_revision = "0071_runtime_health_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE model_api_routes AS r
        SET vision_test_status = 'unsupported',
            last_vision_test_error = 'vision_model_incompatible',
            last_vision_test_at = now(),
            updated_at = now()
        FROM model_api_credentials AS c
        WHERE c.user_id = r.user_id
          AND c.id = r.credential_id
          AND lower(c.provider) IN ('deepseek', 'deep_seek')
          AND r.vision_test_status <> 'unsupported'
        """
    )


def downgrade() -> None:
    # The previous "ok" values were false positives produced by a text-only
    # connectivity check, so there is no truthful state to restore.
    pass
