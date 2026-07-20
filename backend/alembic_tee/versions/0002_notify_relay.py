"""TEE 影子库：Notify Relay 两表（列与主库 0020_notify_relay 对齐）。

Revision ID: 0002_notify_relay
Revises: 0001_tee_baseline
Create Date: 2026-07-18

列定义必须与 backend/alembic/versions/0020_notify_relay.py 对齐——
tee_shadow/reconciler.py 的全列 SELECT/UPSERT 依赖两侧同形（见 reconciler
头注释）。notify_relay_logs.id 是 GENERATED ALWAYS AS IDENTITY，镜像写与
reconciler 都必须 OVERRIDING SYSTEM VALUE 搬主库发的号。

一处有意的约束差异：TEE 侧 device_token **不带 UNIQUE**。主库的 UNIQUE 是
enroll 幂等键；影子表的完整性由主库保证，不需要重复约束。带上反而破坏
reconciler 收敛：换机顶替场景（同 device_token 换绑到另一 auth_token）若
best-effort 镜像恰好丢了 DELETE，陈旧行仍占着 device_token，reconciler 按
auth_token upsert 存活行就会在 prune 之前撞唯一约束报错——治不了它本要治的
漏写。去掉后 upsert 成功、陈旧行随后被 prune（RDS 无此 PK ⇒ orphan），两侧
收敛（Codex review P2）。
"""

from alembic import op


revision = "0002_notify_relay"
down_revision = "0001_tee_baseline"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS notify_relay_configs (
    auth_token    TEXT PRIMARY KEY,
    device_token  TEXT NOT NULL,
    user_id       TEXT,
    apns_env      TEXT NOT NULL DEFAULT 'production'
                  CHECK (apns_env IN ('sandbox', 'production')),
    disabled      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS notify_relay_logs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    auth_token    TEXT NOT NULL,
    push_type     SMALLINT NOT NULL CHECK (push_type IN (1, 2, 3, 4)),
    target_token  TEXT NOT NULL,
    apns_env      TEXT,
    status        SMALLINT NOT NULL DEFAULT 1 CHECK (status IN (1, 2, 3)),
    err_msg       TEXT,
    content       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notify_relay_logs_token_idx
    ON notify_relay_logs (auth_token, created_at DESC);
CREATE INDEX IF NOT EXISTS notify_relay_logs_created_idx
    ON notify_relay_logs (created_at);
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS notify_relay_logs")
    op.execute("DROP TABLE IF EXISTS notify_relay_configs")
