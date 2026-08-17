"""Relax the V2 trajectory envelope CHECKs to accept plaintext OR envelope.

v6 makes content encryption a per-user preference (default plaintext). The 0043
constraints hard-required a dual-recipient envelope on every row, so a
plaintext-tier user's V2 rows could not be inserted at all — the plan's risk
register calls this out as "V2 强制加密未改按偏好 → 明文档用户 V2 数据无法直读".

Both shapes stay pinned on the invariants that actually matter (tenant binding,
row identity, non-empty body), so this is a widening, not a removal:

  - envelope shape : body_ct + nonce + K_user + K_enclave (unchanged from 0043)
  - plaintext shape: body (string)

Revision ID: 0072_relax_v2_envelope_shape
Revises: 0071_runtime_health_idx
"""

from alembic import op


revision = "0072_relax_v2_envelope_shape"
down_revision = "0071_runtime_health_idx"
branch_labels = None
depends_on = None


# Shared shape predicate. `col` is the JSONB column being constrained.
def _shape_check(col: str) -> str:
    return f"""
      jsonb_typeof({col}) = 'object'
      AND {col} ? 'owner_user_id'
      AND {col} ? 'id'
      AND {col} ? 'visibility'
      AND jsonb_typeof({col}->'owner_user_id') = 'string'
      AND jsonb_typeof({col}->'id') = 'string'
      AND jsonb_typeof({col}->'visibility') = 'string'
      AND {col}->>'owner_user_id' = user_id
      AND {col}->>'visibility' = 'shared'
      AND length({col}->>'id') > 0
      AND (
        (
          -- 信封形状（与 0043 一致）
          {col} ? 'body_ct' AND {col} ? 'nonce'
          AND {col} ? 'K_user' AND {col} ? 'K_enclave' AND {col} ? 'v'
          AND jsonb_typeof({col}->'body_ct') = 'string'
          AND jsonb_typeof({col}->'nonce') = 'string'
          AND jsonb_typeof({col}->'K_user') = 'string'
          AND jsonb_typeof({col}->'K_enclave') = 'string'
          AND jsonb_typeof({col}->'v') = 'number'
          AND length({col}->>'body_ct') > 0
          AND length({col}->>'nonce') > 0
          AND length({col}->>'K_user') > 0
          AND length({col}->>'K_enclave') > 0
          AND {col} - ARRAY[
            'v','id','owner_user_id','visibility','body_ct','nonce','K_user',
            'K_enclave','enclave_pk_fpr','content_pk_fpr'
          ]::text[] = '{{}}'::jsonb
        )
        OR
        (
          -- 明文形状（v6 默认档）：与 TEE 侧实测形状一致
          {col} ? 'body'
          AND jsonb_typeof({col}->'body') = 'string'
          AND NOT ({col} ? 'body_ct')
          AND {col} - ARRAY['id','owner_user_id','visibility','body']::text[]
              = '{{}}'::jsonb
        )
      )
    """


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE v2_trajectory_events
          DROP CONSTRAINT IF EXISTS ck_v2_trajectory_envelope;
        ALTER TABLE v2_trajectory_events
          ADD CONSTRAINT ck_v2_trajectory_envelope
          CHECK ({_shape_check('payload_envelope')});

        ALTER TABLE v2_trajectory_reviews
          DROP CONSTRAINT IF EXISTS ck_v2_trajectory_review_envelope;
        ALTER TABLE v2_trajectory_reviews
          ADD CONSTRAINT ck_v2_trajectory_review_envelope
          CHECK (
            review_envelope IS NULL OR ({_shape_check('review_envelope')})
          );
    """)


def downgrade() -> None:
    # Intentionally not restoring the strict 0043 predicates: by the time this
    # runs, plaintext rows may already exist and re-adding the envelope-only
    # CHECK would fail the table scan. Dropping is the only safe direction.
    op.execute("""
        ALTER TABLE v2_trajectory_events
          DROP CONSTRAINT IF EXISTS ck_v2_trajectory_envelope;
        ALTER TABLE v2_trajectory_reviews
          DROP CONSTRAINT IF EXISTS ck_v2_trajectory_review_envelope;
    """)
