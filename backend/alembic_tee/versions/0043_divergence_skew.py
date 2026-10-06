"""Record how far apart the two shadow readings were taken.

Revision ID: 0043_divergence_skew
Revises: 0042_perceptkit_retraction

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


revision = "0043_divergence_skew"
down_revision = "0042_perceptkit_retraction"
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


#: TEE 这条链的约定：每个新 head 要把自己写进 `phase4_primary_prepared`
#: 的 tee_heads —— 那是"这个库准备好到哪一版"的记录。漏了的话，
#: 迁移跑完了但那条记录还停在上一版，看的人以为没升上来。
#: （写 0042_divergence_skew 时漏了，被 test_tee_0029_upgrades_to_voice_merge_head 抓到；
#: 0042_perceptkit_retraction 也漏了，改链顺序时一并补上。）
_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0043_divergence_skew"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    op.execute("ALTER TABLE perceptkit_shadow_divergence "
               "DROP COLUMN IF EXISTS last_skew_sec, "
               "DROP COLUMN IF EXISTS max_skew_sec")
