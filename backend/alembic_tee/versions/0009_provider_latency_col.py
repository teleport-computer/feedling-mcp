"""TEE 跟上 provider_health 的 recent_latency_ms（RDS 0068_provider_latency）

Revision ID: 0009_provider_latency
Revises: 0008_model_api_vision_cols
Create Date: 2026-07-30

跟 0008 同型，但这次是**先补、不是事后补**：RDS 侧的
0068_provider_latency 与本 revision 同一批提交。

`provider_health` 在 SNAPSHOT lane。按 §3「加列漂移没有红灯」：加列不建表，
撞不上「两侧无公共列」护栏，交集 COPY 会照常 `ok: true`、行数也对得上，
只有那一列的数据静静地不进 TEE，在 snapshot failures 和 CI 上完全静默。
唯一信号是 `missing_in_tee`。所以加列必须同批写 alembic_tee revision，
而不是等巡检翻出来。

（写 RDS 0068 时我判断"SNAPSHOT 用列交集所以不需要 alembic_tee"——那个判断
只覆盖了"会不会炸复制"，漏了"数据会不会到"。0068 的注释已随本 revision 更正。）

列定义照抄 backend/alembic/versions/0068_provider_latency_health.py。
DDL 幂等（ADD COLUMN IF NOT EXISTS）。

⚠️ 合进 test 之后**必须有人手工执行**——tee-migrate.yml 依赖的 4 个 repo
secret 截至 2026-07-29 还没建，alembic_tee 目前仍是手工通道
（步骤见 docs/TEE_POSTGRES_SHADOW_PROVISIONING.md §2）。0007 就是因为没人跑
而让 TEE 停在 0006 的。
"""

from alembic import op

revision = "0009_provider_latency"
down_revision = "0008_model_api_vision_cols"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE provider_health
  ADD COLUMN IF NOT EXISTS recent_latency_ms DOUBLE PRECISION;
"""

_DOWN = """
ALTER TABLE provider_health
  DROP COLUMN IF EXISTS recent_latency_ms;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
