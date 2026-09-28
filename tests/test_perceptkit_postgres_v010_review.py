"""Independent-review regressions for the PerceptKit v0.10 PostgreSQL adapter."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import sys
import threading
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
psycopg = pytest.importorskip("psycopg")

from perceptkit.contracts.errors import RetryableMutationError  # noqa: E402
from perceptkit.contracts.mutation import fact_key, rule_key  # noqa: E402
from perceptkit.contracts.records import (  # noqa: E402
    AggregateGeneration,
    CalendarEventMirror,
    DailyAggregate,
    ReminderItemMirror,
)
from perception.perceptkit_adapter import schema  # noqa: E402
from perception.perceptkit_adapter.storage import PostgresStorage  # noqa: E402


DSN = os.environ.get("PERCEPTKIT_TEST_PG")
pytestmark = pytest.mark.skipif(not DSN, reason="requires PERCEPTKIT_TEST_PG")
UTC = timezone.utc
T0 = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
DAY = date(2026, 9, 28)


def connect():
    return psycopg.connect(DSN, autocommit=True)


def storage(conn=None):
    return PostgresStorage(conn or connect())


@pytest.fixture
def clean_v010_review():
    with connect() as conn:
        conn.execute(schema.DDL)
        conn.execute(schema.TRUNCATE)
    yield


def migration_module(name):
    path = Path(__file__).parent.parent / "backend" / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"task7a_review_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def apply_0115_shape(conn):
    for name in (
        "0106_perceptkit_objects", "0107_perceptkit_mirror_source",
        "0108_perceptkit_retraction", "0109_divergence_skew",
        "0110_divergence_observed_at", "0115_outbox_source_fact",
    ):
        conn.execute(migration_module(name)._UP)


def normalized_database_shape(conn, namespace):
    columns = conn.execute(
        """SELECT table_name,column_name,data_type,is_nullable
           FROM information_schema.columns WHERE table_schema=%s
             AND table_name LIKE 'perceptkit_%%'
           ORDER BY table_name,column_name""", (namespace,)).fetchall()

    def normalize(definition):
        definition = definition.replace(f'"{namespace}".', "").replace(
            f"{namespace}.", "")
        definition = re.sub(
            r"CREATE (UNIQUE )?INDEX [^ ]+ ON ",
            lambda match: f"CREATE {match.group(1) or ''}INDEX ON ",
            definition,
        )
        return " ".join(definition.split())

    indexes = conn.execute(
        """SELECT tablename,indexdef FROM pg_indexes
           WHERE schemaname=%s AND tablename LIKE 'perceptkit_%%'
           ORDER BY tablename,indexname""", (namespace,)).fetchall()
    indexes = sorted((table, normalize(definition)) for table, definition in indexes)
    constraints = conn.execute(
        """SELECT rel.relname,con.contype,pg_get_constraintdef(con.oid)
           FROM pg_constraint con
           JOIN pg_class rel ON rel.oid=con.conrelid
           JOIN pg_namespace ns ON ns.oid=rel.relnamespace
           WHERE ns.nspname=%s AND rel.relname LIKE 'perceptkit_%%'
           ORDER BY rel.relname,con.contype,pg_get_constraintdef(con.oid)""",
        (namespace,),
    ).fetchall()
    constraints = sorted(
        (table, kind, normalize(definition))
        for table, kind, definition in constraints
    )
    return columns, indexes, constraints


def generation(generation_id="candidate", *, start=DAY, end=DAY):
    days = tuple(start + timedelta(days=i) for i in range((end - start).days + 1))
    return AggregateGeneration(
        generation_id, "u1", "steps", "daily", 2, start, end,
        status="complete", completeness="complete", accounted_dates=days,
        created_at=T0, updated_at=T0,
    )


def aggregate(local_date=DAY, generation_id="candidate"):
    return DailyAggregate(
        "u1", "steps", local_date, "daily", 2, {"n": 1},
        generation_id=generation_id, updated_at=T0,
    )


def test_declared_generation_rejects_row_outside_requested_range(clean_v010_review):
    store = storage()
    store.put_aggregate_generation(generation())
    with pytest.raises(ValueError, match="requested range"):
        store.put_aggregate(aggregate(DAY + timedelta(days=1)))


def test_activation_scans_all_generation_rows_for_out_of_range_data(clean_v010_review):
    store = storage()
    store.put_aggregate_generation(generation())
    store.put_aggregate(aggregate())
    store._q(
        """INSERT INTO perceptkit_daily_aggregate
           (subject_id,signal,local_date,aggregation_kind,aggregation_version,
            generation_id,typed_aggregate,completeness,incomplete_reasons,
            source_coverage,version)
           VALUES ('u1','steps',%s,'daily',2,'candidate','{}','complete','[]','{}',0)""",
        (DAY + timedelta(days=1),),
    )
    assert store.activate_aggregate_generation(
        subject_id="u1", signal="steps", aggregation_kind="daily",
        generation_id="candidate", expected_active_generation_id=None,
        activated_at=T0,
    ) is False


def test_0117_head_matches_fresh_indexes_and_constraints():
    legacy_schema = f"legacy_review_{uuid.uuid4().hex[:8]}"
    fresh_schema = f"fresh_review_{uuid.uuid4().hex[:8]}"
    migration = migration_module("0116_perceptkit_v010_storage")
    definitions_migration = migration_module("0117_perceptkit_definition_history")
    with connect() as conn:
        conn.execute(f'CREATE SCHEMA "{legacy_schema}"')
        conn.execute(f'CREATE SCHEMA "{fresh_schema}"')
        try:
            conn.execute(f'SET search_path TO "{legacy_schema}"')
            apply_0115_shape(conn)
            conn.execute(migration._UP)
            conn.execute(definitions_migration._UP)
            conn.execute(f'SET search_path TO "{fresh_schema}"')
            conn.execute(schema.DDL)
            assert normalized_database_shape(conn, legacy_schema) == \
                normalized_database_shape(conn, fresh_schema)
        finally:
            conn.execute("SET search_path TO public")
            conn.execute(f'DROP SCHEMA "{legacy_schema}" CASCADE')
            conn.execute(f'DROP SCHEMA "{fresh_schema}" CASCADE')


def test_actual_0115_unknown_signal_event_cannot_race_past_scrub():
    legacy_schema = f"legacy_event_{uuid.uuid4().hex[:8]}"
    migration = migration_module("0116_perceptkit_v010_storage")
    definitions_migration = migration_module("0117_perceptkit_definition_history")
    setup = connect()
    claimant_conn = connect()
    scrubber_conn = connect()
    try:
        setup.execute(f'CREATE SCHEMA "{legacy_schema}"')
        setup.execute(f'SET search_path TO "{legacy_schema}"')
        apply_0115_shape(setup)
        setup.execute(
            """INSERT INTO perceptkit_event_outbox
               (event_id,subject_id,definition_id,definition_version,event_type,
                occurred_at,detected_at,delivery_state,attempt_count,fact_snapshot,
                source,source_event_id)
               VALUES ('legacy-event','u1','weight.changed',1,'weight.changed',
                       %s,%s,'pending',0,'{"current":71}','ios','sample-1')""",
            (T0, T0),
        )
        setup.execute(migration._UP)
        setup.execute(definitions_migration._UP)
        claimant_conn.execute(f'SET search_path TO "{legacy_schema}"')
        scrubber_conn.execute(f'SET search_path TO "{legacy_schema}"')
        claimant = storage(claimant_conn)
        scrubber = storage(scrubber_conn)
        barrier = threading.Barrier(2)
        claimed = []

        def claim():
            barrier.wait()
            claimed.append(claimant.claim_pending_event(
                worker_id="worker", now=T0 + timedelta(seconds=1), lease_seconds=60))

        thread = threading.Thread(target=claim)
        thread.start()
        barrier.wait()
        hit = scrubber.scrub_event_snapshots(
            subject_id="u1", signal="health_weight", source="ios",
            source_event_id="sample-1", now=T0 + timedelta(seconds=1),
        )
        thread.join(5)
        assert claimed == [None]
        assert hit == 1
        stored = scrubber.list_events(subject_id="u1")[0]
        assert stored.delivery_state == "invalidated"
        assert stored.invalidation_reason == "fact_retracted"
        assert stored.fact_dependencies_complete is False
        assert "signal" not in stored.fact_snapshot
        counts = scrubber.purge_subject(subject_id="u1")
        assert counts["perceptkit_event_outbox"] == 1
        assert scrubber.list_events(subject_id="u1") == []
    finally:
        claimant_conn.close()
        scrubber_conn.close()
        setup.execute("SET search_path TO public")
        setup.execute(f'DROP SCHEMA IF EXISTS "{legacy_schema}" CASCADE')
        setup.close()


def test_caught_nested_mutation_failure_poisons_outer_transaction(clean_v010_review):
    writer, observer = storage(), storage()
    with pytest.raises(RetryableMutationError):
        with writer.mutation_transaction() as outer:
            outer.acquire((rule_key("u1", "d", "scope"),))
            writer.put_rule_state(
                subject_id="u1", definition_id="d", scope_key="scope",
                state={"signal": "steps"},
            )
            try:
                with writer.mutation_transaction() as inner:
                    inner.acquire((fact_key("u1", "steps", "ios", "fact-1"),))
            except RetryableMutationError:
                pass
    assert observer.get_rule_state(
        subject_id="u1", definition_id="d", scope_key="scope") is None


def test_retention_drains_more_than_one_batch_and_reconciles_actual_rows(
        clean_v010_review):
    conn = connect()
    store = storage(conn)
    start = DAY - timedelta(days=2001)
    store.put_aggregate_generation(generation("retention", start=start, end=DAY))
    store._q(
        """UPDATE perceptkit_aggregate_generation
           SET status='active',activated_at=%s WHERE subject_id='u1' AND signal='steps'
             AND aggregation_kind='daily' AND generation_id='retention'""", (T0,))
    store._q(
        """INSERT INTO perceptkit_active_aggregate_generation
           (subject_id,signal,aggregation_kind,generation_id,activated_at)
           VALUES ('u1','steps','daily','retention',%s)""", (T0,))
    store._q(
        """INSERT INTO perceptkit_daily_aggregate
           (subject_id,signal,local_date,aggregation_kind,aggregation_version,
            generation_id,typed_aggregate,completeness,incomplete_reasons,
            source_coverage,version)
           SELECT 'u1','steps',day::date,'daily',2,'retention',
                  jsonb_build_object('day',day::date),'complete','[]','{}',0
           FROM generate_series(%s::date,%s::date,interval '1 day') day""",
        (start, DAY),
    )

    assert store.delete_aggregates(subject_id="u1", signal="steps", before=DAY) == 2001
    assert store._q(
        "SELECT count(*) FROM perceptkit_daily_aggregate WHERE local_date < %s",
        (DAY,),
    ) == [(0,)]
    active = store.get_active_aggregate_generation(
        subject_id="u1", signal="steps", aggregation_kind="daily")
    assert active.requested_start_date == DAY
    assert active.requested_end_date == DAY
    assert active.accounted_dates == (DAY,)

    assert store.delete_aggregates(
        subject_id="u1", signal="steps", before=DAY + timedelta(days=1)) == 1
    assert store.get_active_aggregate_generation(
        subject_id="u1", signal="steps", aggregation_kind="daily") is None


def test_retention_preserves_no_row_incomplete_scope_when_nothing_is_deleted(
        clean_v010_review):
    store = storage()
    missing = DAY + timedelta(days=1)
    candidate = AggregateGeneration(
        "sparse-retained", "u1", "steps", "daily", 2, DAY, missing,
        status="active", completeness="incomplete", accounted_dates=(DAY,),
        incomplete_dates=(missing,), incomplete_reasons=("fact_retracted",),
        created_at=T0, updated_at=T0, activated_at=T0,
    )
    store.put_aggregate_generation(candidate)
    store.put_aggregate(aggregate(DAY, "sparse-retained"))
    store._q(
        """INSERT INTO perceptkit_active_aggregate_generation
           (subject_id,signal,aggregation_kind,generation_id,activated_at)
           VALUES ('u1','steps','daily','sparse-retained',%s)""", (T0,))

    assert store.delete_aggregates(subject_id="u1", signal="steps", before=DAY) == 0
    active = store.get_active_aggregate_generation(
        subject_id="u1", signal="steps", aggregation_kind="daily")
    assert active.requested_start_date == DAY
    assert active.requested_end_date == missing
    assert active.accounted_dates == (DAY,)
    assert active.incomplete_dates == (missing,)
    assert active.completeness == "incomplete"
    assert active.incomplete_reasons == ("fact_retracted",)


def test_retention_truncates_left_boundary_but_keeps_right_durable_scope(
        clean_v010_review):
    store = storage()
    old = DAY - timedelta(days=1)
    missing = DAY + timedelta(days=1)
    candidate = AggregateGeneration(
        "bounded-retention", "u1", "steps", "daily", 2, old, missing,
        status="active", completeness="incomplete", accounted_dates=(old, DAY),
        incomplete_dates=(missing,), incomplete_reasons=("fact_retracted",),
        created_at=T0, updated_at=T0, activated_at=T0,
    )
    store.put_aggregate_generation(candidate)
    store.put_aggregate(aggregate(old, "bounded-retention"))
    store.put_aggregate(aggregate(DAY, "bounded-retention"))
    store._q(
        """INSERT INTO perceptkit_active_aggregate_generation
           (subject_id,signal,aggregation_kind,generation_id,activated_at)
           VALUES ('u1','steps','daily','bounded-retention',%s)""", (T0,))

    assert store.delete_aggregates(subject_id="u1", signal="steps", before=DAY) == 1
    active = store.get_active_aggregate_generation(
        subject_id="u1", signal="steps", aggregation_kind="daily")
    assert active.requested_start_date == DAY
    assert active.requested_end_date == missing
    assert active.accounted_dates == (DAY,)
    assert active.incomplete_dates == (missing,)
    assert active.completeness == "incomplete"
    assert active.incomplete_reasons == ("fact_retracted",)

    assert store.delete_aggregates(
        subject_id="u1", signal="steps", before=missing + timedelta(days=1)) == 1
    assert store.get_active_aggregate_generation(
        subject_id="u1", signal="steps", aggregation_kind="daily") is None


def test_calendar_pages_have_total_primary_key_order(clean_v010_review):
    store = storage()
    identities = [
        ("z", "acct-2", "cal-2"),
        ("a", "acct-2", "cal-1"),
        ("a", "acct-1", "cal-2"),
        ("a", "acct-1", "cal-1"),
    ]
    store.upsert_calendar_events(subject_id="u1", events=[
        CalendarEventMirror(
            subject_id="u1", source=source, source_account_id=account,
            source_calendar_id=calendar, source_event_id="same-id",
            event_fields={"title": calendar, "start_at": T0},
        )
        for source, account, calendar in identities
    ])
    store._q(
        """CREATE INDEX IF NOT EXISTS review_calendar_reverse
           ON perceptkit_calendar_mirror
             (subject_id,source DESC,source_account_id DESC,
              source_calendar_id DESC,source_event_id DESC)""")
    store._q("CLUSTER perceptkit_calendar_mirror USING review_calendar_reverse")
    store._q("SET enable_indexscan=off")
    store._q("SET enable_bitmapscan=off")
    rows = list(store.list_calendar_events(subject_id="u1", limit=2, offset=0))
    rows += list(store.list_calendar_events(subject_id="u1", limit=2, offset=2))
    got = [(row.source, row.source_account_id, row.source_calendar_id) for row in rows]
    assert got == sorted(identities)


def test_reminder_pages_have_total_primary_key_order(clean_v010_review):
    store = storage()
    identities = [
        ("z", "acct-2", "list-2"),
        ("a", "acct-2", "list-1"),
        ("a", "acct-1", "list-2"),
        ("a", "acct-1", "list-1"),
    ]
    store.upsert_reminders(subject_id="u1", items=[
        ReminderItemMirror(
            subject_id="u1", source=source, source_account_id=account,
            source_list_id=reminder_list, source_reminder_id="same-id",
            reminder_fields={
                "title": reminder_list, "due_at": T0, "is_completed": False,
            },
        )
        for source, account, reminder_list in identities
    ])
    store._q(
        """CREATE INDEX IF NOT EXISTS review_reminder_reverse
           ON perceptkit_reminder_mirror
             (subject_id,source DESC,source_account_id DESC,
              source_list_id DESC,source_reminder_id DESC)""")
    store._q("CLUSTER perceptkit_reminder_mirror USING review_reminder_reverse")
    store._q("SET enable_indexscan=off")
    store._q("SET enable_bitmapscan=off")
    rows = list(store.list_reminders(subject_id="u1", limit=2, offset=0))
    rows += list(store.list_reminders(subject_id="u1", limit=2, offset=2))
    got = [(row.source, row.source_account_id, row.source_list_id) for row in rows]
    assert got == sorted(identities)
