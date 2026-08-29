"""PerceptKit ``StoragePort`` 的 Postgres 实现。

这是 kit 之外的东西 —— kit 只给逻辑对象和端口语义，"用什么库、建什么表"
归宿主。它存在的意义是让一致性测试有一个**真数据库**的实现可以跑：
内存实现天然原子、天然没有并发，那十条保证在它上面永远是绿的。

## 三处必须靠数据库本身，而不是靠代码自觉

**跨租户隔离靠主键，不靠 WHERE。** ``subject_id`` 在每张表的主键前缀里。
只靠每个查询记得加条件的话，漏一个就是把一个人的数据算到另一个人头上，
而且不报错。

**当前值的并发靠 CAS，不靠读后写。** ``compare_and_put_current`` 带
``expected_version``，输给别人就返回 False 由调用方重试 —— 两个 worker
同时读到旧版本、都写一遍的话，晚的那个会把新值盖回去。

**发件箱的 claim 靠 token 栅栏。** 租约过期的 worker 醒过来，只比状态
不比 token 就会把一条别人正在处理的事件改回去。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterator, Sequence

from perceptkit.contracts import delivery as _delivery
from perceptkit.contracts.receipt import (
    INGEST_ACCEPTED,
    INGEST_CONFLICT,
    INGEST_DUPLICATE,
    IngestReceipt,
    WakeReceipt,
)
from perceptkit.contracts.records import (
    CalendarEventMirror,
    CurrentProjection,
    DailyAggregate,
    DurableDedupeIdentity,
    EventOutboxEntry,
    ReminderItemMirror,
    SourceSyncState,
    StoredObservation,
)

from . import schema as _schema


def _j(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def _rev(value: Any) -> str | None:
    """revision 一律按文本存。

    **不转成数字。** ``"10"`` 和 ``10`` 是两种不同的东西，比较规则也不同 ——
    存的时候统一成文本，比较交给 kit 的 ``decide_current_update``，
    它比不了会明说比不了，而不是编一个顺序出来。
    """
    return None if value is None else str(value)


class PostgresStorage:
    """一个 ``StoragePort`` 实现。构造时给一个 psycopg 连接。

    刻意接受**一条连接**而不是连接池：``transaction()`` 要求同一个事务里
    的写全在一条连接上，池会把它们分到不同连接、原子性就没了。
    宿主的 worker 每轮自己取一条连接构造一个实例。
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self._depth = 0

    # -- 事务 ------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """可重入。嵌套时**不开新事务**，跟着最外层一起提交或回滚 ——
        内层单独提交会让"要么都成、要么都不成"变成一句空话。"""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._depth = 1
        try:
            with self.conn.transaction():
                yield
        finally:
            self._depth = 0

    def _q(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    # -- 上报幂等 --------------------------------------------------------

    def claim_report(self, *, subject_id, producer, report_id,
                     payload_digest, received_at) -> IngestReceipt:
        rows = self._q(
            """
            INSERT INTO perceptkit_ingest_receipt
              (subject_id, producer, report_id, payload_digest, received_at, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (subject_id, producer, report_id) DO NOTHING
            RETURNING 1
            """,
            (subject_id, producer, report_id, payload_digest,
             received_at, INGEST_ACCEPTED),
        )
        if rows:
            return IngestReceipt(subject_id, producer, report_id, payload_digest,
                                 received_at, INGEST_ACCEPTED)
        prior = self._q(
            "SELECT payload_digest, received_at FROM perceptkit_ingest_receipt "
            "WHERE subject_id=%s AND producer=%s AND report_id=%s",
            (subject_id, producer, report_id),
        )[0]
        # 同 id 同内容 = 重传，返回原结果；同 id 不同内容 = 冲突，
        # **不静默覆盖** —— 覆盖了就永远说不清哪份数据生效了。
        status = INGEST_DUPLICATE if prior[0] == payload_digest else INGEST_CONFLICT
        return IngestReceipt(subject_id, producer, report_id, prior[0],
                             prior[1], status)

    # -- 观测 ------------------------------------------------------------

    def append_observation(self, observation: StoredObservation) -> bool:
        o = observation
        rows = self._q(
            """
            INSERT INTO perceptkit_observation
              (subject_id, observation_id, signal, signal_schema_version, source,
               occurred_at, received_at, availability, effective_local_date,
               typed_value, timezone, source_event_id, source_revision, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (subject_id, observation_id) DO NOTHING
            RETURNING 1
            """,
            (o.subject_id, o.observation_id, o.signal, o.signal_schema_version,
             o.source, o.occurred_at, o.received_at, o.availability,
             o.effective_local_date, _j(o.typed_value), o.timezone,
             o.source_event_id, _rev(o.source_revision), o.created_at),
        )
        return bool(rows)

    def list_observations(self, *, subject_id, signal, start=None, end=None,
                          cursor=None, limit=100):
        sql = ["SELECT * FROM perceptkit_observation "
               "WHERE subject_id=%s AND signal=%s"]
        params: list[Any] = [subject_id, signal]
        if start is not None:
            sql.append("AND occurred_at >= %s"); params.append(start)
        if end is not None:
            sql.append("AND occurred_at <= %s"); params.append(end)
        # 游标用 (occurred_at, observation_id) 而不是 OFFSET：中间插入一条
        # 迟到数据会让 OFFSET 分页漏掉或重复一条，而且不报错。
        if cursor:
            at, oid = json.loads(cursor)
            sql.append("AND (occurred_at, observation_id) > (%s, %s)")
            params += [datetime.fromisoformat(at), oid]
        sql.append("ORDER BY occurred_at, observation_id LIMIT %s")
        params.append(limit + 1)

        rows = self._q(" ".join(sql), params)
        page = [self._observation(r) for r in rows[:limit]]
        nxt = None
        if len(rows) > limit and page:
            nxt = json.dumps([page[-1].occurred_at.isoformat(),
                              page[-1].observation_id])
        return page, nxt

    @staticmethod
    def _observation(r: tuple) -> StoredObservation:
        return StoredObservation(
            subject_id=r[0], observation_id=r[1], signal=r[2],
            signal_schema_version=r[3], source=r[4], occurred_at=r[5],
            received_at=r[6], availability=r[7], effective_local_date=r[8],
            typed_value=r[9], timezone=r[10], source_event_id=r[11],
            source_revision=r[12], created_at=r[13],
        )

    def delete_observations(self, *, subject_id, signal=None, before=None) -> int:
        sql = ["DELETE FROM perceptkit_observation WHERE subject_id=%s"]
        params: list[Any] = [subject_id]
        if signal is not None:
            sql.append("AND signal=%s"); params.append(signal)
        if before is not None:
            sql.append("AND occurred_at < %s"); params.append(before)
        with self.conn.cursor() as cur:
            cur.execute(" ".join(sql), params)
            return cur.rowcount

    # -- 当前值 ----------------------------------------------------------

    def get_current(self, *, subject_id, signals):
        if not signals:
            return {}
        rows = self._q(
            "SELECT * FROM perceptkit_current "
            "WHERE subject_id=%s AND signal = ANY(%s)",
            (subject_id, list(signals)),
        )
        out: dict[str, list[CurrentProjection]] = {}
        for r in rows:
            out.setdefault(r[1], []).append(CurrentProjection(
                subject_id=r[0], signal=r[1], dimension_key=r[2], typed_value=r[3],
                availability=r[4], observed_at=r[5], received_at=r[6],
                expires_at=r[7], source_observation_id=r[8], source_revision=r[9],
                version=r[10], content_digest=r[11],
            ))
        return out

    def compare_and_put_current(self, projection: CurrentProjection, *,
                                expected_version: int) -> bool:
        p = projection
        cols = (p.subject_id, p.signal, p.dimension_key, _j(p.typed_value),
                p.availability, p.observed_at, p.received_at, p.expires_at,
                p.source_observation_id, _rev(p.source_revision), p.version,
                p.content_digest)
        if expected_version < 0:
            rows = self._q(
                """
                INSERT INTO perceptkit_current
                  (subject_id, signal, dimension_key, typed_value, availability,
                   observed_at, received_at, expires_at, source_observation_id,
                   source_revision, version, content_digest)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (subject_id, signal, dimension_key) DO NOTHING
                RETURNING 1
                """, cols)
            return bool(rows)
        # 带上 expected_version 的条件更新。**返回值必须认** —— 忽略它的话，
        # 两个并发事务都读到旧版本，较新的那个 CAS 失败被静默丢掉，
        # 当前值停在旧数据上，没有任何地方报错。
        rows = self._q(
            """
            UPDATE perceptkit_current
               SET typed_value=%s, availability=%s, observed_at=%s, received_at=%s,
                   expires_at=%s, source_observation_id=%s, source_revision=%s,
                   version=%s, content_digest=%s
             WHERE subject_id=%s AND signal=%s AND dimension_key=%s
               AND version=%s
            RETURNING 1
            """,
            (_j(p.typed_value), p.availability, p.observed_at, p.received_at,
             p.expires_at, p.source_observation_id, _rev(p.source_revision),
             p.version, p.content_digest,
             p.subject_id, p.signal, p.dimension_key, expected_version),
        )
        return bool(rows)

    # -- 聚合 ------------------------------------------------------------

    def get_aggregate(self, *, subject_id, signal, start_date, end_date,
                      aggregation_kind=None):
        sql = ["SELECT * FROM perceptkit_daily_aggregate "
               "WHERE subject_id=%s AND signal=%s AND local_date BETWEEN %s AND %s"]
        params: list[Any] = [subject_id, signal, start_date, end_date]
        if aggregation_kind is not None:
            sql.append("AND aggregation_kind=%s"); params.append(aggregation_kind)
        return [
            DailyAggregate(
                subject_id=r[0], signal=r[1], local_date=r[2], aggregation_kind=r[3],
                aggregation_version=r[4], typed_aggregate=r[5],
                timezone_attribution=r[6], source_coverage=r[7], updated_at=r[8],
            )
            for r in self._q(" ".join(sql), params)
        ]

    def put_aggregate(self, aggregate: DailyAggregate) -> None:
        a = aggregate
        self._q(
            """
            INSERT INTO perceptkit_daily_aggregate
              (subject_id, signal, local_date, aggregation_kind, aggregation_version,
               typed_aggregate, timezone_attribution, source_coverage, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (subject_id, signal, local_date, aggregation_kind,
                         aggregation_version)
            DO UPDATE SET typed_aggregate = EXCLUDED.typed_aggregate,
                          timezone_attribution = EXCLUDED.timezone_attribution,
                          source_coverage = EXCLUDED.source_coverage,
                          updated_at = EXCLUDED.updated_at
            """,
            (a.subject_id, a.signal, a.local_date, a.aggregation_kind,
             a.aggregation_version, _j(a.typed_aggregate), a.timezone_attribution,
             _j(a.source_coverage), a.updated_at),
        )

    # -- 去重身份 --------------------------------------------------------

    def remember_identity(self, identity: DurableDedupeIdentity) -> bool:
        i = identity
        rows = self._q(
            """
            INSERT INTO perceptkit_dedupe_identity
              (subject_id, signal, source, digest, first_applied_at,
               aggregate_scope, retain_until)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (subject_id, signal, source, digest) DO NOTHING
            RETURNING 1
            """,
            (i.subject_id, i.signal, i.source, i.source_event_identity_digest,
             i.first_applied_at, i.aggregate_scope, i.retain_until),
        )
        return bool(rows)

    def has_seen_identity(self, *, subject_id, signal, source, digest) -> bool:
        return bool(self._q(
            "SELECT 1 FROM perceptkit_dedupe_identity "
            "WHERE subject_id=%s AND signal=%s AND source=%s AND digest=%s",
            (subject_id, signal, source, digest),
        ))

    # -- 规则状态 --------------------------------------------------------

    def get_rule_state(self, *, subject_id, definition_id, scope_key):
        rows = self._q(
            "SELECT state FROM perceptkit_rule_state "
            "WHERE subject_id=%s AND definition_id=%s AND scope_key=%s",
            (subject_id, definition_id, scope_key),
        )
        return rows[0][0] if rows else None

    def put_rule_state(self, *, subject_id, definition_id, scope_key, state) -> None:
        self._q(
            """
            INSERT INTO perceptkit_rule_state
              (subject_id, definition_id, scope_key, state)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (subject_id, definition_id, scope_key)
            DO UPDATE SET state = EXCLUDED.state
            """,
            (subject_id, definition_id, scope_key, _j(state)),
        )

    # -- 发件箱 ----------------------------------------------------------

    def enqueue_event(self, entry: EventOutboxEntry) -> bool:
        e = entry
        rows = self._q(
            """
            INSERT INTO perceptkit_event_outbox
              (event_id, subject_id, definition_id, definition_version, event_type,
               occurred_at, detected_at, delivery_state, attempt_count,
               fact_snapshot, next_attempt_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING 1
            """,
            (e.event_id, e.subject_id, e.definition_id, e.definition_version,
             e.event_type, e.occurred_at, e.detected_at, e.delivery_state,
             e.attempt_count, _j(e.fact_snapshot), e.next_attempt_at),
        )
        return bool(rows)

    def claim_pending_event(self, *, worker_id, now, lease_seconds):
        """原子地捞一条并占住它。

        ``FOR UPDATE SKIP LOCKED`` 是这里的关键：两个 worker 同时捞，
        各拿各的，不会都拿到同一条 —— 没有它就要靠"读了再更新"，
        中间那一瞬两个人都会以为自己拿到了。
        """
        expires = datetime.fromtimestamp(now.timestamp() + lease_seconds,
                                         tz=timezone.utc)
        token = f"{worker_id}:{now.timestamp()}"
        rows = self._q(
            """
            WITH picked AS (
              SELECT event_id FROM perceptkit_event_outbox
               WHERE (
                       delivery_state = 'pending'
                       AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
                     )
                     -- 租约过期的 claimed 也要能被接管：原持有者可能已经死了，
                     -- 不接管的话这条事件永远卡在 claimed，用户永远收不到。
                     -- 接管时换一个新 claim_token，原持有者就算活过来也
                     -- 改不动它（栅栏在 record_wake_receipt 那边）。
                     OR (delivery_state = 'claimed' AND claim_expires_at <= %s)
               ORDER BY detected_at
               FOR UPDATE SKIP LOCKED
               LIMIT 1
            )
            UPDATE perceptkit_event_outbox o
               SET delivery_state = 'claimed', attempt_count = o.attempt_count + 1,
                   claim_token = %s, claimed_by = %s, claim_expires_at = %s
              FROM picked
             WHERE o.event_id = picked.event_id
            RETURNING o.*
            """,
            (now, now, token, worker_id, expires),
        )
        return self._outbox(rows[0]) if rows else None

    def list_pending_events(self, *, subject_id=None, limit=100):
        sql = ["SELECT * FROM perceptkit_event_outbox WHERE delivery_state IN "
               "('pending','claimed')"]
        params: list[Any] = []
        if subject_id is not None:
            sql.append("AND subject_id=%s"); params.append(subject_id)
        sql.append("ORDER BY detected_at LIMIT %s"); params.append(limit)
        return [self._outbox(r) for r in self._q(" ".join(sql), params)]

    @staticmethod
    def _outbox(r: tuple) -> EventOutboxEntry:
        return EventOutboxEntry(
            event_id=r[0], subject_id=r[1], definition_id=r[2],
            definition_version=r[3], event_type=r[4], occurred_at=r[5],
            detected_at=r[6], delivery_state=r[7], attempt_count=r[8],
            fact_snapshot=r[9], next_attempt_at=r[10], claim_token=r[11],
        )

    def record_wake_receipt(self, *, receipt: WakeReceipt, next_state: str,
                            claim_token=None, next_attempt_at=None) -> bool:
        self._q(
            """
            INSERT INTO perceptkit_wake_receipt
              (event_id, attempt_id, status, received_at, runtime_ref, reason)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (event_id, attempt_id) DO NOTHING
            """,
            (receipt.event_id, receipt.attempt_id, receipt.status,
             receipt.received_at, receipt.runtime_ref, receipt.reason),
        )
        # 🔴 栅栏：只有还持着同一个 token 的人才能推进状态。租约过期的 worker
        # 醒过来时新持有者已经换了 token，它这一句更新到零行 —— 正是要的结果。
        sql = ("UPDATE perceptkit_event_outbox "
               "SET delivery_state=%s, next_attempt_at=%s WHERE event_id=%s")
        params: list[Any] = [next_state, next_attempt_at, receipt.event_id]
        if claim_token is not None:
            sql += " AND claim_token=%s"; params.append(claim_token)
        sql += " RETURNING 1"
        return bool(self._q(sql, params))

    # -- 来源镜像 --------------------------------------------------------

    def upsert_calendar_events(self, *, subject_id, events) -> None:
        for e in events:
            self._q(
                """
                INSERT INTO perceptkit_calendar_mirror
                  (subject_id, source_account_id, source_calendar_id,
                   source_event_id, event_fields, source_revision,
                   recurrence_identity, source_created_at, source_updated_at,
                   last_seen_sync_id, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (subject_id, source_account_id, source_calendar_id,
                             source_event_id)
                DO UPDATE SET event_fields = EXCLUDED.event_fields,
                              source_revision = EXCLUDED.source_revision,
                              recurrence_identity = EXCLUDED.recurrence_identity,
                              source_updated_at = EXCLUDED.source_updated_at,
                              last_seen_sync_id = EXCLUDED.last_seen_sync_id,
                              updated_at = EXCLUDED.updated_at
                """,
                (e.subject_id, e.source_account_id, e.source_calendar_id,
                 e.source_event_id, _j(e.event_fields), _rev(e.source_revision),
                 e.recurrence_identity, e.source_created_at, e.source_updated_at,
                 e.last_seen_sync_id, e.updated_at),
            )

    def upsert_reminders(self, *, subject_id, items) -> None:
        for r in items:
            self._q(
                """
                INSERT INTO perceptkit_reminder_mirror
                  (subject_id, source_account_id, source_list_id,
                   source_reminder_id, reminder_fields, source_revision,
                   source_created_at, source_updated_at, last_seen_sync_id,
                   updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (subject_id, source_account_id, source_list_id,
                             source_reminder_id)
                DO UPDATE SET reminder_fields = EXCLUDED.reminder_fields,
                              source_revision = EXCLUDED.source_revision,
                              source_updated_at = EXCLUDED.source_updated_at,
                              last_seen_sync_id = EXCLUDED.last_seen_sync_id,
                              updated_at = EXCLUDED.updated_at
                """,
                (r.subject_id, r.source_account_id, r.source_list_id,
                 r.source_reminder_id, _j(r.reminder_fields), _rev(r.source_revision),
                 r.source_created_at, r.source_updated_at, r.last_seen_sync_id,
                 r.updated_at),
            )

    def list_calendar_events(self, *, subject_id, start=None, end=None, limit=50):
        rows = self._q(
            "SELECT * FROM perceptkit_calendar_mirror WHERE subject_id=%s",
            (subject_id,),
        )
        out = []
        for r in rows:
            fields = dict(r[4])
            at = fields.get("start_at")
            if isinstance(at, str):
                at = datetime.fromisoformat(at)
                fields["start_at"] = at
            # 时间不明的**保留** —— 和删除那边同一条纪律：证明不了它在范围外，
            # 就不能替用户把它藏起来。
            if at is not None and start is not None and at < start:
                continue
            if at is not None and end is not None and at > end:
                continue
            out.append(CalendarEventMirror(
                subject_id=r[0], source_account_id=r[1], source_calendar_id=r[2],
                source_event_id=r[3], event_fields=fields, source_revision=r[5],
                recurrence_identity=r[6], source_created_at=r[7],
                source_updated_at=r[8], last_seen_sync_id=r[9], updated_at=r[10],
            ))
        out.sort(key=lambda m: (m.event_fields.get("start_at") is None,
                                m.event_fields.get("start_at") or datetime.min,
                                m.source_event_id))
        return out[:limit]

    def list_reminders(self, *, subject_id, include_completed=False, limit=50):
        rows = self._q(
            "SELECT * FROM perceptkit_reminder_mirror WHERE subject_id=%s",
            (subject_id,),
        )
        out = [
            ReminderItemMirror(
                subject_id=r[0], source_account_id=r[1], source_list_id=r[2],
                source_reminder_id=r[3], reminder_fields=dict(r[4]),
                source_revision=r[5], source_created_at=r[6],
                source_updated_at=r[7], last_seen_sync_id=r[8], updated_at=r[9],
            )
            for r in rows
            if include_completed or not dict(r[4]).get("is_completed")
        ]
        out.sort(key=lambda m: m.source_reminder_id)
        return out[:limit]

    def apply_source_snapshot(self, *, subject_id, source, collection_kind,
                              sync_id, coverage_start, coverage_end,
                              snapshot_kind) -> int:
        # 增量同步没有资格删任何东西 —— 它只知道"变了什么"，不知道"还剩什么"。
        if snapshot_kind != "full":
            return 0
        if collection_kind == "calendar":
            table, at_key = "perceptkit_calendar_mirror", "start_at"
            fields_col = "event_fields"
        else:
            table, at_key = "perceptkit_reminder_mirror", "due_at"
            fields_col = "reminder_fields"
        # 🔴 只删【能证明落在覆盖范围内】的。时间不明的一律不删 ——
        # 证明不了它在范围内，就没有资格删它。拿局部窗口删窗口外的数据，
        # 会让用户发现自己去年的日程凭空消失，而且不可逆。
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM {table}
                 WHERE subject_id = %s
                   AND (last_seen_sync_id IS DISTINCT FROM %s)
                   AND ({fields_col} ->> %s) IS NOT NULL
                   AND ({fields_col} ->> %s)::timestamptz BETWEEN %s AND %s
                """,
                (subject_id, sync_id, at_key, at_key, coverage_start, coverage_end),
            )
            return cur.rowcount

    def get_sync_state(self, *, subject_id, source, collection_kind):
        rows = self._q(
            "SELECT * FROM perceptkit_sync_state "
            "WHERE subject_id=%s AND source=%s AND collection_kind=%s",
            (subject_id, source, collection_kind),
        )
        if not rows:
            return None
        r = rows[0]
        return SourceSyncState(
            subject_id=r[0], source=r[1], collection_kind=r[2], last_sync_id=r[3],
            last_successful_sync_at=r[4], coverage_start=r[5], coverage_end=r[6],
            cursor=r[7],
        )

    def put_sync_state(self, state: SourceSyncState) -> None:
        s = state
        self._q(
            """
            INSERT INTO perceptkit_sync_state
              (subject_id, source, collection_kind, last_sync_id,
               last_successful_sync_at, coverage_start, coverage_end, cursor)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (subject_id, source, collection_kind)
            DO UPDATE SET last_sync_id = EXCLUDED.last_sync_id,
                          last_successful_sync_at = EXCLUDED.last_successful_sync_at,
                          coverage_start = EXCLUDED.coverage_start,
                          coverage_end = EXCLUDED.coverage_end,
                          cursor = EXCLUDED.cursor
            """,
            (s.subject_id, s.source, s.collection_kind, s.last_sync_id,
             s.last_successful_sync_at, s.coverage_start, s.coverage_end, s.cursor),
        )

    # -- 删除 ------------------------------------------------------------

    def purge_subject(self, *, subject_id) -> dict[str, int]:
        """按人删干净。**一个事务里做完** —— 删一半就断电的话，
        用户会处在"有些数据没了、有些还在"的状态，而且没人知道删到哪了。"""
        counts: dict[str, int] = {}
        with self.transaction():
            for table in _schema.TABLES:
                if table == "perceptkit_wake_receipt":
                    continue                    # 它按 event_id 走，下面单独处理
                with self.conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {table} WHERE subject_id = %s",
                                (subject_id,))
                    counts[table] = cur.rowcount
            with self.conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM perceptkit_wake_receipt WHERE event_id IN "
                    "(SELECT event_id FROM perceptkit_event_outbox "
                    " WHERE subject_id = %s)", (subject_id,))
                counts["perceptkit_wake_receipt"] = cur.rowcount
        return counts


__all__ = ["PostgresStorage"]
