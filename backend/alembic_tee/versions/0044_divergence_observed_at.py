"""Both sides' own observed_at on the comparison table (TEE chain).

Revision ID: 0044_divergence_observed_at
Revises: 0043_divergence_skew

``_UP`` is byte-identical to 0110 on the RDS chain.
"""

from alembic import op


revision = "0044_divergence_observed_at"
down_revision = "0043_divergence_skew"
branch_labels = None
depends_on = None


_UP = """
-- 两边**各自的**取值时刻，不只是它们的差。
--
-- 差值（0109 加的 skew）回答了「谁读得晚多久」，但丢了两件事：谁更晚
-- （skew 取了绝对值），以及绝对时间（没法和别的东西对时间线）。
--
-- 外部复核 2026-09-03 §5 要的就是这两格：prod 上 0.09% 的比对是 `differ`，
-- 而「取值时刻不同」和「其中一条路算错了」光看值和次数分不开 ——
-- 分不开就不能下线老路。
ALTER TABLE perceptkit_shadow_divergence
  ADD COLUMN IF NOT EXISTS last_live_at TIMESTAMPTZ;
ALTER TABLE perceptkit_shadow_divergence
  ADD COLUMN IF NOT EXISTS last_kit_at  TIMESTAMPTZ;
"""


#: TEE 这条链的约定：每个新 head 要把自己写进 `phase4_primary_prepared`
#: 的 tee_heads —— 那是"这个库准备好到哪一版"的记录。
_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0044_divergence_observed_at"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    # 同 0110：删列会丢掉判据。
    pass
