"""事件记下是被哪条源事实触发的，撤回时才抹得到它。

Revision ID: 0115_outbox_source_fact
Revises: 0114_agent_canvas_cards

``_UP`` is byte-identical to the paired revision on the other chain.
"""

from alembic import op


revision = "0115_outbox_source_fact"
down_revision = "0114_agent_canvas_cards"
branch_labels = None
depends_on = None


_UP = """
-- 这条事件是**被哪条源事实触发**的。
--
-- 用户在健康 app 里删掉一条体重之后，"体重 72kg 触发了涨重提醒"这条记录里
-- 的 72 也不该再留着（hx 2026-09-17 拍板，是「删除不再提供原值」的延伸）。
-- 要抹就得先认得出是哪几条 —— 而事件此前只记了规则 id 和版本，没记来源。
--
-- 两列都可空：这之前落库的事件没有这个信息，编一个比留空更坏。
-- 老行抹不掉是已知缺口，不是故障。
ALTER TABLE perceptkit_event_outbox
  ADD COLUMN IF NOT EXISTS source           TEXT;
ALTER TABLE perceptkit_event_outbox
  ADD COLUMN IF NOT EXISTS source_event_id  TEXT;

-- 抹值是按 (人, 信号, 来源, 样本id) 找行。没有这个索引就是全表扫，
-- 而撤回发生在用户点"删除"的那一刻，是同步路径。
CREATE INDEX IF NOT EXISTS perceptkit_event_outbox_source
  ON perceptkit_event_outbox (subject_id, source, source_event_id);
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # 刻意 no-op：删列会丢掉"这条提醒是谁触发的"，而那正是撤回能不能抹到它
    # 的唯一依据。留着两格 NULL 不影响任何旧代码。
    pass
