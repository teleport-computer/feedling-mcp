"""TEE 补齐 SNAPSHOT lane 两张表落后 RDS 的 10 列（spec 2026-07-27-tee
-full-table-alignment Task 10）。

Revision ID: 0005_snapshot_column_catchup
Revises: 0004_full_table_alignment
Create Date: 2026-07-28

背景：0004 派生 DDL 时是从 prod RDS 取的 schema，但 prod 与 test RDS 在滚动
部署窗口里跑在不同进度上——0059/0060/0061 给 v2_turn_metrics /
v2_wake_schedule 加的列，07-27 派生时 test RDS 还没跑到，TEE 侧因此少了这
10 列。这本该被 snapshot_table 的严格列位置 COPY 直接打死（`row field count
is N, expected M`），Task 10 的另一半修复（交集 + missing_in_tee/missing_in_
rds 上报）让漂移不再致命，但真实缺的列仍然该补——本迁移就是把 TEE 追平到
与 prod/test RDS 一致的 0062_v2_failure_reply 状态。

列定义逐列照抄 backend/alembic/versions/0059_v2_incident_wake_guards.py /
0060_v2_wake_failure_backoff.py / 0061_v2_adaptive_tail_metrics.py 的 _UP。
0061 在 RDS 侧还带了两条 CHECK 约束 + 一个部分索引，本迁移一并搬过来——
TEE 侧目前没有已知写路径依赖它们，但列值本身应该服从同样的完整性约束，
不带索引反而会让漂移检测（Task 10 之外）用不上它。

**不要**补 `model_api_routes.thinking_fallback`——全仓零命中，是 test RDS
的历史残留迁移遗留，不是任何代码路径需要的列；TEE 不该跟着长歪。它由
snapshot_table 的交集逻辑消化，报进 missing_in_tee，不需要 DDL 追平。

DDL 幂等（ADD COLUMN IF NOT EXISTS / DROP ... IF EXISTS），与 0001 baseline
的安全性质一致。
"""

from alembic import op

revision = "0005_snapshot_column_catchup"
down_revision = "0004_full_table_alignment"
branch_labels = None
depends_on = None


_UP = """
-- 照抄 0059_v2_incident_wake_guards
ALTER TABLE v2_wake_schedule
  ADD COLUMN IF NOT EXISTS self_wake_streak INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS self_wake_user_seq BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS self_wake_last_effect_id TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS self_wake_last_effect_accepted BOOLEAN;

-- 照抄 0060_v2_wake_failure_backoff
ALTER TABLE v2_wake_schedule
  ADD COLUMN IF NOT EXISTS proactive_fail_streak INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS proactive_fail_user_seq BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS proactive_backoff_until TIMESTAMPTZ;

-- 照抄 0061_v2_adaptive_tail_metrics
ALTER TABLE v2_turn_metrics
  ADD COLUMN IF NOT EXISTS effective_tail_turns INT,
  ADD COLUMN IF NOT EXISTS tail_fallback BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS prompt_frontier_exhaustion_count INT NOT NULL DEFAULT 0;

ALTER TABLE v2_turn_metrics
  DROP CONSTRAINT IF EXISTS ck_v2_turn_metrics_effective_tail_turns;
ALTER TABLE v2_turn_metrics
  ADD CONSTRAINT ck_v2_turn_metrics_effective_tail_turns
  CHECK (effective_tail_turns IS NULL OR effective_tail_turns >= 0);

ALTER TABLE v2_turn_metrics
  DROP CONSTRAINT IF EXISTS ck_v2_turn_metrics_frontier_exhaustion_count;
ALTER TABLE v2_turn_metrics
  ADD CONSTRAINT ck_v2_turn_metrics_frontier_exhaustion_count
  CHECK (prompt_frontier_exhaustion_count >= 0);

CREATE INDEX IF NOT EXISTS idx_v2_turn_metrics_tail_lane_created
  ON v2_turn_metrics (lane, created_at DESC)
  WHERE effective_tail_turns IS NOT NULL;
"""

_DOWN = """
DROP INDEX IF EXISTS idx_v2_turn_metrics_tail_lane_created;
ALTER TABLE v2_turn_metrics
  DROP CONSTRAINT IF EXISTS ck_v2_turn_metrics_frontier_exhaustion_count,
  DROP CONSTRAINT IF EXISTS ck_v2_turn_metrics_effective_tail_turns,
  DROP COLUMN IF EXISTS prompt_frontier_exhaustion_count,
  DROP COLUMN IF EXISTS tail_fallback,
  DROP COLUMN IF EXISTS effective_tail_turns;

ALTER TABLE v2_wake_schedule
  DROP COLUMN IF EXISTS proactive_backoff_until,
  DROP COLUMN IF EXISTS proactive_fail_user_seq,
  DROP COLUMN IF EXISTS proactive_fail_streak;

ALTER TABLE v2_wake_schedule
  DROP COLUMN IF EXISTS self_wake_last_effect_accepted,
  DROP COLUMN IF EXISTS self_wake_last_effect_id,
  DROP COLUMN IF EXISTS self_wake_user_seq,
  DROP COLUMN IF EXISTS self_wake_streak;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
