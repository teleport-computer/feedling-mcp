"""PerceptKit v0.10 migration and real two-connection fencing evidence."""
from __future__ import annotations

import importlib
import importlib.util
import os
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
psycopg = pytest.importorskip("psycopg")

from perceptkit.contracts.errors import RetryableMutationError  # noqa: E402
from perceptkit.contracts.mutation import (  # noqa: E402
    aggregate_generation_key,
    aggregate_key,
    current_key,
    event_key,
    fact_key,
    rule_key,
)
from perceptkit.contracts.records import (  # noqa: E402
    CurrentProjection,
    DailyAggregate,
    EventOutboxEntry,
    StoredObservation,
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


@pytest.fixture
def clean_v010():
    with connect() as conn:
        conn.execute(schema.DDL)
        conn.execute(schema.TRUNCATE)
    yield


def storage():
    return PostgresStorage(connect())


def observation(oid="obs-1"):
    return StoredObservation(
        oid, "u1", "steps", 1, "ios", T0, T0, "observed", DAY,
        typed_value={"step_count": 10}, source_event_id="fact-1", source_revision=1)


def current(dimension="steps", version=0):
    return CurrentProjection(
        "u1", "steps", dimension, {"step_count": 10}, "observed", T0, T0,
        source="ios", source_event_id="fact-1", source_revision=1,
        version=version, content_digest="digest")


def event(event_id="event-1"):
    ref = {"subject_id": "u1", "signal": "steps", "source": "ios",
           "source_event_id": "fact-1", "fact_key": "fact-1",
           "observation_id": "obs-1", "source_revision": 1, "role": "current"}
    return EventOutboxEntry(
        event_id, "u1", "steps.changed", 1, "steps.changed", T0, T0,
        fact_snapshot={"signal": "steps", "previous": 1, "current": 10},
        source="ios", source_event_id="fact-1", fact_dependencies=(ref,),
        fact_dependencies_complete=True)


def migration_module(name):
    path = Path(__file__).parent.parent / "backend" / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"task7a_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_upgrade_through_0117_matches_fresh_columns_and_marks_legacy_incomplete():
    legacy_schema = f"legacy_{uuid.uuid4().hex[:10]}"
    fresh_schema = f"fresh_{uuid.uuid4().hex[:10]}"
    modules = [
        "0106_perceptkit_objects", "0107_perceptkit_mirror_source",
        "0108_perceptkit_retraction", "0109_divergence_skew",
        "0110_divergence_observed_at", "0115_outbox_source_fact",
    ]
    migration = migration_module("0116_perceptkit_v010_storage")
    definitions_migration = migration_module("0117_perceptkit_definition_history")
    with connect() as conn:
        conn.execute(f'CREATE SCHEMA "{legacy_schema}"')
        conn.execute(f'CREATE SCHEMA "{fresh_schema}"')
        try:
            conn.execute(f'SET search_path TO "{legacy_schema}"')
            for name in modules:
                mod = migration_module(name)
                conn.execute(mod._UP)
            conn.execute(
                """INSERT INTO perceptkit_daily_aggregate
                   (subject_id,signal,local_date,aggregation_kind,aggregation_version,
                    typed_aggregate,source_coverage)
                   VALUES ('u','steps','2026-09-01','daily',2,'{"n":1}','{}')""")
            conn.execute(migration._UP)
            conn.execute(definitions_migration._UP)
            conn.execute(definitions_migration._UP)  # head upgrade is retry-safe
            legacy = conn.execute(
                "SELECT status,completeness,incomplete_reasons FROM "
                "perceptkit_aggregate_generation").fetchone()
            assert legacy[0:2] == ("incomplete", "incomplete")
            assert "legacy_coverage_unverified" in legacy[2]
            assert conn.execute(
                "SELECT count(*) FROM perceptkit_active_aggregate_generation").fetchone() == (0,)

            conn.execute(f'SET search_path TO "{fresh_schema}"')
            conn.execute(schema.DDL)
            conn.execute(schema.DDL)  # fresh helper is retry-safe

            def shape(name):
                return conn.execute(
                    """SELECT table_name,column_name,data_type,is_nullable
                       FROM information_schema.columns WHERE table_schema=%s
                         AND table_name LIKE 'perceptkit_%%'
                       ORDER BY table_name,column_name""", (name,)).fetchall()
            assert shape(legacy_schema) == shape(fresh_schema)
        finally:
            conn.execute("SET search_path TO public")
            conn.execute(f'DROP SCHEMA "{legacy_schema}" CASCADE')
            conn.execute(f'DROP SCHEMA "{fresh_schema}" CASCADE')


def test_fact_owner_blocks_competing_correction_and_hides_partial_writes(clean_v010):
    first, second, observer = storage(), storage(), storage()
    barrier = threading.Barrier(2)
    result = []

    def compete():
        barrier.wait()
        try:
            with second.mutation_transaction() as owner:
                owner.acquire((fact_key("u1", "steps", "ios", "fact-1"),))
        except RetryableMutationError:
            result.append("fenced")

    with first.mutation_transaction() as owner:
        owner.acquire((fact_key("u1", "steps", "ios", "fact-1"),))
        assert observer._q(
            "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted")[0][0] >= 1
        first.append_observation(observation())
        thread = threading.Thread(target=compete)
        thread.start(); barrier.wait(); thread.join(5)
        assert observer.list_observations(subject_id="u1", signal="steps")[0] == []
    assert result == ["fenced"]
    assert len(observer.list_observations(subject_id="u1", signal="steps")[0]) == 1


def test_current_same_dimension_is_fenced_but_different_dimension_proceeds(clean_v010):
    first, second = storage(), storage()
    with first.mutation_transaction() as owner:
        owner.acquire((current_key("u1", "steps", "A"),))
        with pytest.raises(RetryableMutationError):
            with second.mutation_transaction() as rival:
                rival.acquire((current_key("u1", "steps", "A"),))
        with second.mutation_transaction() as independent:
            independent.acquire((current_key("u1", "steps", "B"),))
            assert second.compare_and_put_current(current("B"), expected_version=-1)


def test_generation_and_aggregate_resources_are_real_database_fences(clean_v010):
    first, second = storage(), storage()
    generation = aggregate_generation_key("u1", "steps", "daily")
    row_key = aggregate_key("u1", "steps", DAY, "daily", 2)
    with first.mutation_transaction() as owner:
        owner.acquire((generation,))
        with pytest.raises(RetryableMutationError):
            second.put_aggregate(DailyAggregate(
                "u1", "steps", DAY, "daily", 2, {"n": 1}, updated_at=T0))
    with first.mutation_transaction() as owner:
        owner.acquire((row_key,))
        with pytest.raises(RetryableMutationError):
            with second.mutation_transaction() as rival:
                rival.acquire((row_key,))


def test_rule_state_correction_replay_has_one_owner(clean_v010):
    first, second = storage(), storage()
    key = rule_key("u1", "steps.changed", "2026-09-28@v1")
    with first.mutation_transaction() as owner:
        owner.acquire((key,))
        first.put_rule_state(subject_id="u1", definition_id="steps.changed",
                             scope_key="2026-09-28@v1", state={"signal": "steps"})
        with pytest.raises(RetryableMutationError):
            with second.mutation_transaction() as rival:
                rival.acquire((key,))
    assert second.get_rule_state(subject_id="u1", definition_id="steps.changed",
                                 scope_key="2026-09-28@v1") == {"signal": "steps"}


def test_invalidation_before_durable_start_prevents_dispatch(clean_v010):
    claimant, invalidator = storage(), storage()
    claimant.enqueue_event(event())
    barrier = threading.Barrier(2)
    claimed = []

    def race_claim():
        barrier.wait()
        claimed.append(claimant.claim_pending_event(
            worker_id="w", now=T0, lease_seconds=60))

    with invalidator.mutation_transaction() as owner:
        owner.acquire((event_key("u1", "steps"),))
        thread = threading.Thread(target=race_claim)
        thread.start(); barrier.wait()
        invalidator.scrub_event_snapshots(
            subject_id="u1", signal="steps", source="ios", source_event_id="fact-1",
            now=T0 + timedelta(seconds=1))
        thread.join(5)
    assert claimed == [None]
    assert claimant.list_events(subject_id="u1")[0].delivery_state == "invalidated"


def test_durable_start_then_invalidation_becomes_unknown_not_retryable(clean_v010):
    starter, invalidator = storage(), storage()
    starter.enqueue_event(event())
    claimed = starter.claim_pending_event(worker_id="w", now=T0, lease_seconds=60)
    assert starter.begin_event_dispatch(
        event_id=claimed.event_id, claim_token=claimed.claim_token,
        now=T0 + timedelta(seconds=1)) is not None
    invalidator.scrub_event_snapshots(
        subject_id="u1", signal="steps", source="ios", source_event_id="fact-1",
        now=T0 + timedelta(seconds=2))
    stored = starter.list_events(subject_id="u1")[0]
    assert stored.delivery_state == "unknown"
    assert starter.claim_pending_event(
        worker_id="other", now=T0 + timedelta(days=1), lease_seconds=60) is None


def test_forced_failure_exposes_no_report_observation_event_split(clean_v010):
    writer, observer = storage(), storage()
    with pytest.raises(RuntimeError):
        with writer.transaction():
            writer.claim_report(subject_id="u1", producer="ios", report_id="r1",
                                payload_digest="v2:d", received_at=T0)
            writer.append_observation(observation())
            writer.enqueue_event(event())
            assert observer.list_observations(subject_id="u1", signal="steps")[0] == []
            assert observer.list_events(subject_id="u1") == []
            raise RuntimeError("forced intermediate failure")
    assert observer.list_observations(subject_id="u1", signal="steps")[0] == []
    assert observer.list_events(subject_id="u1") == []


def test_lock_order_failure_is_rollback_only_even_when_caught(clean_v010):
    writer, observer = storage(), storage()
    with pytest.raises(RetryableMutationError):
        with writer.mutation_transaction() as owner:
            owner.acquire((rule_key("u1", "d", "scope"),))
            writer.put_rule_state(subject_id="u1", definition_id="d",
                                  scope_key="scope", state={"signal": "steps"})
            try:
                owner.acquire((fact_key("u1", "steps", "ios", "fact-1"),))
            except RetryableMutationError:
                pass
    assert observer.get_rule_state(subject_id="u1", definition_id="d", scope_key="scope") is None
