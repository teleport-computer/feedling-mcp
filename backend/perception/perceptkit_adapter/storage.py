"""PostgreSQL implementation of PerceptKit's v0.10 ``StoragePort``.

One instance owns one psycopg connection.  Mutation transactions use stable
transaction-scoped advisory locks; business rows remain tenant-leading and
all publication pointers are explicit database facts.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator, Sequence

from perceptkit.contracts import delivery as _delivery
from perceptkit.contracts.errors import RetryableMutationError
from perceptkit.contracts.mutation import (
    aggregate_generation_key,
    canonical_keys,
    event_key,
)
from perceptkit.contracts.receipt import (
    INGEST_ACCEPTED,
    INGEST_CONFLICT,
    INGEST_DUPLICATE,
    IngestReceipt,
    WakeReceipt,
)
from perceptkit.contracts.records import (
    AggregateGeneration,
    CalendarEventMirror,
    ConflictRecord,
    CurrentProjection,
    DailyAggregate,
    DurableDedupeIdentity,
    EventOutboxEntry,
    ReminderItemMirror,
    SourceSyncState,
    StoredObservation,
    _compare_revisions,
)

from . import schema as _schema


_CAL_AT = ("CASE WHEN event_fields->>'start_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' "
           "THEN (event_fields->>'start_at')::timestamptz END")
_REM_AT = ("CASE WHEN reminder_fields->>'due_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' "
           "THEN (reminder_fields->>'due_at')::timestamptz END")
_SWEEP_MAX_ROWS = 2000
_CAL_COLS = ("subject_id, source, source_account_id, source_calendar_id, "
             "source_event_id, event_fields, source_revision, recurrence_identity, "
             "source_created_at, source_updated_at, last_seen_sync_id, updated_at")
_REM_COLS = ("subject_id, source, source_account_id, source_list_id, "
             "source_reminder_id, reminder_fields, source_revision, "
             "last_seen_sync_id, updated_at")
_SYNC_COLS = ("subject_id, source, collection_kind, sync_cursor, coverage_start, "
              "coverage_end, snapshot_kind, last_attempted_at, "
              "last_successful_sync_at, last_error_code")


def _j(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def _rev(value: Any) -> str | None:
    return None if value is None else str(value)


def _stable_lock_id(key: tuple[str, ...]) -> int:
    raw = json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


def _dates(value: Any) -> tuple[date, ...]:
    return tuple(date.fromisoformat(x) if isinstance(x, str) else x for x in (value or ()))


def _observation_payload(o: StoredObservation) -> dict[str, Any]:
    return asdict(o)


def _observation_from_payload(raw: dict[str, Any]) -> StoredObservation:
    value = dict(raw)
    for key in ("occurred_at", "received_at", "created_at"):
        if isinstance(value.get(key), str):
            value[key] = datetime.fromisoformat(value[key])
    if isinstance(value.get("effective_local_date"), str):
        value["effective_local_date"] = date.fromisoformat(value["effective_local_date"])
    return StoredObservation(**value)


class _PostgresMutationOwner:
    def __init__(self, storage: "PostgresStorage") -> None:
        self.storage = storage
        self.keys: set[tuple[str, ...]] = set()
        self.active = True
        self.failed = False

    def _fail(self, reason: str) -> None:
        # A nested owner shares the ambient database transaction.  Failing
        # only the inner Python object would let a caller catch the exception
        # and commit writes made by the outer owner without a valid fence.
        for owner in self.storage._mutation_owners:
            if owner.active:
                owner.failed = True
        raise RetryableMutationError(reason)

    def validate(self) -> None:
        if not self.active or self.failed:
            self._fail("mutation owner expired, aborted or lost its fence")

    def acquire(self, keys: Sequence[tuple[str, ...]]) -> None:
        self.validate()
        requested = tuple(keys)
        if requested != canonical_keys(requested):
            self._fail("mutation keys must be sorted and unique")
        new = set(requested) - self.keys
        if new and self.keys and min(new) < max(self.keys):
            self._fail("mutation lock order violation")
        if any(owner is not self and owner.active for owner in self.storage._mutation_owners):
            self._fail("nested mutation owner requires an independent transaction")
        for key in sorted(new):
            with self.storage.conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (_stable_lock_id(key),))
                if not cur.fetchone()[0]:
                    self._fail("mutation resource is owned by another operation")
            self.keys.add(key)


class PostgresStorage:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self._depth = 0
        self._mutation_owners: list[_PostgresMutationOwner] = []

    @contextmanager
    def transaction(self) -> Iterator[None]:
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

    @contextmanager
    def mutation_transaction(self):
        owner = _PostgresMutationOwner(self)
        self._mutation_owners.append(owner)
        try:
            with self.transaction():
                yield owner
                owner.validate()
        finally:
            owner.active = False
            self._mutation_owners.remove(owner)

    def _fence(self) -> None:
        for owner in self._mutation_owners:
            owner.validate()

    def _q(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        self._fence()
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def _try_internal_lock(self, key: tuple[str, ...]) -> bool:
        """Acquire a port-owned fence when no Kit mutation owner is present."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (_stable_lock_id(key),))
            return bool(cur.fetchone()[0])

    # -- Conflicts and reports -------------------------------------------

    def put_conflict(self, record: ConflictRecord) -> ConflictRecord:
        r = record
        self._q(
            """INSERT INTO perceptkit_conflict
               (subject_id,conflict_id,signal,source,fact_key,candidate_revision,
                semantic_digest,content_digest,kind,reason,candidate,created_at,
                updated_at,status,resolved_at,resolution_revision,
                resolution_semantic_digest,resolution_observation_id)
               VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,
                       %s::jsonb,%s,%s)
               ON CONFLICT (subject_id,conflict_id) DO NOTHING""",
            (r.subject_id, r.conflict_id, r.signal, r.source, r.fact_key,
             _j(r.candidate_revision), r.semantic_digest, r.content_digest,
             r.kind, r.reason, _j(_observation_payload(r.candidate)), r.created_at,
             r.updated_at, r.status, r.resolved_at, _j(r.resolution_revision),
             r.resolution_semantic_digest, r.resolution_observation_id),
        )
        return next(item for item in self.list_conflicts(
            subject_id=r.subject_id, fact_key=r.fact_key)
                    if item.conflict_id == r.conflict_id)

    def list_conflicts(self, *, subject_id, signal=None, source=None, fact_key=None,
                       status=None, start=None, end=None, limit=None, offset=0):
        if status not in (None, "pending", "resolved"):
            raise ValueError("conflict status must be pending or resolved")
        sql = ["SELECT subject_id,conflict_id,signal,source,fact_key,candidate_revision,"
               "semantic_digest,content_digest,kind,reason,candidate,created_at,updated_at,"
               "status,resolved_at,resolution_revision,resolution_semantic_digest,"
               "resolution_observation_id FROM perceptkit_conflict WHERE subject_id=%s"]
        params: list[Any] = [subject_id]
        for col, value in (("signal", signal), ("source", source),
                           ("fact_key", fact_key), ("status", status)):
            if value is not None:
                sql.append(f"AND {col}=%s"); params.append(value)
        if start is not None:
            sql.append("AND created_at >= %s"); params.append(start)
        if end is not None:
            sql.append("AND created_at <= %s"); params.append(end)
        sql.append("ORDER BY created_at, conflict_id")
        if limit is not None:
            sql.append("LIMIT %s"); params.append(limit)
        if offset:
            sql.append("OFFSET %s"); params.append(offset)
        return [ConflictRecord(
            conflict_id=r[1], subject_id=r[0], signal=r[2], source=r[3], fact_key=r[4],
            candidate_revision=r[5], semantic_digest=r[6], content_digest=r[7],
            kind=r[8], reason=r[9], candidate=_observation_from_payload(r[10]),
            created_at=r[11], updated_at=r[12], status=r[13], resolved_at=r[14],
            resolution_revision=r[15], resolution_semantic_digest=r[16],
            resolution_observation_id=r[17]) for r in self._q(" ".join(sql), params)]

    def resolve_conflict(self, *, subject_id, conflict_id, revision,
                         semantic_digest, observation_id, resolved_at):
        rows = self._q(
            "SELECT candidate_revision,status,resolution_revision,"
            "resolution_semantic_digest,resolution_observation_id "
            "FROM perceptkit_conflict WHERE subject_id=%s AND conflict_id=%s FOR UPDATE",
            (subject_id, conflict_id))
        if not rows:
            return False
        candidate, status, old_revision, old_digest, old_observation = rows[0]
        if status == "resolved":
            return (old_revision == revision and old_digest == semantic_digest
                    and old_observation == observation_id)
        if _compare_revisions(revision, candidate) != 1:
            return False
        return bool(self._q(
            """UPDATE perceptkit_conflict SET status='resolved',updated_at=%s,resolved_at=%s,
               resolution_revision=%s::jsonb,resolution_semantic_digest=%s,
               resolution_observation_id=%s
               WHERE subject_id=%s AND conflict_id=%s AND status='pending' RETURNING 1""",
            (resolved_at, resolved_at, _j(revision), semantic_digest, observation_id,
             subject_id, conflict_id)))

    def claim_report(self, *, subject_id, producer, report_id, payload_digest,
                     received_at) -> IngestReceipt:
        rows = self._q(
            """INSERT INTO perceptkit_ingest_receipt
               (subject_id,producer,report_id,payload_digest,received_at,status)
               VALUES (%s,%s,%s,%s,%s,'accepted')
               ON CONFLICT (subject_id,producer,report_id) DO NOTHING RETURNING 1""",
            (subject_id, producer, report_id, payload_digest, received_at))
        if rows:
            return IngestReceipt(subject_id, producer, report_id, payload_digest,
                                 received_at, INGEST_ACCEPTED)
        prior = self._q(
            "SELECT payload_digest,received_at,status,error_code,observations_applied "
            "FROM perceptkit_ingest_receipt WHERE subject_id=%s AND producer=%s AND report_id=%s",
            (subject_id, producer, report_id))[0]
        if prior[0] == payload_digest and prior[2] != INGEST_ACCEPTED:
            return IngestReceipt(subject_id, producer, report_id, prior[0], prior[1],
                                 prior[2], prior[3], 0)
        status = INGEST_DUPLICATE if prior[0] == payload_digest else INGEST_CONFLICT
        return IngestReceipt(subject_id, producer, report_id, prior[0], prior[1], status,
                             None if status == INGEST_DUPLICATE else "digest_mismatch", 0)

    def finalize_report(self, receipt: IngestReceipt) -> None:
        rows = self._q(
            "SELECT payload_digest,status,error_code,observations_applied FROM "
            "perceptkit_ingest_receipt WHERE subject_id=%s AND producer=%s AND report_id=%s FOR UPDATE",
            (receipt.subject_id, receipt.producer, receipt.report_id))
        if not rows or rows[0][0] != receipt.payload_digest:
            raise ValueError("report finalization requires matching claim")
        prior = rows[0]
        if prior[1] != INGEST_ACCEPTED and (
                prior[1], prior[2], prior[3]) != (
                    receipt.status, receipt.error_code, receipt.observations_applied):
            raise ValueError("cannot overwrite a terminal report failure")
        self._q(
            "UPDATE perceptkit_ingest_receipt SET status=%s,error_code=%s,observations_applied=%s "
            "WHERE subject_id=%s AND producer=%s AND report_id=%s AND payload_digest=%s",
            (receipt.status, receipt.error_code, receipt.observations_applied,
             receipt.subject_id, receipt.producer, receipt.report_id, receipt.payload_digest))

    def backfill_report_digest(self, *, subject_id, producer, report_id,
                               expected_digest, payload_digest):
        rows = self._q(
            "SELECT payload_digest FROM perceptkit_ingest_receipt "
            "WHERE subject_id=%s AND producer=%s AND report_id=%s FOR UPDATE",
            (subject_id, producer, report_id))
        if not rows:
            return False
        old = rows[0][0]
        if old == payload_digest:
            return True
        if old != expected_digest or old.startswith("v2:") or not payload_digest.startswith("v2:"):
            return False
        return bool(self._q(
            "UPDATE perceptkit_ingest_receipt SET payload_digest=%s "
            "WHERE subject_id=%s AND producer=%s AND report_id=%s AND payload_digest=%s RETURNING 1",
            (payload_digest, subject_id, producer, report_id, expected_digest)))

    # -- Observation and Current -----------------------------------------

    def append_observation(self, observation: StoredObservation) -> bool:
        o = observation
        return bool(self._q(
            """INSERT INTO perceptkit_observation
               (subject_id,observation_id,signal,signal_schema_version,source,occurred_at,
                received_at,availability,effective_local_date,typed_value,timezone,
                source_event_id,source_revision,source_revision_value,created_at,
                source_units,source_values,timezone_source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s,
                       %s::jsonb,%s::jsonb,%s)
               ON CONFLICT (subject_id,observation_id) DO NOTHING RETURNING 1""",
            (o.subject_id, o.observation_id, o.signal, o.signal_schema_version,
             o.source, o.occurred_at, o.received_at, o.availability,
             o.effective_local_date, _j(o.typed_value), o.timezone,
             o.source_event_id, _rev(o.source_revision), _j(o.source_revision),
             o.created_at, _j(o.source_units), _j(o.source_values), o.timezone_source)))

    def list_observations(self, *, subject_id, signal, start=None, end=None,
                          cursor=None, limit=100):
        cols = ("subject_id,observation_id,signal,signal_schema_version,source,occurred_at,"
                "received_at,availability,effective_local_date,typed_value,timezone,"
                "source_event_id,source_revision_value,created_at,source_units,source_values,"
                "timezone_source")
        sql = [f"SELECT {cols} FROM perceptkit_observation WHERE subject_id=%s AND signal=%s"]
        params: list[Any] = [subject_id, signal]
        if start is not None:
            sql.append("AND occurred_at >= %s"); params.append(start)
        if end is not None:
            sql.append("AND occurred_at <= %s"); params.append(end)
        if cursor:
            at, oid = json.loads(cursor)
            sql.append("AND (occurred_at,observation_id) > (%s,%s)")
            params.extend((datetime.fromisoformat(at), oid))
        sql.append("ORDER BY occurred_at,observation_id LIMIT %s"); params.append(limit + 1)
        rows = self._q(" ".join(sql), params)
        page = [StoredObservation(
            subject_id=r[0], observation_id=r[1], signal=r[2], signal_schema_version=r[3],
            source=r[4], occurred_at=r[5], received_at=r[6], availability=r[7],
            effective_local_date=r[8], typed_value=r[9], timezone=r[10],
            source_event_id=r[11], source_revision=r[12], created_at=r[13],
            source_units=r[14], source_values=r[15], timezone_source=r[16])
            for r in rows[:limit]]
        nxt = None
        if len(rows) > limit and page:
            nxt = _j([page[-1].occurred_at.isoformat(), page[-1].observation_id])
        return page, nxt

    def delete_observations(self, *, subject_id, signal=None, before=None) -> int:
        sql = ["DELETE FROM perceptkit_observation WHERE subject_id=%s"]
        params: list[Any] = [subject_id]
        if signal is not None:
            sql.append("AND signal=%s"); params.append(signal)
        if before is not None:
            sql.append("AND occurred_at < %s"); params.append(before)
        with self.conn.cursor() as cur:
            self._fence(); cur.execute(" ".join(sql), params); return cur.rowcount

    def get_current(self, *, subject_id, signals):
        if not signals:
            return {}
        rows = self._q(
            """SELECT subject_id,signal,dimension_key,typed_value,availability,observed_at,
               received_at,expires_at,source_observation_id,source_revision_value,source,
               source_event_id,version,content_digest,timezone,timezone_source,
               source_units,source_values FROM perceptkit_current
               WHERE subject_id=%s AND signal=ANY(%s) ORDER BY signal,dimension_key""",
            (subject_id, list(signals)))
        out: dict[str, list[CurrentProjection]] = {signal: [] for signal in signals}
        for r in rows:
            out[r[1]].append(CurrentProjection(
                subject_id=r[0], signal=r[1], dimension_key=r[2], typed_value=r[3],
                availability=r[4], observed_at=r[5], received_at=r[6], expires_at=r[7],
                source_observation_id=r[8], source_revision=r[9], source=r[10],
                source_event_id=r[11], version=r[12], content_digest=r[13], timezone=r[14],
                timezone_source=r[15], source_units=r[16], source_values=r[17]))
        return out

    def compare_and_put_current(self, projection: CurrentProjection, *, expected_version):
        p = projection
        values = (p.subject_id, p.signal, p.dimension_key, _j(p.typed_value), p.availability,
                  p.observed_at, p.received_at, p.expires_at, p.source_observation_id,
                  _rev(p.source_revision), _j(p.source_revision), p.source, p.source_event_id,
                  p.version, p.content_digest, p.timezone, p.timezone_source,
                  _j(p.source_units), _j(p.source_values))
        if expected_version < 0:
            return bool(self._q(
                """INSERT INTO perceptkit_current
                   (subject_id,signal,dimension_key,typed_value,availability,observed_at,
                    received_at,expires_at,source_observation_id,source_revision,
                    source_revision_value,source,source_event_id,version,content_digest,
                    timezone,timezone_source,source_units,source_values)
                   VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,
                           %s,%s,%s::jsonb,%s::jsonb)
                   ON CONFLICT (subject_id,signal,dimension_key) DO NOTHING RETURNING 1""", values))
        return bool(self._q(
            """UPDATE perceptkit_current SET typed_value=%s::jsonb,availability=%s,
               observed_at=%s,received_at=%s,expires_at=%s,source_observation_id=%s,
               source_revision=%s,source_revision_value=%s::jsonb,source=%s,
               source_event_id=%s,version=%s,content_digest=%s,timezone=%s,
               timezone_source=%s,source_units=%s::jsonb,source_values=%s::jsonb
               WHERE subject_id=%s AND signal=%s AND dimension_key=%s AND version=%s RETURNING 1""",
            (_j(p.typed_value), p.availability, p.observed_at, p.received_at, p.expires_at,
             p.source_observation_id, _rev(p.source_revision), _j(p.source_revision), p.source,
             p.source_event_id, p.version, p.content_digest, p.timezone, p.timezone_source,
             _j(p.source_units), _j(p.source_values), p.subject_id, p.signal,
             p.dimension_key, expected_version)))

    # -- Aggregates ------------------------------------------------------

    @staticmethod
    def _generation(r: tuple) -> AggregateGeneration:
        return AggregateGeneration(
            generation_id=r[3], subject_id=r[0], signal=r[1], aggregation_kind=r[2],
            aggregation_version=r[4], requested_start_date=r[5], requested_end_date=r[6],
            status=r[7], completeness=r[8], accounted_dates=_dates(r[9]),
            incomplete_dates=_dates(r[10]), incomplete_reasons=tuple(r[11] or ()),
            failure_reason=r[12], created_at=r[13], updated_at=r[14], activated_at=r[15])

    @staticmethod
    def _generation_identity(g: AggregateGeneration):
        return (g.generation_id, g.subject_id, g.signal, g.aggregation_kind,
                g.aggregation_version, g.requested_start_date, g.requested_end_date)

    def put_aggregate_generation(self, generation: AggregateGeneration) -> bool:
        g = generation
        rows = self._q(
            """INSERT INTO perceptkit_aggregate_generation
               (subject_id,signal,aggregation_kind,generation_id,aggregation_version,
                requested_start_date,requested_end_date,status,completeness,
                accounted_dates,incomplete_dates,incomplete_reasons,failure_reason,
                created_at,updated_at,activated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s)
               ON CONFLICT (subject_id,signal,aggregation_kind,generation_id) DO NOTHING
               RETURNING 1""",
            (g.subject_id, g.signal, g.aggregation_kind, g.generation_id,
             g.aggregation_version, g.requested_start_date, g.requested_end_date,
             g.status, g.completeness, _j([d.isoformat() for d in g.accounted_dates]),
             _j([d.isoformat() for d in g.incomplete_dates]), _j(g.incomplete_reasons),
             g.failure_reason, g.created_at, g.updated_at, g.activated_at))
        if rows:
            return True
        old = self.get_aggregate_generation(subject_id=g.subject_id, signal=g.signal,
                                            aggregation_kind=g.aggregation_kind,
                                            generation_id=g.generation_id)
        if old is None or self._generation_identity(old) != self._generation_identity(g):
            raise ValueError("conflicting aggregate generation identity")
        return False

    def update_aggregate_generation(self, generation: AggregateGeneration) -> None:
        g = generation
        old = self.get_aggregate_generation(subject_id=g.subject_id, signal=g.signal,
                                            aggregation_kind=g.aggregation_kind,
                                            generation_id=g.generation_id)
        if old is None:
            raise ValueError("aggregate generation does not exist")
        if self._generation_identity(old) != self._generation_identity(g):
            raise ValueError("aggregate generation immutable identity changed")
        allowed = {
            "building": {"building", "complete", "failed", "incomplete"},
            "complete": {"complete", "active", "failed"},
            "active": {"active", "complete"},
            "incomplete": {"incomplete", "failed"},
            "failed": {"failed"},
        }
        if g.status not in allowed[old.status]:
            raise ValueError(f"invalid aggregate generation transition {old.status}->{g.status}")
        self._q(
            """UPDATE perceptkit_aggregate_generation SET status=%s,completeness=%s,
               accounted_dates=%s::jsonb,incomplete_dates=%s::jsonb,
               incomplete_reasons=%s::jsonb,failure_reason=%s,updated_at=%s,activated_at=%s
               WHERE subject_id=%s AND signal=%s AND aggregation_kind=%s AND generation_id=%s""",
            (g.status, g.completeness, _j([d.isoformat() for d in g.accounted_dates]),
             _j([d.isoformat() for d in g.incomplete_dates]), _j(g.incomplete_reasons),
             g.failure_reason, g.updated_at, g.activated_at, g.subject_id, g.signal,
             g.aggregation_kind, g.generation_id))

    def get_aggregate_generation(self, *, subject_id, signal, aggregation_kind, generation_id):
        rows = self._q(
            """SELECT subject_id,signal,aggregation_kind,generation_id,aggregation_version,
               requested_start_date,requested_end_date,status,completeness,accounted_dates,
               incomplete_dates,incomplete_reasons,failure_reason,created_at,updated_at,activated_at
               FROM perceptkit_aggregate_generation
               WHERE subject_id=%s AND signal=%s AND aggregation_kind=%s AND generation_id=%s""",
            (subject_id, signal, aggregation_kind, generation_id))
        return self._generation(rows[0]) if rows else None

    def list_aggregate_generations(self, *, subject_id, signal, aggregation_kind,
                                   start_date=None, end_date=None, limit=None, offset=0):
        sql = ["""SELECT subject_id,signal,aggregation_kind,generation_id,aggregation_version,
                  requested_start_date,requested_end_date,status,completeness,accounted_dates,
                  incomplete_dates,incomplete_reasons,failure_reason,created_at,updated_at,activated_at
                  FROM perceptkit_aggregate_generation
                  WHERE subject_id=%s AND signal=%s AND aggregation_kind=%s"""]
        params: list[Any] = [subject_id, signal, aggregation_kind]
        if start_date is not None:
            sql.append("AND requested_end_date >= %s"); params.append(start_date)
        if end_date is not None:
            sql.append("AND requested_start_date <= %s"); params.append(end_date)
        sql.append("ORDER BY created_at NULLS FIRST,generation_id")
        if limit is not None:
            sql.append("LIMIT %s"); params.append(limit)
        if offset:
            sql.append("OFFSET %s"); params.append(offset)
        return [self._generation(r) for r in self._q(" ".join(sql), params)]

    def get_active_aggregate_generation(self, *, subject_id, signal, aggregation_kind):
        rows = self._q(
            """SELECT g.subject_id,g.signal,g.aggregation_kind,g.generation_id,
               g.aggregation_version,g.requested_start_date,g.requested_end_date,g.status,
               g.completeness,g.accounted_dates,g.incomplete_dates,g.incomplete_reasons,
               g.failure_reason,g.created_at,g.updated_at,g.activated_at
               FROM perceptkit_active_aggregate_generation a
               JOIN perceptkit_aggregate_generation g USING
                 (subject_id,signal,aggregation_kind,generation_id)
               WHERE a.subject_id=%s AND a.signal=%s AND a.aggregation_kind=%s""",
            (subject_id, signal, aggregation_kind))
        return self._generation(rows[0]) if rows else None

    @staticmethod
    def _aggregate(r: tuple) -> DailyAggregate:
        return DailyAggregate(
            subject_id=r[0], signal=r[1], local_date=r[2], aggregation_kind=r[3],
            aggregation_version=r[4], generation_id=r[5], typed_aggregate=r[6],
            completeness=r[7], incomplete_reasons=tuple(r[8] or ()),
            timezone_attribution=r[9], source_coverage=r[10], updated_at=r[11],
            version=r[12])

    def get_aggregate(self, *, subject_id, signal, start_date, end_date,
                      aggregation_kind=None, limit=None, offset=0):
        sql = ["""SELECT d.subject_id,d.signal,d.local_date,d.aggregation_kind,
                  d.aggregation_version,d.generation_id,d.typed_aggregate,d.completeness,
                  d.incomplete_reasons,d.timezone_attribution,d.source_coverage,d.updated_at,
                  d.version FROM perceptkit_daily_aggregate d
                  LEFT JOIN perceptkit_active_aggregate_generation a
                    ON (a.subject_id,a.signal,a.aggregation_kind,a.generation_id)=
                       (d.subject_id,d.signal,d.aggregation_kind,d.generation_id)
                  WHERE d.subject_id=%s AND d.signal=%s AND d.local_date BETWEEN %s AND %s"""]
        params: list[Any] = [subject_id, signal, start_date, end_date]
        if aggregation_kind is not None:
            sql.append("AND d.aggregation_kind=%s"); params.append(aggregation_kind)
        sql.append("ORDER BY d.local_date,d.aggregation_kind,(a.generation_id IS NULL),"
                   "d.aggregation_version,d.generation_id")
        if limit is not None:
            sql.append("LIMIT %s"); params.append(limit)
        if offset:
            sql.append("OFFSET %s"); params.append(offset)
        return [self._aggregate(r) for r in self._q(" ".join(sql), params)]

    def _bootstrap_generation(self, a: DailyAggregate) -> tuple[DailyAggregate, AggregateGeneration]:
        gid = a.generation_id or f"legacy-v{a.aggregation_version}"
        if a.generation_id != gid:
            a = replace(a, generation_id=gid)
        old = self.get_aggregate_generation(subject_id=a.subject_id, signal=a.signal,
                                            aggregation_kind=a.aggregation_kind,
                                            generation_id=gid)
        if old is None:
            active = self.get_active_aggregate_generation(
                subject_id=a.subject_id, signal=a.signal, aggregation_kind=a.aggregation_kind)
            status = "active" if active is None and a.completeness == "complete" else (
                "complete" if a.completeness == "complete" else "incomplete")
            old = AggregateGeneration(
                gid, a.subject_id, a.signal, a.aggregation_kind, a.aggregation_version,
                a.local_date, a.local_date, status=status, completeness=a.completeness,
                accounted_dates=(a.local_date,),
                incomplete_dates=(a.local_date,) if a.completeness == "incomplete" else (),
                incomplete_reasons=a.incomplete_reasons, created_at=a.updated_at,
                updated_at=a.updated_at, activated_at=a.updated_at if status == "active" else None)
            self.put_aggregate_generation(old)
            if status == "active":
                self._q(
                    """INSERT INTO perceptkit_active_aggregate_generation
                       (subject_id,signal,aggregation_kind,generation_id,activated_at)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (a.subject_id, a.signal, a.aggregation_kind, gid,
                     a.updated_at or datetime.now(timezone.utc)))
        elif (old.subject_id, old.signal, old.aggregation_kind, old.aggregation_version) != (
                a.subject_id, a.signal, a.aggregation_kind, a.aggregation_version):
            raise ValueError("aggregate generation scope/version mismatch")
        elif not old.requested_start_date <= a.local_date <= old.requested_end_date:
            # ``legacy-vN`` is the adapter's compatibility generation for old
            # callers that never declared a rebuild range.  It alone may grow
            # while active; an explicit generation's requested range is an
            # immutable publication boundary.
            if old.status != "active" or gid != f"legacy-v{a.aggregation_version}":
                raise ValueError("aggregate row falls outside generation requested range")
            start = min(old.requested_start_date, a.local_date)
            end = max(old.requested_end_date, a.local_date)
            required = {start + timedelta(days=i) for i in range((end - start).days + 1)}
            accounted = set(old.accounted_dates) | {a.local_date}
            gaps = required - accounted
            old = replace(
                old, requested_start_date=start, requested_end_date=end,
                completeness="incomplete" if gaps or old.incomplete_dates else old.completeness,
                accounted_dates=tuple(sorted(accounted)),
                incomplete_dates=tuple(sorted(set(old.incomplete_dates) | gaps)),
                incomplete_reasons=tuple(sorted(set(old.incomplete_reasons)
                                                | ({"unaccounted_bootstrap_gap"} if gaps else set()))),
                updated_at=a.updated_at or old.updated_at)
            self._q(
                """UPDATE perceptkit_aggregate_generation SET requested_start_date=%s,
                   requested_end_date=%s,completeness=%s,accounted_dates=%s::jsonb,
                   incomplete_dates=%s::jsonb,incomplete_reasons=%s::jsonb,updated_at=%s
                   WHERE subject_id=%s AND signal=%s AND aggregation_kind=%s AND generation_id=%s""",
                (old.requested_start_date, old.requested_end_date, old.completeness,
                 _j([d.isoformat() for d in old.accounted_dates]),
                 _j([d.isoformat() for d in old.incomplete_dates]), _j(old.incomplete_reasons),
                 old.updated_at, old.subject_id, old.signal, old.aggregation_kind, old.generation_id))
        if a.local_date in old.incomplete_dates or a.completeness == "incomplete":
            reasons = set(a.incomplete_reasons)
            if a.local_date in old.incomplete_dates:
                reasons.update(old.incomplete_reasons)
            a = replace(a, completeness="incomplete", incomplete_reasons=tuple(sorted(reasons)))
        return a, old

    def put_aggregate(self, aggregate: DailyAggregate) -> None:
        with self.transaction():
            if (not self._mutation_owners and not self._try_internal_lock(
                    aggregate_generation_key(aggregate.subject_id, aggregate.signal,
                                             aggregate.aggregation_kind))):
                raise RetryableMutationError("aggregate generation is owned by another operation")
            a, _ = self._bootstrap_generation(aggregate)
            self._q(
                """INSERT INTO perceptkit_daily_aggregate
                   (subject_id,signal,local_date,aggregation_kind,aggregation_version,
                    generation_id,typed_aggregate,completeness,incomplete_reasons,
                    timezone_attribution,source_coverage,updated_at,version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s::jsonb,%s,0)
                   ON CONFLICT (subject_id,signal,local_date,aggregation_kind,
                                aggregation_version,generation_id)
                   DO UPDATE SET typed_aggregate=EXCLUDED.typed_aggregate,
                     completeness=EXCLUDED.completeness,
                     incomplete_reasons=EXCLUDED.incomplete_reasons,
                     timezone_attribution=EXCLUDED.timezone_attribution,
                     source_coverage=EXCLUDED.source_coverage,updated_at=EXCLUDED.updated_at,
                     version=perceptkit_daily_aggregate.version+1""",
                (a.subject_id, a.signal, a.local_date, a.aggregation_kind,
                 a.aggregation_version, a.generation_id, _j(a.typed_aggregate),
                 a.completeness, _j(a.incomplete_reasons), a.timezone_attribution,
                 _j(a.source_coverage), a.updated_at))

    def compare_and_put_aggregate(self, aggregate: DailyAggregate, *, expected_version):
        with self.transaction():
            if (not self._mutation_owners and not self._try_internal_lock(
                    aggregate_generation_key(aggregate.subject_id, aggregate.signal,
                                             aggregate.aggregation_kind))):
                raise RetryableMutationError("aggregate generation is owned by another operation")
            a, _ = self._bootstrap_generation(aggregate)
            if expected_version < 0:
                return bool(self._q(
                    """INSERT INTO perceptkit_daily_aggregate
                       (subject_id,signal,local_date,aggregation_kind,aggregation_version,
                        generation_id,typed_aggregate,completeness,incomplete_reasons,
                        timezone_attribution,source_coverage,updated_at,version)
                       VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s::jsonb,%s,0)
                       ON CONFLICT DO NOTHING RETURNING 1""",
                    (a.subject_id, a.signal, a.local_date, a.aggregation_kind,
                     a.aggregation_version, a.generation_id, _j(a.typed_aggregate),
                     a.completeness, _j(a.incomplete_reasons), a.timezone_attribution,
                     _j(a.source_coverage), a.updated_at)))
            return bool(self._q(
                """UPDATE perceptkit_daily_aggregate SET typed_aggregate=%s::jsonb,
                   completeness=%s,incomplete_reasons=%s::jsonb,timezone_attribution=%s,
                   source_coverage=%s::jsonb,updated_at=%s,version=version+1
                   WHERE subject_id=%s AND signal=%s AND local_date=%s
                     AND aggregation_kind=%s AND aggregation_version=%s AND generation_id=%s
                     AND version=%s RETURNING 1""",
                (_j(a.typed_aggregate), a.completeness, _j(a.incomplete_reasons),
                 a.timezone_attribution, _j(a.source_coverage), a.updated_at,
                 a.subject_id, a.signal, a.local_date, a.aggregation_kind,
                 a.aggregation_version, a.generation_id, expected_version)))

    def activate_aggregate_generation(self, *, subject_id, signal, aggregation_kind,
                                      generation_id, expected_active_generation_id,
                                      activated_at):
        with self.transaction():
            if (not self._mutation_owners and not self._try_internal_lock(
                    aggregate_generation_key(subject_id, signal, aggregation_kind))):
                raise RetryableMutationError("aggregate generation is owned by another operation")
            pointer = self._q(
                "SELECT generation_id FROM perceptkit_active_aggregate_generation "
                "WHERE subject_id=%s AND signal=%s AND aggregation_kind=%s FOR UPDATE",
                (subject_id, signal, aggregation_kind))
            actual = pointer[0][0] if pointer else None
            if actual != expected_active_generation_id:
                return False
            g = self.get_aggregate_generation(subject_id=subject_id, signal=signal,
                                              aggregation_kind=aggregation_kind,
                                              generation_id=generation_id)
            if g is None or g.status != "complete" or g.completeness != "complete":
                return False
            required = {g.requested_start_date + timedelta(days=i)
                        for i in range((g.requested_end_date - g.requested_start_date).days + 1)}
            if set(g.accounted_dates) != required or g.incomplete_dates:
                return False
            if actual is not None:
                old = self.get_aggregate_generation(
                    subject_id=subject_id, signal=signal, aggregation_kind=aggregation_kind,
                    generation_id=actual)
                if old is None or g.requested_start_date > old.requested_start_date \
                        or g.requested_end_date < old.requested_end_date:
                    return False
            # Scan the generation's entire durable row set.  A range-bounded
            # read would hide a corrupt/out-of-contract row just outside the
            # candidate window and incorrectly allow publication.
            candidate_rows = [self._aggregate(r) for r in self._q(
                """SELECT subject_id,signal,local_date,aggregation_kind,
                          aggregation_version,generation_id,typed_aggregate,completeness,
                          incomplete_reasons,timezone_attribution,source_coverage,updated_at,
                          version FROM perceptkit_daily_aggregate
                   WHERE subject_id=%s AND signal=%s AND aggregation_kind=%s
                     AND generation_id=%s ORDER BY local_date,aggregation_version""",
                (subject_id, signal, aggregation_kind, generation_id),
            )]
            if ({r.local_date for r in candidate_rows} != required
                    or any(r.completeness != "complete"
                           or r.aggregation_version != g.aggregation_version
                           or r.subject_id != subject_id or r.signal != signal
                           or r.aggregation_kind != aggregation_kind for r in candidate_rows)):
                return False
            if actual is None:
                changed = self._q(
                    """INSERT INTO perceptkit_active_aggregate_generation
                       (subject_id,signal,aggregation_kind,generation_id,activated_at)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1""",
                    (subject_id, signal, aggregation_kind, generation_id, activated_at))
            else:
                changed = self._q(
                    """UPDATE perceptkit_active_aggregate_generation
                       SET generation_id=%s,activated_at=%s
                       WHERE subject_id=%s AND signal=%s AND aggregation_kind=%s
                         AND generation_id=%s RETURNING 1""",
                    (generation_id, activated_at, subject_id, signal,
                     aggregation_kind, actual))
            if not changed:
                return False
            if actual is not None:
                self._q(
                    "UPDATE perceptkit_aggregate_generation SET status='complete' "
                    "WHERE subject_id=%s AND signal=%s AND aggregation_kind=%s AND generation_id=%s",
                    (subject_id, signal, aggregation_kind, actual))
            self._q(
                """UPDATE perceptkit_aggregate_generation SET status='active',
                   activated_at=%s,updated_at=%s WHERE subject_id=%s AND signal=%s
                   AND aggregation_kind=%s AND generation_id=%s""",
                (activated_at, activated_at, subject_id, signal,
                 aggregation_kind, generation_id))
            return True

    def mark_active_aggregate_incomplete(self, *, subject_id, signal, aggregation_kind,
                                         local_date, reason, updated_at):
        with self.transaction():
            active = self.get_active_aggregate_generation(
                subject_id=subject_id, signal=signal, aggregation_kind=aggregation_kind)
            if active is None:
                return False
            incomplete_dates = tuple(sorted(set(active.incomplete_dates) | {local_date}))
            reasons = tuple(sorted(set(active.incomplete_reasons) | {reason}))
            self._q(
                """UPDATE perceptkit_aggregate_generation SET completeness='incomplete',
                   requested_start_date=LEAST(requested_start_date,%s),
                   requested_end_date=GREATEST(requested_end_date,%s),
                   incomplete_dates=%s::jsonb,incomplete_reasons=%s::jsonb,updated_at=%s
                   WHERE subject_id=%s AND signal=%s AND aggregation_kind=%s AND generation_id=%s""",
                (local_date, local_date, _j([d.isoformat() for d in incomplete_dates]),
                 _j(reasons), updated_at, subject_id, signal, aggregation_kind,
                 active.generation_id))
            self._q(
                """UPDATE perceptkit_daily_aggregate SET completeness='incomplete',
                   incomplete_reasons=%s::jsonb,updated_at=%s
                   WHERE subject_id=%s AND signal=%s AND aggregation_kind=%s
                     AND generation_id=%s AND local_date=%s""",
                (_j(reasons), updated_at, subject_id, signal, aggregation_kind,
                 active.generation_id, local_date))
            return True

    def account_active_aggregate_range(self, *, subject_id, signal, aggregation_kind,
                                       start_date, end_date, updated_at):
        if end_date < start_date:
            raise ValueError("aggregate accounted range end precedes start")
        with self.transaction():
            active = self.get_active_aggregate_generation(
                subject_id=subject_id, signal=signal, aggregation_kind=aggregation_kind)
            if active is None:
                return
            accounted = set(active.accounted_dates)
            accounted.update(start_date + timedelta(days=i)
                             for i in range((end_date - start_date).days + 1))
            self._q(
                """UPDATE perceptkit_aggregate_generation
                   SET requested_start_date=LEAST(requested_start_date,%s),
                       requested_end_date=GREATEST(requested_end_date,%s),
                       accounted_dates=%s::jsonb,updated_at=%s
                   WHERE subject_id=%s AND signal=%s AND aggregation_kind=%s AND generation_id=%s""",
                (start_date, end_date, _j([d.isoformat() for d in sorted(accounted)]),
                 updated_at, subject_id, signal, aggregation_kind, active.generation_id))

    def delete_aggregates(self, *, subject_id, signal, before) -> int:
        with self.transaction():
            removed = 0
            while True:
                with self.conn.cursor() as cur:
                    self._fence()
                    cur.execute(
                        "DELETE FROM perceptkit_daily_aggregate WHERE ctid IN ("
                        " SELECT ctid FROM perceptkit_daily_aggregate WHERE subject_id=%s"
                        " AND signal=%s AND local_date < %s LIMIT %s)",
                        (subject_id, signal, before, _SWEEP_MAX_ROWS))
                    batch = cur.rowcount
                removed += batch
                if batch < _SWEEP_MAX_ROWS:
                    break
            active_rows = self._q(
                "SELECT aggregation_kind,generation_id FROM perceptkit_active_aggregate_generation "
                "WHERE subject_id=%s AND signal=%s FOR UPDATE", (subject_id, signal))
            for kind, gid in active_rows:
                g = self.get_aggregate_generation(subject_id=subject_id, signal=signal,
                                                  aggregation_kind=kind, generation_id=gid)
                remaining = self._q(
                    """SELECT local_date,BOOL_OR(completeness='incomplete')
                       FROM perceptkit_daily_aggregate
                       WHERE subject_id=%s AND signal=%s AND aggregation_kind=%s
                         AND generation_id=%s GROUP BY local_date ORDER BY local_date""",
                    (subject_id, signal, kind, gid))
                actual_dates = {row[0] for row in remaining}
                retained_accounted = {day for day in g.accounted_dates if day >= before}
                retained_incomplete = {day for day in g.incomplete_dates if day >= before}
                scope = actual_dates | retained_accounted | retained_incomplete
                if not scope:
                    self._q(
                        "DELETE FROM perceptkit_active_aggregate_generation WHERE subject_id=%s "
                        "AND signal=%s AND aggregation_kind=%s", (subject_id, signal, kind))
                    self._q(
                        "UPDATE perceptkit_aggregate_generation SET status='complete' WHERE "
                        "subject_id=%s AND signal=%s AND aggregation_kind=%s AND generation_id=%s",
                        (subject_id, signal, kind, gid))
                    continue
                start, end = min(scope), max(scope)
                accounted = actual_dates | retained_accounted
                required = {start + timedelta(days=i)
                            for i in range((end - start).days + 1)}
                incomplete = tuple(sorted(
                    retained_incomplete
                    | {row[0] for row in remaining if row[1]}
                    | (required - accounted - retained_incomplete)))
                reasons = set(g.incomplete_reasons) if incomplete else set()
                if required - accounted - retained_incomplete:
                    reasons.add("retention_remaining_gap")
                self._q(
                    """UPDATE perceptkit_aggregate_generation SET requested_start_date=%s,
                       requested_end_date=%s,
                       accounted_dates=%s::jsonb,incomplete_dates=%s::jsonb,completeness=%s,
                       incomplete_reasons=%s::jsonb WHERE subject_id=%s AND signal=%s
                       AND aggregation_kind=%s AND generation_id=%s""",
                    (start, end, _j([d.isoformat() for d in sorted(accounted)]),
                     _j([d.isoformat() for d in incomplete]),
                     "incomplete" if incomplete else "complete",
                     _j(sorted(reasons)),
                     subject_id, signal, kind, gid))
            return removed

    # -- Durable identities ----------------------------------------------

    def remember_identity(self, identity: DurableDedupeIdentity) -> bool:
        i = identity
        return bool(self._q(
            """INSERT INTO perceptkit_dedupe_identity
               (subject_id,signal,source,digest,first_applied_at,aggregate_scope,
                retain_until,fact_key,source_revision_value,semantic_digest,
                legacy_content_digest,effective_local_date,dimension_key)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
               ON CONFLICT (subject_id,signal,source,digest) DO NOTHING RETURNING 1""",
            (i.subject_id, i.signal, i.source, i.source_event_identity_digest,
             i.first_applied_at, i.aggregate_scope, i.retain_until, i.fact_key,
             _j(i.source_revision), i.semantic_digest, i.legacy_content_digest,
             i.effective_local_date, i.dimension_key)))

    def has_seen_identity(self, *, subject_id, signal, source, digest):
        return bool(self._q(
            "SELECT 1 FROM perceptkit_dedupe_identity WHERE subject_id=%s AND signal=%s "
            "AND source=%s AND digest=%s", (subject_id, signal, source, digest)))

    @staticmethod
    def _identity(r: tuple) -> DurableDedupeIdentity:
        return DurableDedupeIdentity(
            subject_id=r[0], signal=r[1], source=r[2],
            source_event_identity_digest=r[3], first_applied_at=r[4],
            aggregate_scope=r[5], retain_until=r[6], fact_key=r[7],
            source_revision=r[8], semantic_digest=r[9], legacy_content_digest=r[10],
            effective_local_date=r[11], dimension_key=r[12])

    def list_identities(self, *, subject_id, signal, source=None, fact_key=None):
        sql = ["""SELECT subject_id,signal,source,digest,first_applied_at,aggregate_scope,
                  retain_until,fact_key,source_revision_value,semantic_digest,
                  legacy_content_digest,effective_local_date,dimension_key
                  FROM perceptkit_dedupe_identity WHERE subject_id=%s AND signal=%s"""]
        params: list[Any] = [subject_id, signal]
        if source is not None:
            sql.append("AND source=%s"); params.append(source)
        if fact_key is not None:
            sql.append("AND (fact_key IS NULL OR fact_key=%s)"); params.append(fact_key)
        sql.append("ORDER BY source,digest")
        return [self._identity(r) for r in self._q(" ".join(sql), params)]

    def backfill_identity(self, identity: DurableDedupeIdentity) -> None:
        rows = self.list_identities(subject_id=identity.subject_id, signal=identity.signal,
                                    source=identity.source)
        existing = next((x for x in rows if x.source_event_identity_digest
                         == identity.source_event_identity_digest), None)
        if existing is None:
            raise ValueError("cannot backfill an unseen identity")
        candidate = existing
        for field in ("fact_key", "source_revision", "semantic_digest",
                      "legacy_content_digest", "effective_local_date", "dimension_key"):
            old, new = getattr(candidate, field), getattr(identity, field)
            if (field == "effective_local_date" and old is None and new is not None
                    and existing.semantic_digest is not None):
                raise ValueError("conflicting durable identity metadata")
            if old is None and new is not None:
                candidate = replace(candidate, **{field: new})
            elif old is not None and new != old:
                raise ValueError("conflicting durable identity metadata")
        if candidate != identity:
            raise ValueError("conflicting durable identity metadata")
        self._q(
            """UPDATE perceptkit_dedupe_identity SET fact_key=%s,
               source_revision_value=%s::jsonb,semantic_digest=%s,legacy_content_digest=%s,
               effective_local_date=%s,dimension_key=%s
               WHERE subject_id=%s AND signal=%s AND source=%s AND digest=%s""",
            (identity.fact_key, _j(identity.source_revision), identity.semantic_digest,
             identity.legacy_content_digest, identity.effective_local_date,
             identity.dimension_key, identity.subject_id, identity.signal,
             identity.source, identity.source_event_identity_digest))

    # -- Rule state and event outbox -------------------------------------

    def get_rule_state(self, *, subject_id, definition_id, scope_key):
        rows = self._q(
            "SELECT state FROM perceptkit_rule_state WHERE subject_id=%s "
            "AND definition_id=%s AND scope_key=%s",
            (subject_id, definition_id, scope_key))
        return rows[0][0] if rows else None

    def list_rule_states(self, *, subject_id):
        return [(r[0], r[1], r[2]) for r in self._q(
            "SELECT definition_id,scope_key,state FROM perceptkit_rule_state "
            "WHERE subject_id=%s ORDER BY definition_id,scope_key", (subject_id,))]

    def put_rule_state(self, *, subject_id, definition_id, scope_key, state):
        self._q(
            """INSERT INTO perceptkit_rule_state (subject_id,definition_id,scope_key,state)
               VALUES (%s,%s,%s,%s::jsonb) ON CONFLICT (subject_id,definition_id,scope_key)
               DO UPDATE SET state=EXCLUDED.state""",
            (subject_id, definition_id, scope_key, _j(state)))

    @staticmethod
    def _event_signal(entry: EventOutboxEntry) -> str:
        signal = entry.fact_snapshot.get("signal")
        if isinstance(signal, str):
            return signal
        if entry.fact_dependencies:
            value = entry.fact_dependencies[0].get("signal")
            if isinstance(value, str):
                return value
        return ""

    def enqueue_event(self, entry: EventOutboxEntry) -> bool:
        e = entry
        return bool(self._q(
            """INSERT INTO perceptkit_event_outbox
               (event_id,subject_id,definition_id,definition_version,event_type,
                occurred_at,detected_at,delivery_state,attempt_count,fact_snapshot,
                next_attempt_at,claim_token,claimed_by,claim_expires_at,source,
                source_event_id,dedupe_key,budget_reservation_id,created_at,
                fact_dependencies,fact_dependencies_complete,dispatch_started_at,
                invalidated_at,invalidation_reason,signal)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,
                       %s,%s,%s::jsonb,%s,%s,%s,%s,%s)
               ON CONFLICT (event_id) DO NOTHING RETURNING 1""",
            (e.event_id, e.subject_id, e.definition_id, e.definition_version,
             e.event_type, e.occurred_at, e.detected_at, e.delivery_state,
             e.attempt_count, _j(e.fact_snapshot), e.next_attempt_at, e.claim_token,
             e.lease_owner, e.lease_expires_at, e.source, e.source_event_id,
             e.dedupe_key, e.budget_reservation_id, e.created_at,
             _j(e.fact_dependencies), e.fact_dependencies_complete,
             e.dispatch_started_at, e.invalidated_at, e.invalidation_reason,
             self._event_signal(e))))

    @staticmethod
    def _outbox(r: tuple) -> EventOutboxEntry:
        return EventOutboxEntry(
            event_id=r[0], subject_id=r[1], definition_id=r[2], definition_version=r[3],
            event_type=r[4], occurred_at=r[5], detected_at=r[6], delivery_state=r[7],
            attempt_count=r[8], fact_snapshot=r[9], next_attempt_at=r[10],
            claim_token=r[11], lease_owner=r[12], lease_expires_at=r[13], source=r[14],
            source_event_id=r[15], dedupe_key=r[16], budget_reservation_id=r[17],
            created_at=r[18], fact_dependencies=tuple(r[19] or ()),
            fact_dependencies_complete=r[20], dispatch_started_at=r[21],
            invalidated_at=r[22], invalidation_reason=r[23])

    @staticmethod
    def _outbox_columns() -> str:
        return ("event_id,subject_id,definition_id,definition_version,event_type,"
                "occurred_at,detected_at,delivery_state,attempt_count,fact_snapshot,"
                "next_attempt_at,claim_token,claimed_by,claim_expires_at,source,"
                "source_event_id,dedupe_key,budget_reservation_id,created_at,"
                "fact_dependencies,fact_dependencies_complete,dispatch_started_at,"
                "invalidated_at,invalidation_reason")

    def claim_pending_event(self, *, worker_id, now, lease_seconds):
        with self.transaction():
            # An external call may have started before the worker vanished.  An
            # expired start is uncertain, never a blind retry.
            self._q(
                """UPDATE perceptkit_event_outbox SET delivery_state='unknown'
                   WHERE delivery_state='claimed' AND dispatch_started_at IS NOT NULL
                     AND claim_expires_at <= %s""", (now,))
            rows = self._q(
                """SELECT event_id,subject_id,signal FROM perceptkit_event_outbox
                   WHERE legacy_scope_unknown=FALSE
                     AND invalidated_at IS NULL AND dispatch_started_at IS NULL AND (
                     (delivery_state='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=%s))
                     OR (delivery_state='claimed' AND claim_expires_at<=%s))
                   ORDER BY detected_at,event_id LIMIT 1""", (now, now))
            if not rows:
                return None
            event_id = rows[0][0]
            if not self._try_internal_lock(event_key(rows[0][1], rows[0][2])):
                return None
            live = self._q(
                """SELECT 1 FROM perceptkit_event_outbox WHERE event_id=%s
                   AND legacy_scope_unknown=FALSE
                   AND invalidated_at IS NULL AND dispatch_started_at IS NULL AND (
                     (delivery_state='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=%s))
                     OR (delivery_state='claimed' AND claim_expires_at<=%s)) FOR UPDATE""",
                (event_id, now, now))
            if not live:
                return None
            token = f"{worker_id}:{uuid.uuid4().hex}"
            expires = now + timedelta(seconds=lease_seconds)
            claimed = self._q(
                f"""UPDATE perceptkit_event_outbox SET delivery_state='claimed',
                    attempt_count=attempt_count+1,claim_token=%s,claimed_by=%s,
                    claim_expires_at=%s,budget_reservation_id='resv_'||event_id||'_'||(attempt_count+1)
                    WHERE event_id=%s RETURNING {self._outbox_columns()}""",
                (token, worker_id, expires, event_id))
            return self._outbox(claimed[0]) if claimed else None

    def begin_event_dispatch(self, *, event_id, claim_token, now):
        with self.transaction():
            scope = self._q(
                "SELECT subject_id,signal,legacy_scope_unknown "
                "FROM perceptkit_event_outbox WHERE event_id=%s", (event_id,))
            if (not scope or scope[0][2]
                    or not self._try_internal_lock(event_key(scope[0][0], scope[0][1]))):
                return None
            rows = self._q(
                f"""UPDATE perceptkit_event_outbox SET dispatch_started_at=%s
                    WHERE event_id=%s AND delivery_state='claimed' AND claim_token=%s
                      AND claim_token IS NOT NULL AND invalidated_at IS NULL
                      AND dispatch_started_at IS NULL AND claim_expires_at>%s
                    RETURNING {self._outbox_columns()}""",
                (now, event_id, claim_token, now))
            return self._outbox(rows[0]) if rows else None

    def mark_dispatch_unknown(self, *, event_id, claim_token):
        with self.transaction():
            scope = self._q("SELECT subject_id,signal FROM perceptkit_event_outbox WHERE event_id=%s",
                            (event_id,))
            if not scope or not self._try_internal_lock(event_key(scope[0][0], scope[0][1])):
                return False
            return bool(self._q(
                """UPDATE perceptkit_event_outbox SET delivery_state='unknown'
                   WHERE event_id=%s AND claim_token=%s AND claim_token IS NOT NULL
                     AND dispatch_started_at IS NOT NULL AND delivery_state IN ('claimed','unknown')
                   RETURNING 1""", (event_id, claim_token)))

    def scrub_event_snapshots(self, *, subject_id, signal, source, source_event_id,
                              now, reason="fact_retracted", observation_ids=None,
                              canonical_fact_key=None):
        with self.transaction():
            if not self._try_internal_lock(event_key(subject_id, signal)):
                raise RetryableMutationError("event scope is owned by another operation")
            rows = self._q(
                f"SELECT {self._outbox_columns()},legacy_scope_unknown "
                "FROM perceptkit_event_outbox "
                "WHERE subject_id=%s AND invalidated_at IS NULL "
                "AND (signal=%s OR legacy_scope_unknown=TRUE) FOR UPDATE",
                (subject_id, signal))
            hit = 0
            for raw in rows:
                entry = self._outbox(raw)
                legacy_scope_unknown = raw[24]
                matches = (legacy_scope_unknown
                           or not entry.fact_dependencies_complete or not entry.fact_dependencies
                           or any(
                               (ref.get("subject_id"), ref.get("signal"), ref.get("source"),
                                ref.get("source_event_id")) ==
                               (subject_id, signal, source, source_event_id)
                               and (observation_ids is None
                                    or ref.get("observation_id") in observation_ids)
                               and (canonical_fact_key is None
                                    or ref.get("fact_key") == canonical_fact_key)
                               for ref in entry.fact_dependencies))
                if not matches:
                    continue
                audit_keys = ("event_id", "definition_id", "definition_version", "subject_id",
                              "type", "signal", "field", "occurred_at", "received_at",
                              "schema_version")
                snap = {key: entry.fact_snapshot[key] for key in audit_keys
                        if key in entry.fact_snapshot}
                snap.update(previous=None, current=None, retracted=reason == "fact_retracted",
                            invalidated=True,
                            context={"scope": entry.fact_snapshot.get("context", {}).get("scope")})
                state = entry.delivery_state
                if state in (_delivery.PENDING, _delivery.CLAIMED):
                    state = _delivery.UNKNOWN if entry.dispatch_started_at else _delivery.INVALIDATED
                keep_claim = state == _delivery.UNKNOWN
                keep_budget = state in (_delivery.UNKNOWN, _delivery.DELIVERED)
                self._q(
                    """UPDATE perceptkit_event_outbox SET fact_snapshot=%s::jsonb,
                       delivery_state=%s,invalidated_at=%s,invalidation_reason=%s,
                       claim_token=%s,claimed_by=%s,claim_expires_at=%s,budget_reservation_id=%s
                       WHERE event_id=%s""",
                    (_j(snap), state, now, reason,
                     entry.claim_token if keep_claim else None,
                     entry.lease_owner if keep_claim else None,
                     entry.lease_expires_at if keep_claim else None,
                     entry.budget_reservation_id if keep_budget else None,
                     entry.event_id))
                hit += 1
            return hit

    def record_wake_receipt(self, *, receipt: WakeReceipt, next_state,
                            claim_token=None, next_attempt_at=None):
        with self.transaction():
            rows = self._q(
                f"SELECT {self._outbox_columns()} FROM perceptkit_event_outbox "
                "WHERE event_id=%s", (receipt.event_id,))
            if not rows:
                raise KeyError(f"unknown event_id {receipt.event_id!r}")
            entry = self._outbox(rows[0])
            if not self._try_internal_lock(event_key(
                    entry.subject_id, self._event_signal(entry))):
                raise RetryableMutationError("event scope is owned by another operation")
            rows = self._q(
                f"SELECT {self._outbox_columns()} FROM perceptkit_event_outbox "
                "WHERE event_id=%s FOR UPDATE", (receipt.event_id,))
            entry = self._outbox(rows[0])
            self._q(
                """INSERT INTO perceptkit_wake_receipt
                   (event_id,attempt_id,status,received_at,runtime_ref,reason)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (receipt.event_id, receipt.attempt_id, receipt.status,
                 receipt.received_at, receipt.runtime_ref, receipt.reason))
            if (not claim_token or entry.claim_token != claim_token
                    or entry.delivery_state not in (_delivery.CLAIMED, _delivery.UNKNOWN)):
                return False
            if entry.invalidated_at is not None and next_state in (
                    _delivery.PENDING, _delivery.DEAD_LETTER):
                next_state = _delivery.INVALIDATED
            _delivery.assert_transition(entry.delivery_state, next_state)
            self._q(
                """UPDATE perceptkit_event_outbox SET delivery_state=%s,next_attempt_at=%s,
                   claimed_by=NULL,claim_expires_at=NULL,claim_token=NULL,
                   dispatch_started_at=CASE WHEN %s='pending' THEN NULL ELSE dispatch_started_at END,
                   budget_reservation_id=CASE WHEN %s='delivered' THEN budget_reservation_id ELSE NULL END
                   WHERE event_id=%s AND claim_token=%s""",
                (next_state, next_attempt_at, next_state, next_state,
                 receipt.event_id, claim_token))
            return next_state

    def list_pending_events(self, *, subject_id=None, limit=100):
        sql = [f"SELECT {self._outbox_columns()} FROM perceptkit_event_outbox "
               "WHERE delivery_state IN ('pending','claimed')"]
        params: list[Any] = []
        if subject_id is not None:
            sql.append("AND subject_id=%s"); params.append(subject_id)
        sql.append("ORDER BY detected_at,event_id LIMIT %s"); params.append(limit)
        return [self._outbox(r) for r in self._q(" ".join(sql), params)]

    def list_events(self, *, subject_id, delivery_states=None, event_type=None,
                    start=None, end=None, limit=50, offset=0):
        sql = [f"SELECT {self._outbox_columns()} FROM perceptkit_event_outbox WHERE subject_id=%s"]
        params: list[Any] = [subject_id]
        if delivery_states is not None:
            sql.append("AND delivery_state=ANY(%s)"); params.append(list(delivery_states))
        if event_type is not None:
            sql.append("AND event_type=%s"); params.append(event_type)
        if start is not None:
            sql.append("AND occurred_at >= %s"); params.append(start)
        if end is not None:
            sql.append("AND occurred_at <= %s"); params.append(end)
        sql.append("ORDER BY occurred_at DESC,event_id DESC LIMIT %s OFFSET %s")
        params.extend((limit, offset))
        return [self._outbox(r) for r in self._q(" ".join(sql), params)]

    # -- Source mirrors --------------------------------------------------

    def upsert_calendar_events(self, *, subject_id, events):
        for e in events:
            if e.subject_id != subject_id:
                raise ValueError("calendar subject does not match operation subject")
            self._q(
                """INSERT INTO perceptkit_calendar_mirror
                   (subject_id,source,source_account_id,source_calendar_id,source_event_id,
                    event_fields,source_revision,recurrence_identity,source_created_at,
                    source_updated_at,last_seen_sync_id,updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (subject_id,source,source_account_id,source_calendar_id,source_event_id)
                   DO UPDATE SET event_fields=EXCLUDED.event_fields,
                     source_revision=EXCLUDED.source_revision,
                     recurrence_identity=EXCLUDED.recurrence_identity,
                     source_created_at=EXCLUDED.source_created_at,
                     source_updated_at=EXCLUDED.source_updated_at,
                     last_seen_sync_id=EXCLUDED.last_seen_sync_id,updated_at=EXCLUDED.updated_at""",
                (e.subject_id, e.source, e.source_account_id, e.source_calendar_id,
                 e.source_event_id, _j(e.event_fields), _rev(e.source_revision),
                 e.recurrence_identity, e.source_created_at, e.source_updated_at,
                 e.last_seen_sync_id, e.updated_at))

    def upsert_reminders(self, *, subject_id, items):
        for r in items:
            if r.subject_id != subject_id:
                raise ValueError("reminder subject does not match operation subject")
            self._q(
                """INSERT INTO perceptkit_reminder_mirror
                   (subject_id,source,source_account_id,source_list_id,source_reminder_id,
                    reminder_fields,source_revision,last_seen_sync_id,updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                   ON CONFLICT (subject_id,source,source_account_id,source_list_id,source_reminder_id)
                   DO UPDATE SET reminder_fields=EXCLUDED.reminder_fields,
                     source_revision=EXCLUDED.source_revision,
                     last_seen_sync_id=EXCLUDED.last_seen_sync_id,updated_at=EXCLUDED.updated_at""",
                (r.subject_id, r.source, r.source_account_id, r.source_list_id,
                 r.source_reminder_id, _j(r.reminder_fields), _rev(r.source_revision),
                 r.last_seen_sync_id, r.updated_at))

    def list_calendar_events(self, *, subject_id, start=None, end=None, limit=50, offset=0):
        sql = [f"SELECT {_CAL_COLS} FROM perceptkit_calendar_mirror WHERE subject_id=%s"]
        params: list[Any] = [subject_id]
        if start is not None:
            sql.append(f"AND ({_CAL_AT} IS NULL OR {_CAL_AT} >= %s)"); params.append(start)
        if end is not None:
            sql.append(f"AND ({_CAL_AT} IS NULL OR {_CAL_AT} <= %s)"); params.append(end)
        sql.append(
            f"ORDER BY {_CAL_AT} NULLS LAST,source,source_account_id,"
            "source_calendar_id,source_event_id LIMIT %s OFFSET %s")
        params.extend((limit, offset))
        out = []
        for r in self._q(" ".join(sql), params):
            fields = dict(r[5])
            at = fields.get("start_at")
            if isinstance(at, str):
                try:
                    fields["start_at"] = datetime.fromisoformat(at)
                except ValueError:
                    pass
            out.append(CalendarEventMirror(
                subject_id=r[0], source=r[1], source_account_id=r[2],
                source_calendar_id=r[3], source_event_id=r[4], event_fields=fields,
                source_revision=r[6], recurrence_identity=r[7], source_created_at=r[8],
                source_updated_at=r[9], last_seen_sync_id=r[10], updated_at=r[11]))
        return out

    def list_reminders(self, *, subject_id, include_completed=False, limit=50, offset=0):
        sql = [f"SELECT {_REM_COLS} FROM perceptkit_reminder_mirror WHERE subject_id=%s"]
        params: list[Any] = [subject_id]
        if not include_completed:
            sql.append("AND COALESCE((reminder_fields->>'is_completed')::bool,false)=false")
        sql.append(
            f"ORDER BY {_REM_AT} NULLS LAST,source,source_account_id,"
            "source_list_id,source_reminder_id LIMIT %s OFFSET %s")
        params.extend((limit, offset))
        return [ReminderItemMirror(
            subject_id=r[0], source=r[1], source_account_id=r[2], source_list_id=r[3],
            source_reminder_id=r[4], reminder_fields=dict(r[5]), source_revision=r[6],
            last_seen_sync_id=r[7], updated_at=r[8])
            for r in self._q(" ".join(sql), params)]

    def delete_source_items(self, *, subject_id, source, collection_kind, deleted_items):
        if collection_kind not in ("calendar", "reminder"):
            raise ValueError("collection_kind must be calendar or reminder")
        if not deleted_items:
            return 0
        if collection_kind == "calendar":
            table, coll, item = ("perceptkit_calendar_mirror", "source_calendar_id",
                                 "source_event_id")
        else:
            table, coll, item = ("perceptkit_reminder_mirror", "source_list_id",
                                 "source_reminder_id")
        total = 0
        with self.conn.cursor() as cur:
            self._fence()
            for d in deleted_items:
                cur.execute(
                    f"DELETE FROM {table} WHERE subject_id=%s AND source=%s "
                    f"AND source_account_id=%s AND {coll}=%s AND {item}=%s",
                    (subject_id, source, d.source_account_id,
                     d.source_collection_id, d.source_item_id))
                total += cur.rowcount
        return total

    def apply_source_snapshot(self, *, subject_id, source, collection_kind, sync_id,
                              coverage_start, coverage_end, snapshot_kind):
        if snapshot_kind != "full":
            return 0
        if collection_kind == "calendar":
            table, key, fields = "perceptkit_calendar_mirror", "start_at", "event_fields"
        elif collection_kind == "reminder":
            table, key, fields = "perceptkit_reminder_mirror", "due_at", "reminder_fields"
        else:
            raise ValueError("collection_kind must be calendar or reminder")
        with self.conn.cursor() as cur:
            self._fence()
            cur.execute(
                f"""DELETE FROM {table} WHERE subject_id=%s AND source=%s
                    AND last_seen_sync_id IS DISTINCT FROM %s AND ({fields}->>%s) IS NOT NULL
                    AND ({fields}->>%s)::timestamptz BETWEEN %s AND %s""",
                (subject_id, source, sync_id, key, key, coverage_start, coverage_end))
            return cur.rowcount

    def record_retraction(self, retraction):
        return bool(self._q(
            """INSERT INTO perceptkit_retraction
               (subject_id,signal,source,source_event_id,observed_at)
               VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1""",
            (retraction.subject_id, retraction.signal, retraction.source,
             retraction.source_event_id, retraction.observed_at)))

    def list_retractions(self, *, subject_id, signal, source_event_ids=None):
        from perceptkit.contracts.retraction import Retraction
        sql = ["SELECT subject_id,signal,source_event_id,source,observed_at "
               "FROM perceptkit_retraction WHERE subject_id=%s AND signal=%s"]
        params: list[Any] = [subject_id, signal]
        if source_event_ids is not None:
            sql.append("AND source_event_id=ANY(%s)"); params.append(list(source_event_ids))
        sql.append("ORDER BY source,source_event_id")
        return [Retraction(*r) for r in self._q(" ".join(sql), params)]

    def get_sync_state(self, *, subject_id, source, collection_kind):
        rows = self._q(
            f"SELECT {_SYNC_COLS} FROM perceptkit_sync_state WHERE subject_id=%s "
            "AND source=%s AND collection_kind=%s", (subject_id, source, collection_kind))
        if not rows:
            return None
        r = rows[0]
        return SourceSyncState(
            subject_id=r[0], source=r[1], collection_kind=r[2], sync_cursor=r[3],
            coverage_start=r[4], coverage_end=r[5], snapshot_kind=r[6],
            last_attempted_at=r[7], last_successful_sync_at=r[8], last_error_code=r[9])

    def put_sync_state(self, state: SourceSyncState):
        s = state
        self._q(
            f"""INSERT INTO perceptkit_sync_state ({_SYNC_COLS})
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (subject_id,source,collection_kind) DO UPDATE SET
                  sync_cursor=EXCLUDED.sync_cursor,coverage_start=EXCLUDED.coverage_start,
                  coverage_end=EXCLUDED.coverage_end,snapshot_kind=EXCLUDED.snapshot_kind,
                  last_attempted_at=EXCLUDED.last_attempted_at,
                  last_successful_sync_at=EXCLUDED.last_successful_sync_at,
                  last_error_code=EXCLUDED.last_error_code""",
            (s.subject_id, s.source, s.collection_kind, s.sync_cursor,
             s.coverage_start, s.coverage_end, s.snapshot_kind, s.last_attempted_at,
             s.last_successful_sync_at, s.last_error_code))

    # -- Subject purge ---------------------------------------------------

    def purge_subject(self, *, subject_id):
        counts: dict[str, int] = {}
        with self.transaction():
            with self.conn.cursor() as cur:
                self._fence()
                cur.execute(
                    "DELETE FROM perceptkit_wake_receipt WHERE event_id IN "
                    "(SELECT event_id FROM perceptkit_event_outbox WHERE subject_id=%s)",
                    (subject_id,))
                counts["perceptkit_wake_receipt"] = cur.rowcount
            for table in _schema.TABLES:
                if table == "perceptkit_wake_receipt":
                    continue
                with self.conn.cursor() as cur:
                    self._fence()
                    cur.execute(f"DELETE FROM {table} WHERE subject_id=%s", (subject_id,))
                    counts[table] = cur.rowcount
        return counts


__all__ = ["PostgresStorage"]
