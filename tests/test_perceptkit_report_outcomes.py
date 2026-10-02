"""Durable per-observation Report outcomes on IO's real PostgreSQL adapter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
psycopg = pytest.importorskip("psycopg")

import perceptkit  # noqa: E402
from perceptkit import PerceptionKit  # noqa: E402
from perceptkit.contracts import IngestContext  # noqa: E402
from perceptkit.rules import EventDefinition  # noqa: E402
from perception.perceptkit_adapter import schema  # noqa: E402
from perception.perceptkit_adapter.storage import PostgresStorage  # noqa: E402


DSN = os.environ.get("PERCEPTKIT_TEST_PG")
pytestmark = pytest.mark.skipif(not DSN, reason="requires PERCEPTKIT_TEST_PG")
T0 = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
_TEST_SCHEMA: str | None = None


def connect():
    conn = psycopg.connect(DSN, autocommit=True)
    if _TEST_SCHEMA is not None:
        conn.execute(f'SET search_path TO "{_TEST_SCHEMA}"')
    return conn


@pytest.fixture
def clean_report_outcomes():
    global _TEST_SCHEMA
    namespace = f"report_outcome_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{namespace}"')
        conn.execute(f'SET search_path TO "{namespace}"')
        conn.execute(schema.DDL)
    _TEST_SCHEMA = namespace
    try:
        yield
    finally:
        _TEST_SCHEMA = None
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{namespace}" CASCADE')


def _valid(eid: str = "valid-1", count: int = 10) -> dict:
    return {
        "signal": "steps",
        "signal_schema_version": 1,
        "occurred_at": T0.isoformat(),
        "local_date": T0.date().isoformat(),
        "availability": "observed",
        "source_event_id": eid,
        "value": {"step_count": count},
    }


def _invalid() -> dict:
    return {
        "signal": "unknown_signal",
        "signal_schema_version": 1,
        "occurred_at": T0.isoformat(),
        "availability": "observed",
        "value": {},
    }


def _report(report_id: str, observations: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "report_id": report_id,
        "producer": "ios",
        "observations": observations,
    }


def _rule() -> EventDefinition:
    return EventDefinition.parse({
        "id": "io.report-outcome",
        "version": 1,
        "source": {"signal": "steps"},
        "condition": {"type": "occurrence"},
        "event": {"type": "io.report-outcome"},
    })


def test_mixed_report_persists_exact_json_and_replays_without_duplicate_fact_or_event(
        clean_report_outcomes):
    envelope = _report("mixed-report", [_invalid(), _valid()])
    with connect() as conn:
        storage = PostgresStorage(conn)
        first = PerceptionKit(storage, definitions=[_rule()]).ingest(
            envelope, context=IngestContext("u1", T0))
        row = conn.execute(
            "SELECT status,observations_applied,observations_rejected "
            "FROM perceptkit_ingest_receipt WHERE report_id='mixed-report'"
        ).fetchone()
        issue = first.receipt.observations_rejected[0]
        assert row == (
            "accepted",
            1,
            [{
                "index": issue.index,
                "code": issue.code,
                "problems": list(issue.problems),
            }],
        )
        assert issue.index == 0
        assert issue.code == "validation_failed"
        assert issue.problems and all(len(problem) <= 256 for problem in issue.problems)
        before = (
            conn.execute("SELECT count(*) FROM perceptkit_observation").fetchone()[0],
            conn.execute("SELECT count(*) FROM perceptkit_event_outbox").fetchone()[0],
        )

    # A fresh connection/adapter must reconstruct the outcome from PostgreSQL.
    with connect() as conn:
        replay = PerceptionKit(PostgresStorage(conn), definitions=[_rule()]).ingest(
            envelope, context=IngestContext("u1", T0))
        after = (
            conn.execute("SELECT count(*) FROM perceptkit_observation").fetchone()[0],
            conn.execute("SELECT count(*) FROM perceptkit_event_outbox").fetchone()[0],
        )

    assert first.receipt.status == "accepted"
    assert replay.receipt.status == "duplicate"
    assert replay.receipt.observations_applied == 0
    assert replay.receipt.observations_rejected == first.receipt.observations_rejected
    assert replay.rejected == [(0, first.receipt.observations_rejected[0].problems)]
    assert before == after == (1, 1)


def test_all_invalid_report_is_accepted_and_its_zero_applied_outcome_replays(
        clean_report_outcomes):
    envelope = _report("all-invalid", [_invalid()])
    with connect() as conn:
        first = PerceptionKit(PostgresStorage(conn)).ingest(
            envelope, context=IngestContext("u1", T0))
    with connect() as conn:
        replay = PerceptionKit(PostgresStorage(conn)).ingest(
            envelope, context=IngestContext("u1", T0))
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM perceptkit_observation),"
            "(SELECT count(*) FROM perceptkit_event_outbox)"
        ).fetchone()

    assert first.receipt.status == "accepted"
    assert first.receipt.observations_applied == 0
    assert len(first.receipt.observations_rejected) == 1
    assert replay.receipt.status == "duplicate"
    assert replay.receipt.observations_applied == 0
    assert replay.receipt.observations_rejected == first.receipt.observations_rejected
    assert counts == (0, 0)


def test_report_digest_conflict_is_not_mislabeled_as_a_fact_conflict(
        clean_report_outcomes):
    with connect() as conn:
        storage = PostgresStorage(conn)
        PerceptionKit(storage).ingest(
            _report("same-id", [_valid(count=10)]),
            context=IngestContext("u1", T0),
        )
        clash = PerceptionKit(storage).ingest(
            _report("same-id", [_valid(count=11)]),
            context=IngestContext("u1", T0),
        )
    assert clash.receipt.status == "conflict"
    assert clash.receipt.error_code == "report_digest_conflict"
    assert clash.receipt.observations_rejected == ()


def test_report_outcome_rolls_back_with_sibling_fact_and_event(
        clean_report_outcomes):
    class CrashAfterFinalize(PostgresStorage):
        def finalize_report(self, receipt):
            super().finalize_report(receipt)
            raise RuntimeError("forced finalize crash")

    with connect() as conn:
        with pytest.raises(RuntimeError, match="forced finalize crash"):
            PerceptionKit(CrashAfterFinalize(conn), definitions=[_rule()]).ingest(
                _report("rollback-report", [_invalid(), _valid()]),
                context=IngestContext("u1", T0),
            )
    with connect() as conn:
        assert conn.execute(
            "SELECT (SELECT count(*) FROM perceptkit_ingest_receipt),"
            "(SELECT count(*) FROM perceptkit_observation),"
            "(SELECT count(*) FROM perceptkit_event_outbox)"
        ).fetchone() == (0, 0, 0)


@pytest.mark.parametrize("corrupt", [
    {"index": 0, "code": "validation_failed", "problems": ["bad"]},
    [{"index": 0, "code": "made_up", "problems": ["bad"]}],
    [
        {"index": 0, "code": "validation_failed", "problems": ["bad"]},
        {"index": 0, "code": "fact_conflict", "problems": ["bad"]},
    ],
    [{"index": 0, "code": "validation_failed", "problems": "bad"}],
])
def test_corrupt_persisted_observation_rejections_fail_closed(
        clean_report_outcomes, corrupt):
    with connect() as conn:
        conn.execute(
            """INSERT INTO perceptkit_ingest_receipt
               (subject_id,producer,report_id,payload_digest,received_at,status,
                observations_rejected)
               VALUES ('u1','ios','corrupt','v2:corrupt',%s,'accepted',%s::jsonb)""",
            (T0, json.dumps(corrupt)),
        )
        with pytest.raises((TypeError, ValueError), match="observations_rejected"):
            PostgresStorage(conn).claim_report(
                subject_id="u1", producer="ios", report_id="corrupt",
                payload_digest="v2:corrupt", received_at=T0,
            )


def test_restart_conformance_runs_in_two_independent_processes_on_one_pg_schema():
    namespace = f"report_restart_{uuid.uuid4().hex[:12]}"
    with connect() as conn:
        conn.execute(f'CREATE SCHEMA "{namespace}"')
        try:
            conn.execute(f'SET search_path TO "{namespace}"')
            conn.execute(schema.DDL)
        finally:
            conn.execute("SET search_path TO public")

    code = r"""
import os
import psycopg
from perceptkit.conformance import (
    prepare_report_receipt_restart_conformance,
    verify_report_receipt_restart_conformance,
)
from perception.perceptkit_adapter.storage import PostgresStorage

conn = psycopg.connect(os.environ["REPORT_OUTCOME_DSN"], autocommit=True)
try:
    namespace = os.environ["REPORT_OUTCOME_SCHEMA"]
    conn.execute(f'SET search_path TO "{namespace}"')
    storage = PostgresStorage(conn)
    check = (prepare_report_receipt_restart_conformance
             if os.environ["REPORT_OUTCOME_PHASE"] == "prepare"
             else verify_report_receipt_restart_conformance)
    problems = check(storage)
    if problems:
        raise SystemExit("; ".join(problems))
finally:
    conn.close()
"""
    kit_src = str(Path(perceptkit.__file__).resolve().parents[1])
    backend = str(Path(__file__).parent.parent / "backend")
    env = dict(os.environ)
    env.update({
        "REPORT_OUTCOME_DSN": DSN,
        "REPORT_OUTCOME_SCHEMA": namespace,
        "PYTHONPATH": os.pathsep.join((kit_src, backend)),
    })
    try:
        for phase in ("prepare", "verify"):
            phase_env = dict(env, REPORT_OUTCOME_PHASE=phase)
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=Path(__file__).parent.parent,
                env=phase_env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            assert result.returncode == 0, (
                f"{phase} subprocess failed:\n{result.stdout}\n{result.stderr}")
    finally:
        with connect() as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{namespace}" CASCADE')
