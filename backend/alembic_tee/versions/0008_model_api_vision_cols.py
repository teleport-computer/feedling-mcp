"""TEE 补 model_api_routes 落后 RDS 的 4 个 vision 列（0066_model_api_vision_route）

Revision ID: 0008_model_api_vision_cols
Revises: 0007_chat_activity_snapshot
Create Date: 2026-07-29

又一条被机制自己抓到、而不是被人翻出来的缺口——和 0006 同型。

发现方式：2026-07-29 巡检两环境的 `tee_sync_runs.report->'snapshot'`，
`model_api_routes` 一直是 `ok: true`（交集 COPY 把缺的列跳过去照常同步），
但 `missing_in_tee` 里静静列着这 4 列：

    ["thinking_fallback", "is_vision", "vision_test_status",
     "last_vision_test_at", "last_vision_test_error"]

注意这条**不报 failure**——这正是它危险的地方：整表还在同步、行数也对得上，
只是这 4 列的数据一直没进 TEE，没有任何红灯。`missing_in_tee` 是唯一的信号，
所以那个字段必须有人定期看（runbook 见
docs/TEE_POSTGRES_SHADOW_PROVISIONING.md §3）。

来源：0066_model_api_vision_route 是本分支收尾后合进 test 的。写它的人接了
`chat_turn_activity_events` 的 TEE 迁移（0007），但没注意到同一批里
`model_api_routes` 的加列也需要跟进——加列不建表，不会撞上"两侧无公共列"的
护栏，所以在 CI 和 snapshot failures 上都是静默的。

**不搬** 0066 里的 `model_api_routes_one_vision` partial unique index。
这与 0004 baseline 的既定政策一致：0004 建这张表时同样没有搬 0014 的
`model_api_routes_one_active` 与 `model_api_routes_uniq`。理由是 TEE 侧是
SNAPSHOT lane 整表替换的只读影子，业务唯一约束在这里没有防护价值（RDS 侧
已经保证），却会在任何边界数据上让整表 COPY 失败——把一个 RDS 侧的数据问题
放大成 TEE 侧的整表停止同步。

**thinking_fallback 故意不补**（0005 已经这么裁决过一次，此处重申）：它是
test RDS 的历史残留列，全仓 grep 零命中，prod RDS 没有。TEE 不跟着某一个
环境长歪，让它在 `missing_in_tee` 里一直报着，作为"这列该被清掉"的提醒。

列定义逐列照抄 backend/alembic/versions/0066_model_api_vision_route.py。
DDL 幂等（ADD COLUMN IF NOT EXISTS），与 0001 baseline 的安全性质一致。
"""

from alembic import op

revision = "0008_model_api_vision_cols"
down_revision = "0007_chat_activity_snapshot"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE model_api_routes
  ADD COLUMN IF NOT EXISTS is_vision BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS vision_test_status TEXT NOT NULL DEFAULT 'untested',
  ADD COLUMN IF NOT EXISTS last_vision_test_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_vision_test_error TEXT NOT NULL DEFAULT '';
"""

_DOWN = """
ALTER TABLE model_api_routes
  DROP COLUMN IF EXISTS last_vision_test_error,
  DROP COLUMN IF EXISTS last_vision_test_at,
  DROP COLUMN IF EXISTS vision_test_status,
  DROP COLUMN IF EXISTS is_vision;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
