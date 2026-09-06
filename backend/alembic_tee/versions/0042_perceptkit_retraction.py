"""Source retractions, and the source-fact identity on the current value.

Revision ID: 0042_perceptkit_retraction
Revises: 0041_perceptkit_mirror_source

A retraction says "that fact no longer stands". It is deliberately not a
fourth availability state: availability answers "did this attempt get a
value", and mixing the two makes an old host read a retraction as a sensor
fault -- which keeps showing the deleted number as last known.

``_UP`` is byte-identical to the paired revision on the other chain.
"""

from alembic import op


revision = "0042_perceptkit_retraction"
down_revision = "0041_perceptkit_mirror_source"
branch_labels = None
depends_on = None


_UP = """-- Facts the source withdrew. See schema.py for why `source` is in the key.
CREATE TABLE IF NOT EXISTS perceptkit_retraction (
  subject_id      TEXT        NOT NULL,
  signal          TEXT        NOT NULL,
  source          TEXT        NOT NULL,
  source_event_id TEXT        NOT NULL,
  observed_at     TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (subject_id, signal, source, source_event_id)
);

-- The current projection needs to know which source fact it came from.
-- Without it a retraction cannot locate the current value at all for
-- current-only signals, which store no observation to reverse-look-up.
ALTER TABLE perceptkit_current
  ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE perceptkit_current
  ADD COLUMN IF NOT EXISTS source_event_id TEXT;
"""


#: TEE 这条链的约定：每个新 head 要把自己写进 `phase4_primary_prepared`
#: 的 tee_heads —— 那是"这个库准备好到哪一版"的记录。漏了的话，
#: 迁移跑完了但那条记录还停在上一版，看的人以为没升上来。
#: 部分升级（只升到这一版就停）是真实存在的，所以中间版本也要记。
_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0042_perceptkit_retraction"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    # 刻意留成 no-op，和 0040/0041/0106/0107 对这几张表的处理一致。
    #
    # 撤回记录**不能**跟着降级删掉：它记的是「用户在健康 app 里删了这条」，
    # 删掉之后再升回来，那些数值会重新出现在当前值和趋势里 —— 用户已经
    # 删过的数字又被 agent 说出来。
    #
    # 但也不能 `raise`：整条链会在这一版卡死，任何要经过它的降级 / 重放
    # 都跑不动（0075 那几条重放测试就是这么红的）。no-op 让链走得通，
    # 表留着，再升上来时 `CREATE TABLE IF NOT EXISTS` 原样接上。
    pass
