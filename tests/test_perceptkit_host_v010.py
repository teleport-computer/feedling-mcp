"""IO host obligations that sit above the PerceptKit StoragePort."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from perceptkit import PerceptionKit  # noqa: E402
from perceptkit.conformance import run_definition_provider_conformance  # noqa: E402
from perceptkit.contracts import IngestContext  # noqa: E402
from perceptkit.contracts.records import EventOutboxEntry  # noqa: E402
from perceptkit.rules import EventDefinition  # noqa: E402

from perception.perceptkit_adapter import schema, worker  # noqa: E402
from perception.perceptkit_adapter.storage import PostgresStorage  # noqa: E402


DSN = os.environ.get("PERCEPTKIT_TEST_PG")
pytestmark = pytest.mark.skipif(not DSN, reason="requires PERCEPTKIT_TEST_PG")
UTC = timezone.utc
T0 = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)


def connect():
    return psycopg.connect(DSN, autocommit=True)


@pytest.fixture
def clean_host():
    with connect() as conn:
        conn.execute(schema.DDL)
        conn.execute(schema.TRUNCATE)
    yield


def _rule(value: int = 10) -> EventDefinition:
    return EventDefinition.parse({
        "id": "io.test.steps", "version": 1,
        "source": {"signal": "steps", "field": "step_count"},
        "condition": {"type": "threshold_crossing", "operator": "gte",
                      "value": value},
        "event": {"type": "io.test.steps"},
    })


def _event(event_id: str = "evt-host") -> EventOutboxEntry:
    return EventOutboxEntry(
        event_id=event_id, subject_id="u1", definition_id="io.test", definition_version=1,
        event_type="io.test", occurred_at=T0, detected_at=T0,
        delivery_state="pending", fact_snapshot={"signal": "steps"},
    )


def test_real_postgres_definition_provider_conformance(clean_host):
    from perception.perceptkit_adapter.definitions import PostgresDefinitionProvider

    opened = []

    def factory(live):
        conn = connect()
        opened.append(conn)
        return PostgresDefinitionProvider(conn, lambda _subject: tuple(live))

    try:
        assert run_definition_provider_conformance(factory) == []
    finally:
        for conn in opened:
            conn.close()


def test_definition_archive_conflict_rolls_back_the_whole_real_pg_ingest(clean_host):
    from perceptkit.ports.definitions import DefinitionArchiveConflictError
    from perception.perceptkit_adapter.definitions import PostgresDefinitionProvider

    with connect() as conn:
        storage = PostgresStorage(conn)
        archived = PostgresDefinitionProvider(conn, lambda _subject: ())
        archived.archive_definition(_rule(10))
        conflicting = PostgresDefinitionProvider(conn, lambda _subject: (_rule(20),))
        kit = PerceptionKit(storage=storage, definitions=conflicting)
        report = {
            "schema_version": 1, "report_id": "archive-conflict", "producer": "ios",
            "observations": [{
                "signal": "steps", "signal_schema_version": 1,
                "occurred_at": T0.isoformat(), "local_date": T0.date().isoformat(),
                "availability": "observed", "source_event_id": "step-1",
                "value": {"step_count": 20},
            }],
        }
        with pytest.raises(DefinitionArchiveConflictError):
            kit.ingest(report, context=IngestContext("u1", T0))
        assert conn.execute("SELECT count(*) FROM perceptkit_ingest_receipt").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM perceptkit_observation").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM perceptkit_event_outbox").fetchone() == (0,)


def test_production_kit_uses_the_transaction_bound_persistent_provider(clean_host,
                                                                       monkeypatch):
    from perception.perceptkit_adapter import shadow

    monkeypatch.setattr(shadow, "wakes_enabled", lambda: True)
    with connect() as conn:
        kit = shadow._kit(PostgresStorage(conn))
        assert kit.definition_provider_status() == {
            "persistent": True, "production_ready": True,
        }
        assert kit._definitions.conn is conn


def test_wake_receipt_contains_the_authoritative_runtime_reference():
    from perception.perceptkit_adapter.wake_port import (
        FeedlingWakePort, RuntimeEnqueueResult,
    )
    from perceptkit.contracts.event import EventCondition, PerceptionEvent

    event = PerceptionEvent(
        event_id="evt-ref", definition_id="io.test", definition_version=1,
        subject_id="u1", type="io.test", signal="steps", occurred_at=T0,
        received_at=T0, condition=EventCondition(type="occurrence"),
    )
    attempt = type("Attempt", (), {"attempt_id": "evt-ref:1"})()
    receipt = FeedlingWakePort(submit=lambda _event: RuntimeEnqueueResult(
        accepted=True, runtime_ref="v2-job:42",
    )).wake(event, attempt)
    assert receipt.status == "accepted"
    assert receipt.runtime_ref == "v2-job:42"


def test_durable_enqueue_then_throw_reconciles_without_a_second_runtime_call(clean_host):
    from perception.perceptkit_adapter.wake_port import (
        FeedlingWakePort, RuntimeEnqueueResult,
    )

    with connect() as conn:
        store = PostgresStorage(conn)
        store.enqueue_event(_event())
        calls = []

        def uncertain(_event):
            calls.append("enqueue")
            raise RuntimeError("connection lost after downstream commit")

        first = worker.run_once(
            storage_factory=lambda: store, wake=FeedlingWakePort(submit=uncertain),
            worker_id="w1", now=T0,
        )
        assert first.unknown == ["evt-host"]
        reconciled = worker.reconcile_unknown(
            storage_factory=lambda: store,
            lookup=lambda event: RuntimeEnqueueResult(
                accepted=True, runtime_ref="resident-job:evt-host"),
            now=T0,
        )
        assert reconciled.delivered == ["evt-host"]
        assert calls == ["enqueue"]
        assert conn.execute(
            "SELECT status,runtime_ref FROM perceptkit_wake_receipt "
            "WHERE event_id='evt-host'"
        ).fetchone() == ("accepted", "resident-job:evt-host")


def test_missing_runtime_evidence_leaves_the_attempt_unknown(clean_host):
    from perception.perceptkit_adapter.wake_port import FeedlingWakePort

    with connect() as conn:
        store = PostgresStorage(conn)
        store.enqueue_event(_event("evt-no-evidence"))

        def uncertain(_event):
            raise RuntimeError("no authoritative enqueue result")

        first = worker.run_once(
            storage_factory=lambda: store, wake=FeedlingWakePort(submit=uncertain),
            worker_id="w1", now=T0,
        )
        reconciled = worker.reconcile_unknown(
            storage_factory=lambda: store, lookup=lambda _event: None, now=T0,
        )
        assert first.unknown == ["evt-no-evidence"]
        assert not reconciled.delivered
        assert conn.execute(
            "SELECT delivery_state FROM perceptkit_event_outbox "
            "WHERE event_id='evt-no-evidence'"
        ).fetchone() == ("unknown",)
