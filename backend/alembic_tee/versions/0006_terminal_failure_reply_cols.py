"""TEE 补齐 v2_terminal_failure_outbox 落后 RDS 的 7 列（0062_v2_failure_reply）

Revision ID: 0006_terminal_failure_reply
Revises: 0005_snapshot_column_catchup
Create Date: 2026-07-28

**这条缺口是被机制自己抓到的，不是人翻出来的**——值得记一笔，因为它正是
Task 10 那套列漂移上报存在的理由。

时间线：Task 10 的交集逻辑部署到 test 后第一个 tick，`tee_sync_runs.report`
里冒出两条 `missing_in_tee`：
  - `model_api_routes: ["thinking_fallback"]`  → test RDS 的历史残留列，
    全仓 grep 零命中，**故意不补**（TEE 不跟着某个环境长歪，让它一直报着）。
  - `v2_terminal_failure_outbox: [7 列]`       → 真缺口，就是本迁移补的这些。

来源与 0005 同型：0062_v2_failure_reply 是本分支在飞的时候合进 test 的，
0004 派生 DDL 时（07-27，从 prod 取）这 7 列还不存在。实测两个环境的 RDS 都已
是 22 列、两侧 TEE 都停在 15 列，所以 test/prod 都要补。

如果没有交集上报，这张表会以 `row field count is 22, expected 15` 每个 tick
失败一次，而在此之前的旧行为下没人会注意到——它当时行数为 0，失败计数混在
另外 26 张里。

列定义逐列照抄 backend/alembic/versions/0062_v2_failure_reply.py 的 _UP，
含三条 CHECK 与那个 partial index。

**不搬** 0062 里的两处内容：
  1. `UPDATE ... SET reply_delivered_at = COALESCE(...)` 回填——那是为了防止
     迁移落地时给历史失败补发聊天气泡，是 RDS 侧的业务语义。TEE 是只读影子，
     没有发气泡的路径，而且 SNAPSHOT lane 每个 tick 整表替换，回填出来的值
     下一秒就会被 RDS 的真实值覆盖。
  2. `agent_jobs` 上的两个索引——那是给 admin 端点扫描加速的，与本表无关；
     TEE 侧没有那些查询路径。

DDL 幂等（ADD COLUMN IF NOT EXISTS / DROP CONSTRAINT IF EXISTS），与 0001
baseline 的安全性质一致。
"""

from alembic import op

revision = "0006_terminal_failure_reply"
down_revision = "0005_snapshot_column_catchup"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE v2_terminal_failure_outbox
  ADD COLUMN IF NOT EXISTS error_class TEXT NOT NULL DEFAULT 'unknown',
  ADD COLUMN IF NOT EXISTS reply_frontier_seq BIGINT,
  ADD COLUMN IF NOT EXISTS reply_parent_message_id TEXT,
  ADD COLUMN IF NOT EXISTS reply_delivered_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS reply_attempt_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS reply_last_attempt_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS reply_next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE v2_terminal_failure_outbox
  DROP CONSTRAINT IF EXISTS v2_terminal_failure_error_class_check,
  DROP CONSTRAINT IF EXISTS v2_terminal_failure_reply_frontier_check,
  DROP CONSTRAINT IF EXISTS v2_terminal_failure_reply_attempt_check,
  ADD CONSTRAINT v2_terminal_failure_error_class_check
    CHECK (error_class <> '' AND length(error_class) <= 64),
  ADD CONSTRAINT v2_terminal_failure_reply_frontier_check
    CHECK (reply_frontier_seq IS NULL OR reply_frontier_seq >= 0),
  ADD CONSTRAINT v2_terminal_failure_reply_attempt_check
    CHECK (reply_attempt_count >= 0);

CREATE INDEX IF NOT EXISTS v2_terminal_failure_reply_pending_idx
  ON v2_terminal_failure_outbox
     (reply_next_attempt_at, reply_last_attempt_at, created_at, job_id)
  WHERE reply_delivered_at IS NULL;
"""

_DOWN = """
DROP INDEX IF EXISTS v2_terminal_failure_reply_pending_idx;

ALTER TABLE v2_terminal_failure_outbox
  DROP CONSTRAINT IF EXISTS v2_terminal_failure_error_class_check,
  DROP CONSTRAINT IF EXISTS v2_terminal_failure_reply_frontier_check,
  DROP CONSTRAINT IF EXISTS v2_terminal_failure_reply_attempt_check;

ALTER TABLE v2_terminal_failure_outbox
  DROP COLUMN IF EXISTS error_class,
  DROP COLUMN IF EXISTS reply_frontier_seq,
  DROP COLUMN IF EXISTS reply_parent_message_id,
  DROP COLUMN IF EXISTS reply_delivered_at,
  DROP COLUMN IF EXISTS reply_attempt_count,
  DROP COLUMN IF EXISTS reply_last_attempt_at,
  DROP COLUMN IF EXISTS reply_next_attempt_at;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
