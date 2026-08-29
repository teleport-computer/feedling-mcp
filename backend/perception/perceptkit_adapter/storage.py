"""A Postgres implementation of PerceptKit's ``StoragePort``.

This lives outside the kit on purpose: the kit defines logical objects and
port semantics, and choosing a database is the host's job. Its reason for
existing is that the conformance suite needs a real database to run against.
In-memory storage is atomic and single-threaded by nature, so the ten
guarantees are always green there.

Three things are enforced by the database rather than by remembering.

Cross-tenant isolation lives in the primary key, not in a WHERE clause.
``subject_id`` leads every key; one query that forgets the condition files
one person's data under another and raises nothing.

The current value uses compare-and-put, not read-then-write.
``compare_and_put_current`` takes ``expected_version`` and returns False when
it loses, leaving the caller to re-read and re-decide. Two workers that both
read the old version and both write would have the later one overwrite the
newer value.

Claiming an outbox row hands out a fencing token. A worker returning from an
expired lease that compares only the state, not the token, would undo the
progress of whoever holds the row now.
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
    """Store every revision as text.

    Never coerced to a number. ``"10"`` and ``10`` are different things with
    different ordering rules; storing text and leaving the comparison to the
    kit's ``decide_current_update`` means an incomparable pair is reported as
    a conflict instead of being given an invented order.
    """
    return None if value is None else str(value)


class PostgresStorage:
    """A ``StoragePort`` backed by one psycopg connection.

    Deliberately a single connection rather than a pool: ``transaction()``
    requires every write in one transaction to be on the same connection, and
    a pool would spread them across several, at which point atomicity is gone.
    A host's worker takes a connection per round and builds one of these.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self._depth = 0

    # -- Transactions --------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Re-entrant. A nested call joins the outermost transaction rather
        than opening its own; an inner commit would turn "all or nothing" into
        a phrase with nothing behind it."""
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

    # -- Report idempotency --------------------------------------------------

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
        # Same id and same content is a retransmission, so return the original
        # result. Same id with different content is a conflict, never a silent
        # overwrite -- overwriting leaves "which version took effect"
        # permanently unanswerable.
        status = INGEST_DUPLICATE if prior[0] == payload_digest else INGEST_CONFLICT
        return IngestReceipt(subject_id, producer, report_id, prior[0],
                             prior[1], status)

    # -- Observations --------------------------------------------------------

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
        # The cursor is (occurred_at, observation_id), not an OFFSET. A
        # late-arriving row inserted mid-scan makes OFFSET paging skip or
        # repeat an item, silently.
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

    # -- Current values ------------------------------------------------------

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
        # A conditional update on expected_version. The result must be read:
        # ignore it and two concurrent transactions both see the old version,
        # the newer write loses its CAS and disappears, and the current value
        # sits on stale data with nothing reporting it.
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

    # -- Aggregates ----------------------------------------------------------

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

    # -- Dedupe identities ---------------------------------------------------

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

    # -- Rule state ----------------------------------------------------------

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

    # -- Outbox --------------------------------------------------------------

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
        """Atomically pick one row and take ownership of it.

        ``FOR UPDATE SKIP LOCKED`` is what makes this safe: two workers pulling
        at once each get their own row. Without it the pattern is
        read-then-update, and in the gap between the two both workers believe
        they hold the same event.
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
                     -- An expired claim must be takeable: the original holder may be
                     -- dead, and without takeover the event sits in `claimed` forever
                     -- and simply never arrives. Takeover issues a fresh claim_token,
                     -- so a revived holder cannot move it (the fence lives in
                     -- record_wake_receipt).
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
        # The fence: only a holder still carrying the same token may advance
        # the state. A worker waking from an expired lease finds the token
        # replaced, so this update touches zero rows -- which is the point.
        sql = ("UPDATE perceptkit_event_outbox "
               "SET delivery_state=%s, next_attempt_at=%s WHERE event_id=%s")
        params: list[Any] = [next_state, next_attempt_at, receipt.event_id]
        if claim_token is not None:
            sql += " AND claim_token=%s"; params.append(claim_token)
        sql += " RETURNING 1"
        return bool(self._q(sql, params))

    # -- Source mirrors ------------------------------------------------------

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
            # Items with no known time are kept, the same discipline as the
            # delete path: without proof it falls outside the window, we do not
            # hide it from the user.
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
        # An incremental sync has no standing to delete: it knows what
        # changed, not what remains.
        if snapshot_kind != "full":
            return 0
        if collection_kind == "calendar":
            table, at_key = "perceptkit_calendar_mirror", "start_at"
            fields_col = "event_fields"
        else:
            table, at_key = "perceptkit_reminder_mirror", "due_at"
            fields_col = "reminder_fields"
        # Delete only what can be proven to fall inside the covered window.
        # Items with no known time are never deleted -- without proof they are
        # in range there is no standing to remove them. Deleting outside the
        # window is how a user finds last year's calendar gone, and it does not
        # come back.
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

    # -- Deletion ------------------------------------------------------------

    def purge_subject(self, *, subject_id) -> dict[str, int]:
        """Delete everything for one subject, in a single transaction. Losing
        power halfway leaves the user with some data gone and some still there,
        and no record of where it stopped."""
        counts: dict[str, int] = {}
        with self.transaction():
            for table in _schema.TABLES:
                if table == "perceptkit_wake_receipt":
                    continue                    # keyed by event_id; below
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
