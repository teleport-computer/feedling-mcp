"""Both sides' own observed_at on the comparison table.

Revision ID: 0110_divergence_observed_at
Revises: 0109_divergence_skew

``_UP`` is byte-identical to the paired revision on the other chain.
Both columns are nullable: rows written before this have no timestamps to
report, and inventing them would be worse than leaving them unknown.
"""

from alembic import op


revision = "0110_divergence_observed_at"
down_revision = "0109_divergence_skew"
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


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # 刻意 no-op：删列会丢掉已经采集到的证据，而这两格正是"能不能下线老路"
    # 的判据。留着列不影响任何旧代码（它们只是多两格没人读的 NULL）。
    pass
