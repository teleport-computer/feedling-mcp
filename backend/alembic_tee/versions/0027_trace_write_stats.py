"""TEE twin of RDS 0095: daily trace write-rate counters (T138 块 0).

Revision ID: 0027_trace_write_stats
Revises: 0026_chat_daily_rollup

test 的 primary 是 TEE，尺子必须建在真正被写的那一份上，否则量了个空表。
DDL 与 RDS 0095 逐字一致，由 test_tee_migrations_reuse_the_rds_contract_sql 守住。

Seven 裁决 retention = **永久保留**，所以容量规划不再是"选个保留天数"，而是
"这条水管每天流多少、永远流下去"。现有的 68 MB/天、25 GB/年是基于「人均 200
条/天」的估算，**从来没有实测过**；补完探针后还会涨。永久保留下拿估算当地基
是不能接受的。

这张表就是那把尺子。它**不是** trace 本身的存储（那是后面的 trace_events），
只是逐日计数，行数是 天 × 进程启动实例 × subsystem × type × lane。

``writer_id`` 不是业务维度，而是重试幂等键。每个进程启动实例把自己的累计绝对值
用 ``GREATEST`` 写回；若数据库已经提交但客户端收到异常，重试同一绝对值不会重复
累加。进程重启会换一个 writer_id，旧行仍在，所以跨重启汇总不会归零。

## ⚠️ 块 0 不是"跑个查询统计一下"

旧 ring 受 48h TTL + 1000/2500 条 cap 双重截断，**结构上给不出真实的 7 天速率**：
它保存的是"截断之后剩下的"，不是"发生过的"。所以必须先建能活过重启的持久日
计数器，再连续量 7 天取峰值。

## 为什么带 ``event_type`` 维度

只按 subsystem 聚合会**把新增量摊平**（codex2 preflight 指出，成立）。补完 C 块
探针后，增量集中在特定 type —— C1 的路线创建/测试、C3 的 schema name 级事件。
按 subsystem 看只见总量微涨，估不出这些新 type 各自的上界，而容量规划要的正是
上界。

刻意**不带 route 维度**：trace 事件并非每条都有 route，加了会大量落 unknown，
而维度基数直接变成这把尺子自己的写入成本。若后续需要分 V1/V2 增长曲线，再加。

## 为什么计数器必须同时报自己的缺口

现有 ring 会丢事件（队列满、刷盘异常、进程崩溃），而丢掉的那些**不会**出现在
任何计数里。如果只数"写成功了多少"，量出来的速率天然偏小 —— 而偏小的速率会
让容量规划做出偏小的结论，那正是这次要避免的事。

所以每天三个数分开记，**精度不同的量不许混**（Seven 2026-08-19 裁决决策 1）：

- ``persisted_events/bytes`` —— 确实写进去的，精确
- ``known_drop_events/bytes`` —— 只有 queue.Full。确定没入队，能给精确值
- ``at_risk_events/bytes`` —— 刷盘异常涉及的量。⚠️ **报错 ≠ 没落库**：
  commit ambiguity 下只知道"这批 N 条报错了"，不知道 N 条是不是都没进去。
  **不许把它当成丢失**，它是"结果未知"

非正常退出（崩溃）那一类连计数器本身都一起没了，**结构上不可能在这张表里
体现** —— 它只能由进程存活信号推断出一个时间窗，不在本表范围内。这条限制
写在这里，免得有人把 persisted_events 的和当成"全部发生过的事件数"。
"""

from alembic import op


revision = "0027_trace_write_stats"
down_revision = "0026_chat_daily_rollup"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE IF NOT EXISTS trace_write_stats (
    day               TEXT NOT NULL,
    writer_id         TEXT NOT NULL,
    subsystem         TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    lane              TEXT NOT NULL,
    persisted_events  BIGINT NOT NULL DEFAULT 0,
    persisted_bytes   BIGINT NOT NULL DEFAULT 0,
    known_drop_events BIGINT NOT NULL DEFAULT 0,
    known_drop_bytes  BIGINT NOT NULL DEFAULT 0,
    at_risk_events    BIGINT NOT NULL DEFAULT 0,
    at_risk_bytes     BIGINT NOT NULL DEFAULT 0,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, writer_id, subsystem, event_type, lane),
    CONSTRAINT trace_write_stats_day_format
        CHECK (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
    CONSTRAINT trace_write_stats_nonneg
        CHECK (
            persisted_events >= 0 AND persisted_bytes >= 0
            AND known_drop_events >= 0 AND known_drop_bytes >= 0
            AND at_risk_events >= 0 AND at_risk_bytes >= 0
        )
);
CREATE INDEX IF NOT EXISTS ix_trace_write_stats_day
    ON trace_write_stats (day DESC);
"""

_DOWN = "DROP TABLE IF EXISTS trace_write_stats"


_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0027_trace_write_stats"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
