"""PerceptKit 逻辑存储对象 → Postgres 的建表语句。

**这是 DDL 的唯一来源。** alembic 迁移和一致性测试都引用这里的常量，
所以"测试用的表"和"线上真的建出来的表"结构上不可能分叉 ——
分叉了的话，一致性测试全绿而线上照样出错，那正是这套测试要防的事。

## 几处刻意的设计

``subject_id`` 进每一张表的主键前缀。跨租户隔离靠的是主键本身，
不是靠每个查询自己记得加 WHERE —— 漏一个查询就是把一个人的数据算到
另一个人头上，而且不报错。

去重身份（``perceptkit_dedupe_identity``）**和明细分表**。明细有保留期、
聚合可以永久，去重记录必须比明细活得久 —— 放同一张表里，清理明细就会
把它一起带走，然后旧数据重放会把永久聚合的数字加两遍且无法回滚。

发件箱的 claim 用 ``claim_token`` 做栅栏。租约过期的 worker 醒过来
不能覆盖新持有者的状态 —— 只比状态不比 token 的话，它会把一条正在被
别人处理的事件改回去。
"""
from __future__ import annotations

#: 建表。**幂等**：每一句都是 IF NOT EXISTS，重复执行安全。
DDL = """
CREATE TABLE IF NOT EXISTS perceptkit_ingest_receipt (
  subject_id      TEXT        NOT NULL,
  producer        TEXT        NOT NULL,
  report_id       TEXT        NOT NULL,
  payload_digest  TEXT        NOT NULL,
  received_at     TIMESTAMPTZ NOT NULL,
  status          TEXT        NOT NULL,
  PRIMARY KEY (subject_id, producer, report_id)
);

CREATE TABLE IF NOT EXISTS perceptkit_observation (
  subject_id            TEXT        NOT NULL,
  observation_id        TEXT        NOT NULL,
  signal                TEXT        NOT NULL,
  signal_schema_version INT         NOT NULL,
  source                TEXT        NOT NULL,
  occurred_at           TIMESTAMPTZ NOT NULL,
  received_at           TIMESTAMPTZ NOT NULL,
  availability          TEXT        NOT NULL,
  effective_local_date  DATE        NOT NULL,
  typed_value           JSONB,
  timezone              TEXT,
  source_event_id       TEXT,
  source_revision       TEXT,
  created_at            TIMESTAMPTZ,
  PRIMARY KEY (subject_id, observation_id)
);

-- 时间线查询按 (subject, signal, occurred_at) 走；保留期清理按 occurred_at 扫。
CREATE INDEX IF NOT EXISTS perceptkit_observation_timeline
  ON perceptkit_observation (subject_id, signal, occurred_at, observation_id);

CREATE TABLE IF NOT EXISTS perceptkit_current (
  subject_id            TEXT        NOT NULL,
  signal                TEXT        NOT NULL,
  dimension_key         TEXT        NOT NULL,
  typed_value           JSONB,
  availability          TEXT        NOT NULL,
  observed_at           TIMESTAMPTZ NOT NULL,
  received_at           TIMESTAMPTZ NOT NULL,
  expires_at            TIMESTAMPTZ,
  source_observation_id TEXT,
  source_revision       TEXT,
  version               INT         NOT NULL DEFAULT 0,
  content_digest        TEXT,
  PRIMARY KEY (subject_id, signal, dimension_key)
);

CREATE TABLE IF NOT EXISTS perceptkit_daily_aggregate (
  subject_id           TEXT        NOT NULL,
  signal               TEXT        NOT NULL,
  local_date           DATE        NOT NULL,
  aggregation_kind     TEXT        NOT NULL,
  aggregation_version  INT         NOT NULL,
  typed_aggregate      JSONB       NOT NULL,
  timezone_attribution TEXT,
  source_coverage      JSONB       NOT NULL DEFAULT '{}'::jsonb,
  updated_at           TIMESTAMPTZ,
  PRIMARY KEY (subject_id, signal, local_date, aggregation_kind, aggregation_version)
);

-- 明细过期而聚合永久时，这张表必须比明细活得久。**单独一张表**就是为了
-- 让"清理明细"物理上碰不到它。
CREATE TABLE IF NOT EXISTS perceptkit_dedupe_identity (
  subject_id   TEXT        NOT NULL,
  signal       TEXT        NOT NULL,
  source       TEXT        NOT NULL,
  digest          TEXT        NOT NULL,
  first_applied_at TIMESTAMPTZ NOT NULL,
  -- 这条身份是为哪个永久聚合守着的。清理明细时靠它判断"这个还不能删"。
  aggregate_scope TEXT,
  retain_until    TIMESTAMPTZ,
  PRIMARY KEY (subject_id, signal, source, digest)
);

CREATE TABLE IF NOT EXISTS perceptkit_rule_state (
  subject_id    TEXT  NOT NULL,
  definition_id TEXT  NOT NULL,
  scope_key     TEXT  NOT NULL,
  state         JSONB NOT NULL,
  PRIMARY KEY (subject_id, definition_id, scope_key)
);

CREATE TABLE IF NOT EXISTS perceptkit_event_outbox (
  event_id           TEXT        PRIMARY KEY,
  subject_id         TEXT        NOT NULL,
  definition_id      TEXT        NOT NULL,
  definition_version INT         NOT NULL,
  event_type         TEXT        NOT NULL,
  occurred_at        TIMESTAMPTZ NOT NULL,
  detected_at        TIMESTAMPTZ NOT NULL,
  delivery_state     TEXT        NOT NULL,
  attempt_count      INT         NOT NULL DEFAULT 0,
  fact_snapshot      JSONB       NOT NULL DEFAULT '{}'::jsonb,
  next_attempt_at    TIMESTAMPTZ,
  claim_token        TEXT,
  claimed_by         TEXT,
  claim_expires_at   TIMESTAMPTZ
);

-- worker 捞活走这条：按状态 + 到期时间挑，同一 subject 的先后不重要。
CREATE INDEX IF NOT EXISTS perceptkit_event_outbox_claimable
  ON perceptkit_event_outbox (delivery_state, next_attempt_at)
  WHERE delivery_state IN ('pending', 'claimed');

CREATE TABLE IF NOT EXISTS perceptkit_wake_receipt (
  event_id    TEXT        NOT NULL,
  attempt_id  TEXT        NOT NULL,
  status      TEXT        NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  runtime_ref TEXT,
  reason      TEXT,
  PRIMARY KEY (event_id, attempt_id)
);

CREATE TABLE IF NOT EXISTS perceptkit_calendar_mirror (
  subject_id          TEXT        NOT NULL,
  source_account_id   TEXT        NOT NULL,
  source_calendar_id  TEXT        NOT NULL,
  source_event_id     TEXT        NOT NULL,
  event_fields        JSONB       NOT NULL,
  source_revision     TEXT,
  recurrence_identity TEXT,
  source_created_at   TIMESTAMPTZ,
  source_updated_at   TIMESTAMPTZ,
  last_seen_sync_id   TEXT,
  updated_at          TIMESTAMPTZ,
  PRIMARY KEY (subject_id, source_account_id, source_calendar_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS perceptkit_reminder_mirror (
  subject_id         TEXT        NOT NULL,
  source_account_id  TEXT        NOT NULL,
  source_list_id     TEXT        NOT NULL,
  source_reminder_id TEXT        NOT NULL,
  reminder_fields    JSONB       NOT NULL,
  source_revision    TEXT,
  source_created_at  TIMESTAMPTZ,
  source_updated_at  TIMESTAMPTZ,
  last_seen_sync_id  TEXT,
  updated_at         TIMESTAMPTZ,
  PRIMARY KEY (subject_id, source_account_id, source_list_id, source_reminder_id)
);

CREATE TABLE IF NOT EXISTS perceptkit_sync_state (
  subject_id             TEXT        NOT NULL,
  source                 TEXT        NOT NULL,
  collection_kind        TEXT        NOT NULL,
  last_sync_id           TEXT,
  last_successful_sync_at TIMESTAMPTZ,
  coverage_start         TIMESTAMPTZ,
  coverage_end           TIMESTAMPTZ,
  cursor                 TEXT,
  PRIMARY KEY (subject_id, source, collection_kind)
);
"""

#: 一次清空所有表。**只给测试用** —— 生产上删数据走 purge_subject
#: 和保留期清理，两者都按 subject / 按时间界定范围。
TRUNCATE = """
TRUNCATE perceptkit_ingest_receipt, perceptkit_observation, perceptkit_current,
         perceptkit_daily_aggregate, perceptkit_dedupe_identity,
         perceptkit_rule_state, perceptkit_event_outbox, perceptkit_wake_receipt,
         perceptkit_calendar_mirror, perceptkit_reminder_mirror,
         perceptkit_sync_state;
"""

TABLES = (
    "perceptkit_ingest_receipt", "perceptkit_observation", "perceptkit_current",
    "perceptkit_daily_aggregate", "perceptkit_dedupe_identity",
    "perceptkit_rule_state", "perceptkit_event_outbox", "perceptkit_wake_receipt",
    "perceptkit_calendar_mirror", "perceptkit_reminder_mirror",
    "perceptkit_sync_state",
)

__all__ = ["DDL", "TRUNCATE", "TABLES"]
