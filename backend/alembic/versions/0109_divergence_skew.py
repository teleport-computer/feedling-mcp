"""Record how far apart the two shadow readings were taken.

Revision ID: 0109_divergence_skew
Revises: 0108_perceptkit_retraction

A `differ` row says the live path and the kit hold different values. It does
not say why. Either one side read the sensor later than the other -- expected
on anything that changes by the second, which is where every one of these
lands -- or one side is wrong, which is the only case worth acting on.

Nothing in the table could tell those apart, so the whole class stayed
unexplained, and an unexplained divergence is what keeps the live path alive.

``_UP`` is byte-identical to the paired revision on the other chain. Both
columns are nullable: rows written before this migration have no skew to
report, and inventing one would be worse than leaving it unknown.
"""

from alembic import op


revision = "0109_divergence_skew"
down_revision = "0108_perceptkit_retraction"
branch_labels = None
depends_on = None


_UP = """
-- Two paths can hold different values for two completely different reasons:
-- one read the sensor later than the other, or one of them is wrong. The
-- values alone cannot separate those, and 0.09% of production comparisons
-- came back `differ` with no way to tell which kind they were -- which is
-- what blocks retiring the live path.
ALTER TABLE perceptkit_shadow_divergence
  ADD COLUMN IF NOT EXISTS last_skew_sec DOUBLE PRECISION;
ALTER TABLE perceptkit_shadow_divergence
  ADD COLUMN IF NOT EXISTS max_skew_sec  DOUBLE PRECISION;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute("ALTER TABLE perceptkit_shadow_divergence "
               "DROP COLUMN IF EXISTS last_skew_sec, "
               "DROP COLUMN IF EXISTS max_skew_sec")
