"""TEE 明文库 baseline schema（spec §4 — TEE shadow Postgres）

Revision ID: 0001_tee_baseline
Revises:
Create Date: 2026-07-07

独立于 backend/alembic/（RDS 密文信封链）的第二条 Alembic 链，版本表
alembic_tee_version（见 env.py），两条链互不感知。

三类表:

1. 13 张「明文运维表」——DDL 从 backend/alembic/versions/ 原样搬（含 0012 补的
   per-user ON DELETE CASCADE FK，指向本库自己的 users 表）：
   server_config, global_blobs, users, user_blobs, user_logs,
   perception_items, perception_daily, copytext_strings, copytext_meta,
   genesis_import_jobs, genesis_import_outputs, agent_runtime_instances,
   agent_runtime_supervisor_heartbeats。

2. 明文内容表——行形照抄 RDS 对应表，但 doc/body 是明文（TEE 侧读路径不再过
   enclave 解密）：chat_messages, memory_moments, world_book_entries, frames
   （frames 是新形状：R2 存储层指针，不落 inline 密文——spec §4；对应 RDS 的
   frame_envelopes，同款 per-user CASCADE FK + PK 模式）。
   genesis_import_chunks 不建：chunks 是 staging 数据，冻结窗口处理，不复制
   （见 Task 5 决策）。

3. 运维表：tee_replication_cursors（复制水位线）、
   tee_pending_device_migration（local_only 解不开、等 iOS 重传的行）。

DDL 是幂等的（IF NOT EXISTS），呼应 RDS baseline 的安全性质。
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_tee_baseline"
down_revision = None
branch_labels = None
depends_on = None


_DDL = """
-- ---------------------------------------------------------------------
-- 1) 13 张明文运维表（原样抄自 RDS 链 0001/0002/0004x2/0006/0008/0010，
--    per-user 表补 0012 的 ON DELETE CASCADE FK）
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS server_config (
    key   TEXT PRIMARY KEY,
    value BYTEA NOT NULL
);

CREATE TABLE IF NOT EXISTS global_blobs (
    key TEXT PRIMARY KEY,
    doc JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    created_at TEXT,
    doc        JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS user_blobs (
    user_id TEXT NOT NULL,
    kind    TEXT NOT NULL,
    doc     JSONB NOT NULL,
    PRIMARY KEY (user_id, kind),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_logs (
    user_id  TEXT NOT NULL,
    stream   TEXT NOT NULL,
    seq      BIGINT GENERATED ALWAYS AS IDENTITY,
    ts       DOUBLE PRECISION,
    item_key TEXT,
    doc      JSONB NOT NULL,
    PRIMARY KEY (user_id, stream, seq),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS logs_stream_seq_idx ON user_logs (user_id, stream, seq);
CREATE INDEX IF NOT EXISTS logs_stream_ts_idx  ON user_logs (user_id, stream, ts);
CREATE INDEX IF NOT EXISTS logs_item_key_idx
    ON user_logs (user_id, stream, item_key) WHERE item_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS perception_items (
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,              -- 'photo' | 'calendar' | 'workout' | 'sleep' | 'vitals'
    item_id    TEXT NOT NULL,
    ts         DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION,           -- optional TTL; NULL = keep
    doc        JSONB NOT NULL,
    PRIMARY KEY (user_id, kind, item_id),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS perception_items_user_kind_ts_idx
    ON perception_items (user_id, kind, ts DESC);
CREATE INDEX IF NOT EXISTS perception_items_expires_idx
    ON perception_items (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS perception_daily (
    user_id    TEXT NOT NULL,
    date       TEXT NOT NULL,              -- device-local date 'YYYY-MM-DD'
    signal     TEXT NOT NULL,              -- canonical catalog signal key
    doc        JSONB NOT NULL,             -- per-shape running summary stats
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, date, signal),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS perception_daily_user_signal_date_idx
    ON perception_daily (user_id, signal, date DESC);

CREATE TABLE IF NOT EXISTS copytext_strings (
    key        TEXT NOT NULL,
    lang       TEXT NOT NULL,              -- 'en' | 'zh-Hans'
    value      TEXT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (key, lang)
);

CREATE TABLE IF NOT EXISTS copytext_meta (
    id        BOOLEAN PRIMARY KEY DEFAULT TRUE,   -- single-row guard
    revision  BIGINT NOT NULL DEFAULT 0,
    CHECK (id)
);
INSERT INTO copytext_meta (id, revision) VALUES (TRUE, 0)
    ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS genesis_import_jobs (
    user_id             TEXT NOT NULL,
    job_id              TEXT NOT NULL,
    status              TEXT NOT NULL,
    source_kind         TEXT NOT NULL DEFAULT 'unknown',
    file_manifest_hash  TEXT NOT NULL DEFAULT '',
    total_chunks        INTEGER NOT NULL DEFAULT 0,
    received_chunks     INTEGER NOT NULL DEFAULT 0,
    processed_chunks    INTEGER NOT NULL DEFAULT 0,
    total_bytes         BIGINT NOT NULL DEFAULT 0,
    received_bytes      BIGINT NOT NULL DEFAULT 0,
    privacy_mode        TEXT NOT NULL DEFAULT '',
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    output              JSONB NOT NULL DEFAULT '{}'::jsonb,
    memory_action_count INTEGER NOT NULL DEFAULT 0,
    identity_status     TEXT NOT NULL DEFAULT '',
    persona_ref         TEXT NOT NULL DEFAULT '',
    persona_sha256      TEXT NOT NULL DEFAULT '',
    error               TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized_at        TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    PRIMARY KEY (user_id, job_id),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS genesis_jobs_user_status_idx
    ON genesis_import_jobs (user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS genesis_jobs_ready_idx
    ON genesis_import_jobs (user_id, completed_at DESC)
    WHERE status = 'done';

-- NOTE: genesis_import_chunks is intentionally NOT created here — chunks are
-- staging data handled in the freeze window, not replicated to TEE (Task 5).

CREATE TABLE IF NOT EXISTS genesis_import_outputs (
    user_id      TEXT NOT NULL,
    job_id       TEXT NOT NULL,
    output_type  TEXT NOT NULL,
    ref          TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT '',
    doc          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, job_id, output_type),
    FOREIGN KEY (user_id, job_id)
        REFERENCES genesis_import_jobs (user_id, job_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS genesis_outputs_job_idx
    ON genesis_import_outputs (user_id, job_id);

CREATE TABLE IF NOT EXISTS agent_runtime_instances (
    user_id           TEXT PRIMARY KEY,
    driver            TEXT NOT NULL,
    status            TEXT NOT NULL,          -- starting | running | idle | error
    pid               INTEGER,
    lease_owner       TEXT,                   -- supervisor id holding the lease
    lease_expires_at  TIMESTAMPTZ,
    session_ref       TEXT,                   -- driver resume handle (Claude/Codex)
    runtime_home      TEXT NOT NULL,
    last_heartbeat_at TIMESTAMPTZ,
    last_active_at    TIMESTAMPTZ,
    error             TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS agent_runtime_instances_lease_idx
    ON agent_runtime_instances (lease_owner, lease_expires_at);

CREATE TABLE IF NOT EXISTS agent_runtime_supervisor_heartbeats (
    owner           TEXT PRIMARY KEY,        -- "<hostname>:<pid>" of the runner
    host            TEXT,
    shard_index     INTEGER NOT NULL DEFAULT 0,
    shard_count     INTEGER NOT NULL DEFAULT 1,
    max_children    INTEGER NOT NULL DEFAULT 0,   -- 0 = unlimited
    active_children INTEGER NOT NULL DEFAULT 0,
    host_all        BOOLEAN NOT NULL DEFAULT false,
    gateway         BOOLEAN NOT NULL DEFAULT false,
    version         TEXT,
    payload         JSONB NOT NULL DEFAULT '{}',  -- full rich payload for diagnostics
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_runtime_supervisor_heartbeats_updated_idx
    ON agent_runtime_supervisor_heartbeats (updated_at);

-- ---------------------------------------------------------------------
-- 2) 明文内容表（行形照抄 RDS，doc/body 明文化；per-user CASCADE FK）
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS chat_messages (
    user_id TEXT NOT NULL,
    seq     BIGINT GENERATED ALWAYS AS IDENTITY,
    msg_id  TEXT NOT NULL,
    ts      DOUBLE PRECISION NOT NULL,
    doc     JSONB NOT NULL,
    PRIMARY KEY (user_id, msg_id),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS chat_user_seq_idx ON chat_messages (user_id, seq);

CREATE TABLE IF NOT EXISTS memory_moments (
    user_id     TEXT NOT NULL,
    moment_id   TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT '',
    doc         JSONB NOT NULL,
    PRIMARY KEY (user_id, moment_id),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS memory_user_occ_idx ON memory_moments (user_id, occurred_at);

CREATE TABLE IF NOT EXISTS world_book_entries (
    user_id    TEXT NOT NULL,
    entry_id   TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    doc        JSONB NOT NULL,
    PRIMARY KEY (user_id, entry_id),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS world_book_user_updated_idx ON world_book_entries (user_id, updated_at);

-- frames: new shape vs. RDS frame_envelopes — inline ciphertext never lands in
-- a TEE row; body_* columns point at the R2-backed, storage-layer-encrypted
-- object (spec §4). Same per-user PK + CASCADE FK pattern as frame_envelopes.
CREATE TABLE IF NOT EXISTS frames (
    user_id                  TEXT NOT NULL,
    frame_id                 TEXT NOT NULL,
    ts                       DOUBLE PRECISION NOT NULL,
    meta                     JSONB,
    body_storage_key         TEXT,
    body_storage_key_version TEXT,
    body_mime                TEXT,
    body_sha256              TEXT,
    body_size_bytes          BIGINT,
    PRIMARY KEY (user_id, frame_id),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS frames_user_ts_idx ON frames (user_id, ts);

-- ---------------------------------------------------------------------
-- 3) 运维表：复制水位线 + 待人工/设备迁移队列
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tee_replication_cursors (
    table_name   TEXT PRIMARY KEY,
    watermark_ts DOUBLE PRECISION NOT NULL DEFAULT 0,
    watermark_id TEXT NOT NULL DEFAULT '',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tee_pending_device_migration (
    user_id    TEXT NOT NULL,
    table_name TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    reason     TEXT,
    marked_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, table_name, item_id)
);
"""

_DROP_DDL = """
DROP TABLE IF EXISTS tee_pending_device_migration;
DROP TABLE IF EXISTS tee_replication_cursors;
DROP TABLE IF EXISTS frames;
DROP TABLE IF EXISTS world_book_entries;
DROP TABLE IF EXISTS memory_moments;
DROP TABLE IF EXISTS chat_messages;
DROP TABLE IF EXISTS agent_runtime_supervisor_heartbeats;
DROP TABLE IF EXISTS agent_runtime_instances;
DROP TABLE IF EXISTS genesis_import_outputs;
DROP TABLE IF EXISTS genesis_import_jobs;
DROP TABLE IF EXISTS copytext_meta;
DROP TABLE IF EXISTS copytext_strings;
DROP TABLE IF EXISTS perception_daily;
DROP TABLE IF EXISTS perception_items;
DROP TABLE IF EXISTS user_logs;
DROP TABLE IF EXISTS user_blobs;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS global_blobs;
DROP TABLE IF EXISTS server_config;
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute(_DROP_DDL)
