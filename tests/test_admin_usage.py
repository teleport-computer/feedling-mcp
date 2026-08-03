"""Admin Usage query normalization and canonical link behavior."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import unescape
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track as _data_track  # noqa: E402
from admin import admin_core as _admin_core  # noqa: E402
from admin import usage as _usage  # noqa: E402
from accounts import registry  # noqa: E402
from core import reqctx  # noqa: E402
import db  # noqa: E402
from model_api_runtime.v2 import (  # noqa: E402
    jobs_store,
    provider_attempt_rollup,
    usage_reporting,
    usage_rollup,
)

from conftest import seed_user  # noqa: E402


NOW_UTC = datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc)


def _load_usage_scale_harness():
    path = Path(__file__).parent.parent / "scripts/perf/admin_usage_scale.py"
    spec = importlib.util.spec_from_file_location("admin_usage_scale_harness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_ranked_flags_probe():
    path = (
        Path(__file__).parent.parent
        / "scripts/perf/admin_usage_ranked_flags_temp_probe.py"
    )
    if not path.exists():
        pytest.fail("ranked-flags TEMP probe is not implemented")
    spec = importlib.util.spec_from_file_location(
        "admin_usage_ranked_flags_temp_probe", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_narrow_call_probe():
    path = (
        Path(__file__).parent.parent
        / "scripts/perf/admin_usage_narrow_call_temp_probe.py"
    )
    if not path.exists():
        pytest.fail("narrow-call TEMP probe is not implemented")
    spec = importlib.util.spec_from_file_location(
        "admin_usage_narrow_call_temp_probe", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_narrow_call_probe_schema_contains_only_identity_and_flags():
    probe = _load_narrow_call_probe()

    assert probe.NARROW_IDENTITY_COLUMNS == (
        "local_day",
        "user_id",
        "cohort_lane",
        "requested_provider",
        "requested_model",
        "resolved_provider",
        "resolved_model",
        "effective_usage_known",
    )
    statements = probe._narrow_table_ddl(
        relation="admin_usage_daily_call_dimensions"
    )
    sql = " ".join(statements).lower()
    assert sql.count("create temp table") == 1
    assert sql.count("create") == 4
    assert all(name in sql for name in probe.FLAG_COLUMN_NAMES)
    for forbidden in (
        "call_id",
        "attempts",
        "input_tokens",
        "cost_kind",
        "currency",
        "ttft_samples",
        "refreshed_at",
        "llm_usage_daily_call_memberships",
    ):
        assert forbidden not in sql


def test_narrow_call_probe_storage_gate_requires_both_byte_limits():
    probe = _load_narrow_call_probe()
    healthy = {
        "heap_bytes": 330_000_000,
        "index_bytes": 320_000_000,
        "total_bytes": 650_000_000,
    }

    assert probe._narrow_storage_passed(
        healthy, membership_total_bytes=2_820_399_104
    ) is True
    assert probe._narrow_storage_passed(
        {**healthy, "total_bytes": 700_000_001},
        membership_total_bytes=2_820_399_104,
    ) is False
    assert probe._narrow_storage_passed(
        healthy, membership_total_bytes=2_400_000_000
    ) is False


def test_narrow_call_probe_ddl_creates_exact_temporary_postgresql_schema():
    probe = _load_narrow_call_probe()
    relation = "admin_usage_daily_call_dimensions"

    with db.get_pool().connection() as conn, conn.transaction():
        try:
            for statement in probe._narrow_table_ddl(relation=relation):
                conn.execute(statement)
            persistence = conn.execute(
                "SELECT relpersistence FROM pg_class "
                "WHERE oid=%s::regclass",
                (relation,),
            ).fetchone()[0]
            columns = conn.execute(
                "SELECT a.attname,format_type(a.atttypid,a.atttypmod) "
                "FROM pg_attribute a WHERE a.attrelid=%s::regclass "
                "AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",
                (relation,),
            ).fetchall()
            indexes = tuple(
                row[0]
                for row in conn.execute(
                    "SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                    "WHERE indrelid=%s::regclass ORDER BY indexrelid::regclass::text",
                    (relation,),
                ).fetchall()
            )
        finally:
            conn.execute(f"DROP TABLE IF EXISTS {relation}")

    assert persistence == "t"
    assert columns == [
        ("local_day", "date"),
        ("user_id", "text"),
        ("cohort_lane", "text"),
        ("requested_provider", "text"),
        ("requested_model", "text"),
        ("resolved_provider", "text"),
        ("resolved_model", "text"),
        ("effective_usage_known", "boolean"),
        *((name, "bigint") for name in probe.FLAG_COLUMN_NAMES),
    ]
    assert len(indexes) == 3
    assert any(" UNIQUE " in definition for definition in indexes)
    assert any("(user_id, local_day)" in definition for definition in indexes)
    assert any(
        "(local_day, resolved_provider, resolved_model, user_id, cohort_lane)"
        in definition
        and "INCLUDE (requested_provider, requested_model, effective_usage_known)"
        in definition
        for definition in indexes
    )
    assert all(
        flag not in " ".join(indexes) for flag in probe.FLAG_COLUMN_NAMES
    )


def test_ranked_probe_declares_exact_32_column_matrix():
    probe = _load_ranked_flags_probe()
    expected = tuple(
        f"{metric}_{selector}_{completeness}"
        for selector in ("all", "provider", "model", "provider_model")
        for completeness in ("all", "effective")
        for metric in (
            "logical_calls_cohort",
            "logical_calls_requested",
            "missing_outer_ordinals",
            "missing_inner_ordinals",
        )
    )

    assert probe.FLAG_COLUMN_NAMES == expected
    assert len(set(expected)) == 32


def test_ranked_probe_uses_16_stable_ranks_and_no_persistent_ddl():
    probe = _load_ranked_flags_probe()
    sql = probe._ranked_flag_ctes(
        priced_relation="priced", gap_relation="call_gaps"
    )
    normalized = " ".join(sql.split())

    assert normalized.count("row_number() OVER") == 16
    assert normalized.count("ORDER BY p.attempt_id") == 16
    assert "CREATE TABLE" not in normalized
    assert "llm_usage_daily_call_memberships" not in normalized


def test_ranked_probe_candidate_rollup_sql_has_no_distinct_call_path():
    probe = _load_ranked_flags_probe()
    sql = " ".join(
        probe._candidate_query_sql(
            selector="provider_model", completeness="all"
        ).split()
    ).lower()

    assert "llm_usage_daily_call_memberships" not in sql
    assert "call_id" not in sql
    assert "count(distinct" not in sql
    assert "logical_calls_cohort_provider_model_all" in sql
    assert "logical_calls_requested_provider_model_all" in sql


def test_ranked_probe_hard_gate_requires_every_nonpersistent_scale_witness():
    probe = _load_ranked_flags_probe()
    healthy = {
        "temp_relation": {
            "rows": 731_199,
            "heap_bytes": 1,
            "index_bytes": 1,
            "total_bytes": 2,
        },
        "build": {
            "days": 365,
            "total_ms": 1.0,
            "p50_ms": 1.0,
            "p95_ms": 1.0,
            "max_ms": 119_999.0,
        },
        "raw_edge": {"attempts": 8_208, "logical_calls": 8_208},
        "cohorts": {
            name: {
                "exact": True,
                "ordinary_ms": 2_999.0,
                "cold": {"execution_ms": 2_999.0, "temp_read_blocks": 0, "temp_written_blocks": 0},
                "warm": {"execution_ms": 2_999.0, "temp_read_blocks": 0, "temp_written_blocks": 0},
                "candidate_forbidden_path_absent": True,
            }
            for name in ("unfiltered", "provider_model_filtered")
        },
        "persistent_state": {"unchanged": True},
        "session_close": {"persistent_probe_objects": []},
    }

    assert probe._probe_passed(healthy) is True
    for mutate in (
        lambda item: item["temp_relation"].update(rows=731_198),
        lambda item: item["build"].update(max_ms=120_000.0),
        lambda item: item["raw_edge"].update(attempts=8_209),
        lambda item: item["cohorts"]["unfiltered"].update(exact=False),
        lambda item: item["cohorts"]["unfiltered"]["cold"].update(execution_ms=3_000.0),
        lambda item: item["cohorts"]["unfiltered"]["warm"].update(temp_written_blocks=1),
        lambda item: item["persistent_state"].update(unchanged=False),
        lambda item: item["session_close"].update(persistent_probe_objects=["bad"]),
    ):
        candidate = json.loads(json.dumps(healthy))
        mutate(candidate)
        assert probe._probe_passed(candidate) is False


@pytest.mark.parametrize(
    ("selector", "completeness", "filter_params"),
    [
        ("all", "all", ()),
        ("provider", "all", ("p1",)),
        ("model", "all", ("m1",)),
        ("provider_model", "all", ("p1", "m1")),
        ("provider_model", "effective", (True, "p1", "m1")),
    ],
)
def test_ranked_probe_candidate_sql_parses_on_postgresql(
    selector, completeness, filter_params
):
    probe = _load_ranked_flags_probe()
    flag_projection = ",".join(
        f"0::bigint AS {name}" for name in probe.FLAG_COLUMN_NAMES
    )
    with db.get_pool().connection() as conn, conn.transaction():
        for relation in (
            "admin_usage_ranked_dimensions",
            "admin_usage_ranked_raw_dimensions",
        ):
            conn.execute(
                f"CREATE TEMP TABLE {relation} ON COMMIT DROP AS "  # noqa: S608
                "SELECT d.*," + flag_projection + " FROM "
                "llm_usage_daily_attempt_dimensions d WITH NO DATA"
            )
        row = conn.execute(
            "EXPLAIN (FORMAT JSON) "
            + probe._candidate_query_sql(
                selector=selector, completeness=completeness
            ),
            (datetime(2026, 5, 5).date(), datetime(2026, 8, 1).date(), *filter_params),
        ).fetchone()

    assert row[0][0]["Plan"]


def test_ranked_probe_temp_build_statements_execute_on_empty_postgresql():
    probe = _load_ranked_flags_probe()
    flag_projection = ",".join(
        f"0::bigint AS {name}" for name in probe.FLAG_COLUMN_NAMES
    )
    local_day = datetime(2026, 8, 1).date()
    with db.get_pool().connection() as conn, conn.transaction():
        conn.execute(
            "CREATE TEMP TABLE admin_usage_ranked_dimensions ON COMMIT DROP AS "
            "SELECT d.*," + flag_projection + " FROM "
            "llm_usage_daily_attempt_dimensions d WITH NO DATA"
        )
        conn.execute(
            "CREATE TEMP TABLE admin_usage_ranked_raw_dimensions "
            "(LIKE admin_usage_ranked_dimensions INCLUDING DEFAULTS) ON COMMIT DROP"
        )
        day_cursor = conn.execute(
            probe._day_rank_update_sql(
                provider_attempt_rollup._effective_attempt_ctes()
            ),
            (
                datetime(2026, 7, 31, 16, tzinfo=timezone.utc),
                datetime(2026, 8, 1, 16, tzinfo=timezone.utc),
                local_day,
            ),
        )
        raw_cursor = conn.execute(
            probe._raw_ranked_insert_sql(
                provider_attempt_rollup._effective_attempt_ctes(
                    cohort_where="false"
                )
            )
        )

    assert day_cursor.rowcount == 0
    assert raw_cursor.rowcount == 0


def _ranked_probe_totals(conn, rows):
    probe = _load_ranked_flags_probe()
    statement = probe._ranked_flag_select(
        priced_relation="priced", gap_relation="call_gaps"
    )
    columns = ",".join(f"sum({name})::bigint" for name in probe.FLAG_COLUMN_NAMES)
    values = ",".join(
        conn.execute("SELECT quote_literal(%s)", (value,)).fetchone()[0]
        if isinstance(value, str)
        else ("true" if value is True else "false")
        for row in rows
        for value in row
    )
    row_width = len(rows[0])
    tuples = ",".join(
        "(" + ",".join(values.split(",")[offset : offset + row_width]) + ")"
        for offset in range(0, len(rows) * row_width, row_width)
    )
    result = conn.execute(
        "WITH priced(attempt_id,call_id,requested_provider,requested_model,"
        "resolved_provider,resolved_model,effective_usage_known) AS (VALUES "
        + tuples
        + "),call_gaps(call_id,missing_outer_ordinals,missing_inner_ordinals)"
        " AS (VALUES ('call-a',1::bigint,0::bigint)),ranked AS ("
        + statement
        + ") SELECT "
        + columns
        + " FROM ranked"
    ).fetchone()
    return dict(zip(probe.FLAG_COLUMN_NAMES, result, strict=True))


def test_ranked_probe_deduplicates_one_call_for_each_resolved_selector():
    rows = (
        ("00000000-0000-5000-8000-000000000001", "call-a", "req", "a", "p1", "shared", True),
        ("00000000-0000-5000-8000-000000000002", "call-a", "req", "b", "p2", "shared", False),
    )
    with db.get_pool().connection() as conn:
        totals = _ranked_probe_totals(conn, rows)

    assert totals["logical_calls_cohort_all_all"] == 1
    assert totals["logical_calls_cohort_provider_all"] == 2
    assert totals["logical_calls_cohort_model_all"] == 1
    assert totals["logical_calls_cohort_provider_model_all"] == 2
    assert totals["logical_calls_requested_all_all"] == 2


def test_ranked_probe_splits_effective_completeness_but_keeps_all_exact():
    rows = (
        ("00000000-0000-5000-8000-000000000001", "call-a", "req", "same", "p1", "shared", True),
        ("00000000-0000-5000-8000-000000000002", "call-a", "req", "same", "p1", "shared", False),
    )
    with db.get_pool().connection() as conn:
        totals = _ranked_probe_totals(conn, rows)

    assert totals["logical_calls_cohort_all_all"] == 1
    assert totals["logical_calls_cohort_all_effective"] == 2
    assert totals["logical_calls_requested_all_all"] == 1
    assert totals["logical_calls_requested_all_effective"] == 2
    assert totals["missing_outer_ordinals_all_all"] == 1
    assert totals["missing_outer_ordinals_all_effective"] == 2


def test_ranked_probe_matches_canonical_calls_for_full_filter_matrix():
    probe = _load_ranked_flags_probe()
    attempts = (
        ("00000000-0000-5000-8000-000000000001", "call-a", "u1", "chat", "req", "a", "p1", "shared", True, "unknown", None),
        ("00000000-0000-5000-8000-000000000002", "call-a", "u1", "chat", "req", "b", "p2", "shared", False, "unknown", None),
        ("00000000-0000-5000-8000-000000000003", "call-b", "u1", "maintenance", "req", "a", "p1", "m1", True, "authoritative", "USD"),
        ("00000000-0000-5000-8000-000000000004", "call-b", "u1", "maintenance", "req", "a", "p1", "m2", True, "estimated", "USD"),
        ("00000000-0000-5000-8000-000000000005", "call-c", "u2", "chat", "req", "c", "p2", "m1", False, "unknown", None),
    )
    gaps = {"call-a": (1, 0), "call-b": (0, 1), "call-c": (0, 0)}
    placeholders = ",".join(
        "(" + ",".join(("%s",) * len(attempts[0])) + ")" for _ in attempts
    )
    gap_placeholders = ",".join("(%s,%s,%s)" for _ in gaps)
    with db.get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "WITH priced(attempt_id,call_id,user_id,cohort_lane,"
                "requested_provider,requested_model,resolved_provider,"
                "resolved_model,effective_usage_known,cost_kind,currency) AS (VALUES "
                + placeholders
                + "),call_gaps(call_id,missing_outer_ordinals,missing_inner_ordinals)"
                " AS (VALUES "
                + gap_placeholders
                + ") "
                + probe._ranked_dimension_select(
                    priced_relation="priced", gap_relation="call_gaps"
                ),
                tuple(value for row in attempts for value in row)
                + tuple(value for call_id, values in gaps.items() for value in (call_id, *values)),
            )
            dimensions = tuple(cur.fetchall())

    raw = tuple(
        {
            "call_id": row[1], "user_id": row[2], "cohort_lane": row[3],
            "requested": (row[4], row[5]), "resolved": (row[6], row[7]),
            "effective": row[8],
        }
        for row in attempts
    )
    for user_id in (None, "u1", "u2"):
        for lane in (None, "chat", "maintenance"):
            for provider in (None, "p1", "p2"):
                for model in (None, "shared", "m1", "m2"):
                    for completeness in ("all", "metered", "unknown"):
                        selected = tuple(
                            row for row in raw
                            if (user_id is None or row["user_id"] == user_id)
                            and (lane is None or row["cohort_lane"] == lane)
                            and (provider is None or row["resolved"][0] == provider)
                            and (model is None or row["resolved"][1] == model)
                            and (
                                completeness == "all"
                                or row["effective"] is (completeness == "metered")
                            )
                        )
                        expected_calls = {row["call_id"] for row in selected}
                        expected_requested = {
                            identity: len({row["call_id"] for row in selected if row["requested"] == identity})
                            for identity in {row["requested"] for row in selected}
                        }
                        expected_resolved = {
                            identity: len({row["call_id"] for row in selected if row["resolved"] == identity})
                            for identity in {row["resolved"] for row in selected}
                        }
                        selector = (
                            "provider_model" if provider and model else
                            "provider" if provider else "model" if model else "all"
                        )
                        stored_completeness = "all" if completeness == "all" else "effective"
                        cohort_column = f"logical_calls_cohort_{selector}_{stored_completeness}"
                        requested_column = f"logical_calls_requested_{selector}_{stored_completeness}"
                        resolved_column = f"logical_calls_cohort_provider_model_{stored_completeness}"
                        outer_column = f"missing_outer_ordinals_{selector}_{stored_completeness}"
                        inner_column = f"missing_inner_ordinals_{selector}_{stored_completeness}"
                        selected_dimensions = tuple(
                            row for row in dimensions
                            if (user_id is None or row["user_id"] == user_id)
                            and (lane is None or row["cohort_lane"] == lane)
                            and (provider is None or row["resolved_provider"] == provider)
                            and (model is None or row["resolved_model"] == model)
                            and (
                                completeness == "all"
                                or row["effective_usage_known"] is (completeness == "metered")
                            )
                        )
                        actual_requested = {}
                        actual_resolved = {}
                        for row in selected_dimensions:
                            requested_identity = (row["requested_provider"], row["requested_model"])
                            resolved_identity = (row["resolved_provider"], row["resolved_model"])
                            actual_requested[requested_identity] = actual_requested.get(requested_identity, 0) + row[requested_column]
                            actual_resolved[resolved_identity] = actual_resolved.get(resolved_identity, 0) + row[resolved_column]
                        actual_requested = {key: value for key, value in actual_requested.items() if value}
                        actual_resolved = {key: value for key, value in actual_resolved.items() if value}
                        context = (user_id, lane, provider, model, completeness)
                        assert sum(row[cohort_column] for row in selected_dimensions) == len(expected_calls), context
                        assert actual_requested == expected_requested, context
                        assert actual_resolved == expected_resolved, context
                        assert sum(row[outer_column] for row in selected_dimensions) == sum(gaps[call_id][0] for call_id in expected_calls), context
                        assert sum(row[inner_column] for row in selected_dimensions) == sum(gaps[call_id][1] for call_id in expected_calls), context


def test_admin_usage_scale_harness_self_check():
    """The opt-in scale proof must keep its timing and SQL safety gates."""
    result = subprocess.run(
        [
            sys.executable,
            "scripts/perf/admin_usage_scale.py",
            "--self-test",
        ],
        cwd=Path(__file__).parent.parent,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "p50_ms": 30.0,
        "p95_ms": 50.0,
        "sensitive_column_check": "passed",
        "time_range_check": "passed",
    }


def test_scale_evidence_atomic_write_preserves_previous_artifact_on_replace_failure(
    tmp_path, monkeypatch
):
    harness = _load_usage_scale_harness()
    output = tmp_path / "scale-evidence.json"
    output.write_text('{"previous":true}\n', encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr(harness.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        harness._write_evidence_atomic(
            output,
            '{"passed":false,"workflow":{"terminal_phase":"cleanup_failed"}}\n',
        )

    assert output.read_text(encoding="utf-8") == '{"previous":true}\n'
    assert list(tmp_path.glob(".scale-evidence.json.*.tmp")) == []


def test_scale_failed_evidence_atomic_write_is_complete_json(tmp_path):
    harness = _load_usage_scale_harness()
    output = tmp_path / "failed-scale-evidence.json"
    evidence = {
        "passed": False,
        "workflow": {
            "terminal_phase": "fixture_workload_failed",
            "failure": {
                "phase": "fixture_workload",
                "type": "RuntimeError",
                "message": "timing failed",
            },
            "business_status": "not_run",
        },
    }

    harness._write_evidence_atomic(
        output, json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    )

    assert json.loads(output.read_text(encoding="utf-8")) == evidence
    assert list(tmp_path.glob(".failed-scale-evidence.json.*.tmp")) == []


def test_admin_usage_scale_uses_approved_three_second_p95_budget():
    harness = _load_usage_scale_harness()

    assert harness.P95_BUDGET_MS == 3_000.0
    assert 2_999.999 < harness.P95_BUDGET_MS
    assert not 3_000.0 < harness.P95_BUDGET_MS


def test_admin_usage_scale_attempt_fixture_is_explicit_and_bounded():
    harness = _load_usage_scale_harness()

    assert harness._resolve_attempt_rows(3_000_000, None) == 3_000_000
    assert harness._resolve_attempt_rows(3_000_000, 0) == 0
    assert harness._resolve_attempt_rows(3_000_000, 1_500_000) == 1_500_000
    for invalid in (-1, 3_000_001):
        with pytest.raises(SystemExit, match="attempt-rows"):
            harness._resolve_attempt_rows(3_000_000, invalid)


@pytest.mark.parametrize(
    ("formal", "users", "history_days", "fails"),
    [
        (True, 1_999, 365, True),
        (True, 2_001, 365, True),
        (True, 2_000, 364, True),
        (True, 2_000, 366, True),
        (True, 2_000, 365, False),
        (False, 7, 90, False),
    ],
)
def test_formal_configuration_locks_users_and_history_days(
    formal, users, history_days, fails
):
    harness = _load_usage_scale_harness()

    def call():
        harness._validate_run_configuration(
            formal=formal,
            rows=3_000_000 if formal else 100,
            attempt_rows=3_000_000 if formal else 100,
            users=users,
            history_days=history_days,
        )

    if fails:
        with pytest.raises(SystemExit, match="formal mode requires exactly"):
            call()
    else:
        call()


def test_formal_source_expectation_is_derived_from_seed_formula_and_fixed_window():
    harness = _load_usage_scale_harness()
    start_at, end_at = harness._production_window()
    partition = SimpleNamespace(
        raw_days=(
            datetime(2026, 5, 4, tzinfo=timezone.utc).date(),
            datetime(2026, 8, 2, tzinfo=timezone.utc).date(),
        )
    )

    expected = harness._derive_expected_source_counts(
        rows=3_000_000,
        attempt_rows=3_000_000,
        history_days=365,
        start_at=start_at,
        end_at=end_at,
        raw_bounds=harness._raw_edge_bounds(
            partition, start_at=start_at, end_at=end_at
        ),
    )

    assert expected == {
        "total_rows": 3_000_000,
        "attempt_rows": 3_000_000,
        "rows_in_90d": 739_736,
        "attempt_rows_in_90d_job_cohort": 739_736,
        "expected_raw_edge_turn_rows": 8_208,
        "expected_raw_edge_attempt_rows": 8_208,
        "expected_raw_edge_logical_calls": 8_208,
    }


def test_formal_resume_integrity_statements_parse_on_postgresql():
    harness = _load_usage_scale_harness()
    checks = harness._semantic_integrity_checks(
        prefix="scale_usage_42e02f444a_", usage_rollup=usage_rollup
    )

    with db.get_pool().connection() as conn:
        for name, sql, params, fields in checks:
            row = conn.execute("EXPLAIN (FORMAT JSON) " + sql, params).fetchone()
            assert row[0][0]["Plan"]
            assert name
            assert fields


def test_formal_resume_integrity_statements_execute_bounded_on_empty_postgresql():
    harness = _load_usage_scale_harness()
    checks = harness._semantic_integrity_checks(
        prefix="scale_usage_42e02f444a_", usage_rollup=usage_rollup
    )

    with db.get_pool().connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for name, sql, original_params, fields in checks:
            params = list(original_params)
            if name == "source_turns":
                params[4] = 10
            elif name == "source_attempts":
                params[5] = 10
            result, evidence = harness._execute_integrity_statement(
                conn,
                name=name,
                sql=sql,
                params=tuple(params),
                result_fields=fields,
            )
            assert tuple(result) == fields
            assert evidence["statement_timeout_ms"] == 180_000
            assert evidence["elapsed_ms"] >= 0
            assert evidence["plan_relations"]


def test_integrity_timeout_error_includes_check_elapsed_timeout_and_plan():
    harness = _load_usage_scale_harness()

    class Cursor:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class Connection:
        calls = 0

        def execute(self, sql, params=()):
            self.calls += 1
            if self.calls == 1:
                return Cursor((None,))
            if self.calls == 2:
                return Cursor(
                    ([{"Plan": {"Node Type": "Seq Scan", "Relation Name": "facts"}}],)
                )
            raise RuntimeError("canceling statement due to statement timeout")

    with pytest.raises(
        RuntimeError,
        match=(
            "attempt_dimensions.*elapsed_ms=.*statement_timeout_ms=180000"
            ".*plan_relations=.*facts"
        ),
    ):
        harness._execute_integrity_statement(
            Connection(),
            name="attempt_dimensions",
            sql="SELECT 1 FROM facts",
            params=(),
            result_fields=("expected_rows",),
        )


def test_attempt_dimension_exact_check_does_not_force_wide_cte_materialization():
    harness = _load_usage_scale_harness()
    sql = harness._attempt_dimension_integrity_sql()

    assert "expected AS MATERIALIZED" not in sql
    assert "actual AS MATERIALIZED" not in sql
    assert "FULL JOIN" in sql
    assert "IS DISTINCT FROM" in sql


def test_membership_exact_anti_join_keeps_actual_relation_indexable():
    harness = _load_usage_scale_harness()
    sql = harness._membership_integrity_sql()

    assert "MATERIALIZED" not in sql
    assert "NOT EXISTS" not in sql
    assert "JOIN llm_provider_attempts" in sql
    assert "JOIN v2_turn_metrics" in sql
    assert "IS DISTINCT FROM" in sql
    assert "missing_outer_ordinals" in sql
    assert "missing_inner_ordinals" in sql


def test_scale_workflow_runs_business_only_after_verified_empty_cleanup():
    harness = _load_usage_scale_harness()
    events = []
    counts = {"users": 1}

    def prepare(arm_cleanup):
        events.append("prepare")
        arm_cleanup()

    def cleanup():
        events.append("cleanup")
        counts["users"] = 0

    def collect_counts():
        events.append("count")
        return dict(counts)

    result = harness._execute_scale_workflow(
        prepare_fixture=prepare,
        run_fixture_workload=lambda: events.append("workload"),
        cleanup_fixture=cleanup,
        collect_database_counts=collect_counts,
        produce_business=lambda: (
            events.append("business"),
            {"business": {"passed": True}},
        )[1],
    )

    assert events == [
        "prepare",
        "workload",
        "cleanup",
        "count",
        "count",
        "business",
        "count",
    ]
    assert result["terminal_phase"] == "complete"
    assert result["failure"] is None
    assert result["business_status"] == "passed"
    assert result["post_fixture_empty_counts"] == {"users": 0}
    assert result["business_database_counts"] == {
        "pre": {"users": 0},
        "post": {"users": 0},
    }


def test_scale_workflow_timing_failure_cleans_and_never_runs_business():
    harness = _load_usage_scale_harness()
    events = []
    counts = {"users": 1}

    def prepare(arm_cleanup):
        events.append("prepare")
        arm_cleanup()

    def fail_workload():
        events.append("workload")
        raise RuntimeError("timing failed")

    def cleanup():
        events.append("cleanup")
        counts["users"] = 0

    result = harness._execute_scale_workflow(
        prepare_fixture=prepare,
        run_fixture_workload=fail_workload,
        cleanup_fixture=cleanup,
        collect_database_counts=lambda: (
            events.append("count"),
            dict(counts),
        )[1],
        produce_business=lambda: events.append("business"),
    )

    assert events == ["prepare", "workload", "cleanup", "count"]
    assert result["terminal_phase"] == "fixture_workload_failed"
    assert result["failure"] == {
        "phase": "fixture_workload",
        "type": "RuntimeError",
        "message": "timing failed",
    }
    assert result["cleanup"]["status"] == "passed"
    assert result["post_fixture_empty_counts"] == {"users": 0}
    assert result["business_status"] == "not_run"


def test_scale_workflow_cleanup_failure_never_runs_business():
    harness = _load_usage_scale_harness()
    events = []

    def prepare(arm_cleanup):
        events.append("prepare")
        arm_cleanup()

    def fail_cleanup():
        events.append("cleanup")
        raise RuntimeError("cleanup failed")

    result = harness._execute_scale_workflow(
        prepare_fixture=prepare,
        run_fixture_workload=lambda: events.append("workload"),
        cleanup_fixture=fail_cleanup,
        collect_database_counts=lambda: (
            events.append("count"),
            {"users": 1},
        )[1],
        produce_business=lambda: events.append("business"),
    )

    assert events == ["prepare", "workload", "cleanup", "count"]
    assert result["terminal_phase"] == "cleanup_failed"
    assert result["failure"] == {
        "phase": "fixture_cleanup",
        "type": "RuntimeError",
        "message": "cleanup failed",
    }
    assert result["cleanup"]["status"] == "failed"
    assert result["post_fixture_empty_counts"] == {"users": 1}
    assert result["business_status"] == "not_run"


def test_scale_workflow_business_failure_collects_post_counts_in_finally():
    harness = _load_usage_scale_harness()
    events = []
    counts = {"users": 1}

    def prepare(arm_cleanup):
        events.append("prepare")
        arm_cleanup()

    def cleanup():
        events.append("cleanup")
        counts["users"] = 0

    def collect_counts():
        events.append("count")
        return dict(counts)

    def fail_business():
        events.append("business")
        assert counts == {"users": 0}
        raise ValueError("business failed")

    result = harness._execute_scale_workflow(
        prepare_fixture=prepare,
        run_fixture_workload=lambda: events.append("workload"),
        cleanup_fixture=cleanup,
        collect_database_counts=collect_counts,
        produce_business=fail_business,
    )

    assert events == [
        "prepare",
        "workload",
        "cleanup",
        "count",
        "count",
        "business",
        "count",
    ]
    assert result["terminal_phase"] == "business_failed"
    assert result["failure"] == {
        "phase": "business_producer",
        "type": "ValueError",
        "message": "business failed",
    }
    assert result["business_status"] == "failed"
    assert result["business_database_counts"]["post"] == {"users": 0}


def _run_entry_args(tmp_path, *, resumed=False, validate_only=False):
    return SimpleNamespace(
        rows=3_000_000 if resumed else 100,
        attempt_rows=3_000_000 if resumed else 100,
        users=2_000 if resumed else 10,
        runs=5,
        history_days=365,
        non_formal=not resumed,
        resume_prefix="scale_usage_42e02f444a_" if resumed else "",
        validate_resume_only=validate_only,
        keep_data=False,
        database_url=(
            "postgresql://postgres:test@127.0.0.1:55432/"
            "feedling_usage_scale_entry_test"
        ),
        output=str(tmp_path / "entry-evidence.json"),
        business_path_output="",
        precondition_note="entry integration spy",
    )


def _install_run_entry_boundary(monkeypatch, harness):
    monkeypatch.setenv("DATABASE_URL", os.environ.get("DATABASE_URL", ""))
    monkeypatch.setenv(
        "FEEDLING_V2_USAGE_ROLLUP_ENABLED",
        os.environ.get("FEEDLING_V2_USAGE_ROLLUP_ENABLED", ""),
    )

    class Connection:
        pass

    class Pool:
        def connection(self):
            return nullcontext(Connection())

    pool = Pool()
    monkeypatch.setattr(
        harness,
        "_validate_scale_database_url",
        lambda _url: {
            "database": "feedling_usage_scale_entry_test",
            "host": "127.0.0.1",
            "port": 55432,
        },
    )
    monkeypatch.setattr(
        harness, "_install_validated_scale_pool", lambda _db, _identity: pool
    )
    monkeypatch.setattr(
        harness, "_validate_connected_scale_database", lambda _conn, identity: identity
    )
    monkeypatch.setattr(harness, "_assert_schema", lambda _conn: {"valid": True})
    monkeypatch.setattr(harness, "_assert_empty_dedicated_database", lambda _conn: None)
    monkeypatch.setattr(
        harness,
        "_validate_rolling_partition",
        lambda _partition: {"rollup_days": ["2026-08-01"], "raw_days": []},
    )
    monkeypatch.setattr(
        harness,
        "_seed_fixture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("copied production workflow used")
        ),
    )
    monkeypatch.setattr(
        harness,
        "_capture_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("copied production workflow used")
        ),
    )
    monkeypatch.setattr(harness, "_delete_fixture", lambda *_args: None)
    monkeypatch.setattr(
        harness,
        "_fixture_counts",
        lambda *_args: {"users": 0, "turn_watermark": 0, "attempt_watermark": 0},
    )
    monkeypatch.setattr(harness, "_database_counts", lambda _conn: {"users": 0})
    return pool


def _install_workflow_callback_spy(monkeypatch, harness, *, scenario, events):
    state = {"rows": 0}

    def build(context):
        evidence = context["evidence"]
        resumed = context["resumed"]

        def prepare(arm_cleanup):
            events.append("prepare_resume" if resumed else "prepare_fresh")
            arm_cleanup()
            state["rows"] = 1

        def workload():
            events.append("workload")
            if scenario == "timing_failure":
                raise RuntimeError("timing failed")

        def cleanup():
            events.append("cleanup")
            if scenario == "cleanup_failure":
                raise RuntimeError("cleanup failed")
            state["rows"] = 0
            evidence["cleanup"] = {
                "foreign_key_cascade_verified": True,
                "watermark_removed": True,
                "residual_counts": {"users": 0},
            }

        def counts():
            events.append("count")
            return {"users": state["rows"]}

        def business():
            events.append("business")
            assert state["rows"] == 0
            if scenario == "business_failure":
                raise ValueError("business failed")
            return {
                "pool": {"measurement": "entry-spy"},
                "business": {"producer": "entry-spy"},
            }

        return {
            "prepare_fixture": prepare,
            "run_fixture_workload": workload,
            "cleanup_fixture": cleanup,
            "collect_database_counts": counts,
            "produce_business": business,
        }

    monkeypatch.setattr(
        harness, "_build_scale_workflow_callbacks", build, raising=False
    )


@pytest.mark.parametrize("resumed", [False, True])
def test_run_entry_uses_single_workflow_for_fresh_and_resume_happy_path(
    tmp_path, monkeypatch, resumed
):
    harness = _load_usage_scale_harness()
    _install_run_entry_boundary(monkeypatch, harness)
    if resumed:
        monkeypatch.setattr(
            harness,
            "_collect_resume_snapshot",
            lambda *_args, **_kwargs: _healthy_formal_resume_snapshot(),
        )
    events = []
    _install_workflow_callback_spy(
        monkeypatch, harness, scenario="happy", events=events
    )

    exit_code = harness._run(_run_entry_args(tmp_path, resumed=resumed))
    artifact = json.loads(
        (tmp_path / "entry-evidence.json").read_text(encoding="utf-8")
    )

    assert exit_code == 1
    assert artifact["workflow"]["terminal_phase"] == "complete"
    assert artifact["workflow"]["business_result"] == {
        "pool": {"measurement": "entry-spy"},
        "business": {"producer": "entry-spy"},
    }
    assert artifact["business_path"] == {"producer": "entry-spy"}
    assert events == [
        "prepare_resume" if resumed else "prepare_fresh",
        "workload",
        "cleanup",
        "count",
        "count",
        "business",
        "count",
    ]


@pytest.mark.parametrize(
    ("scenario", "terminal_phase", "business_status", "expected_events"),
    [
        (
            "timing_failure",
            "fixture_workload_failed",
            "not_run",
            ["prepare_fresh", "workload", "cleanup", "count"],
        ),
        (
            "cleanup_failure",
            "cleanup_failed",
            "not_run",
            ["prepare_fresh", "workload", "cleanup", "count"],
        ),
        (
            "business_failure",
            "business_failed",
            "failed",
            [
                "prepare_fresh",
                "workload",
                "cleanup",
                "count",
                "count",
                "business",
                "count",
            ],
        ),
    ],
)
def test_run_entry_writes_atomic_failure_from_single_workflow(
    tmp_path,
    monkeypatch,
    scenario,
    terminal_phase,
    business_status,
    expected_events,
):
    harness = _load_usage_scale_harness()
    _install_run_entry_boundary(monkeypatch, harness)
    events = []
    _install_workflow_callback_spy(
        monkeypatch, harness, scenario=scenario, events=events
    )

    exit_code = harness._run(_run_entry_args(tmp_path))
    artifact = json.loads(
        (tmp_path / "entry-evidence.json").read_text(encoding="utf-8")
    )

    assert exit_code == 1
    assert artifact["passed"] is False
    assert artifact["workflow"]["terminal_phase"] == terminal_phase
    assert artifact["workflow"]["business_status"] == business_status
    assert artifact["workflow"]["business_result"] is None
    assert events == expected_events
    if scenario == "timing_failure":
        assert artifact["workflow"]["post_fixture_empty_counts"] == {"users": 0}
    if scenario == "cleanup_failure":
        assert artifact["workflow"]["post_fixture_empty_counts"] == {"users": 1}
    if scenario == "business_failure":
        assert artifact["workflow"]["business_database_counts"] == {
            "pre": {"users": 0},
            "post": {"users": 0},
        }


def test_run_entry_invalid_resume_never_arms_or_mutates_fixture(
    tmp_path, monkeypatch
):
    harness = _load_usage_scale_harness()
    _install_run_entry_boundary(monkeypatch, harness)
    invalid = _healthy_formal_resume_snapshot()
    invalid["prefix_counts"]["users"] = 1_999
    monkeypatch.setattr(
        harness,
        "_collect_resume_snapshot",
        lambda *_args, **_kwargs: invalid,
    )

    exit_code = harness._run(_run_entry_args(tmp_path, resumed=True))
    artifact = json.loads(
        (tmp_path / "entry-evidence.json").read_text(encoding="utf-8")
    )

    assert exit_code == 1
    assert artifact["workflow"]["phase_trace"] == ["prepare_fixture"]
    assert artifact["workflow"]["failure"]["phase"] == "prepare_fixture"
    assert artifact["workflow"]["cleanup"] == {
        "armed": False,
        "status": "not_run",
        "failure": None,
    }
    assert artifact["workflow"]["business_status"] == "not_run"
    assert artifact["workflow"]["business_result"] is None
    assert artifact["fixture"]["preexisting_counts"] is None
    assert artifact["fixture"]["resume_validation"] is None


def test_run_entry_validate_only_is_read_only_and_skips_workflow(
    tmp_path, monkeypatch
):
    harness = _load_usage_scale_harness()
    _install_run_entry_boundary(monkeypatch, harness)
    snapshot = _healthy_formal_resume_snapshot()
    monkeypatch.setattr(
        harness,
        "_collect_resume_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )

    exit_code = harness._run(
        _run_entry_args(tmp_path, resumed=True, validate_only=True)
    )
    artifact = json.loads(
        (tmp_path / "entry-evidence.json").read_text(encoding="utf-8")
    )

    assert exit_code == 0
    assert artifact["validated"] is True
    assert artifact["resume_validation"] == {
        "pre": snapshot,
        "post": None,
        "stable": None,
    }
    assert "workflow" not in artifact


def test_database_counts_observe_user_insert_and_exact_cleanup():
    harness = _load_usage_scale_harness()
    user_id = "scale_count_probe_user"
    with db.get_pool().connection() as conn:
        before = harness._database_counts(conn)
        try:
            conn.execute(
                "INSERT INTO users (user_id,doc,created_at) VALUES (%s,%s,%s)",
                (user_id, Jsonb({"scale_count_probe": True}), NOW_UTC.isoformat()),
            )
            during = harness._database_counts(conn)
        finally:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))
        after = harness._database_counts(conn)

    assert set(before) == {
        "users",
        "v2_turn_metrics",
        "llm_provider_attempts",
        "llm_provider_attempt_corrections",
        "llm_rate_cards",
        "v2_usage_daily_users",
        "v2_usage_daily_dimensions",
        "v2_usage_rollup_watermarks",
        "llm_usage_daily_attempt_dimensions",
        "llm_usage_daily_call_memberships",
        "llm_usage_rollup_watermarks",
        "llm_usage_rollup_dirty_days",
    }
    assert during == {**before, "users": before["users"] + 1}
    assert after == before


def _healthy_scale_workflow_evidence():
    zero = {
        "users": 0,
        "v2_turn_metrics": 0,
        "llm_provider_attempts": 0,
    }
    return {
        "terminal_phase": "complete",
        "failure": None,
        "cleanup": {"armed": True, "status": "passed", "failure": None},
        "post_fixture_empty_counts": zero.copy(),
        "business_database_counts": {
            "pre": zero.copy(),
            "post": zero.copy(),
        },
        "business_status": "passed",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item["post_fixture_empty_counts"].update(users=1),
        lambda item: item["business_database_counts"]["pre"].update(users=1),
        lambda item: item["business_database_counts"]["post"].update(users=1),
    ],
)
def test_workflow_evidence_gate_rejects_nonzero_database_counts(mutate):
    harness = _load_usage_scale_harness()
    evidence = _healthy_scale_workflow_evidence()

    assert harness._workflow_evidence_passed(evidence) is True
    mutate(evidence)
    assert harness._workflow_evidence_passed(evidence) is False


def _healthy_formal_resume_snapshot():
    counts = {
        "users": 2_000,
        "v2_turn_metrics": 3_000_000,
        "llm_provider_attempts": 3_000_000,
        "llm_provider_attempt_corrections": 0,
        "v2_usage_daily_users": 731_199,
        "v2_usage_daily_dimensions": 731_199,
        "llm_usage_daily_attempt_dimensions": 731_199,
        "llm_usage_daily_call_memberships": 3_000_000,
        "turn_watermark": 1,
        "attempt_watermark": 1,
        "dirty_days": 0,
    }
    return {
        "prefix": "scale_usage_42e02f444a_",
        "global_counts": counts.copy(),
        "prefix_counts": counts.copy(),
        "user_shape": {
            "invalid_user_ids": 0,
            "invalid_fixture_docs": 0,
            "missing_user_sequence": 0,
            "invalid_created_at": 0,
        },
        "source_integrity": {
            "turn_distinct_jobs": 3_000_000,
            "attempt_distinct_ids": 3_000_000,
            "attempt_distinct_calls": 3_000_000,
            "orphan_turn_users": 0,
            "orphan_attempt_users": 0,
            "attempt_job_user_mismatches": 0,
            "non_runtime_attempts": 0,
        },
        "rollup_integrity": {
            "membership_distinct_attempts": 3_000_000,
            "membership_orphans": 0,
            "attempts_without_membership": 0,
        },
        "reference_counts": {"llm_rate_cards": 0},
        "semantic_integrity": {
            "source_turns": {
                "expected_rows": 3_000_000,
                "actual_rows": 3_000_000,
                "mismatched_rows": 0,
            },
            "source_attempts": {
                "expected_rows": 3_000_000,
                "actual_rows": 3_000_000,
                "mismatched_rows": 0,
            },
            "daily_users": {
                "expected_rows": 731_199,
                "actual_rows": 731_199,
                "mismatched_rows": 0,
            },
            "daily_dimensions": {
                "expected_rows": 731_199,
                "actual_rows": 731_199,
                "mismatched_rows": 0,
            },
            "attempt_dimensions": {
                "expected_rows": 731_199,
                "actual_rows": 731_199,
                "mismatched_rows": 0,
            },
            "memberships": {
                "expected_rows": 3_000_000,
                "actual_rows": 3_000_000,
                "mismatched_rows": 0,
            },
        },
        "integrity_query_evidence": {
            name: {
                "passed": True,
                "statement_timeout_ms": 180_000,
                "elapsed_ms": 1.0,
                "plan_relations": ["v2_turn_metrics"],
            }
            for name in (
                "source_turns",
                "source_attempts",
                "daily_users",
                "daily_dimensions",
                "attempt_dimensions",
                "memberships",
            )
        },
        "turn_watermark": {
            "bootstrap_complete": True,
            "dirty_from_day": None,
            "dirty_through_day": None,
            "last_error": None,
        },
        "attempt_watermark": {
            "bootstrap_complete": True,
            "completed_through_day": "2026-08-02",
            "retained_from": "2025-06-29",
            "retention_pending_from": None,
        },
        "source": {
            "total_rows": 3_000_000,
            "attempt_rows": 3_000_000,
            "rows_in_90d": 739_736,
            "attempt_rows_in_90d_job_cohort": 739_736,
            "expected_raw_edge_turn_rows": 8_208,
            "expected_raw_edge_attempt_rows": 8_208,
            "expected_raw_edge_logical_calls": 8_208,
        },
    }


@pytest.mark.parametrize(
    "prefix",
    [
        "scale_usage_42e02f444a",
        "scale_usage_42E02F444A_",
        "scale_usage_42e02f444_",
        "scale_usage_42e02f444aa_",
        "other_42e02f444a_",
        "scale_usage_42e02f444a_%",
    ],
)
def test_formal_resume_rejects_wrong_prefix(prefix):
    harness = _load_usage_scale_harness()

    with pytest.raises(SystemExit, match="resume-prefix"):
        harness._validate_resume_prefix(prefix, formal=True)


def test_formal_resume_rejects_nonformal_mode():
    harness = _load_usage_scale_harness()

    with pytest.raises(SystemExit, match="formal-only"):
        harness._validate_resume_prefix("scale_usage_42e02f444a_", formal=False)


@pytest.mark.parametrize(
    ("resumed", "keep_data", "validate_only", "message"),
    [
        (False, False, True, "requires --resume-prefix"),
        (True, True, False, "cannot be combined with --keep-data"),
        (True, True, True, "cannot be combined with --keep-data"),
    ],
)
def test_formal_resume_mode_options_fail_closed(
    resumed, keep_data, validate_only, message
):
    harness = _load_usage_scale_harness()

    with pytest.raises(SystemExit, match=message):
        harness._validate_resume_mode_options(
            resumed=resumed,
            keep_data=keep_data,
            validate_only=validate_only,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda item: item["prefix_counts"].update(
                llm_provider_attempts=2_999_999
            ),
            "partial",
        ),
        (
            lambda item: item["global_counts"].update(v2_turn_metrics=3_000_001),
            "foreign",
        ),
        (
            lambda item: item.update(prefix="scale_usage_deadbeef00_"),
            "prefix mismatch",
        ),
        (
            lambda item: item["source_integrity"].update(
                attempt_job_user_mismatches=1
            ),
            "reuse mismatch",
        ),
        (
            lambda item: item["user_shape"].update(invalid_created_at=1),
            "fixture users",
        ),
        (
            lambda item: item["reference_counts"].update(llm_rate_cards=1),
            "foreign pricing reference",
        ),
        (
            lambda item: item["source"].update(
                rows_in_90d=739_735,
                attempt_rows_in_90d_job_cohort=739_735,
            ),
            "deterministic source mismatch",
        ),
        (
            lambda item: item["semantic_integrity"]["source_turns"].update(
                mismatched_rows=1
            ),
            "semantic integrity mismatch",
        ),
        (
            lambda item: item["semantic_integrity"]["daily_users"].update(
                mismatched_rows=1
            ),
            "semantic integrity mismatch",
        ),
        (
            lambda item: item["semantic_integrity"]["attempt_dimensions"].update(
                mismatched_rows=1
            ),
            "semantic integrity mismatch",
        ),
        (
            lambda item: item["semantic_integrity"]["memberships"].update(
                mismatched_rows=1,
            ),
            "semantic integrity mismatch",
        ),
        (
            lambda item: item["integrity_query_evidence"]["memberships"].update(
                passed=False
            ),
            "integrity query evidence",
        ),
        (
            lambda item: item["attempt_watermark"].update(
                completed_through_day="2026-07-31"
            ),
            "partial or stale.*actual=.*2026-07-31",
        ),
        (
            lambda item: item["attempt_watermark"].update(
                retained_from="2026-05-05"
            ),
            "partial or stale.*actual=.*2026-05-05",
        ),
    ],
)
def test_formal_resume_snapshot_rejects_partial_foreign_or_reused_fixture(
    mutation, message
):
    harness = _load_usage_scale_harness()
    snapshot = _healthy_formal_resume_snapshot()
    mutation(snapshot)

    with pytest.raises(RuntimeError, match=message):
        harness._validate_resume_snapshot(
            snapshot, prefix="scale_usage_42e02f444a_"
        )


def test_formal_resume_snapshot_accepts_exact_complete_fixture():
    harness = _load_usage_scale_harness()
    snapshot = _healthy_formal_resume_snapshot()

    assert (
        harness._validate_resume_snapshot(
            snapshot, prefix="scale_usage_42e02f444a_"
        )
        is snapshot
    )


def test_formal_user_shape_rejects_single_same_count_created_at_mutation():
    harness = _load_usage_scale_harness()
    prefix = "scale_usage_a11ce00001_"
    with db.get_pool().connection() as conn:
        try:
            harness._seed_fixture(
                conn,
                prefix=prefix,
                rows=1,
                attempt_rows=1,
                users=1,
                end_at=harness.SCALE_NOW_UTC,
                history_days=365,
            )
            before = harness._fixture_counts(conn, prefix)
            conn.execute(
                "UPDATE users SET created_at=%s "
                "WHERE user_id=%s",
                (
                    (harness.SCALE_NOW_UTC - timedelta(days=365, seconds=-1))
                    .isoformat(),
                    prefix + "000000",
                ),
            )
            after = harness._fixture_counts(conn, prefix)
            shape = harness._collect_user_shape(
                conn,
                prefix=prefix,
                users=1,
                expected_created_at=harness.SCALE_NOW_UTC - timedelta(days=365),
            )
        finally:
            harness._delete_fixture(conn, prefix)

    assert before == after
    assert shape["invalid_created_at"] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("installation_id", "installation-1"),
        ("runtime", "runtime-1"),
        ("turn_id", "turn-1"),
        ("round_id", "round-1"),
        ("provider_request_id", "request-1"),
        ("usage_unknown_reason", "provider_omitted"),
        ("latency_ms", 1.0),
        ("rate_card_version", "rate-1"),
    ],
)
def test_source_attempt_exact_check_rejects_same_count_nullable_field_mutation(
    field, value
):
    harness = _load_usage_scale_harness()
    prefix = "scale_usage_b11ce00001_"
    with db.get_pool().connection() as conn:
        try:
            harness._seed_fixture(
                conn,
                prefix=prefix,
                rows=1,
                attempt_rows=1,
                users=1,
                end_at=harness.SCALE_NOW_UTC,
                history_days=365,
            )
            before = harness._fixture_counts(conn, prefix)
            conn.execute(
                f"UPDATE llm_provider_attempts SET {field}=%s "  # noqa: S608
                "WHERE user_id=%s",
                (value, prefix + "000000"),
            )
            after = harness._fixture_counts(conn, prefix)
            params = (
                prefix,
                1,
                prefix,
                harness.SCALE_NOW_UTC,
                365 * 86_400,
                1,
                prefix,
                prefix,
                prefix,
            )
            row = conn.execute(
                harness._source_attempt_integrity_sql(), params
            ).fetchone()
        finally:
            harness._delete_fixture(conn, prefix)

    assert before == after
    assert tuple(map(int, row)) == (1, 1, 1)


def test_attempt_source_exact_check_excludes_only_wallclock_metadata():
    harness = _load_usage_scale_harness()
    sql = harness._source_attempt_integrity_sql()

    assert "created_at" not in sql
    assert "updated_at" not in sql


def test_fresh_formal_empty_database_gate_rejects_rate_card_reference_state():
    harness = _load_usage_scale_harness()

    class Cursor:
        def __init__(self, value):
            self._value = value

        def fetchone(self):
            return (self._value,)

    class Connection:
        def execute(self, sql):
            return Cursor(1 if "FROM llm_rate_cards" in sql else 0)

    with pytest.raises(RuntimeError, match="llm_rate_cards.*1"):
        harness._assert_empty_dedicated_database(Connection())


def test_admin_usage_scale_raw_edge_bounds_are_exact_half_open_query_edges():
    harness = _load_usage_scale_harness()
    start_at, end_at = harness._production_window()
    partition = SimpleNamespace(
        raw_days=(
            datetime(2026, 5, 4, tzinfo=timezone.utc).date(),
            datetime(2026, 8, 2, tzinfo=timezone.utc).date(),
        )
    )

    assert harness._raw_edge_bounds(
        partition, start_at=start_at, end_at=end_at
    ) == (
        (
            datetime(2026, 5, 4, 12, 30, tzinfo=timezone.utc),
            datetime(2026, 5, 4, 16, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 8, 1, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc),
        ),
    )


def test_admin_usage_scale_formal_gate_requires_attempt_index_for_both_cohorts():
    harness = _load_usage_scale_harness()
    cohorts = {
        name: {
            "timing": {"p95_ms": 2_999.0},
            "attempt_ledger_statement_count": 1,
            "attempt_runtime_job_index_used": True,
            "attempt_rollup_relations_used": True,
            "attempt_full_history_scan_absent": True,
            "attempt_rate_card_probe_loops_absent": True,
            "attempt_full_window_call_probe_loops_absent": True,
        }
        for name in ("unfiltered", "provider_model_filtered")
    }

    cleanup = {
        "foreign_key_cascade_verified": True,
        "watermark_removed": True,
        "residual_counts": {"users": 0, "watermark": 0, "dirty_days": 0},
    }
    source = {"total_rows": 3_000_000, "attempt_rows": 3_000_000}
    fixture = {
        "rows": 3_000_000,
        "attempt_rows": 3_000_000,
        "users": 2_000,
        "history_days": 365,
        "resumed": False,
    }
    def gate():
        return harness._formal_gate_passed(
            cohorts, cleanup, source=source, fixture=fixture, formal=True
        )
    assert gate() is True
    cohorts["provider_model_filtered"]["attempt_runtime_job_index_used"] = False
    assert gate() is False
    cohorts["provider_model_filtered"]["attempt_runtime_job_index_used"] = True
    cohorts["provider_model_filtered"]["attempt_full_history_scan_absent"] = False
    assert gate() is False
    cohorts["provider_model_filtered"]["attempt_full_history_scan_absent"] = True
    cleanup["residual_counts"]["dirty_days"] = 1
    assert gate() is False


def test_admin_usage_scale_formal_gate_rejects_small_or_nonformal_fixture_even_if_healthy():
    harness = _load_usage_scale_harness()
    cohorts = {
        name: {
            "timing": {"p95_ms": 1.0},
            "attempt_ledger_statement_count": 1,
            "attempt_runtime_job_index_used": True,
            "attempt_rollup_relations_used": True,
            "attempt_full_history_scan_absent": True,
            "attempt_rate_card_probe_loops_absent": True,
            "attempt_full_window_call_probe_loops_absent": True,
        }
        for name in ("unfiltered", "provider_model_filtered")
    }
    cleanup = {
        "foreign_key_cascade_verified": True,
        "watermark_removed": True,
        "residual_counts": {"all": 0},
    }

    assert not harness._formal_gate_passed(
        cohorts,
        cleanup,
        source={"total_rows": 100, "attempt_rows": 100},
        fixture={"rows": 100, "attempt_rows": 100},
        formal=True,
    )
    assert not harness._formal_gate_passed(
        cohorts,
        cleanup,
        source={"total_rows": 3_000_000, "attempt_rows": 3_000_000},
        fixture={
            "rows": 3_000_000,
            "attempt_rows": 3_000_000,
            "users": 2_000,
            "history_days": 365,
            "resumed": False,
        },
        formal=False,
    )


def test_formal_gate_requires_exact_config_and_resume_prevalidation():
    harness = _load_usage_scale_harness()
    cohorts = {
        name: {
            "timing": {"p95_ms": 1.0},
            "attempt_ledger_statement_count": 1,
            "attempt_runtime_job_index_used": True,
            "attempt_rollup_relations_used": True,
            "attempt_full_history_scan_absent": True,
            "attempt_rate_card_probe_loops_absent": True,
            "attempt_full_window_call_probe_loops_absent": True,
        }
        for name in ("unfiltered", "provider_model_filtered")
    }
    cleanup = {
        "foreign_key_cascade_verified": True,
        "watermark_removed": True,
        "residual_counts": {"all": 0},
    }
    pre = _healthy_formal_resume_snapshot()
    fixture = {
        "rows": 3_000_000,
        "attempt_rows": 3_000_000,
        "users": 2_000,
        "history_days": 365,
        "resumed": True,
        "prefix": "scale_usage_42e02f444a_",
        "resume_validation": {"pre": pre, "exact_prevalidated": True},
    }

    def gate():
        return harness._formal_gate_passed(
            cohorts,
            cleanup,
            source={"total_rows": 3_000_000, "attempt_rows": 3_000_000},
            fixture=fixture,
            formal=True,
        )

    assert gate() is True
    fixture["users"] = 1_999
    assert gate() is False
    fixture["users"] = 2_000
    fixture["history_days"] = 364
    assert gate() is False
    fixture["history_days"] = 365
    fixture["resume_validation"]["exact_prevalidated"] = False
    assert gate() is False
    fixture["resume_validation"]["exact_prevalidated"] = True
    pre["semantic_integrity"]["daily_dimensions"]["mismatched_rows"] = 1
    assert gate() is False


def _synthetic_attempt_plan(*nodes):
    return {"Node Type": "Result", "Plans": list(nodes)}


def _synthetic_scan(relation, *, rows, loops=1, index=None, node_type="Index Scan"):
    return {
        "Node Type": node_type,
        "Relation Name": relation,
        "Index Name": index,
        "Actual Rows": rows,
        "Actual Loops": loops,
    }


def _synthetic_formal_gate(harness, guards):
    cohorts = {
        name: {
            "timing": {"p95_ms": 2_999.0},
            "attempt_ledger_statement_count": 1,
            "attempt_runtime_job_index_used": guards[
                "attempt_runtime_job_index_used"
            ],
            "attempt_rollup_relations_used": guards[
                "attempt_rollup_relations_used"
            ],
            "attempt_full_history_scan_absent": guards[
                "attempt_full_history_scan_absent"
            ],
            "attempt_rate_card_probe_loops_absent": guards[
                "attempt_rate_card_probe_loops_absent"
            ],
            "attempt_full_window_call_probe_loops_absent": guards[
                "attempt_full_window_call_probe_loops_absent"
            ],
        }
        for name in ("unfiltered", "provider_model_filtered")
    }
    cleanup = {
        "foreign_key_cascade_verified": True,
        "watermark_removed": True,
        "residual_counts": {"users": 0, "watermark": 0, "dirty_days": 0},
    }
    return harness._formal_gate_passed(
        cohorts,
        cleanup,
        source={"total_rows": 3_000_000, "attempt_rows": 3_000_000},
        fixture={
            "rows": 3_000_000,
            "attempt_rows": 3_000_000,
            "users": 2_000,
            "history_days": 365,
            "resumed": False,
        },
        formal=True,
    )


def test_admin_usage_scale_attempt_plan_guards_use_complete_plan_not_display_slice():
    harness = _load_usage_scale_harness()
    harmless = [{"Node Type": "Aggregate"} for _ in range(128)]
    dangerous_rate = _synthetic_scan("llm_rate_cards", rows=1, loops=1_000)
    dangerous_call = _synthetic_scan(
        "llm_provider_attempts",
        rows=1,
        loops=700_000,
        index="ix_llm_provider_attempts_call",
    )
    root = _synthetic_attempt_plan(
        *harmless,
        dangerous_rate,
        dangerous_call,
        _synthetic_scan("llm_usage_daily_attempt_dimensions", rows=10),
        _synthetic_scan("llm_usage_daily_call_memberships", rows=10),
        _synthetic_scan(
            "llm_provider_attempts",
            rows=1_000,
            index="ix_llm_provider_attempts_runtime_job",
        ),
    )

    guards = harness._attempt_plan_guards(
        root,
        total_attempt_rows=3_000_000,
        expected_raw_edge_attempt_rows=1_000,
        expected_raw_edge_logical_calls=900,
    )

    assert guards["attempt_rate_card_probe_loops_absent"] is False
    assert guards["attempt_full_window_call_probe_loops_absent"] is False
    assert guards["complete_plan_node_count"] > 128
    assert _synthetic_formal_gate(harness, guards) is False


def test_admin_usage_scale_attempt_plan_guards_reject_near_full_scan_and_accept_edge():
    harness = _load_usage_scale_harness()
    common = (
        _synthetic_scan("llm_usage_daily_attempt_dimensions", rows=90),
        _synthetic_scan("llm_usage_daily_call_memberships", rows=90),
        _synthetic_scan("llm_rate_cards", rows=3, loops=1, node_type="Seq Scan"),
    )
    near_full = _synthetic_attempt_plan(
        *common,
        _synthetic_scan(
            "llm_provider_attempts",
            rows=2_999_999,
            index="ix_llm_provider_attempts_runtime_job",
        ),
    )
    safe_edge = _synthetic_attempt_plan(
        *common,
        _synthetic_scan(
            "llm_provider_attempts",
            rows=1_050,
            index="ix_llm_provider_attempts_runtime_job",
        ),
        _synthetic_scan(
            "llm_provider_attempts",
            rows=2,
            loops=400,
            index="ix_llm_provider_attempts_call",
        ),
    )

    rejected = harness._attempt_plan_guards(
        near_full,
        total_attempt_rows=3_000_000,
        expected_raw_edge_attempt_rows=1_000,
        expected_raw_edge_logical_calls=900,
    )
    accepted = harness._attempt_plan_guards(
        safe_edge,
        total_attempt_rows=3_000_000,
        expected_raw_edge_attempt_rows=1_000,
        expected_raw_edge_logical_calls=900,
    )

    assert rejected["attempt_full_history_scan_absent"] is False
    assert accepted["attempt_full_history_scan_absent"] is True
    assert accepted["attempt_rate_card_probe_loops_absent"] is True
    assert accepted["attempt_full_window_call_probe_loops_absent"] is True
    assert _synthetic_formal_gate(harness, rejected) is False
    assert _synthetic_formal_gate(harness, accepted) is True


def test_admin_usage_scale_small_fixture_rejects_single_near_full_scan_and_call_probe():
    harness = _load_usage_scale_harness()
    common = (
        _synthetic_scan("llm_usage_daily_attempt_dimensions", rows=1),
        _synthetic_scan("llm_usage_daily_call_memberships", rows=1),
        _synthetic_scan(
            "llm_provider_attempts",
            rows=1,
            index="ix_llm_provider_attempts_runtime_job",
        ),
    )
    dangerous_scans = (50, 49)

    for rows in dangerous_scans:
        guards = harness._attempt_plan_guards(
            _synthetic_attempt_plan(
                *common,
                _synthetic_scan(
                    "llm_provider_attempts",
                    rows=rows,
                    index="ix_llm_provider_attempts_runtime_job",
                ),
            ),
            total_attempt_rows=50,
            expected_raw_edge_attempt_rows=1,
            expected_raw_edge_logical_calls=1,
        )
        assert guards["attempt_full_history_scan_absent"] is False
        assert guards["max_single_attempt_scan_rows"] == rows
        assert guards["single_attempt_scan_limit"] == 3
        assert guards["near_full_attempt_scan_threshold"] == 49
        assert _synthetic_formal_gate(harness, guards) is False

    for loops in dangerous_scans:
        call_node = {
            "Node Type": "Index Scan",
            "Index Name": "ix_llm_provider_attempts_call",
            "Actual Rows": 1,
            "Actual Loops": loops,
        }
        guards = harness._attempt_plan_guards(
            _synthetic_attempt_plan(*common, call_node),
            total_attempt_rows=50,
            expected_raw_edge_attempt_rows=1,
            expected_raw_edge_logical_calls=1,
        )
        assert guards["attempt_full_window_call_probe_loops_absent"] is False
        assert guards["max_single_call_probe_loops"] == loops
        assert guards["single_call_probe_loop_limit"] == 2
        assert _synthetic_formal_gate(harness, guards) is False

    legitimate = harness._attempt_plan_guards(
        _synthetic_attempt_plan(
            *common,
            _synthetic_scan(
                "llm_provider_attempts",
                rows=3,
                index="ix_llm_provider_attempts_runtime_job",
            ),
            {
                "Node Type": "Index Scan",
                "Index Name": "ix_llm_provider_attempts_call",
                "Actual Rows": 1,
                "Actual Loops": 3,
            },
        ),
        total_attempt_rows=50,
        expected_raw_edge_attempt_rows=1,
        expected_raw_edge_logical_calls=2,
    )
    assert legitimate["attempt_full_history_scan_absent"] is True
    assert legitimate["attempt_full_window_call_probe_loops_absent"] is True
    assert _synthetic_formal_gate(harness, legitimate) is True


def test_admin_usage_scale_empty_probe_lists_pass_only_when_complete_plan_has_none():
    harness = _load_usage_scale_harness()
    clean = _synthetic_attempt_plan(
        _synthetic_scan("llm_usage_daily_attempt_dimensions", rows=90),
        _synthetic_scan("llm_usage_daily_call_memberships", rows=90),
        _synthetic_scan(
            "llm_provider_attempts",
            rows=1_000,
            index="ix_llm_provider_attempts_runtime_job",
        ),
    )
    hidden = _synthetic_attempt_plan(
        *([{"Node Type": "Aggregate"}] * 128),
        _synthetic_scan("llm_rate_cards", rows=1, loops=2),
    )

    clean_guards = harness._attempt_plan_guards(
        clean,
        total_attempt_rows=3_000_000,
        expected_raw_edge_attempt_rows=1_000,
        expected_raw_edge_logical_calls=900,
    )
    hidden_guards = harness._attempt_plan_guards(
        hidden,
        total_attempt_rows=3_000_000,
        expected_raw_edge_attempt_rows=1_000,
        expected_raw_edge_logical_calls=900,
    )

    assert clean_guards["rate_card_scan_nodes"] == 0
    assert clean_guards["call_probe_nodes"] == 0
    assert clean_guards["attempt_rate_card_probe_loops_absent"] is True
    assert clean_guards["attempt_full_window_call_probe_loops_absent"] is True
    assert hidden_guards["rate_card_scan_nodes"] == 1
    assert hidden_guards["attempt_rate_card_probe_loops_absent"] is False
    assert _synthetic_formal_gate(harness, clean_guards) is True
    assert _synthetic_formal_gate(harness, hidden_guards) is False


def test_admin_usage_scale_seeds_and_cleans_content_free_attempt_fixture():
    harness = _load_usage_scale_harness()
    prefix = "scale_usage_unit_attempt_"
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM users WHERE left(user_id,length(%s))=%s",
            (prefix, prefix),
        )
        try:
            harness._seed_fixture(
                conn,
                prefix=prefix,
                rows=4,
                attempt_rows=3,
                users=2,
                end_at=harness.SCALE_NOW_UTC,
                history_days=365,
            )
            counts = harness._fixture_counts(conn, prefix)
            shapes = conn.execute(
                "SELECT min(job_id),max(job_id),min(outer_attempt_ordinal),"
                "min(inner_attempt_ordinal),min(revision),count(*) FILTER (WHERE "
                "source='runtime_recorder' AND state='completed' "
                "AND completeness='complete') FROM llm_provider_attempts "
                "WHERE user_id LIKE %s",
                (prefix + "%",),
            ).fetchone()

            assert counts["users"] == 2
            assert counts["v2_turn_metrics"] == 4
            assert counts["llm_provider_attempts"] == 3
            assert counts["llm_provider_attempt_corrections"] == 0
            assert shapes == (1, 3, 1, 1, 2, 3)
        finally:
            harness._delete_fixture(conn, prefix)
        counts = harness._fixture_counts(conn, prefix)

    assert all(
        value == 0
        for key, value in counts.items()
        if key != "watermark"
    )


def test_admin_usage_scale_uses_rolling_90d_partition_and_dedicated_local_db():
    harness = _load_usage_scale_harness()

    start_at, end_at = harness._production_window()
    assert start_at.isoformat() == "2026-05-04T12:30:00+00:00"
    assert end_at.isoformat() == "2026-08-02T12:30:00+00:00"
    partition = usage_reporting.rollup_partition(
        _usage.UsageQuery(
            start_at_utc=start_at,
            end_at_utc=end_at,
            timezone="Asia/Shanghai",
            preset="90d",
        )
    )
    assert partition is not None
    assert len(partition.rollup_days) == 89
    assert partition.rollup_days[0].isoformat() == "2026-05-05"
    assert partition.rollup_days[-1].isoformat() == "2026-08-01"
    assert [day.isoformat() for day in partition.raw_days] == [
        "2026-05-04",
        "2026-08-02",
    ]
    assert harness.FORMAL_ATTEMPT_COMPLETED_THROUGH_DAY == "2026-08-02"
    assert (
        harness.FORMAL_ATTEMPT_RETAINED_FROM
        < "2025-08-02"  # the earliest day in the 365-day formal fixture
        < start_at.date().isoformat()
    )
    assert harness._validate_scale_database_url(
        "postgresql://postgres:test@127.0.0.1:55432/feedling_usage_scale_task4d"
    ) == {
        "database": "feedling_usage_scale_task4d",
        "host": "127.0.0.1",
        "port": 55432,
    }
    for unsafe in (
        "postgresql://postgres:test@127.0.0.1:55432/postgres",
        "postgresql://postgres:test@feedling-prod.example.com:5432/feedling_usage_scale_task4d",
        "postgresql://postgres:test@127.0.0.1:55432/feedling_test",
        "host=localhost port=55432 dbname=feedling_usage_scale_task4d",
        "host=::1 port=55432 dbname=feedling_usage_scale_task4d",
        "postgresql://postgres:test@127.0.0.1:55432/feedling_usage_scale_task4d?host=prod.example.com",
        "postgresql://postgres:test@127.0.0.1:55432/feedling_usage_scale_task4d?hostaddr=10.0.0.10",
        "postgresql://postgres:test@127.0.0.1:55432/feedling_usage_scale_task4d?hostaddr=::1",
        "postgresql://postgres:test@127.0.0.1:55432/feedling_usage_scale_task4d?port=5432",
        "postgresql://postgres:test@127.0.0.1:55432/feedling_usage_scale_task4d?dbname=postgres",
        "host=127.0.0.1,prod.example.com port=55432 dbname=feedling_usage_scale_task4d",
        "host=127.0.0.1 port=55432 dbname=feedling_usage_scale_task4d service=prod",
        "host=127.0.0.1 port=55432 dbname=feedling_usage_scale_task4d servicefile=/tmp/prod.conf",
        "service=prod",
    ):
        with pytest.raises(SystemExit, match="dedicated local PostgreSQL"):
            harness._validate_scale_database_url(unsafe)


def test_admin_usage_scale_revalidates_connected_database_identity():
    harness = _load_usage_scale_harness()
    validate = getattr(harness, "_validate_connected_scale_database", None)
    assert validate is not None, "connected database identity check is required"
    expected = {
        "database": "feedling_usage_scale_task4d",
        "host": "127.0.0.1",
        "port": 55432,
    }
    safe = SimpleNamespace(
        info=SimpleNamespace(
            dbname="feedling_usage_scale_task4d",
            host="127.0.0.1",
            hostaddr="127.0.0.1",
            port=55432,
        )
    )
    assert validate(safe, expected) == expected

    for field, value in (
        ("dbname", "postgres"),
        ("host", "prod.example.com"),
        ("hostaddr", "::1"),
        ("port", 5432),
    ):
        info = SimpleNamespace(
            dbname="feedling_usage_scale_task4d",
            host="127.0.0.1",
            hostaddr="127.0.0.1",
            port=55432,
        )
        setattr(info, field, value)
        with pytest.raises(RuntimeError, match="connected PostgreSQL identity"):
            validate(SimpleNamespace(info=info), expected)

    missing_hostaddr = SimpleNamespace(
        info=SimpleNamespace(
            dbname="feedling_usage_scale_task4d",
            host="127.0.0.1",
            port=55432,
        )
    )
    with pytest.raises(RuntimeError, match="connected PostgreSQL identity"):
        validate(missing_hostaddr, expected)


def test_admin_usage_scale_revalidates_every_pool_checkout():
    harness = _load_usage_scale_harness()
    validated_pool_type = getattr(harness, "_ValidatedScalePool", None)
    assert validated_pool_type is not None, "every scale pool checkout must be validated"
    expected = {
        "database": "feedling_usage_scale_task4d",
        "host": "127.0.0.1",
        "port": 55432,
    }

    def connection(hostaddr):
        return SimpleNamespace(
            info=SimpleNamespace(
                dbname="feedling_usage_scale_task4d",
                host="127.0.0.1",
                hostaddr=hostaddr,
                port=55432,
            )
        )

    class ConnectionContext:
        def __init__(self, conn):
            self.conn = conn
            self.exited = False

        def __enter__(self):
            return self.conn

        def __exit__(self, *_args):
            self.exited = True

    class Pool:
        def __init__(self):
            self.contexts = [
                ConnectionContext(connection("127.0.0.1")),
                ConnectionContext(connection("::1")),
            ]

        def connection(self):
            return self.contexts.pop(0)

    real_pool = Pool()
    pool = validated_pool_type(real_pool, expected)
    with pool.connection() as conn:
        assert conn.info.hostaddr == "127.0.0.1"
    assert real_pool.contexts[0].exited is False

    with pytest.raises(RuntimeError, match="connected PostgreSQL identity"):
        with pool.connection():
            pytest.fail("unsafe connection was yielded to the harness")


def test_admin_usage_scale_installs_validated_pool_for_production_modules():
    harness = _load_usage_scale_harness()
    install = getattr(harness, "_install_validated_scale_pool", None)
    assert install is not None, "production modules must receive the validated pool"
    raw_pool = object()

    class DatabaseModule:
        def __init__(self):
            self.calls = 0

        def get_pool(self):
            self.calls += 1
            return raw_pool

    database_module = DatabaseModule()
    expected = {
        "database": "feedling_usage_scale_task4d",
        "host": "127.0.0.1",
        "port": 55432,
    }

    pool = install(database_module, expected)

    assert database_module.calls == 1
    assert database_module.get_pool() is pool
    assert pool._pool is raw_pool


def test_admin_usage_scale_records_and_enforces_rolling_partition():
    harness = _load_usage_scale_harness()
    validate = getattr(harness, "_validate_rolling_partition", None)
    assert validate is not None, "rolling partition gate is required"
    start_at, end_at = harness._production_window()
    partition = usage_reporting.rollup_partition(
        _usage.UsageQuery(
            start_at_utc=start_at,
            end_at_utc=end_at,
            timezone="Asia/Shanghai",
            preset="90d",
        )
    )
    assert partition is not None

    assert validate(partition) == {
        "rollup_days": [day.isoformat() for day in partition.rollup_days],
        "raw_days": [day.isoformat() for day in partition.raw_days],
    }
    with pytest.raises(AssertionError, match="89 full rollup days and 2 partial"):
        validate(
            usage_reporting.RollupPartition(
                rollup_days=partition.rollup_days,
                raw_days=partition.raw_days[:1],
            )
        )


def test_admin_usage_scale_retention_index_evidence_gate_is_auditable():
    harness = _load_usage_scale_harness()
    gate = getattr(harness, "_retention_index_evidence_passed", None)
    assert gate is not None
    evidence = {
        "present": True,
        "valid": True,
        "definition_exact": True,
        "index_bytes": 4096,
        "attempt_rows": 300_000,
        "bytes_per_attempt": 0.013653,
        "attempt_table_total_bytes": 10_000_000,
        "index_share_of_attempt_total": 0.0004096,
        "maintenance": {
            "idx_scan": 2,
            "idx_tup_read": 20,
            "idx_tup_fetch": 10,
        },
    }
    assert gate(evidence) is True
    assert gate({**evidence, "definition_exact": False}) is False
    assert gate({**evidence, "bytes_per_attempt": None}) is False
    assert gate({**evidence, "maintenance": {}}) is False


def test_admin_usage_scale_business_path_evidence_gate_is_fail_closed():
    harness = _load_usage_scale_harness()
    gate = harness._business_path_evidence_passed
    commit = "a" * 40
    evidence = {
        "pool": {"peak_occupancy": 7, "capacity": 20, "timeouts": 0},
        "recorder": {
            "requests": 10_000,
            "p95_ms": 4.2,
            "business_errors": 0,
            "results_match_baseline": True,
        },
        "providers": {
            provider: {
                "requests": 100,
                "p95_ms": 800.0,
                "business_errors": 0,
                "results_match_baseline": True,
            }
            for provider in ("openrouter", "anthropic", "google")
        },
    }
    # A summary-only document used to satisfy this gate.  The gate now delegates to
    # the producer validator, so evidence without provenance and raw paired samples
    # must fail closed even when all of its summary claims look healthy.
    assert gate(evidence, expected_commit=commit) is False
    assert gate({}, expected_commit=commit) is False
    assert (
        gate(
            {**evidence, "pool": {**evidence["pool"], "timeouts": 1}},
            expected_commit=commit,
        )
        is False
    )
    broken = json.loads(json.dumps(evidence))
    broken["providers"]["anthropic"]["results_match_baseline"] = False
    assert gate(broken, expected_commit=commit) is False


def test_admin_usage_scale_sql_guards_allow_rollup_only_and_bound_raw_ranges():
    harness = _load_usage_scale_harness()
    rollup_only = [
        (
            "SELECT sum(all_turns) FROM v2_usage_daily_users "
            "WHERE local_day = ANY(%s)",
            (("2026-08-01",),),
        )
    ]
    harness.assert_content_free_metric_sql(rollup_only)
    harness.assert_metric_time_ranges(rollup_only)

    hybrid = [
        (
            "WITH raw_ranges AS (SELECT %s::timestamptz start_at, "
            "%s::timestamptz end_at) SELECT m.prompt_tokens "
            "FROM v2_turn_metrics m JOIN raw_ranges rr "
            "ON m.created_at >= rr.start_at AND m.created_at < rr.end_at",
            ("start", "end"),
        )
    ]
    harness.assert_content_free_metric_sql(hybrid)
    harness.assert_metric_time_ranges(hybrid)

    with pytest.raises(AssertionError, match="half-open"):
        harness.assert_metric_time_ranges(
            [(hybrid[0][0].replace("m.created_at < rr.end_at", "true"), hybrid[0][1])]
        )


@pytest.fixture
def usage_rows():
    user_ids = [
        "u_usage_alpha",
        "u_usage_beta",
        "u_usage_idle",
        "u_usage_future",
    ]
    for user_id in user_ids:
        seed_user(
            user_id,
            created_at=(
                "2026-07-04T00:00:00+00:00"
                if user_id == "u_usage_future"
                else "2026-06-01T00:00:00+00:00"
            ),
        )
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO memory_moments (user_id,moment_id,occurred_at,doc) "
            "VALUES (%s,%s,%s,%s)",
            (
                "u_usage_alpha",
                "usage-memory",
                "2026-06-15T00:00:00+00:00",
                Jsonb({"created_at": "2026-06-15T00:00:00+00:00"}),
            ),
        )
        conn.execute(
            "INSERT INTO chat_messages (user_id,msg_id,ts,doc) VALUES (%s,%s,%s,%s)",
            (
                "u_usage_beta",
                "usage-message",
                datetime(2026, 6, 20, tzinfo=timezone.utc).timestamp(),
                Jsonb({"role": "user", "source": "chat"}),
            ),
        )
        conn.execute(
            "INSERT INTO chat_messages (user_id,msg_id,ts,doc) VALUES (%s,%s,%s,%s)",
            (
                "u_usage_idle",
                "usage-verify-ping",
                datetime(2026, 6, 20, tzinfo=timezone.utc).timestamp(),
                Jsonb({"role": "user", "source": "verify_ping"}),
            ),
        )
        conn.execute(
            "INSERT INTO memory_moments (user_id,moment_id,occurred_at,doc) "
            "VALUES (%s,%s,%s,%s)",
            (
                "u_usage_idle",
                "usage-future-memory",
                "2026-07-04T00:00:00+00:00",
                Jsonb({"created_at": "2026-06-25T00:00:00+00:00"}),
            ),
        )
        metrics = [
            # user, day, lane, provider, model, prompt, completion, cache r/w/m,
            # usage/cache calls, calls, retries, failed, latency
            ("u_usage_alpha", "2026-07-01T10:00:00+00:00", "chat", "openrouter", "gpt-a", 100, 20, 40, 5, 60, 2, 2, 2, 1, False, 100),
            ("u_usage_alpha", "2026-07-02T10:00:00+00:00", "heartbeat", "anthropic", "claude-b", 50, 10, None, None, None, 1, 0, 1, 0, True, 300),
            ("u_usage_beta", "2026-07-01T11:00:00+00:00", "chat", "openrouter", "gpt-a", None, None, None, None, None, 0, 0, 1, 0, True, 500),
            ("u_usage_beta", "2026-07-02T11:00:00+00:00", "chat", "openrouter", "gpt-a", 30, 5, 10, 0, 0, 1, 1, 2, 1, False, 200),
            ("u_usage_idle", "2026-07-01T12:00:00+00:00", "maintenance", None, None, None, None, None, None, None, 0, 0, 0, 0, False, None),
        ]
        for index, row in enumerate(metrics):
            (
                user_id,
                created_at,
                lane,
                provider,
                model,
                prompt,
                completion,
                cache_read,
                cache_write,
                cache_miss,
                usage_calls,
                cache_calls,
                model_calls,
                retries,
                failed,
                latency_ms,
            ) = row
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,provider,model,prompt_tokens,completion_tokens,"
                "cache_read_tokens,cache_write_tokens,cache_miss_tokens,"
                "usage_reported_calls,cache_reported_calls,model_calls,retries,failed,"
                "status,latency_ms,created_at) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    None,
                    user_id,
                    lane,
                    provider,
                    model,
                    prompt,
                    completion,
                    cache_read,
                    cache_write,
                    cache_miss,
                    usage_calls,
                    cache_calls,
                    model_calls,
                    retries,
                    failed,
                    f"usage-{index}",
                    latency_ms,
                    created_at,
                ),
            )
    try:
        yield user_ids
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id = ANY(%s)", (user_ids,))


def _usage_query(**filters):
    return _usage.UsageQuery(
        start_at_utc=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end_at_utc=datetime(2026, 7, 3, tzinfo=timezone.utc),
        timezone="UTC",
        **filters,
    )


def _insert_attempt(
    conn,
    *,
    attempt_id: str,
    user_id: str,
    call_id: str,
    job_id: int | None = None,
    lane: str = "chat",
    outer: int = 1,
    inner: int = 1,
    retry_kind: str = "initial",
    requested_provider: str = "requested-provider",
    requested_model: str = "requested-model",
    resolved_provider: str = "resolved-provider",
    resolved_model: str = "resolved-model",
    outcome: str = "succeeded",
    usage_known: bool = True,
    possibly_billed: bool = False,
    input_tokens=None,
    output_tokens=None,
    reasoning_tokens=None,
    cache_read_tokens=None,
    cache_write_tokens=None,
    cache_miss_tokens=None,
    ttft_ms=None,
    cost=None,
    currency=None,
    rate_card_version=None,
    started_at: str = "2026-07-01T10:00:00+00:00",
):
    conn.execute(
        "INSERT INTO llm_provider_attempts ("
        "attempt_id,user_id,lane,job_id,call_id,outer_attempt_ordinal,"
        "inner_attempt_ordinal,retry_kind,requested_provider,resolved_provider,"
        "requested_model,resolved_model,transport,started_at,finished_at,state,"
        "outcome,error_class,input_tokens,output_tokens,reasoning_tokens,"
        "cache_read_tokens,cache_write_tokens,cache_miss_tokens,usage_known,"
        "possibly_billed,latency_ms,ttft_ms,cost,currency,rate_card_version,"
        "source,completeness,revision) VALUES ("
        "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'openai_responses',%s,%s,"
        "'completed',%s,'none',%s,%s,%s,%s,%s,%s,%s,%s,120,%s,%s,%s,%s,"
        "'runtime_recorder',%s,2)",
        (
            attempt_id,
            user_id,
            lane,
            job_id,
            call_id,
            outer,
            inner,
            retry_kind,
            requested_provider,
            resolved_provider,
            requested_model,
            resolved_model,
            started_at,
            "2026-07-01T10:00:01+00:00",
            outcome,
            input_tokens,
            output_tokens,
            reasoning_tokens,
            cache_read_tokens,
            cache_write_tokens,
            cache_miss_tokens,
            usage_known,
            possibly_billed,
            ttft_ms,
            cost,
            currency,
            rate_card_version,
            "complete" if usage_known else "usage_unknown",
        ),
    )


@pytest.fixture
def provider_attempt_usage_rows():
    user_id = "u_attempt_usage"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_turn_metrics "
            "(job_id,user_id,lane,provider,model,model_calls,usage_reported_calls,"
            "retries,failed,status,created_at) "
            "VALUES (81001,%s,'chat','turn-provider','turn-model',4,4,99,true,"
            "'attempt-turn',%s)",
            (user_id, "2026-07-01T10:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO llm_rate_cards "
            "(provider,model,version,currency,input_cost_per_million,"
            "output_cost_per_million,reasoning_cost_per_million,"
            "cache_read_cost_per_million,effective_at) "
            "VALUES ('resolved-b','model-b','rate-v1','USD',2,4,6,1,%s) "
            "ON CONFLICT (provider,model,version) DO NOTHING",
            ("2026-06-01T00:00:00+00:00",),
        )
        conn.execute(
            "INSERT INTO llm_rate_cards "
            "(provider,model,version,currency,input_cost_per_million,"
            "output_cost_per_million,reasoning_cost_per_million,"
            "cache_read_cost_per_million,effective_at) "
            "VALUES ('resolved-b','model-b','rate-v2','USD',999,999,999,999,%s) "
            "ON CONFLICT (provider,model,version) DO NOTHING",
            ("2026-07-02T00:00:00+00:00",),
        )
        _insert_attempt(
            conn,
            attempt_id="11111111-1111-5111-8111-111111111111",
            user_id=user_id,
            call_id="logical-a",
            job_id=81001,
            requested_provider="requested-a",
            requested_model="asked-model",
            resolved_provider="resolved-a",
            resolved_model="model-a",
            input_tokens=100,
            output_tokens=20,
            reasoning_tokens=5,
            cache_read_tokens=10,
            cache_write_tokens=2,
            cache_miss_tokens=4,
            ttft_ms=40,
            cost=Decimal("0.50000000"),
            currency="USD",
            rate_card_version="invoice-v1",
        )
        _insert_attempt(
            conn,
            attempt_id="22222222-2222-5222-8222-222222222222",
            user_id=user_id,
            call_id="logical-a",
            job_id=81001,
            outer=2,
            retry_kind="failover",
            requested_provider="requested-a",
            requested_model="asked-model",
            resolved_provider="resolved-b",
            resolved_model="model-b",
            outcome="failed",
            input_tokens=50,
            output_tokens=10,
            reasoning_tokens=4,
            cache_read_tokens=5,
            cache_write_tokens=1,
            cache_miss_tokens=3,
            ttft_ms=80,
        )
        _insert_attempt(
            conn,
            attempt_id="33333333-3333-5333-8333-333333333333",
            user_id=user_id,
            call_id="logical-b",
            job_id=81001,
            usage_known=False,
            possibly_billed=True,
            outcome="timed_out",
        )
        conn.execute(
            "INSERT INTO llm_provider_attempt_corrections "
            "(attempt_id,user_id,revision,reason_code,input_tokens_delta,"
            "output_tokens_delta,reasoning_tokens_delta,cache_read_tokens_delta,"
            "cost_delta,currency) VALUES (%s,%s,3,'late_usage',7,3,2,1,.1,'USD')",
            ("11111111-1111-5111-8111-111111111111", user_id),
        )
    try:
        yield user_id
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


def test_usage_snapshot_uses_attempt_ledger_and_keeps_turn_outcome_truth(
    provider_attempt_usage_rows,
):
    report = jobs_store.usage_report_snapshot(
        _usage_query(user_id=provider_attempt_usage_rows)
    )

    assert report["overview"]["turns"] == 1
    assert report["overview"]["failed_turns"] == 1
    assert report["overview"]["model_calls"] == 4
    attempts = report["attempts"]
    assert attempts["overview"] == {
        "attempts": 3,
        "logical_calls": 2,
        "retry_attempts": 1,
        "failover_attempts": 1,
        "failed_attempts": 2,
        "usage_known_attempts": 2,
        "usage_unknown_attempts": 1,
        "possibly_billed_attempts": 1,
        "input_tokens": 157,
        "output_tokens": 33,
        "reasoning_tokens": 11,
        "cache_read_tokens": 16,
        "cache_write_tokens": 3,
        "cache_miss_tokens": 7,
        "ttft_ms_p50": 60.0,
        "ttft_ms_p95": 78.0,
    }
    assert attempts["coverage"]["whole_turn_model_calls"] == 4
    assert attempts["coverage"]["recorded_logical_calls"] == 2
    assert attempts["coverage"]["logical_call_coverage"] == pytest.approx(.5)
    assert attempts["coverage"]["attempt_sequence_gaps"] == 0
    assert attempts["costs"] == [
        {
            "currency": "USD",
            "authoritative_cost": Decimal("0.60000000"),
            "estimated_cost": Decimal("0.00015300"),
            "authoritative_attempts": 1,
            "estimated_attempts": 1,
            "unknown_attempts": 0,
        },
        {
            "currency": None,
            "authoritative_cost": None,
            "estimated_cost": None,
            "authoritative_attempts": 0,
            "estimated_attempts": 0,
            "unknown_attempts": 1,
        },
    ]
    assert attempts["requested_models"][0]["provider"] == "requested-a"
    assert attempts["requested_models"][0]["model"] == "asked-model"
    resolved = {(row["provider"], row["model"]) for row in attempts["resolved_models"]}
    assert resolved == {
        ("resolved-a", "model-a"),
        ("resolved-b", "model-b"),
        ("resolved-provider", "resolved-model"),
    }


def test_usage_snapshot_joins_attempts_to_turn_job_cohort_not_attempt_time():
    user_id = "u_attempt_job_cohort"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,model_calls,retries,failed,status,created_at) "
                "VALUES (81101,%s,'chat',1,0,false,'in-window-job',%s),"
                "(81102,%s,'chat',1,0,false,'out-window-job',%s)",
                (
                    user_id,
                    "2026-07-01T12:00:00+00:00",
                    user_id,
                    "2026-06-30T12:00:00+00:00",
                ),
            )
            _insert_attempt(
                conn,
                attempt_id="31111111-1111-5111-8111-111111111111",
                user_id=user_id,
                job_id=81101,
                call_id="job-cohort-call",
                input_tokens=10,
            )
            _insert_attempt(
                conn,
                attempt_id="32222222-2222-5222-8222-222222222222",
                user_id=user_id,
                job_id=81101,
                call_id="job-cohort-call",
                outer=2,
                retry_kind="failover",
                input_tokens=20,
                started_at="2026-07-03T00:00:00+00:00",
            )
            _insert_attempt(
                conn,
                attempt_id="33333333-4444-5333-8444-333333333333",
                user_id=user_id,
                job_id=81102,
                call_id="out-of-cohort-call",
                input_tokens=999,
            )

        attempts = jobs_store.usage_report_snapshot(
            _usage_query(user_id=user_id)
        )["attempts"]

        assert attempts["overview"]["attempts"] == 2
        assert attempts["overview"]["logical_calls"] == 1
        assert attempts["overview"]["input_tokens"] == 30
        assert attempts["coverage"]["whole_turn_model_calls"] == 1
        assert attempts["coverage"]["logical_call_coverage"] == pytest.approx(1.0)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


def test_usage_snapshot_has_no_attempt_numerator_without_turn_job_cohort():
    user_id = "u_attempt_no_turn_cohort"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,model_calls,retries,failed,status,created_at) "
                "VALUES (81103,%s,'chat',1,0,false,'old-job',%s)",
                (user_id, "2026-06-30T12:00:00+00:00"),
            )
            _insert_attempt(
                conn,
                attempt_id="34444444-4444-5444-8444-444444444444",
                user_id=user_id,
                job_id=81103,
                call_id="no-turn-cohort-call",
                input_tokens=999,
            )

        attempts = jobs_store.usage_report_snapshot(
            _usage_query(user_id=user_id)
        )["attempts"]

        assert attempts["overview"]["attempts"] == 0
        assert attempts["coverage"]["whole_turn_model_calls"] == 0
        assert attempts["coverage"]["recorded_logical_calls"] == 0
        assert attempts["coverage"]["logical_call_coverage"] is None
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


def test_usage_snapshot_reports_ordinal_gaps_separately_from_call_coverage():
    user_id = "u_attempt_gaps"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,model_calls,retries,failed,status,created_at) "
                "VALUES (81002,%s,'chat',1,0,false,'gap-turn',%s)",
                (user_id, "2026-07-01T10:00:00+00:00"),
            )
            for attempt_id, outer, inner in (
                ("44444444-4444-5444-8444-444444444444", 1, 1),
                ("55555555-5555-5555-8555-555555555555", 3, 1),
                ("66666666-6666-5666-8666-666666666666", 3, 3),
            ):
                _insert_attempt(
                    conn,
                    attempt_id=attempt_id,
                    user_id=user_id,
                    call_id="logical-gap",
                    job_id=81002,
                    outer=outer,
                    inner=inner,
                    usage_known=False,
                )
        report = jobs_store.usage_report_snapshot(_usage_query(user_id=user_id))
        coverage = report["attempts"]["coverage"]
        assert coverage["logical_call_coverage"] == pytest.approx(1.0)
        assert coverage["attempt_sequence_gaps"] == 2
        assert coverage["missing_outer_ordinals"] == 1
        assert coverage["missing_inner_ordinals"] == 1
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


def test_usage_snapshot_gap_detection_uses_full_call_cohort_before_identity_filter():
    user_id = "u_attempt_filtered_gaps"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,provider,model,model_calls,retries,failed,status,created_at) "
                "VALUES (81003,%s,'chat','visible-provider','visible-model',1,0,false,"
                "'filtered-gap-turn',%s)",
                (user_id, "2026-07-01T10:00:00+00:00"),
            )
            _insert_attempt(
                conn,
                attempt_id="68888888-8888-5888-8888-888888888888",
                user_id=user_id,
                call_id="logical-filtered-gap",
                job_id=81003,
                outer=1,
                resolved_provider="hidden-provider",
                resolved_model="hidden-model",
                started_at="2026-06-30T23:59:00+00:00",
            )
            _insert_attempt(
                conn,
                attempt_id="69999999-9999-5999-8999-999999999999",
                user_id=user_id,
                call_id="logical-filtered-gap",
                job_id=81003,
                outer=2,
                retry_kind="failover",
                resolved_provider="visible-provider",
                resolved_model="visible-model",
            )

        coverage = jobs_store.usage_report_snapshot(
            _usage_query(user_id=user_id, provider="visible-provider")
        )["attempts"]["coverage"]

        assert coverage["recorded_logical_calls"] == 1
        assert coverage["whole_turn_model_calls"] is None
        assert coverage["logical_call_coverage"] is None
        assert coverage["logical_call_coverage_reason"] == (
            "provider_model_or_completeness_filters_cannot_attribute_"
            "whole_turn_model_calls"
        )
        assert coverage["missing_outer_ordinals"] == 0
        assert coverage["missing_inner_ordinals"] == 0
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


def test_usage_snapshot_completeness_filter_marks_logical_coverage_unavailable(
    provider_attempt_usage_rows,
):
    attempts = jobs_store.usage_report_snapshot(
        _usage_query(
            user_id=provider_attempt_usage_rows,
            completeness="metered",
        )
    )["attempts"]

    assert attempts["overview"]["attempts"] == 2
    assert attempts["coverage"]["recorded_logical_calls"] == 1
    assert attempts["coverage"]["whole_turn_model_calls"] is None
    assert attempts["coverage"]["logical_call_coverage"] is None
    assert attempts["coverage"]["logical_call_coverage_reason"] == (
        "provider_model_or_completeness_filters_cannot_attribute_"
        "whole_turn_model_calls"
    )


def test_usage_filter_options_include_resolved_attempt_identity_from_turn_cohort():
    user_id = "u_attempt_filter_option"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,provider,model,model_calls,retries,failed,"
                "status,created_at) VALUES (81104,%s,'chat','turn-only-provider',"
                "'turn-only-model',1,0,false,'filter-option-turn',%s)",
                (user_id, "2026-07-01T12:00:00+00:00"),
            )
            _insert_attempt(
                conn,
                attempt_id="35555555-5555-5555-8555-555555555555",
                user_id=user_id,
                job_id=81104,
                call_id="filter-option-call",
                resolved_provider="resolved-only-provider",
                resolved_model="resolved-only-model",
            )

        filters = jobs_store.usage_report_snapshot(
            _usage_query(user_id=user_id)
        )["filters"]

        assert filters["providers"] == [
            "resolved-only-provider",
            "turn-only-provider",
        ]
        assert filters["models"] == [
            "resolved-only-model",
            "turn-only-model",
        ]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


@pytest.mark.parametrize("report_path", ("raw", "hybrid"))
def test_usage_report_executes_one_attempt_ledger_statement(
    monkeypatch, usage_rows, report_path,
):
    attempt_reads = []

    def observer(event, **fields):
        if event == "read" and str(fields.get("section", "")).startswith(
            "attempt_"
        ):
            attempt_reads.append((fields["section"], fields.get("role")))

    monkeypatch.setattr(jobs_store, "_usage_snapshot_observer", observer)

    if report_path == "hybrid":
        _enable_usage_rollup()
    try:
        jobs_store.usage_report_snapshot(
            _usage_query() if report_path == "raw" else _shanghai_usage_query()
        )
    finally:
        if report_path == "hybrid":
            with db.get_pool().connection() as conn:
                conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert attempt_reads == [(
        "attempt_ledger",
        "exporter" if report_path == "raw" else "importer_attempt",
    )]


def test_usage_attempt_partition_intersects_whole_turn_completed_clean_days():
    whole_turn = usage_reporting.RollupPartition(
        rollup_days=tuple(
            datetime(2026, 7, day).date() for day in range(1, 6)
        ),
        raw_days=(datetime(2026, 7, 6).date(),),
    )

    partition = jobs_store._usage_attempt_partition(
        whole_turn,
        completed_through_day=datetime(2026, 7, 4).date(),
        retained_from=datetime(2026, 7, 2).date(),
        dirty_days={datetime(2026, 7, 3).date()},
    )

    assert partition == usage_reporting.RollupPartition(
        rollup_days=(
            datetime(2026, 7, 2).date(),
            datetime(2026, 7, 4).date(),
        ),
        raw_days=(
            datetime(2026, 7, 1).date(),
            datetime(2026, 7, 3).date(),
            datetime(2026, 7, 5).date(),
            datetime(2026, 7, 6).date(),
        ),
        retained_from=datetime(2026, 7, 2).date(),
        retention_truncated=True,
        retention_partial_reason="provider_attempt_retention_window_truncated",
    )


def test_usage_attempt_partition_pending_fence_falls_back_to_surviving_raw():
    whole_turn = usage_reporting.RollupPartition(
        rollup_days=tuple(datetime(2026, 7, day).date() for day in range(1, 5)),
        raw_days=(),
    )

    partition = jobs_store._usage_attempt_partition(
        whole_turn,
        completed_through_day=datetime(2026, 7, 4).date(),
        retained_from=datetime(2026, 7, 1).date(),
        retention_pending_from=datetime(2026, 7, 3).date(),
        dirty_days=(),
    )

    assert partition == usage_reporting.RollupPartition(
        rollup_days=(
            datetime(2026, 7, 3).date(),
            datetime(2026, 7, 4).date(),
        ),
        raw_days=(
            datetime(2026, 7, 1).date(),
            datetime(2026, 7, 2).date(),
        ),
        retained_from=datetime(2026, 7, 1).date(),
        retention_pending_from=datetime(2026, 7, 3).date(),
        retention_truncated=True,
        retention_partial_reason="provider_attempt_retention_pending",
    )


@pytest.mark.parametrize("report_timezone", ["UTC", "America/Los_Angeles"])
def test_usage_attempt_non_shanghai_range_honors_retained_boundary(
    provider_attempt_usage_rows,
    report_timezone,
):
    retained_from = datetime(2026, 7, 2).date()
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks "
            "(rollup_name,bootstrap_complete,retained_from) VALUES (%s,true,%s) "
            "ON CONFLICT (rollup_name) DO UPDATE SET "
            "bootstrap_complete=true,retained_from=excluded.retained_from,"
            "retention_pending_from=NULL",
            (provider_attempt_rollup.ROLLUP_NAME, retained_from),
        )
    query = _usage.UsageQuery(
        start_at_utc=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end_at_utc=datetime(2026, 7, 3, tzinfo=timezone.utc),
        timezone=report_timezone,
        user_id=provider_attempt_usage_rows,
    )
    try:
        attempts = jobs_store._usage_report_snapshot_raw(query)["attempts"]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute(
                "DELETE FROM llm_usage_rollup_watermarks WHERE rollup_name=%s",
                (provider_attempt_rollup.ROLLUP_NAME,),
            )

    assert attempts["overview"]["attempts"] == 0
    assert attempts["coverage"]["retained_from"] == "2026-07-02"
    assert attempts["coverage"]["retention_truncated"] is True
    assert attempts["coverage"]["retention_partial_reason"] == (
        "provider_attempt_retention_window_truncated"
    )
    assert attempts["coverage"]["whole_turn_model_calls"] == 4
    assert attempts["coverage"]["logical_call_coverage"] is None


def test_usage_attempt_pending_fence_keeps_surviving_raw_and_marks_partial(
    provider_attempt_usage_rows,
):
    pending = datetime(2026, 7, 2).date()
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks "
            "(rollup_name,bootstrap_complete,retention_pending_from) "
            "VALUES (%s,true,%s) ON CONFLICT (rollup_name) DO UPDATE SET "
            "bootstrap_complete=true,retained_from=NULL,"
            "retention_pending_from=excluded.retention_pending_from",
            (provider_attempt_rollup.ROLLUP_NAME, pending),
        )
    try:
        attempts = jobs_store._usage_report_snapshot_raw(
            _usage_query(user_id=provider_attempt_usage_rows)
        )["attempts"]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute(
                "DELETE FROM llm_usage_rollup_watermarks WHERE rollup_name=%s",
                (provider_attempt_rollup.ROLLUP_NAME,),
            )

    assert attempts["overview"]["attempts"] == 3
    assert attempts["coverage"]["retention_pending_from"] == "2026-07-02"
    assert attempts["coverage"]["retention_truncated"] is True
    assert attempts["coverage"]["retention_partial_reason"] == (
        "provider_attempt_retention_pending"
    )
    assert attempts["coverage"]["whole_turn_model_calls"] == 4
    assert attempts["coverage"]["logical_call_coverage"] is None


def test_retention_pending_and_destructive_rows_are_atomically_visible(
    provider_attempt_usage_rows,
):
    pending = datetime(2026, 7, 2).date()
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks "
            "(rollup_name,bootstrap_complete) VALUES (%s,true) "
            "ON CONFLICT (rollup_name) DO UPDATE SET "
            "bootstrap_complete=true,retained_from=NULL,retention_pending_from=NULL",
            (provider_attempt_rollup.ROLLUP_NAME,),
        )
    try:
        with psycopg.connect(os.environ["DATABASE_URL"]) as old_reader:
            old_reader.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            assert old_reader.execute(
                "SELECT retention_pending_from FROM llm_usage_rollup_watermarks "
                "WHERE rollup_name=%s",
                (provider_attempt_rollup.ROLLUP_NAME,),
            ).fetchone() == (None,)
            assert old_reader.execute(
                "SELECT count(*) FROM llm_provider_attempts WHERE user_id=%s",
                (provider_attempt_usage_rows,),
            ).fetchone() == (3,)

            with db.get_pool().connection() as writer:
                with writer.transaction():
                    writer.execute(
                        "UPDATE llm_usage_rollup_watermarks SET "
                        "retention_pending_from=%s WHERE rollup_name=%s",
                        (pending, provider_attempt_rollup.ROLLUP_NAME),
                    )
                    writer.execute(
                        "DELETE FROM llm_provider_attempts WHERE user_id=%s",
                        (provider_attempt_usage_rows,),
                    )

            assert old_reader.execute(
                "SELECT retention_pending_from FROM llm_usage_rollup_watermarks "
                "WHERE rollup_name=%s",
                (provider_attempt_rollup.ROLLUP_NAME,),
            ).fetchone() == (None,)
            assert old_reader.execute(
                "SELECT count(*) FROM llm_provider_attempts WHERE user_id=%s",
                (provider_attempt_usage_rows,),
            ).fetchone() == (3,)

        with db.get_pool().connection() as new_reader:
            assert new_reader.execute(
                "SELECT retention_pending_from FROM llm_usage_rollup_watermarks "
                "WHERE rollup_name=%s",
                (provider_attempt_rollup.ROLLUP_NAME,),
            ).fetchone() == (pending,)
            assert new_reader.execute(
                "SELECT count(*) FROM llm_provider_attempts WHERE user_id=%s",
                (provider_attempt_usage_rows,),
            ).fetchone() == (0,)
        attempts = jobs_store._usage_report_snapshot_raw(
            _usage_query(user_id=provider_attempt_usage_rows)
        )["attempts"]
        assert attempts["overview"]["attempts"] == 0
        assert attempts["coverage"]["retention_partial_reason"] == (
            "provider_attempt_retention_pending"
        )
        assert attempts["coverage"]["whole_turn_model_calls"] == 4
        assert attempts["coverage"]["logical_call_coverage"] is None
    finally:
        with db.get_pool().connection() as conn:
            conn.execute(
                "DELETE FROM llm_usage_rollup_watermarks WHERE rollup_name=%s",
                (provider_attempt_rollup.ROLLUP_NAME,),
            )


def test_usage_attempt_payload_marks_cross_retention_range_partial_not_zero(
    provider_attempt_usage_rows,
):
    query = _usage_query(user_id=provider_attempt_usage_rows)
    partition = usage_reporting.RollupPartition(
        rollup_days=(),
        raw_days=(datetime(2026, 7, 1).date(),),
        retained_from=datetime(2026, 7, 2).date(),
        retention_truncated=True,
        retention_partial_reason="provider_attempt_retention_window_truncated",
    )
    with db.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                attempts = jobs_store._usage_attempt_snapshot(
                    cur,
                    query,
                    partition=partition,
                    whole_turn_model_calls=4,
                )["attempts"]

    coverage = attempts["coverage"]
    assert attempts["overview"]["attempts"] == 0
    assert coverage["retained_from"] == "2026-07-02"
    assert coverage["retention_truncated"] is True
    assert coverage["retention_partial_reason"] == (
        "provider_attempt_retention_window_truncated"
    )
    assert coverage["whole_turn_model_calls"] == 4
    assert coverage["logical_call_coverage"] is None
    assert coverage["logical_call_coverage_reason"] == (
        "provider_attempt_retention_window_truncated"
    )


def test_usage_attempt_query_has_disjoint_rollup_raw_and_hybrid_shapes():
    query = _shanghai_usage_query()
    first = datetime(2026, 7, 1).date()
    second = datetime(2026, 7, 2).date()

    rollup_only, _ = jobs_store._usage_attempt_query(
        query,
        usage_reporting.RollupPartition(
            rollup_days=(first, second), raw_days=()
        ),
    )
    assert "llm_usage_daily_attempt_dimensions" in rollup_only
    assert "llm_usage_daily_call_memberships" in rollup_only
    assert "v2_turn_metrics" not in rollup_only
    assert "llm_provider_attempts" not in rollup_only
    assert "llm_provider_attempt_corrections" not in rollup_only

    raw_only, _ = jobs_store._usage_attempt_query(query, None)
    assert "v2_turn_metrics" in raw_only
    assert "llm_provider_attempts" in raw_only
    assert "llm_provider_attempt_corrections" in raw_only
    assert "llm_usage_daily_attempt_dimensions" not in raw_only
    assert "llm_usage_daily_call_memberships" not in raw_only

    hybrid, _ = jobs_store._usage_attempt_query(
        query,
        usage_reporting.RollupPartition(
            rollup_days=(first,), raw_days=(second,)
        ),
    )
    assert "llm_usage_daily_attempt_dimensions" in hybrid
    assert "llm_usage_daily_call_memberships" in hybrid
    assert "v2_turn_metrics" in hybrid
    assert "llm_provider_attempts" in hybrid
    assert "llm_provider_attempt_corrections" in hybrid
    assert "LEFT JOIN LATERAL" not in hybrid
    assert "percentile_cont" in hybrid
    assert "attempt_scope_rows AS MATERIALIZED" in hybrid

    rolling = _usage.UsageQuery(
        start_at_utc=datetime(2026, 5, 4, 12, 30, tzinfo=timezone.utc),
        end_at_utc=datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
        preset="90d",
    )
    rolling_partition = usage_reporting.rollup_partition(rolling)
    assert rolling_partition is not None
    rolling_sql, rolling_params = jobs_store._usage_attempt_query(
        rolling, rolling_partition
    )
    raw_cohort = rolling_sql.split("turn_cohort AS MATERIALIZED", 1)[1].split(
        "), attempt_base", 1
    )[0]
    assert raw_cohort.count("m.created_at >= %s AND m.created_at < %s") == 2
    assert " OR " in raw_cohort
    assert rolling_params[-4:] == (
        datetime(2026, 5, 4, 12, 30, tzinfo=timezone.utc),
        datetime(2026, 5, 4, 16, tzinfo=timezone.utc),
        datetime(2026, 8, 1, 16, tzinfo=timezone.utc),
        datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc),
    )


def test_usage_attempt_hybrid_rollup_only_matches_raw_payload(
    provider_attempt_usage_rows,
):
    query = _shanghai_usage_query(user_id=provider_attempt_usage_rows)
    raw = jobs_store._usage_report_snapshot_raw(query)["attempts"]
    days = (datetime(2026, 7, 1).date(), datetime(2026, 7, 2).date())
    for local_day in days:
        provider_attempt_rollup.recompute_local_day(local_day)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks "
            "(rollup_name,bootstrap_complete,completed_through_day,retained_from) "
            "VALUES (%s,true,%s,%s) ON CONFLICT (rollup_name) DO UPDATE SET "
            "bootstrap_complete=true,completed_through_day=excluded.completed_through_day,"
            "retained_from=excluded.retained_from",
            (provider_attempt_rollup.ROLLUP_NAME, days[-1], days[0]),
        )
    _enable_usage_rollup(days=days)
    try:
        hybrid = jobs_store.usage_report_snapshot(query)["attempts"]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute(
                "DELETE FROM v2_usage_rollup_watermarks WHERE rollup_name=ANY(%s)",
                (["hosted_v2_usage", provider_attempt_rollup.ROLLUP_NAME],),
            )

    assert hybrid["coverage"]["retained_from"] == days[0].isoformat()
    assert hybrid["coverage"]["retention_truncated"] is False
    for coverage in (hybrid["coverage"], raw["coverage"]):
        coverage.pop("retained_from")
        coverage.pop("retention_truncated")
        coverage.pop("retention_partial_reason")
    assert hybrid == raw


def test_usage_attempt_hybrid_partial_dirty_correction_and_ttft_match_raw():
    user_id = "u_attempt_hybrid_edges"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    first = datetime(2026, 7, 1).date()
    second = datetime(2026, 7, 2).date()
    second_attempt = "67777777-7777-5777-8777-777777777777"
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,model_calls,retries,failed,status,created_at) "
                "VALUES (81120,%s,'chat',1,0,false,'attempt-edge-1',%s),"
                "(81121,%s,'chat',1,0,false,'attempt-edge-2',%s)",
                (
                    user_id,
                    "2026-07-01T10:00:00+00:00",
                    user_id,
                    "2026-07-02T10:00:00+00:00",
                ),
            )
            _insert_attempt(
                conn,
                attempt_id="66666666-6666-5666-8666-666666666666",
                user_id=user_id,
                job_id=81120,
                call_id="attempt-edge-call-1",
                input_tokens=10,
                ttft_ms=10,
            )
            _insert_attempt(
                conn,
                attempt_id=second_attempt,
                user_id=user_id,
                job_id=81121,
                call_id="attempt-edge-call-2",
                input_tokens=20,
                ttft_ms=90,
                resolved_provider="edge-provider",
                resolved_model="edge-model",
            )
        for local_day in (first, second):
            provider_attempt_rollup.recompute_local_day(local_day)
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO llm_usage_rollup_watermarks "
                "(rollup_name,bootstrap_complete,completed_through_day,retained_from) "
                "VALUES (%s,true,%s,%s) ON CONFLICT (rollup_name) DO UPDATE SET "
                "bootstrap_complete=true,completed_through_day=excluded.completed_through_day,"
                "retained_from=excluded.retained_from",
                (provider_attempt_rollup.ROLLUP_NAME, second, first),
            )
        _enable_usage_rollup(days=(first, second))
        query = _usage.UsageQuery(
            start_at_utc=datetime(2026, 7, 1, 2, tzinfo=timezone.utc),
            end_at_utc=datetime(2026, 7, 2, 16, tzinfo=timezone.utc),
            timezone="Asia/Shanghai",
            user_id=user_id,
        )
        raw = jobs_store._usage_report_snapshot_raw(query)["attempts"]
        hybrid = jobs_store.usage_report_snapshot(query)["attempts"]
        assert hybrid == raw
        assert hybrid["overview"]["ttft_ms_p50"] == pytest.approx(50)
        assert hybrid["overview"]["ttft_ms_p95"] == pytest.approx(86)

        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO llm_provider_attempt_corrections "
                "(attempt_id,user_id,revision,reason_code,input_tokens_delta) "
                "VALUES (%s,%s,3,'late_usage',7)",
                (second_attempt, user_id),
            )
            conn.execute(
                "INSERT INTO llm_usage_rollup_dirty_days "
                "(rollup_name,local_day,reason,generation) VALUES (%s,%s,'test',0) "
                "ON CONFLICT (rollup_name,local_day) DO UPDATE SET reason='test'",
                (provider_attempt_rollup.ROLLUP_NAME, second),
            )
        dirty_raw = jobs_store._usage_report_snapshot_raw(query)["attempts"]
        dirty_hybrid = jobs_store.usage_report_snapshot(query)["attempts"]
        assert dirty_hybrid == dirty_raw
        assert dirty_hybrid["overview"]["input_tokens"] == 37

        filtered_query = _usage.UsageQuery(
            start_at_utc=query.start_at_utc,
            end_at_utc=query.end_at_utc,
            timezone="Asia/Shanghai",
            user_id=user_id,
            provider="edge-provider",
            model="edge-model",
        )
        assert jobs_store.usage_report_snapshot(filtered_query)["attempts"] == (
            jobs_store._usage_report_snapshot_raw(filtered_query)["attempts"]
        )
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))
            conn.execute(
                "DELETE FROM llm_usage_rollup_dirty_days WHERE rollup_name=%s",
                (provider_attempt_rollup.ROLLUP_NAME,),
            )
            conn.execute(
                "DELETE FROM v2_usage_rollup_watermarks WHERE rollup_name=ANY(%s)",
                (["hosted_v2_usage", provider_attempt_rollup.ROLLUP_NAME],),
            )


def test_usage_snapshot_cost_categories_are_mutually_exclusive():
    user_id = "u_attempt_cost_categories"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,model_calls,retries,failed,status,created_at) "
                "VALUES (81004,%s,'chat',1,0,false,'cost-category-turn',%s)",
                (user_id, "2026-07-01T10:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO llm_rate_cards "
                "(provider,model,version,currency,input_cost_per_million,"
                "cache_read_cost_per_million,cache_write_cost_per_million,"
                "cache_miss_cost_per_million,effective_at) VALUES "
                "('category-provider','category-model','v1','USD',2,1,3,4,%s)",
                ("2026-06-01T00:00:00+00:00",),
            )
            _insert_attempt(
                conn,
                attempt_id="6aaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa",
                user_id=user_id,
                call_id="logical-cost-category",
                job_id=81004,
                resolved_provider="category-provider",
                resolved_model="category-model",
                input_tokens=100,
                cache_read_tokens=20,
                cache_write_tokens=10,
                cache_miss_tokens=40,
            )

        costs = jobs_store.usage_report_snapshot(
            _usage_query(user_id=user_id)
        )["attempts"]["costs"]

        # 40 regular*2 + 20 read*1 + 10 write*3 + 30 non-write miss*4.
        assert costs == [{
            "currency": "USD",
            "authoritative_cost": None,
            "estimated_cost": Decimal("0.00025000"),
            "authoritative_attempts": 0,
            "estimated_attempts": 1,
            "unknown_attempts": 0,
        }]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


def test_usage_snapshot_estimated_cost_requires_every_nonzero_rate_component():
    user_id = "u_attempt_partial_cost_usage"
    attempt_id = "6ccccccc-cccc-5ccc-8ccc-cccccccccccc"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,model_calls,retries,failed,status,created_at) "
                "VALUES (81007,%s,'chat',1,0,false,'partial-cost-turn',%s)",
                (user_id, "2026-07-01T10:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO llm_rate_cards "
                "(provider,model,version,currency,input_cost_per_million,"
                "output_cost_per_million,reasoning_cost_per_million,"
                "cache_read_cost_per_million,cache_write_cost_per_million,"
                "cache_miss_cost_per_million,effective_at) VALUES "
                "('partial-provider','partial-model','v1','USD',2,3,4,5,6,7,%s)",
                ("2026-06-01T00:00:00+00:00",),
            )
            _insert_attempt(
                conn,
                attempt_id=attempt_id,
                user_id=user_id,
                job_id=81007,
                call_id="partial-cost-call",
                resolved_provider="partial-provider",
                resolved_model="partial-model",
                usage_known=False,
            )
            conn.execute(
                "INSERT INTO llm_provider_attempt_corrections "
                "(attempt_id,user_id,revision,reason_code,input_tokens_delta) "
                "VALUES (%s,%s,3,'late_usage',10)",
                (attempt_id, user_id),
            )

        attempts = jobs_store.usage_report_snapshot(
            _usage_query(user_id=user_id)
        )["attempts"]

        assert attempts["overview"]["input_tokens"] == 10
        assert attempts["overview"]["output_tokens"] is None
        assert attempts["costs"] == [{
            "currency": None,
            "authoritative_cost": None,
            "estimated_cost": None,
            "authoritative_attempts": 0,
            "estimated_attempts": 0,
            "unknown_attempts": 1,
        }]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


@pytest.mark.parametrize(
    "zero_rate_column",
    (
        "cache_read_cost_per_million",
        "cache_write_cost_per_million",
        "cache_miss_cost_per_million",
    ),
)
def test_usage_snapshot_requires_cache_allocation_for_zero_differential_rate(
    zero_rate_column,
):
    suffix = zero_rate_column.removeprefix("cache_").removesuffix(
        "_cost_per_million"
    )
    user_id = f"u_attempt_zero_{suffix}_rate"
    provider = f"zero-{suffix}-provider"
    model = f"zero-{suffix}-model"
    rates = {
        "cache_read_cost_per_million": 2,
        "cache_write_cost_per_million": 2,
        "cache_miss_cost_per_million": 2,
    }
    rates[zero_rate_column] = 0
    cache_allocations = {
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_miss_tokens": 0,
    }
    cache_allocations[zero_rate_column.replace("_cost_per_million", "_tokens")] = None
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,model_calls,retries,failed,status,created_at) "
                "VALUES (81020,%s,'chat',1,0,false,%s,%s)",
                (user_id, f"zero-{suffix}-rate-turn", "2026-07-01T10:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO llm_rate_cards "
                "(provider,model,version,currency,input_cost_per_million,"
                "cache_read_cost_per_million,cache_write_cost_per_million,"
                "cache_miss_cost_per_million,effective_at) VALUES "
                "(%s,%s,'v1','USD',2,%s,%s,%s,%s)",
                (
                    provider,
                    model,
                    rates["cache_read_cost_per_million"],
                    rates["cache_write_cost_per_million"],
                    rates["cache_miss_cost_per_million"],
                    "2026-06-01T00:00:00+00:00",
                ),
            )
            _insert_attempt(
                conn,
                attempt_id={
                    "read": "61111111-1111-5111-8111-111111111111",
                    "write": "62222222-2222-5222-8222-222222222222",
                    "miss": "63333333-3333-5333-8333-333333333333",
                }[suffix],
                user_id=user_id,
                job_id=81020,
                call_id=f"zero-{suffix}-rate-call",
                resolved_provider=provider,
                resolved_model=model,
                input_tokens=100,
                **cache_allocations,
            )

        costs = jobs_store.usage_report_snapshot(
            _usage_query(user_id=user_id)
        )["attempts"]["costs"]

        assert costs == [{
            "currency": None,
            "authoritative_cost": None,
            "estimated_cost": None,
            "authoritative_attempts": 0,
            "estimated_attempts": 0,
            "unknown_attempts": 1,
        }]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


def test_usage_snapshot_allows_missing_cache_allocations_at_equal_input_rate():
    user_id = "u_attempt_equal_cache_rates"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,model_calls,retries,failed,status,created_at) "
                "VALUES (81021,%s,'chat',1,0,false,'equal-cache-rates-turn',%s)",
                (user_id, "2026-07-01T10:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO llm_rate_cards "
                "(provider,model,version,currency,input_cost_per_million,"
                "cache_read_cost_per_million,cache_write_cost_per_million,"
                "cache_miss_cost_per_million,effective_at) VALUES "
                "('equal-cache-provider','equal-cache-model','v1','USD',2,2,2,2,%s)",
                ("2026-06-01T00:00:00+00:00",),
            )
            _insert_attempt(
                conn,
                attempt_id="64444444-4444-5444-8444-444444444444",
                user_id=user_id,
                job_id=81021,
                call_id="equal-cache-rate-call",
                resolved_provider="equal-cache-provider",
                resolved_model="equal-cache-model",
                input_tokens=100,
            )

        costs = jobs_store.usage_report_snapshot(
            _usage_query(user_id=user_id)
        )["attempts"]["costs"]

        assert costs == [{
            "currency": "USD",
            "authoritative_cost": None,
            "estimated_cost": Decimal("0.00020000"),
            "authoritative_attempts": 0,
            "estimated_attempts": 1,
            "unknown_attempts": 0,
        }]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


def test_usage_snapshot_keeps_null_currency_when_cost_component_is_currencyless():
    user_id = "u_attempt_null_plus_usd"
    attempt_id = "6ddddddd-dddd-5ddd-8ddd-dddddddddddd"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,model_calls,retries,failed,status,created_at) "
                "VALUES (81008,%s,'chat',1,0,false,'null-usd-cost-turn',%s)",
                (user_id, "2026-07-01T10:00:00+00:00"),
            )
            _insert_attempt(
                conn,
                attempt_id=attempt_id,
                user_id=user_id,
                job_id=81008,
                call_id="null-usd-cost-call",
                cost=Decimal("1.00000000"),
                currency=None,
            )
            conn.execute(
                "INSERT INTO llm_provider_attempt_corrections "
                "(attempt_id,user_id,revision,reason_code,cost_delta,currency) "
                "VALUES (%s,%s,3,'late_cost',.25,'USD')",
                (attempt_id, user_id),
            )

        costs = jobs_store.usage_report_snapshot(
            _usage_query(user_id=user_id)
        )["attempts"]["costs"]

        assert costs == [{
            "currency": None,
            "authoritative_cost": Decimal("1.25000000"),
            "estimated_cost": None,
            "authoritative_attempts": 1,
            "estimated_attempts": 0,
            "unknown_attempts": 0,
        }]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


def test_usage_snapshot_does_not_relabel_authoritative_cost_currency_conflicts():
    user_id = "u_attempt_currency_conflict"
    attempt_id = "6bbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,model_calls,retries,failed,status,created_at) "
                "VALUES (81005,%s,'chat',1,0,false,'currency-conflict-turn',%s)",
                (user_id, "2026-07-01T10:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO llm_rate_cards "
                "(provider,model,version,currency,input_cost_per_million,effective_at) "
                "VALUES ('currency-provider','currency-model','v1','USD',2,%s)",
                ("2026-06-01T00:00:00+00:00",),
            )
            _insert_attempt(
                conn,
                attempt_id=attempt_id,
                user_id=user_id,
                call_id="logical-currency-conflict",
                job_id=81005,
                resolved_provider="currency-provider",
                resolved_model="currency-model",
                input_tokens=100,
                cost=Decimal("1.00000000"),
                currency="USD",
            )
            conn.execute(
                "INSERT INTO llm_provider_attempt_corrections "
                "(attempt_id,user_id,revision,reason_code,cost_delta,currency) "
                "VALUES (%s,%s,3,'late_cost',.25,'EUR')",
                (attempt_id, user_id),
            )

        costs = jobs_store.usage_report_snapshot(
            _usage_query(user_id=user_id)
        )["attempts"]["costs"]

        assert costs == [{
            "currency": None,
            "authoritative_cost": Decimal("1.25000000"),
            "estimated_cost": None,
            "authoritative_attempts": 1,
            "estimated_attempts": 0,
            "unknown_attempts": 0,
        }]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


def test_usage_snapshot_keeps_all_unknown_ledger_usage_and_cost_null():
    user_id = "u_attempt_unknown"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,model_calls,retries,failed,status,created_at) "
                "VALUES (81006,%s,'chat',1,0,false,'unknown-turn',%s)",
                (user_id, "2026-07-01T10:00:00+00:00"),
            )
            _insert_attempt(
                conn,
                attempt_id="77777777-7777-5777-8777-777777777777",
                user_id=user_id,
                call_id="logical-unknown",
                job_id=81006,
                usage_known=False,
            )
        attempts = jobs_store.usage_report_snapshot(
            _usage_query(user_id=user_id)
        )["attempts"]
        assert attempts["overview"]["input_tokens"] is None
        assert attempts["overview"]["output_tokens"] is None
        assert attempts["overview"]["reasoning_tokens"] is None
        assert attempts["costs"] == [{
            "currency": None,
            "authoritative_cost": None,
            "estimated_cost": None,
            "authoritative_attempts": 0,
            "estimated_attempts": 0,
            "unknown_attempts": 1,
        }]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


def test_usage_attempt_section_failure_does_not_hide_whole_turn_sections(
    monkeypatch, usage_rows,
):
    def observer(event, **fields):
        if event == "read" and fields.get("section") == "attempt_ledger":
            raise RuntimeError("attempt ledger injected")

    monkeypatch.setattr(jobs_store, "_usage_snapshot_observer", observer)

    report = jobs_store.usage_report_snapshot(_usage_query())

    assert report["attempts"] is None
    assert report["overview"]["turns"] == 5
    assert report["overview"]["failed_turns"] == 2
    assert report["daily"] is not None
    assert report["users"] is not None
    assert report["models"] is not None


def _shanghai_usage_query(**filters):
    return _usage.UsageQuery(
        start_at_utc=datetime(2026, 6, 30, 16, tzinfo=timezone.utc),
        end_at_utc=datetime(2026, 7, 2, 16, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
        **filters,
    )


def _enable_usage_rollup(
    *,
    dirty_from_day=None,
    dirty_through_day=None,
    days=(datetime(2026, 7, 1).date(), datetime(2026, 7, 2).date()),
):
    refreshed_at = datetime(2026, 8, 2, 10, tzinfo=timezone.utc)
    for local_day in days:
        usage_rollup.recompute_local_day(local_day, refreshed_at=refreshed_at)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_usage_rollup_watermarks "
            "(rollup_name,bootstrap_complete,source_updated_at,source_id,"
            "source_observed_updated_at,source_lag_seconds,refreshed_at,last_success_at,"
            "dirty_from_day,dirty_through_day) "
            "VALUES ('hosted_v2_usage',true,%s,99,%s,12.5,%s,%s,%s,%s) "
            "ON CONFLICT (rollup_name) DO UPDATE SET bootstrap_complete=true,"
            "dirty_from_day=excluded.dirty_from_day,dirty_through_day=excluded.dirty_through_day,"
            "source_updated_at=excluded.source_updated_at,source_id=excluded.source_id,"
            "source_observed_updated_at=excluded.source_observed_updated_at,"
            "source_lag_seconds=excluded.source_lag_seconds,refreshed_at=excluded.refreshed_at,"
            "last_success_at=excluded.last_success_at,last_error=NULL,last_error_at=NULL",
            (
                refreshed_at, refreshed_at, refreshed_at, refreshed_at,
                dirty_from_day, dirty_through_day,
            ),
        )
    return refreshed_at


def _assert_hybrid_matches_raw(query):
    raw = jobs_store._usage_report_snapshot_raw(query)
    hybrid = jobs_store.usage_report_snapshot(query)
    freshness = hybrid["coverage"].pop("rollup")
    assert hybrid == raw
    return freshness


def test_usage_rollup_partition_keeps_partial_and_dirty_days_raw():
    query = _usage.UsageQuery(
        start_at_utc=datetime(2026, 6, 30, 16, tzinfo=timezone.utc),
        end_at_utc=datetime(2026, 7, 3, 18, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
    )

    partition = usage_reporting.rollup_partition(
        query,
        dirty_from_day=datetime(2026, 7, 2).date(),
        dirty_through_day=datetime(2026, 7, 2).date(),
    )

    assert partition.rollup_days == (
        datetime(2026, 7, 1).date(),
        datetime(2026, 7, 3).date(),
    )
    assert partition.raw_days == (
        datetime(2026, 7, 2).date(),
        datetime(2026, 7, 4).date(),
    )


def test_usage_rollup_partition_rejects_non_shanghai_but_accepts_unknown():
    assert usage_reporting.rollup_partition(_usage_query()) is None
    assert usage_reporting.rollup_partition(
        _shanghai_usage_query(completeness="unknown")
    ) == usage_reporting.RollupPartition(
        rollup_days=(
            datetime(2026, 7, 1).date(),
            datetime(2026, 7, 2).date(),
        ),
        raw_days=(),
    )


def test_usage_hybrid_serial_matches_raw_payload_and_exposes_freshness(usage_rows):
    query = _shanghai_usage_query()
    raw = jobs_store._usage_report_snapshot_raw(query)
    refreshed_at = _enable_usage_rollup()
    try:
        hybrid = jobs_store.usage_report_snapshot(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    freshness = hybrid["coverage"].pop("rollup")
    for section in raw:
        if section == "models":
            for actual, expected in zip(hybrid[section], raw[section], strict=True):
                for key in expected:
                    assert actual[key] == expected[key], (section, key, actual, expected)
        else:
            assert hybrid[section] == raw[section], section
    assert freshness == {
        "mode": "hybrid-parallel",
        "refreshed_at": refreshed_at,
        "last_success_at": refreshed_at,
        "processed_updated_at": refreshed_at,
        "processed_id": 99,
        "source_observed_updated_at": refreshed_at,
        "source_lag_seconds": 12.5,
        "last_error_at": None,
        "last_error": None,
        "raw_days": [],
        "rollup_days": ["2026-07-01", "2026-07-02"],
    }


@pytest.mark.parametrize(
    "filters",
    [
        {"lane": "chat", "provider": "openrouter", "model": "gpt-a"},
        {"completeness": "metered"},
        {"completeness": "unknown"},
    ],
)
def test_usage_hybrid_filters_match_raw_including_unknown_metered_intersection(
    usage_rows, filters
):
    _enable_usage_rollup()
    try:
        query = _shanghai_usage_query(**filters)
        freshness = _assert_hybrid_matches_raw(query)
        if filters.get("completeness") == "unknown":
            report = jobs_store.usage_report_snapshot(query)
            assert report["averages"]["tokens_per_metered_turn"] == pytest.approx(35)
            assert report["averages"]["user_day_tokens"] is not None
            beta = next(row for row in report["users"] if row["user_id"] == "u_usage_beta")
            assert beta["metered_turns"] == 1
            assert beta["tokens_per_metered_turn"] == pytest.approx(35)
        assert freshness["mode"] in {"hybrid", "hybrid-parallel"}
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")


def test_usage_hybrid_partial_dirty_and_empty_days_match_disjoint_raw(usage_rows):
    dirty = datetime(2026, 7, 2).date()
    _enable_usage_rollup(
        dirty_from_day=dirty,
        dirty_through_day=dirty,
        days=(
            datetime(2026, 7, 1).date(),
            datetime(2026, 7, 2).date(),
            datetime(2026, 7, 3).date(),
        ),
    )
    query = _usage.UsageQuery(
        start_at_utc=datetime(2026, 6, 30, 18, tzinfo=timezone.utc),
        end_at_utc=datetime(2026, 7, 3, 18, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
    )
    try:
        freshness = _assert_hybrid_matches_raw(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert freshness["rollup_days"] == ["2026-07-03"]
    assert freshness["raw_days"] == ["2026-07-01", "2026-07-02", "2026-07-04"]


def test_usage_distribution_matches_raw_for_filtered_mixed_partition(usage_rows):
    dirty = datetime(2026, 7, 2).date()
    _enable_usage_rollup(dirty_from_day=dirty, dirty_through_day=dirty)
    query = _usage.UsageQuery(
        start_at_utc=datetime(2026, 6, 30, 16, tzinfo=timezone.utc),
        end_at_utc=datetime(2026, 7, 2, 16, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
        provider="openrouter",
        model="gpt-a",
    )
    try:
        freshness = _assert_hybrid_matches_raw(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert freshness["rollup_days"] == ["2026-07-01"]
    assert freshness["raw_days"] == ["2026-07-02"]


def test_usage_parallel_orders_known_zero_before_unknown_tokens():
    users = ("u_usage_known_zero", "u_usage_unknown_order")
    for user_id in users:
        seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_turn_metrics "
            "(user_id,lane,provider,model,prompt_tokens,completion_tokens,"
            "usage_reported_calls,cache_reported_calls,model_calls,retries,failed,"
            "status,created_at) VALUES "
            "(%s,'known-lane','known-provider','known-model',0,0,1,0,1,0,false,'known-zero',%s),"
            "(%s,'unknown-lane','unknown-provider','unknown-model',NULL,NULL,0,0,5,0,false,'unknown-order',%s)",
            (
                users[0], "2026-07-01T10:00:00+00:00",
                users[1], "2026-07-01T11:00:00+00:00",
            ),
        )
    query = _shanghai_usage_query()
    raw = jobs_store._usage_report_snapshot_raw(query)
    _enable_usage_rollup(days=(datetime(2026, 7, 1).date(), datetime(2026, 7, 2).date()))
    try:
        report = jobs_store.usage_report_snapshot(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")
            conn.execute("DELETE FROM users WHERE user_id=ANY(%s)", (list(users),))

    report["coverage"].pop("rollup")
    assert report == raw
    assert [row["user_id"] for row in report["users"]] == list(users)
    assert [row["model"] for row in report["models"]] == [
        "known-model", "unknown-model"
    ]
    assert [row["lane"] for row in report["lanes"]] == [
        "known-lane", "unknown-lane"
    ]


def test_usage_rollup_report_has_no_rows_after_account_deletion():
    user_id = "u_usage_rollup_delete"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_turn_metrics "
            "(user_id,lane,provider,model,prompt_tokens,completion_tokens,"
            "usage_reported_calls,cache_reported_calls,model_calls,retries,failed,"
            "status,created_at) VALUES (%s,'chat','provider','model',10,5,1,0,1,0,false,'delete-rollup',%s)",
            (user_id, "2026-07-01T10:00:00+00:00"),
        )
    _enable_usage_rollup()
    query = _shanghai_usage_query(user_id=user_id)
    before = jobs_store.usage_report_snapshot(query)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))
        residual = conn.execute(
            "SELECT "
            "(SELECT count(*) FROM v2_turn_metrics WHERE user_id=%s),"
            "(SELECT count(*) FROM v2_usage_daily_users WHERE user_id=%s),"
            "(SELECT count(*) FROM v2_usage_daily_dimensions WHERE user_id=%s)",
            (user_id, user_id, user_id),
        ).fetchone()
    try:
        after = jobs_store.usage_report_snapshot(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert before["overview"]["total_tokens"] == 15
    assert residual == (0, 0, 0)
    assert after["overview"]["registered_accounts"] == 0
    assert after["overview"]["turns"] == 0
    assert after["users"] == []
    assert after["models"] == []


def test_usage_hybrid_raw_sql_inlines_scalar_created_at_ranges_for_index_selectivity():
    query = _usage.UsageQuery(
        start_at_utc=datetime(2026, 5, 4, 12, 30, tzinfo=timezone.utc),
        end_at_utc=datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
        preset="90d",
    )
    partition = usage_reporting.rollup_partition(query)
    assert partition is not None
    statement, params = jobs_store._usage_fact_query(query, partition, dimensions=False)

    assert "raw_ranges" not in statement
    assert statement.count("m.created_at >= %s AND m.created_at < %s") == 2
    assert "NOT ((m.created_at AT TIME ZONE" not in statement
    assert params[-4:] == (
        datetime(2026, 5, 4, 12, 30, tzinfo=timezone.utc),
        datetime(2026, 5, 4, 16, tzinfo=timezone.utc),
        datetime(2026, 8, 1, 16, tzinfo=timezone.utc),
        datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc),
    )


def test_usage_rollup_only_sql_omits_authoritative_raw_table_entirely():
    partition = usage_reporting.RollupPartition(
        rollup_days=(
            datetime(2026, 7, 1).date(),
            datetime(2026, 7, 2).date(),
        ),
        raw_days=(),
    )
    statement, params = jobs_store._usage_fact_query(
        _shanghai_usage_query(provider="openrouter", model="gpt-a"),
        partition,
        dimensions=True,
    )

    assert "v2_usage_daily_dimensions" in statement
    assert "v2_turn_metrics" not in statement
    assert "raw_ranges" not in statement
    assert "UNION ALL" not in statement
    assert params == (
        datetime(2026, 7, 1).date(),
        datetime(2026, 7, 3).date(),
        "openrouter",
        "gpt-a",
    )


def test_usage_rollup_sql_compacts_contiguous_days_into_half_open_ranges():
    partition = usage_reporting.RollupPartition(
        rollup_days=(
            datetime(2026, 7, 1).date(),
            datetime(2026, 7, 2).date(),
            datetime(2026, 7, 4).date(),
        ),
        raw_days=(),
    )

    statement, params = jobs_store._usage_fact_query(
        _shanghai_usage_query(), partition, dimensions=False
    )

    assert statement.count("v2_usage_daily_users") == 1
    assert statement.count("local_day >= %s AND local_day < %s") == 2
    assert "local_day=ANY" not in statement
    assert params == (
        datetime(2026, 7, 1).date(),
        datetime(2026, 7, 3).date(),
        datetime(2026, 7, 4).date(),
        datetime(2026, 7, 5).date(),
    )


def test_usage_page_renders_rollup_freshness_without_coercing_unknown_to_zero():
    report = _usage_render_report()
    report["coverage"]["rollup"] = {
        "mode": "hybrid",
        "refreshed_at": datetime(2026, 8, 2, 10, tzinfo=timezone.utc),
        "last_success_at": datetime(2026, 8, 2, 9, 59, tzinfo=timezone.utc),
        "processed_updated_at": datetime(2026, 8, 2, 9, 55, tzinfo=timezone.utc),
        "processed_id": 42,
        "source_observed_updated_at": datetime(2026, 8, 2, 10, tzinfo=timezone.utc),
        "source_lag_seconds": None,
        "last_error_at": datetime(2026, 8, 2, 9, 58, tzinfo=timezone.utc),
        "last_error": "QueryCanceled",
        "raw_days": ["2026-08-02"],
        "rollup_days": ["2026-08-01"],
    }

    with _admin_core.bind("view=usage&preset=30d"):
        body = _data_track._render_usage_page(report, _usage_query())

    assert "Rollup freshness" in body
    assert "lag unknown" in body
    assert "QueryCanceled" in body
    assert "lag 0" not in body


def test_usage_page_labels_turn_and_attempt_sources_and_escapes_identities():
    report = _usage_render_report()
    report["attempts"] = {
        "overview": {
            "attempts": 18,
            "logical_calls": 13,
            "retry_attempts": 5,
            "failover_attempts": 2,
            "failed_attempts": 3,
            "usage_known_attempts": 15,
            "usage_unknown_attempts": 3,
            "possibly_billed_attempts": 1,
            "input_tokens": 1_210,
            "output_tokens": 350,
            "reasoning_tokens": 70,
            "cache_read_tokens": 610,
            "cache_write_tokens": 41,
            "cache_miss_tokens": 302,
            "ttft_ms_p50": 125,
            "ttft_ms_p95": 800,
        },
        "users": [],
        "lanes": [],
        "requested_models": [{
            "provider": "asked<&",
            "model": "model<script>",
            "attempts": 18,
            "logical_calls": 13,
            "retry_attempts": 5,
            "failover_attempts": 2,
            "failed_attempts": 3,
            "usage_known_attempts": 15,
            "usage_unknown_attempts": 3,
            "possibly_billed_attempts": 1,
            "input_tokens": 1_210,
            "output_tokens": 350,
            "reasoning_tokens": 70,
            "cache_read_tokens": 610,
            "cache_write_tokens": 41,
            "cache_miss_tokens": 302,
            "ttft_ms_p50": 125,
            "ttft_ms_p95": 800,
        }],
        "resolved_models": [{
            "provider": "actual>&",
            "model": "resolved</td>",
            "attempts": 18,
            "logical_calls": 13,
            "retry_attempts": 5,
            "failover_attempts": 2,
            "failed_attempts": 3,
            "usage_known_attempts": 15,
            "usage_unknown_attempts": 3,
            "possibly_billed_attempts": 1,
            "input_tokens": 1_210,
            "output_tokens": 350,
            "reasoning_tokens": 70,
            "cache_read_tokens": 610,
            "cache_write_tokens": 41,
            "cache_miss_tokens": 302,
            "ttft_ms_p50": 125,
            "ttft_ms_p95": 800,
        }],
        "costs": [{
            "currency": "USD<script>",
            "authoritative_cost": Decimal("1.25"),
            "estimated_cost": Decimal("0.75"),
            "authoritative_attempts": 8,
            "estimated_attempts": 7,
            "unknown_attempts": 3,
        }],
        "coverage": {
            "whole_turn_model_calls": 14,
            "recorded_logical_calls": 13,
            "logical_call_coverage": 13 / 14,
            "missing_outer_ordinals": 1,
            "missing_inner_ordinals": 2,
            "attempt_sequence_gaps": 3,
        },
    }

    with _admin_core.bind("view=usage&preset=30d"):
        body = _data_track._render_usage_page(report, _usage_query())

    assert "Whole-turn truth" in body
    assert "Provider-attempt ledger" in body
    assert "Possibly billed attempts" in body
    assert "Logical-call coverage" in body
    assert "13 / 14" in body
    assert "Attempt sequence gaps" in body
    assert "Authoritative / estimated / unknown cost" in body
    assert "Requested provider / model" in body
    assert "Resolved provider / model" in body
    assert "asked&lt;&amp;" in body and "model&lt;script&gt;" in body
    assert "actual&gt;&amp;" in body and "resolved&lt;/td&gt;" in body
    assert "USD&lt;script&gt;" in body
    assert "unavailable until P0-B" not in body
    assert "<script>" not in body
    assert (
        "Whole-turn legacy usage projection; canonical attempt accounting is below."
        in body
    )
    assert "canonical attempt accounting is above" not in body


def test_usage_page_explains_unattributable_filtered_logical_coverage():
    report = _usage_render_report()
    report["attempts"] = {
        "overview": {"attempts": 2, "logical_calls": 1},
        "users": [],
        "lanes": [],
        "requested_models": [],
        "resolved_models": [],
        "costs": [],
        "coverage": {
            "whole_turn_model_calls": None,
            "recorded_logical_calls": 1,
            "logical_call_coverage": None,
            "logical_call_coverage_reason": (
                "provider_model_or_completeness_filters_cannot_attribute_"
                "whole_turn_model_calls"
            ),
            "missing_outer_ordinals": 0,
            "missing_inner_ordinals": 0,
            "attempt_sequence_gaps": 0,
        },
    }

    with _admin_core.bind("view=usage&preset=30d&provider=resolved-a"):
        body = _data_track._render_usage_page(
            report,
            _usage_query(provider="resolved-a"),
        )

    assert "Logical-call coverage unavailable" in body
    assert "provider/model/completeness filters" in body
    assert "filtered attempt statistics remain available" in body


def test_usage_page_explains_retention_truncated_attempt_totals():
    report = _usage_render_report()
    report["attempts"] = {
        "overview": {"attempts": 0, "logical_calls": 0},
        "users": [],
        "lanes": [],
        "requested_models": [],
        "resolved_models": [],
        "costs": [],
        "coverage": {
            "whole_turn_model_calls": 12,
            "recorded_logical_calls": 0,
            "logical_call_coverage": None,
            "logical_call_coverage_reason": (
                "provider_attempt_retention_window_truncated"
            ),
            "retained_from": "2025-06-29",
            "retention_truncated": True,
            "retention_partial_reason": (
                "provider_attempt_retention_window_truncated"
            ),
            "missing_outer_ordinals": 0,
            "missing_inner_ordinals": 0,
            "attempt_sequence_gaps": 0,
        },
    }

    with _admin_core.bind("view=usage&preset=all"):
        body = _data_track._render_usage_page(report, _usage_query())

    assert "Provider-attempt totals are partial" in body
    assert "retained from 2025-06-29" in body
    assert "0 / 12" not in body


def test_usage_page_explains_pending_retention_without_claiming_zero():
    report = _usage_render_report()
    report["attempts"] = {
        "overview": {"attempts": 0, "logical_calls": 0},
        "users": [],
        "lanes": [],
        "requested_models": [],
        "resolved_models": [],
        "costs": [],
        "coverage": {
            "whole_turn_model_calls": 12,
            "recorded_logical_calls": 0,
            "logical_call_coverage": None,
            "logical_call_coverage_reason": "provider_attempt_retention_pending",
            "retained_from": None,
            "retention_pending_from": "2025-06-29",
            "retention_truncated": True,
            "retention_partial_reason": "provider_attempt_retention_pending",
            "missing_outer_ordinals": 0,
            "missing_inner_ordinals": 0,
            "attempt_sequence_gaps": 0,
        },
    }

    with _admin_core.bind("view=usage&preset=all"):
        body = _data_track._render_usage_page(report, _usage_query())

    assert "Provider-attempt retention is in progress" in body
    assert "pending boundary 2025-06-29" in body
    assert "0 / 12" not in body


def test_usage_parallel_exported_snapshot_matches_raw_and_imports_before_read(
    monkeypatch, usage_rows
):
    query = _shanghai_usage_query()
    raw = jobs_store._usage_report_snapshot_raw(query)
    _enable_usage_rollup()
    events = []
    monkeypatch.setattr(
        jobs_store,
        "_usage_snapshot_observer",
        lambda event, **fields: events.append((event, fields)),
    )
    try:
        report = jobs_store._usage_report_snapshot_hybrid_parallel(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    freshness = report["coverage"].pop("rollup")
    assert report == raw
    assert freshness["mode"] == "hybrid-parallel"
    assert sum(event == "exported" for event, _ in events) == 1
    for role in ("dimension", "latency"):
        imported = next(
            index for index, (event, fields) in enumerate(events)
            if event == "imported" and role in fields["role"]
        )
        first_read = next(
            index for index, (event, fields) in enumerate(events)
            if event == "read" and fields["role"] == role
        )
        assert imported < first_read


def test_usage_parallel_attempt_overlaps_exporter_core_without_fourth_connection(
    monkeypatch, usage_rows,
):
    query = _shanghai_usage_query()
    _enable_usage_rollup()
    attempt_started = threading.Event()
    core_started = threading.Event()
    main_thread = threading.get_ident()
    original_attempt = jobs_store._usage_attempt_snapshot

    def observed_attempt(*args, **kwargs):
        assert threading.get_ident() != main_thread
        attempt_started.set()
        assert core_started.wait(1), "exporter core did not overlap attempt importer"
        return original_attempt(*args, **kwargs)

    def observer(event, **fields):
        if event == "read" and fields.get("section") == "core_bundle":
            core_started.set()
            assert attempt_started.wait(1), "attempt was scheduled after exporter core"

    monkeypatch.setattr(jobs_store, "_usage_attempt_snapshot", observed_attempt)
    monkeypatch.setattr(jobs_store, "_usage_snapshot_observer", observer)
    try:
        report = jobs_store.usage_report_snapshot(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert report["attempts"] is not None


def test_usage_parallel_attempt_failure_keeps_sibling_latency_bundle(
    monkeypatch, usage_rows,
):
    query = _shanghai_usage_query()
    _enable_usage_rollup()

    def fail_attempt(*_args, **_kwargs):
        raise RuntimeError("injected attempt failure")

    monkeypatch.setattr(jobs_store, "_usage_attempt_snapshot", fail_attempt)
    try:
        report = jobs_store.usage_report_snapshot(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert report["attempts"] is None
    assert report["models"] is not None
    assert report["lanes"] is not None


def test_usage_parallel_latency_restores_report_deadline_after_attempt_cap(
    monkeypatch, usage_rows,
):
    query = _shanghai_usage_query()
    _enable_usage_rollup()
    observed_timeout_ms = []
    original = jobs_store._usage_parallel_latency_bundle

    def inspect_timeout(cur, *args):
        cur.execute("SHOW statement_timeout")
        value = str(cur.fetchone()["statement_timeout"])
        assert value.endswith("ms")
        observed_timeout_ms.append(int(value.removesuffix("ms")))
        return original(cur, *args)

    monkeypatch.setattr(
        jobs_store, "_usage_parallel_latency_bundle", inspect_timeout
    )
    try:
        report = jobs_store.usage_report_snapshot(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert observed_timeout_ms
    assert observed_timeout_ms[0] > (
        jobs_store._RUNTIME_ATTEMPT_USAGE_STATEMENT_TIMEOUT_MS
    )
    assert report["models"] is not None


def test_usage_core_bundle_error_falls_back_to_isolated_sections(
    monkeypatch, usage_rows
):
    query = _shanghai_usage_query()
    expected = jobs_store._usage_report_snapshot_raw(query)
    _enable_usage_rollup()

    def observer(event, **fields):
        if event == "read" and fields.get("section") == "core_bundle":
            raise RuntimeError("bundle injected")

    monkeypatch.setattr(jobs_store, "_usage_snapshot_observer", observer)
    try:
        report = jobs_store._usage_report_snapshot_hybrid_parallel(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    report["coverage"].pop("rollup")
    assert report == expected


def test_usage_core_bundle_timeout_does_not_multiply_fallback_queries(
    monkeypatch, usage_rows
):
    query = _shanghai_usage_query()
    _enable_usage_rollup()
    fallback_called = False

    def observer(event, **fields):
        if event == "read" and fields.get("section") == "core_bundle":
            raise psycopg.errors.QueryCanceled("bundle deadline")

    def forbidden_fallback(*_args, **_kwargs):
        nonlocal fallback_called
        fallback_called = True
        raise AssertionError("deadline must not multiply fallback work")

    monkeypatch.setattr(jobs_store, "_usage_snapshot_observer", observer)
    monkeypatch.setattr(
        jobs_store, "_usage_parallel_core_rows_separate", forbidden_fallback
    )
    try:
        with pytest.raises(psycopg.errors.QueryCanceled, match="bundle deadline"):
            jobs_store._usage_report_snapshot_hybrid_parallel(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert fallback_called is False


def test_usage_parallel_snapshot_excludes_writer_committed_after_export(
    monkeypatch, usage_rows
):
    query = _shanghai_usage_query()
    expected = jobs_store._usage_report_snapshot_raw(query)
    first = datetime(2026, 7, 1).date()
    second = datetime(2026, 7, 2).date()
    _enable_usage_rollup(dirty_from_day=first, dirty_through_day=second)
    inserted = False

    def observer(event, **_fields):
        nonlocal inserted
        if event != "exported" or inserted:
            return
        inserted = True
        with db.get_pool().connection() as writer:
            writer.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,provider,model,prompt_tokens,completion_tokens,"
                "usage_reported_calls,cache_reported_calls,model_calls,retries,failed,"
                "status,latency_ms,created_at) VALUES "
                "(81999,%s,'chat','openrouter','late-model',999,1,1,0,1,0,false,%s,9,%s)",
                ("u_usage_alpha", "parallel-after-export", "2026-07-01T12:30:00+00:00"),
            )
            _insert_attempt(
                writer,
                attempt_id="89999999-9999-5999-8999-999999999999",
                user_id="u_usage_alpha",
                call_id="parallel-after-export",
                job_id=81999,
                input_tokens=999,
                output_tokens=1,
            )

    monkeypatch.setattr(jobs_store, "_usage_snapshot_observer", observer)
    try:
        report = jobs_store._usage_report_snapshot_hybrid_parallel(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_turn_metrics WHERE status=%s", ("parallel-after-export",))
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    report["coverage"].pop("rollup")
    assert inserted is True
    assert report == expected


def test_usage_parallel_uses_at_most_three_connections(monkeypatch, usage_rows):
    query = _shanghai_usage_query()
    _enable_usage_rollup()
    real_pool = db.get_pool()
    lock = threading.Lock()
    active = 0
    maximum = 0

    class Context:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            nonlocal active, maximum
            value = self.inner.__enter__()
            with lock:
                active += 1
                maximum = max(maximum, active)
            return value

        def __exit__(self, *args):
            nonlocal active
            try:
                return self.inner.__exit__(*args)
            finally:
                with lock:
                    active -= 1

    class Pool:
        def connection(self, *args, **kwargs):
            return Context(real_pool.connection(*args, **kwargs))

    monkeypatch.setattr(jobs_store, "_pool", lambda: Pool())
    try:
        report = jobs_store._usage_report_snapshot_hybrid_parallel(query)
    finally:
        with real_pool.connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert report["overview"]["total_tokens"] == 215
    assert active == 0
    assert 2 <= maximum <= 3


@pytest.mark.parametrize("failed_task", [None, "a", "b"])
def test_usage_parallel_three_bin_merge_degrades_only_failed_task(failed_task):
    merge = getattr(jobs_store, "_usage_merge_parallel_task_results", None)
    assert merge is not None, "three-bin result merge must be implemented"
    core = {"totals": {"model_calls": 1}, "distribution": None, "daily": None}
    task_a = None if failed_task == "a" else {
        "distribution": {"p50": 11},
        "models": [{"provider": "p", "model": "m"}],
        "primary": [{"user_id": "u", "provider": "p", "model": "m"}],
        "filters": {"lanes": ["chat"]},
        "lanes": None,
    }
    task_b = None if failed_task == "b" else {
        "daily": [{"local_day": "2026-07-01"}],
        "lanes": [{"lane": "chat"}],
        "latency_models": [{"provider": "p", "model": "m", "latency_ms_p50": 7}],
    }
    latency_lanes = [{"lane": "chat", "latency_ms_p50": 8}]

    merged_core, dimensions, latency_bundle = merge(
        core, task_a, task_b, latency_lanes
    )

    assert core == {
        "totals": {"model_calls": 1},
        "distribution": None,
        "daily": None,
    }
    assert merged_core["distribution"] == (
        None if failed_task == "a" else {"p50": 11}
    )
    assert merged_core["daily"] == (
        None if failed_task == "b" else [{"local_day": "2026-07-01"}]
    )
    assert dimensions["models"] == (
        None if failed_task == "a" else [{"provider": "p", "model": "m"}]
    )
    assert dimensions["lanes"] == (
        None if failed_task == "b" else [{"lane": "chat"}]
    )
    assert dimensions["primary"] == (
        None
        if failed_task == "a"
        else [{"user_id": "u", "provider": "p", "model": "m"}]
    )
    assert latency_bundle["filters"] == (
        None if failed_task == "a" else {"lanes": ["chat"]}
    )
    assert latency_bundle["latency"]["models"] == (
        None
        if failed_task == "b"
        else [{"provider": "p", "model": "m", "latency_ms_p50": 7}]
    )
    assert latency_bundle["latency"]["lanes"] == latency_lanes


def test_usage_parallel_three_bin_merge_preserves_unknown_core_sections():
    merge = jobs_store._usage_merge_parallel_task_results
    core = {
        "distribution": {"p50": 13},
        "daily": [{"local_day": "2026-07-01", "model_calls": 2}],
    }

    merged_core, dimensions, _latency_bundle = merge(
        core,
        {"models": [], "lanes": [], "primary": [], "distribution": None},
        {"daily": None, "lanes": None, "latency_models": []},
        [],
    )

    assert merged_core == core
    assert dimensions == {"models": [], "lanes": [], "primary": []}


def test_usage_process_and_rds_admission_fail_fast_without_extra_query(usage_rows):
    query = _shanghai_usage_query()
    _enable_usage_rollup()
    assert jobs_store._USAGE_REPORT_GATE.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="process admission busy"):
            jobs_store.usage_report_snapshot(query)
    finally:
        jobs_store._USAGE_REPORT_GATE.release()

    with db.get_pool().connection() as holder:
        assert holder.execute(
            "SELECT pg_try_advisory_lock(%s)",
            (jobs_store._USAGE_REPORT_ADVISORY_KEY,),
        ).fetchone()[0]
        try:
            with pytest.raises(RuntimeError, match="RDS admission busy"):
                jobs_store.usage_report_snapshot(query)
            with pytest.raises(RuntimeError, match="RDS admission busy"):
                jobs_store.usage_report_snapshot(_usage_query())
            holder.execute(
                "UPDATE v2_usage_rollup_watermarks SET bootstrap_complete=false "
                "WHERE rollup_name='hosted_v2_usage'"
            )
            with pytest.raises(RuntimeError, match="RDS admission busy"):
                jobs_store.usage_report_snapshot(query)
        finally:
            holder.execute(
                "SELECT pg_advisory_unlock(%s)",
                (jobs_store._USAGE_REPORT_ADVISORY_KEY,),
            )
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_usage_rollup_watermarks")



def test_usage_importer_failure_rolls_back_and_only_degrades_task_a(
    monkeypatch, usage_rows
):
    query = _shanghai_usage_query()
    _enable_usage_rollup()

    def fail_task_a(*_args):
        raise RuntimeError("task A injected")

    monkeypatch.setattr(jobs_store, "_usage_parallel_dimension_rows", fail_task_a)
    try:
        report = jobs_store._usage_report_snapshot_hybrid_parallel(query)
        with db.get_pool().connection() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert report["overview"]["total_tokens"] == 215
    assert report["daily"] is not None
    assert report["users"] is not None
    assert report["models"] is None
    assert report["lanes"] is not None
    assert {row["primary_provider"] for row in report["users"]} == {"unavailable"}


@pytest.mark.parametrize("raw_path", ["utc", "bootstrap"])
def test_usage_raw_fallback_reuses_exporter_while_rds_admission_is_held(
    monkeypatch, usage_rows, raw_path
):
    query = _usage_query() if raw_path == "utc" else _shanghai_usage_query()
    if raw_path == "bootstrap":
        _enable_usage_rollup()
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE v2_usage_rollup_watermarks SET bootstrap_complete=false "
                "WHERE rollup_name='hosted_v2_usage'"
            )
    real_raw = jobs_store._usage_report_snapshot_raw
    expected = real_raw(query)
    observed = []

    def raw_spy(request_query, *, exporter_conn=None):
        assert exporter_conn is not None
        observed.append(exporter_conn.info.backend_pid)
        with db.get_pool().connection() as contender:
            assert not contender.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (jobs_store._USAGE_REPORT_ADVISORY_KEY,),
            ).fetchone()[0]
        return real_raw(request_query, exporter_conn=exporter_conn)

    monkeypatch.setattr(jobs_store, "_usage_report_snapshot_raw", raw_spy)
    try:
        report = jobs_store.usage_report_snapshot(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert len(observed) == 1
    assert report["coverage"]["rollup"]["mode"] == "raw"
    report["coverage"].pop("rollup")
    assert report == expected


def test_usage_importer_pool_failure_uses_exporter_serial_fallback(
    monkeypatch, usage_rows
):
    query = _shanghai_usage_query()
    expected = jobs_store._usage_report_snapshot_raw(query)
    _enable_usage_rollup()
    real_pool = db.get_pool()
    calls = 0
    lock = threading.Lock()

    class Pool:
        def connection(self, *args, **kwargs):
            nonlocal calls
            with lock:
                calls += 1
                current = calls
            if current > 1:
                raise TimeoutError("test importer checkout timeout")
            return real_pool.connection(*args, **kwargs)

    monkeypatch.setattr(jobs_store, "_pool", lambda: Pool())
    try:
        report = jobs_store._usage_report_snapshot_hybrid_parallel(query)
    finally:
        with real_pool.connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    report["coverage"].pop("rollup")
    assert report == expected
    assert calls == 3


def test_usage_latency_failure_keeps_filters_and_dimension_totals(
    monkeypatch, usage_rows
):
    query = _shanghai_usage_query()
    _enable_usage_rollup()

    def fail_latency(*_args):
        raise RuntimeError("latency injected")

    monkeypatch.setattr(
        jobs_store, "_usage_parallel_latency_model_rows", fail_latency
    )
    monkeypatch.setattr(
        jobs_store, "_usage_parallel_latency_lane_rows", fail_latency
    )
    try:
        report = jobs_store._usage_report_snapshot_hybrid_parallel(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert report["filters"] == {
        "lanes": ["chat", "heartbeat", "maintenance"],
        "providers": ["anthropic", "openrouter", "unknown"],
        "models": ["claude-b", "gpt-a", "unknown"],
    }
    assert report["models"] is not None
    assert report["lanes"] is not None
    assert report["averages"]["user_day_tokens"] is not None
    assert report["daily"] is not None
    assert all(row["latency_ms_p50"] is None for row in report["models"])
    assert all(row["latency_ms_p50"] is None for row in report["lanes"])


def test_usage_task_b_failure_only_degrades_task_b(monkeypatch, usage_rows):
    query = _shanghai_usage_query()
    _enable_usage_rollup()

    def fail_task_b(*_args):
        raise RuntimeError("task B injected")

    monkeypatch.setattr(jobs_store, "_usage_parallel_latency_bundle", fail_task_b)
    try:
        report = jobs_store._usage_report_snapshot_hybrid_parallel(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert report["overview"]["total_tokens"] == 215
    assert report["averages"]["user_day_tokens"] is not None
    assert report["filters"] is not None
    assert report["models"] is not None
    assert report["daily"] is None
    assert report["lanes"] is None


def test_usage_distribution_failure_only_degrades_user_day_percentiles(
    monkeypatch, usage_rows
):
    query = _shanghai_usage_query()
    _enable_usage_rollup()

    monkeypatch.setattr(
        jobs_store,
        "_usage_parallel_distribution_row",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("distribution injected")),
    )
    try:
        report = jobs_store._usage_report_snapshot_hybrid_parallel(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert report["overview"]["total_tokens"] == 215
    assert report["averages"]["user_day_tokens"] is None
    assert report["daily"] is not None
    assert report["users"] is not None
    assert report["models"] is not None


def test_usage_daily_failure_does_not_degrade_other_core_sections(
    monkeypatch, usage_rows
):
    query = _shanghai_usage_query()
    _enable_usage_rollup()

    def fail_daily(*_args):
        raise RuntimeError("daily injected")

    monkeypatch.setattr(jobs_store, "_usage_parallel_daily_rows", fail_daily)
    try:
        report = jobs_store._usage_report_snapshot_hybrid_parallel(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert report["overview"]["total_tokens"] == 215
    assert report["daily"] is None
    assert report["users"] is not None
    assert report["averages"]["user_day_tokens"] is not None
    assert report["models"] is not None


def test_usage_parallel_releases_rds_session_admission(usage_rows):
    query = _shanghai_usage_query()
    _enable_usage_rollup()
    try:
        assert jobs_store._usage_report_snapshot_hybrid_parallel(query) is not None
        with db.get_pool().connection() as conn:
            assert conn.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (jobs_store._USAGE_REPORT_ADVISORY_KEY,),
            ).fetchone()[0]
            assert conn.execute(
                "SELECT pg_advisory_unlock(%s)",
                (jobs_store._USAGE_REPORT_ADVISORY_KEY,),
            ).fetchone()[0]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")


def test_usage_parallel_core_failure_does_not_start_expensive_raw_fallback(
    monkeypatch,
):
    raw_calls = 0

    def fail_parallel(_query, **_kwargs):
        raise RuntimeError("core injected")

    def raw_spy(_query, **_kwargs):
        nonlocal raw_calls
        raw_calls += 1
        return {}

    monkeypatch.setattr(jobs_store, "_usage_report_snapshot_hybrid_parallel", fail_parallel)
    monkeypatch.setattr(jobs_store, "_usage_report_snapshot_raw", raw_spy)

    with pytest.raises(RuntimeError, match="core injected"):
        jobs_store.usage_report_snapshot(_shanghai_usage_query())

    assert raw_calls == 0


def test_usage_raw_report_uses_short_pool_acquire(monkeypatch):
    seen = []

    class Pool:
        def connection(self, *, timeout):
            seen.append(timeout)
            raise TimeoutError("raw checkout injected")

    monkeypatch.setattr(jobs_store, "_pool", lambda: Pool())

    with pytest.raises(TimeoutError, match="raw checkout injected"):
        jobs_store._usage_report_snapshot_raw(_usage_query())

    assert seen == [jobs_store._USAGE_REPORT_POOL_TIMEOUT_SECONDS]


def test_usage_raw_report_sets_local_statement_timeout(monkeypatch, usage_rows):
    real_pool = db.get_pool()
    settings = []

    class Cursor:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def execute(self, statement, params=None):
            if "set_config('statement_timeout'" in str(statement):
                settings.append(params[0])
            return self.inner.execute(statement, params) if params is not None else self.inner.execute(statement)

    class CursorContext:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return Cursor(self.inner.__enter__())

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

    class Connection:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def cursor(self, *args, **kwargs):
            return CursorContext(self.inner.cursor(*args, **kwargs))

    class ConnectionContext:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return Connection(self.inner.__enter__())

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

    class Pool:
        def connection(self, *, timeout):
            assert timeout == jobs_store._USAGE_REPORT_POOL_TIMEOUT_SECONDS
            return ConnectionContext(real_pool.connection(timeout=timeout))

    monkeypatch.setattr(jobs_store, "_pool", lambda: Pool())
    jobs_store._usage_report_snapshot_raw(_usage_query())

    assert settings[0] == str(jobs_store._USAGE_REPORT_STATEMENT_TIMEOUT_MS)
    assert 0 < int(settings[1]) <= jobs_store._RUNTIME_ATTEMPT_USAGE_STATEMENT_TIMEOUT_MS


@pytest.mark.parametrize("failed_section", ["distribution", "daily", "users"])
def test_usage_core_optional_sections_fail_independently(
    monkeypatch, usage_rows, failed_section
):
    query = _shanghai_usage_query()
    _enable_usage_rollup()
    original = jobs_store._usage_optional_pg_section

    def inject(cur, name, reader):
        if name == failed_section:
            return original(
                cur,
                name,
                lambda: cur.execute("SELECT missing_usage_optional_column"),
            )
        return original(cur, name, reader)

    monkeypatch.setattr(jobs_store, "_usage_optional_pg_section", inject)
    def force_isolated_fallback(event, **fields):
        if event == "read" and fields.get("section") == "core_bundle":
            raise RuntimeError("force isolated fallback")

    monkeypatch.setattr(
        jobs_store, "_usage_snapshot_observer", force_isolated_fallback
    )
    try:
        report = jobs_store.usage_report_snapshot(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert report["overview"]["total_tokens"] == 215
    if failed_section == "distribution":
        assert report["averages"]["user_day_tokens"] is None
        assert report["daily"] is not None
        assert report["users"] is not None
    elif failed_section == "daily":
        assert report["daily"] is None
        assert report["users"] is not None
    else:
        assert report["daily"] is not None
        assert report["users"] is None


@pytest.mark.parametrize("failed_section", ["models", "lanes", "primary"])
def test_usage_dimension_sections_fail_independently(
    monkeypatch, usage_rows, failed_section
):
    query = _shanghai_usage_query()
    _enable_usage_rollup()
    original = jobs_store._usage_optional_pg_section

    def inject(cur, name, reader):
        if name == failed_section:
            return original(
                cur,
                name,
                lambda: cur.execute("SELECT missing_usage_dimension_column"),
            )
        return original(cur, name, reader)

    monkeypatch.setattr(jobs_store, "_usage_optional_pg_section", inject)
    try:
        report = jobs_store.usage_report_snapshot(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    if failed_section == "models":
        assert report["models"] is None
        assert report["lanes"] is not None
    elif failed_section == "lanes":
        assert report["models"] is not None
        assert report["lanes"] is None
    else:
        assert report["models"] is not None
        assert report["lanes"] is not None
        assert {row["primary_provider"] for row in report["users"]} == {
            "unavailable"
        }


def test_usage_importer_two_statements_share_one_total_budget(
    monkeypatch, usage_rows
):
    query = _shanghai_usage_query()
    _enable_usage_rollup()
    monkeypatch.setattr(jobs_store, "_USAGE_REPORT_STATEMENT_TIMEOUT_MS", 70)

    def two_sleeps(cur, *_args):
        cur.execute("SELECT pg_sleep(.045)")
        cur.execute("SELECT pg_sleep(.045)")
        return {}

    monkeypatch.setattr(jobs_store, "_usage_parallel_dimension_rows", two_sleeps)
    started = time.monotonic()
    try:
        report = jobs_store.usage_report_snapshot(query)
    finally:
        elapsed = time.monotonic() - started
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert elapsed < 0.25
    assert report["models"] is None
    # Both importers share this exhausted deadline; all optional bins may degrade.
    assert report["lanes"] is None


def test_usage_importer_timeout_cancels_and_releases_before_fallback(
    monkeypatch, usage_rows
):
    query = _shanghai_usage_query()
    _enable_usage_rollup()
    monkeypatch.setattr(jobs_store, "_USAGE_REPORT_STATEMENT_TIMEOUT_MS", 50)
    events = []
    monkeypatch.setattr(
        jobs_store,
        "_usage_snapshot_observer",
        lambda event, **fields: events.append((event, fields)),
    )

    def bypass_reader_proxy(cur, *_args):
        cur.connection.execute("SELECT pg_sleep(.3)")
        return {}

    monkeypatch.setattr(
        jobs_store, "_usage_parallel_dimension_rows", bypass_reader_proxy
    )
    try:
        report = jobs_store.usage_report_snapshot(query)
        with db.get_pool().connection(timeout=0.1) as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert any(event == "cancel" for event, _fields in events)
    assert report["models"] is None
    # Cancellation follows the shared deadline, so task B may also time out.
    assert report["lanes"] is None


def test_usage_importer_cancel_owns_connection_until_detach():
    control = jobs_store._UsageImporterControl()
    cancel_entered = threading.Event()
    allow_cancel_return = threading.Event()
    detached = threading.Event()

    class Connection:
        def cancel_safe(self, *, timeout):
            assert timeout == 0.25
            cancel_entered.set()
            assert allow_cancel_return.wait(timeout=1)

    conn = Connection()
    control.attach(conn)
    cancel_thread = threading.Thread(target=control.cancel)
    cancel_thread.start()
    assert cancel_entered.wait(timeout=1)

    def detach():
        control.detach(conn)
        detached.set()

    detach_thread = threading.Thread(target=detach)
    detach_thread.start()
    assert not detached.wait(timeout=0.05)
    allow_cancel_return.set()
    cancel_thread.join(timeout=1)
    detach_thread.join(timeout=1)

    assert detached.is_set()
    assert control._conn is None


def test_usage_importer_executor_never_waits_for_unsettled_future():
    calls = []

    class Future:
        def done(self):
            return False

        def cancel(self):
            calls.append("future.cancel")
            return False

        def result(self, *, timeout):
            calls.append(("future.result", timeout))
            raise jobs_store.FutureTimeoutError()

    class Control:
        def cancel(self, *, timeout):
            calls.append(("control.cancel", timeout))

        def close(self):
            calls.append("control.close")

    class Executor:
        def shutdown(self, *, wait, cancel_futures):
            calls.append(("shutdown", wait, cancel_futures))

    wrapper = jobs_store._UsageImporterExecutor.__new__(
        jobs_store._UsageImporterExecutor
    )
    wrapper._executor = Executor()
    wrapper._owned = [(Future(), Control())]

    with pytest.raises(jobs_store._UsageImporterUnsettled):
        wrapper.__exit__(None, None, None)

    assert ("control.cancel", 0.25) in calls
    assert "control.close" in calls
    assert ("shutdown", False, True) in calls


def test_usage_unsettled_importer_never_starts_serial_fallback(
    monkeypatch, usage_rows
):
    query = _shanghai_usage_query()
    _enable_usage_rollup()
    original = jobs_store._usage_parallel_dimension_rows
    calls = 0

    def counted_reader(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    def force_unsettled(*_args, **_kwargs):
        raise jobs_store._UsageImporterUnsettled("forced unsettled")

    monkeypatch.setattr(
        jobs_store, "_usage_parallel_dimension_rows", counted_reader
    )
    monkeypatch.setattr(jobs_store, "_usage_importer_result", force_unsettled)
    try:
        with pytest.raises(
            jobs_store._UsageImporterUnsettled, match="forced unsettled"
        ):
            jobs_store.usage_report_snapshot(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert calls == 1


def _usage_render_report() -> dict:
    return {
        "overview": {
            "registered_accounts": 12,
            "activated_users": 8,
            "model_active_users": 5,
            "metered_users": 4,
            "active_user_days": 7,
            "turns": 11,
            "model_calls": 14,
            "retries": 3,
            "failed_turns": 2,
            "metered_turns": 9,
            "prompt_tokens": 1_200,
            "completion_tokens": 345,
            "total_tokens": 1_545,
            "cache_read_tokens": 600,
            "cache_write_tokens": 40,
            "cache_miss_tokens": 300,
            "unknown_usage_calls": 2,
        },
        "averages": {
            "tokens_per_calendar_day": 51.5,
            "tokens_per_active_user_day": 220.714,
            "tokens_per_activated_user_day": 6.4375,
            "tokens_per_metered_turn": 171.667,
            "user_day_tokens": {
                "p50": 100.0,
                "p75": 180.0,
                "p90": 260.0,
                "p95": 300.0,
                "max": 400.0,
            },
            "model_calls_per_turn": 1.2727,
            "retries_per_turn": 0.2727,
        },
        "daily": [{
            "local_day": "2026-07-31",
            "turns": 11,
            "model_active_users": 5,
            "metered_turns": 9,
            "model_calls": 14,
            "retries": 3,
            "failed_turns": 2,
            "prompt_tokens": 1_200,
            "completion_tokens": 345,
            "total_tokens": 1_545,
            "cache_read_tokens": 600,
            "cache_write_tokens": 40,
            "cache_miss_tokens": 300,
            "unknown_usage_calls": 2,
            "usage_reported_calls": 12,
            "cache_reported_calls": 8,
            "usage_coverage": 12 / 14,
            "cache_coverage": 8 / 14,
            "cache_hit_ratio": 2 / 3,
            "tokens_per_active_user_day": 309.0,
            "tokens_per_metered_turn": 171.667,
        }],
        "users": [{
            "user_id": "usr_0123456789abcdef",
            "last_model_call_at": datetime(2026, 7, 31, 11, tzinfo=timezone.utc),
            "active_days": 2,
            "turns": 6,
            "model_calls": 8,
            "retries": 2,
            "failed_turns": 1,
            "metered_turns": 5,
            "prompt_tokens": 900,
            "completion_tokens": 200,
            "total_tokens": 1_100,
            "cache_read_tokens": 500,
            "cache_write_tokens": 30,
            "cache_miss_tokens": 200,
            "unknown_usage_calls": 1,
            "usage_reported_calls": 7,
            "cache_reported_calls": 5,
            "usage_coverage": 7 / 8,
            "cache_coverage": 5 / 8,
            "cache_hit_ratio": 5 / 7,
            "primary_provider": "provider<&",
            "primary_model": "model<script>",
            "daily_p50": 500.0,
            "daily_p95": 590.0,
            "tokens_per_calendar_day": 36.667,
            "tokens_per_active_day": 550.0,
            "tokens_per_metered_turn": 220.0,
            "known_token_share": 1100 / 1545,
        }],
        "models": [{
            "provider": "provider<&",
            "model": "model<script>",
            "users": 1,
            "turns": 6,
            "model_calls": 8,
            "retries": 2,
            "failed_turns": 1,
            "metered_turns": 5,
            "prompt_tokens": 900,
            "completion_tokens": 200,
            "total_tokens": 1_100,
            "cache_read_tokens": 500,
            "cache_write_tokens": 30,
            "cache_miss_tokens": 200,
            "unknown_usage_calls": 1,
            "usage_reported_calls": 7,
            "cache_reported_calls": 5,
            "usage_coverage": 7 / 8,
            "cache_coverage": 5 / 8,
            "cache_hit_ratio": 5 / 7,
            "tokens_per_call": 137.5,
            "latency_ms_p50": 1_200,
            "latency_ms_p95": 4_500,
            "failure_rate": 1 / 6,
            "retry_rate": 0.25,
        }],
        "lanes": [{
            "lane": "chat",
            "users": 1,
            "turns": 6,
            "model_calls": 8,
            "retries": 2,
            "failed_turns": 1,
            "metered_turns": 5,
            "prompt_tokens": 900,
            "completion_tokens": 200,
            "total_tokens": 1_100,
            "cache_read_tokens": 500,
            "cache_write_tokens": 30,
            "cache_miss_tokens": 200,
            "unknown_usage_calls": 1,
            "usage_reported_calls": 7,
            "cache_reported_calls": 5,
            "usage_coverage": 7 / 8,
            "cache_coverage": 5 / 8,
            "cache_hit_ratio": 5 / 7,
            "tokens_per_call": 137.5,
            "latency_ms_p50": 1_200,
            "latency_ms_p95": 4_500,
            "failure_rate": 1 / 6,
            "retry_rate": 0.25,
        }],
        "filters": {
            "lanes": ["chat", "heartbeat"],
            "providers": ["provider<&", "other"],
            "models": ["model<script>", "other-model"],
        },
        "coverage": {
            "usage_reported_calls": 12,
            "model_calls": 14,
            "usage_coverage": 12 / 14,
            "cache_reported_calls": 8,
            "cache_coverage": 8 / 14,
            "cache_hit_ratio": 2 / 3,
            "reference_cohort": {
                "basis": "parseable_utc_write_timestamps_at_end_at",
                "unparseable_registered_rows": 1,
                "legacy_memory_rows_without_valid_created_at": 2,
                "limitation": "legacy cohort timestamps are incomplete",
            },
        },
    }


def test_usage_query_defaults_to_rolling_30_days_in_shanghai_timezone():
    query = _usage.parse_usage_query({}, now_utc=NOW_UTC)

    assert query.start_at_utc == datetime(2026, 7, 3, 12, 30, tzinfo=timezone.utc)
    assert query.end_at_utc == NOW_UTC
    assert query.timezone == "Asia/Shanghai"
    assert query.preset == "30d"
    assert query.start_date is None
    assert query.end_date is None


@pytest.mark.parametrize(
    ("preset", "expected_start"),
    [
        ("24h", datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)),
        ("7d", datetime(2026, 7, 26, 12, 30, tzinfo=timezone.utc)),
        ("30d", datetime(2026, 7, 3, 12, 30, tzinfo=timezone.utc)),
        ("90d", datetime(2026, 5, 4, 12, 30, tzinfo=timezone.utc)),
    ],
)
def test_usage_query_presets_are_exact_rolling_windows(preset, expected_start):
    query = _usage.parse_usage_query({"preset": preset}, now_utc=NOW_UTC)

    assert query.start_at_utc == expected_start
    assert query.end_at_utc == NOW_UTC
    assert query.preset == preset


def test_usage_query_custom_dates_are_inclusive_local_dates_with_half_open_utc_bounds():
    query = _usage.parse_usage_query(
        {
            "preset": "custom",
            "start_date": "2026-07-01",
            "end_date": "2026-07-02",
            "timezone": "Asia/Shanghai",
        },
        now_utc=NOW_UTC,
    )

    assert query.start_at_utc == datetime(2026, 6, 30, 16, tzinfo=timezone.utc)
    assert query.end_at_utc == datetime(2026, 7, 2, 16, tzinfo=timezone.utc)
    assert query.preset == "custom"
    assert query.start_date == "2026-07-01"
    assert query.end_date == "2026-07-02"


def test_usage_query_accepts_a_366_day_custom_window():
    query = _usage.parse_usage_query(
        {
            "preset": "custom",
            "start_date": "2026-01-01",
            "end_date": "2027-01-01",
            "timezone": "UTC",
        },
        now_utc=NOW_UTC,
    )

    assert query.preset == "custom"
    assert query.start_at_utc == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert query.end_at_utc == datetime(2027, 1, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "custom_args",
    [
        {"start_date": "2026-01-01", "end_date": "2027-01-02"},
        {"start_date": "2026-07-03", "end_date": "2026-07-02"},
        {"start_date": "not-a-date", "end_date": "2026-07-02"},
    ],
)
def test_usage_query_invalid_custom_windows_fall_back_to_30_days(custom_args):
    query = _usage.parse_usage_query(
        {"preset": "custom", "timezone": "UTC", **custom_args},
        now_utc=NOW_UTC,
    )

    assert query.preset == "30d"
    assert query.start_at_utc == datetime(2026, 7, 3, 12, 30, tzinfo=timezone.utc)
    assert query.end_at_utc == NOW_UTC
    assert query.start_date is None
    assert query.end_date is None


def test_usage_query_invalid_timezone_falls_back_before_converting_custom_dates():
    query = _usage.parse_usage_query(
        {
            "preset": "custom",
            "start_date": "2026-07-01",
            "end_date": "2026-07-01",
            "timezone": "Mars/Olympus",
        },
        now_utc=NOW_UTC,
    )

    assert query.timezone == "Asia/Shanghai"
    assert query.start_at_utc == datetime(2026, 6, 30, 16, tzinfo=timezone.utc)
    assert query.end_at_utc == datetime(2026, 7, 1, 16, tzinfo=timezone.utc)


def test_usage_query_custom_end_without_exclusive_successor_falls_back_to_30_days():
    query = _usage.parse_usage_query(
        {
            "preset": "custom",
            "start_date": "9999-12-31",
            "end_date": "9999-12-31",
            "timezone": "UTC",
        },
        now_utc=NOW_UTC,
    )

    assert query.preset == "30d"
    assert query.start_at_utc == datetime(2026, 7, 3, 12, 30, tzinfo=timezone.utc)
    assert query.end_at_utc == NOW_UTC
    assert query.start_date is None
    assert query.end_date is None


def test_usage_query_local_start_before_utc_min_falls_back_to_30_days():
    query = _usage.parse_usage_query(
        {
            "preset": "custom",
            "start_date": "0001-01-01",
            "end_date": "0001-01-01",
            "timezone": "Asia/Shanghai",
        },
        now_utc=NOW_UTC,
    )

    assert query.preset == "30d"
    assert query.start_at_utc == datetime(2026, 7, 3, 12, 30, tzinfo=timezone.utc)
    assert query.end_at_utc == NOW_UTC
    assert query.start_date is None
    assert query.end_date is None


def test_usage_query_skipped_civil_date_falls_back_to_30_days():
    query = _usage.parse_usage_query(
        {
            "preset": "custom",
            "start_date": "2011-12-30",
            "end_date": "2011-12-30",
            "timezone": "Pacific/Apia",
        },
        now_utc=NOW_UTC,
    )

    assert query.preset == "30d"
    assert query.start_at_utc == datetime(2026, 7, 3, 12, 30, tzinfo=timezone.utc)
    assert query.end_at_utc == NOW_UTC
    assert query.start_date is None
    assert query.end_date is None


def test_usage_query_custom_dates_follow_dst_boundaries_in_selected_timezone():
    query = _usage.parse_usage_query(
        {
            "preset": "custom",
            "start_date": "2026-03-07",
            "end_date": "2026-03-08",
            "timezone": "America/New_York",
        },
        now_utc=NOW_UTC,
    )

    assert query.start_at_utc == datetime(2026, 3, 7, 5, tzinfo=timezone.utc)
    assert query.end_at_utc == datetime(2026, 3, 9, 4, tzinfo=timezone.utc)


def test_usage_query_normalizes_optional_filters_and_completeness():
    query = _usage.parse_usage_query(
        {
            "user_id": "  usr_0123456789abcdef  ",
            "lane": " chat ",
            "provider": " OpenRouter ",
            "model": " openai/gpt-4o-mini ",
            "completeness": " METERED ",
        },
        now_utc=NOW_UTC,
    )

    assert query.user_id == "usr_0123456789abcdef"
    assert query.lane == "chat"
    assert query.provider == "OpenRouter"
    assert query.model == "openai/gpt-4o-mini"
    assert query.completeness == "metered"


def test_usage_query_turns_blank_filters_and_invalid_completeness_into_defaults():
    query = _usage.parse_usage_query(
        {
            "user_id": " ",
            "lane": "",
            "provider": "\t",
            "model": "  ",
            "completeness": "partial",
        },
        now_utc=NOW_UTC,
    )

    assert query.user_id is None
    assert query.lane is None
    assert query.provider is None
    assert query.model is None
    assert query.completeness == "all"


def test_usage_page_href_rebuilds_only_the_canonical_normalized_query():
    with reqctx.bind(
        "view=runtime&preset=custom&start_date=2026-07-01&end_date=2026-07-02"
        "&timezone=Mars%2FOlympus&provider=%20OpenRouter%20&completeness=METERED"
        "&offset=200&admin_key=query-admin-token"
    ):
        href = _data_track._usage_page_href(now_utc=NOW_UTC)

    parsed = urlsplit(href)
    assert parsed.path == "/admin/data-track"
    assert parse_qs(parsed.query) == {
        "view": ["usage"],
        "preset": ["custom"],
        "start_date": ["2026-07-01"],
        "end_date": ["2026-07-02"],
        "timezone": ["Asia/Shanghai"],
        "provider": ["OpenRouter"],
        "completeness": ["metered"],
        "admin_key": ["query-admin-token"],
    }


def test_usage_page_href_keeps_query_admin_auth_with_explicit_usage_query():
    query = _usage.parse_usage_query(
        {"preset": "7d", "timezone": "UTC"},
        now_utc=NOW_UTC,
    )

    with reqctx.bind("admin_key=query-admin-token&offset=999"):
        href = _data_track._usage_page_href(query, offset=100)

    assert parse_qs(urlsplit(href).query) == {
        "view": ["usage"],
        "preset": ["7d"],
        "timezone": ["UTC"],
        "completeness": ["all"],
        "offset": ["100"],
        "admin_key": ["query-admin-token"],
    }


def test_usage_page_href_applies_normalized_drill_down_updates():
    query = _usage.parse_usage_query(
        {"preset": "7d", "timezone": "UTC", "lane": "chat"},
        now_utc=NOW_UTC,
    )

    href = _data_track._usage_page_href(
        query,
        user_id="  usr_0123456789abcdef  ",
        lane=None,
        unrelated="must-not-survive",
    )

    assert parse_qs(urlsplit(href).query) == {
        "view": ["usage"],
        "preset": ["7d"],
        "timezone": ["UTC"],
        "user_id": ["usr_0123456789abcdef"],
        "completeness": ["all"],
    }


def test_usage_page_href_keeps_query_while_adding_normalized_pagination():
    query = _usage.parse_usage_query(
        {"preset": "90d", "provider": "openrouter"},
        now_utc=NOW_UTC,
    )

    href = _data_track._usage_page_href(
        query,
        offset="100",
        sort="calls",
        dir="asc",
    )

    assert parse_qs(urlsplit(href).query) == {
        "view": ["usage"],
        "preset": ["90d"],
        "timezone": ["Asia/Shanghai"],
        "provider": ["openrouter"],
        "completeness": ["all"],
        "offset": ["100"],
        "sort": ["calls"],
        "dir": ["asc"],
    }


def test_usage_page_renders_independent_report_filters_and_drill_down(monkeypatch):
    """A missing Usage dispatch/render path must fail this operator contract."""
    seen = []

    def _report(query):
        seen.append(query)
        return _usage_render_report()

    monkeypatch.setattr(_data_track, "_usage_report", _report)

    body = _admin_core.page_html(
        "view=usage&preset=custom&start_date=2026-07-01&end_date=2026-07-31"
        "&timezone=UTC&lane=chat&provider=provider%3C%26&model=model%3Cscript%3E"
        "&completeness=metered&admin_key=query-admin-token"
    )

    assert len(seen) == 1
    query = seen[0]
    assert query.start_at_utc == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert query.end_at_utc == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert query.timezone == "UTC"
    assert query.lane == "chat"
    assert query.provider == "provider<&"
    assert query.model == "model<script>"
    assert query.completeness == "metered"

    for label in (
        "Usage / 模型用量",
        "Fleet Overview",
        "平均值",
        "每日趋势",
        "Per-user Usage",
        "Provider / Model",
        "数据覆盖与边界",
    ):
        assert label in body
    for value in ("1,545", "85.7%", "2026-07-31", "usr_0123456789abcdef"):
        assert value in body
    assert "24h" in body and "7d" in body and "30d" in body and "90d" in body
    assert "name='start_date'" in body and "name='end_date'" in body
    assert "name='lane'" in body and "name='provider'" in body
    assert "name='model'" in body and "name='completeness'" in body
    assert "unavailable until P0-B" in body
    assert "unavailable until self-host phase" in body
    assert "provider<&" not in body
    assert "model<script>" not in body
    assert "provider&lt;&amp;" in body
    assert "model&lt;script&gt;" in body
    assert (
        "/admin/data-track/users/usr_0123456789abcdef?"
        in body
    )
    assert "admin_key=query-admin-token" in body


def test_usage_filter_form_preserves_rolling_preset_when_adding_provider(monkeypatch):
    """Submitting a provider filter must not turn a rolling 7d cohort into custom."""
    seen = []
    monkeypatch.setattr(
        _data_track,
        "_usage_report",
        lambda query: seen.append(query) or _usage_render_report(),
    )

    body = _admin_core.page_html("view=usage&preset=7d&timezone=UTC")

    form_start = body.index("<form class='usage-filters'")
    form_end = body.index("</form>", form_start)
    form = body[form_start:form_end]
    assert "action='/admin/data-track'" in form
    assert "<select name='preset'>" in form
    assert "<option value='7d' selected>7d</option>" in form
    assert "type='hidden' name='preset' value='custom'" not in form
    assert "<label>User ID<input name='user_id'" in form
    assert "type='hidden' name='user_id'" not in form

    _admin_core.page_html(
        "view=usage&preset=7d&timezone=UTC&provider=other"
        "&start_date=2020-01-01&end_date=2020-01-02"
    )
    assert seen[-1].preset == "7d"
    assert seen[-1].provider == "other"
    assert seen[-1].start_date is None
    assert seen[-1].end_date is None


def test_usage_completeness_filter_marks_activated_average_not_applicable(monkeypatch):
    """Unknown-only usage cannot use the fleet-wide activated-user denominator."""
    report = _usage_render_report()
    report["averages"]["tokens_per_activated_user_day"] = None
    monkeypatch.setattr(_data_track, "_usage_report", lambda _query: report)

    body = _admin_core.page_html("view=usage&preset=7d&completeness=unknown")

    assert "not applicable for filtered cohort" in body


def test_usage_user_sort_and_101_row_pagination_are_deterministic(monkeypatch):
    """Equal metrics keep user_id order and page 101 must remain reachable."""
    report = _usage_render_report()
    template = report["users"][0]
    report["users"] = [
        {
            **template,
            "user_id": f"usr_{index:016x}",
            "model_calls": 5,
            "total_tokens": 100,
        }
        for index in range(101)
    ]
    monkeypatch.setattr(_data_track, "_usage_report", lambda _query: report)

    first = _admin_core.page_html(
        "view=usage&preset=7d&sort=calls&dir=desc&offset=0"
    )
    table_start = first.index("<h2>Per-user Usage</h2>")
    table_end = first.index("</table>", table_start)
    first_table = first[table_start:table_end]
    assert first_table.index("usr_0000000000000000") < first_table.index(
        "usr_0000000000000001"
    )
    assert "usr_0000000000000063" in first_table
    assert "usr_0000000000000064" not in first_table
    assert "offset=100" in first[table_start:table_end]

    second = _admin_core.page_html(
        "view=usage&preset=7d&sort=calls&dir=desc&offset=100"
    )
    second_start = second.index("<h2>Per-user Usage</h2>")
    second_end = second.index("</table>", second_start)
    second_table = second[second_start:second_end]
    assert "usr_0000000000000064" in second_table
    assert "usr_0000000000000063" not in second_table
    assert ">Prev</a>" in second[second_start:second_end]


def test_usage_user_page_forces_path_user_and_keeps_same_query(monkeypatch):
    """The drill-down path cannot silently fall back to the general user page."""
    user_id = "usr_0123456789abcdef"
    seen = []
    monkeypatch.setattr(
        registry,
        "_users",
        [{"user_id": user_id}],
    )

    def _report(query):
        seen.append(query)
        return _usage_render_report()

    monkeypatch.setattr(_data_track, "_usage_report", _report)

    kind, body, status = _admin_core.user_page(
        "view=usage&preset=7d&timezone=UTC&lane=chat&user_id=usr_ffffffffffffffff",
        user_id,
    )

    assert (kind, status) == ("html", 200)
    assert seen[0].user_id == user_id
    assert seen[0].lane == "chat"
    assert "Usage / 模型用量" in body
    assert "单用户钻取" in body
    assert "Lane breakdown" in body
    assert "chat" in body
    assert user_id in body
    assert "usr_ffffffffffffffff" not in body


def test_usage_user_page_keeps_drilldown_path_in_presets_filters_and_sorts(
    monkeypatch,
):
    """Usage controls must not navigate a drill-down back to the fleet page."""
    user_id = "usr_0123456789abcdef"
    monkeypatch.setattr(registry, "_users", [{"user_id": user_id}])
    monkeypatch.setattr(
        _data_track,
        "_usage_report",
        lambda _query: _usage_render_report(),
    )

    kind, body, status = _admin_core.user_page(
        "view=usage&preset=7d&timezone=UTC&lane=chat"
        "&sort=calls&dir=desc&admin_key=query-admin-token",
        user_id,
    )

    assert (kind, status) == ("html", 200)
    expected_path = f"/admin/data-track/users/{user_id}"

    nav_start = body.index("<div class='viewbar'>")
    nav_end = body.index("</div>", nav_start)
    usage_nav = re.search(
        r"href='([^']+)'[^>]*>Usage / 模型用量</a>",
        body[nav_start:nav_end],
    )
    assert usage_nav is not None
    assert urlsplit(unescape(usage_nav.group(1))).path == expected_path

    preset_start = body.index("<div class='sortbar'>")
    preset_end = body.index("</div>", preset_start)
    preset_hrefs = [
        unescape(href)
        for href in re.findall(r"href='([^']+)'", body[preset_start:preset_end])
    ]
    assert len(preset_hrefs) == 4

    form_start = body.index("<form class='usage-filters'")
    form_end = body.index("</form>", form_start)
    form = body[form_start:form_end]
    assert f"action='{expected_path}'" in form
    assert f"<input type='hidden' name='user_id' value='{user_id}'>" in form
    assert "<label>User ID<input name='user_id'" not in form

    user_section = body.index("<h2>Per-user Usage</h2>")
    user_sortbar_end = body.index("</div>", user_section)
    sort_hrefs = [
        unescape(href)
        for href in re.findall(
            r"href='([^']+)'", body[user_section:user_sortbar_end]
        )
    ]
    assert len(sort_hrefs) == 4

    for href in preset_hrefs + sort_hrefs:
        parsed = urlsplit(href)
        params = parse_qs(parsed.query)
        assert parsed.path == expected_path
        assert params["user_id"] == [user_id]
        assert params["admin_key"] == ["query-admin-token"]
    assert "Lane breakdown" in body


def test_usage_user_page_pager_keeps_drilldown_path_and_lane_table(monkeypatch):
    """Paging within a drill-down must retain both its route and lane context."""
    user_id = "usr_0123456789abcdef"
    report = _usage_render_report()
    template = report["users"][0]
    report["users"] = [
        {
            **template,
            "user_id": f"usr_{index:016x}",
            "model_calls": 5,
            "total_tokens": 100,
        }
        for index in range(101)
    ]
    monkeypatch.setattr(registry, "_users", [{"user_id": user_id}])
    monkeypatch.setattr(_data_track, "_usage_report", lambda _query: report)
    expected_path = f"/admin/data-track/users/{user_id}"

    for offset, label in ((0, "Next"), (100, "Prev")):
        kind, body, status = _admin_core.user_page(
            f"view=usage&preset=7d&sort=calls&dir=desc&offset={offset}"
            "&admin_key=query-admin-token",
            user_id,
        )

        assert (kind, status) == ("html", 200)
        user_section = body.index("<h2>Per-user Usage</h2>")
        user_sortbar_end = body.index("</div>", user_section)
        sortbar = body[user_section:user_sortbar_end]
        match = re.search(rf"href='([^']+)'>{label}</a>", sortbar)
        assert match is not None
        pager_href = unescape(match.group(1))
        parsed = urlsplit(pager_href)
        params = parse_qs(parsed.query)
        assert parsed.path == expected_path
        assert params["user_id"] == [user_id]
        assert params["admin_key"] == ["query-admin-token"]
        assert "Lane breakdown" in body


def test_usage_user_pagination_clamps_large_offset_to_last_page(monkeypatch):
    report = _usage_render_report()
    template = report["users"][0]
    report["users"] = [
        {**template, "user_id": f"usr_{index:016x}", "total_tokens": 100}
        for index in range(101)
    ]
    monkeypatch.setattr(_data_track, "_usage_report", lambda _query: report)

    body = _admin_core.page_html("view=usage&preset=7d&offset=9999")

    assert "Showing 101–101 of 101" in body
    assert "usr_0000000000000064" in body
    assert "Showing 0–" not in body


def test_usage_user_pagination_clamps_empty_report_offset_to_zero(monkeypatch):
    report = _usage_render_report()
    report["users"] = []
    monkeypatch.setattr(_data_track, "_usage_report", lambda _query: report)

    body = _admin_core.page_html("view=usage&preset=7d&offset=9999")

    assert "Showing 0–0 of 0" in body


def test_usage_query_failure_is_local_and_runtime_remains_available(monkeypatch):
    """Usage/RDS failure must not make the runtime incident console unusable."""
    def _boom(_query):
        raise RuntimeError("private usage database detail")

    monkeypatch.setattr(_data_track, "_usage_report", _boom)
    monkeypatch.setattr(
        _data_track,
        "_runtime_health_summary",
        lambda **_kw: {
            "window_hours": 24,
            "generated_at": 0,
            "lanes": [],
            "pool": {
                "inflight": 0,
                "pending": 0,
                "live_workers": 1,
                "capacity": 1,
                "oldest_pending_age_sec": None,
            },
        },
    )
    monkeypatch.setattr(
        _data_track, "_runtime_token_by_lane", lambda **_kw: {"lanes": {}}
    )
    monkeypatch.setattr(_data_track, "_runtime_delivery_health", lambda **_kw: {})
    monkeypatch.setattr(
        _data_track,
        "_runtime_user_report",
        lambda **_kw: {"window_hours": 24, "users": []},
    )

    usage_body = _admin_core.page_html("view=usage&preset=30d")
    runtime_body = _admin_core.page_html("view=runtime&hours=24")

    assert "Usage 数据暂时取不到" in usage_body
    assert "private usage database detail" not in usage_body
    assert "Runtime 健康" in runtime_body
    assert "各 lane 健康" in runtime_body


def test_usage_report_is_wired_to_jobs_store():
    """Assembly must replace the content-free stub with the real snapshot."""
    import asgi_app  # noqa: F401

    assert _data_track._usage_report is jobs_store.usage_report_snapshot


def test_usage_snapshot_reconciles_users_days_models_failures_and_unknowns(usage_rows):
    report = jobs_store.usage_report_snapshot(_usage_query())

    assert set(report) == {
        "overview", "averages", "daily", "users", "models", "lanes",
        "filters", "coverage", "attempts"
    }
    assert report["overview"] == {
        "registered_accounts": 3,
        "activated_users": 3,
        "model_active_users": 2,
        "metered_users": 2,
        "active_user_days": 4,
        "turns": 5,
        "model_calls": 6,
        "retries": 2,
        "failed_turns": 2,
        "metered_turns": 3,
        "prompt_tokens": 180,
        "completion_tokens": 35,
        "total_tokens": 215,
        "cache_read_tokens": 50,
        "cache_write_tokens": 5,
        "cache_miss_tokens": 60,
        "unknown_usage_calls": 2,
    }
    averages = report["averages"]
    assert averages["tokens_per_calendar_day"] == pytest.approx(107.5)
    assert averages["tokens_per_active_user_day"] == pytest.approx(53.75)
    assert averages["tokens_per_activated_user_day"] == pytest.approx(215 / 6)
    assert averages["tokens_per_metered_turn"] == pytest.approx(215 / 3)
    assert averages["model_calls_per_turn"] == pytest.approx(1.2)
    assert averages["retries_per_turn"] == pytest.approx(0.4)
    assert averages["user_day_tokens"] == pytest.approx(
        {"p50": 60, "p75": 90, "p90": 108, "p95": 114, "max": 120}
    )

    assert [row["local_day"] for row in report["daily"]] == [
        "2026-07-01", "2026-07-02"
    ]
    first, second = report["daily"]
    assert first["total_tokens"] == 120
    assert first["model_active_users"] == 2
    assert first["tokens_per_active_user_day"] == pytest.approx(60)
    assert first["model_calls"] == 3
    assert first["failed_turns"] == 1
    assert first["unknown_usage_calls"] == 1
    assert second["total_tokens"] == 95
    assert second["model_calls"] == 3
    assert second["retries"] == 1

    users = {row["user_id"]: row for row in report["users"]}
    assert users["u_usage_alpha"]["total_tokens"] == 180
    assert users["u_usage_alpha"]["active_days"] == 2
    assert users["u_usage_alpha"]["primary_provider"] == "openrouter"
    assert users["u_usage_alpha"]["primary_model"] == "gpt-a"
    assert users["u_usage_beta"]["total_tokens"] == 35
    assert users["u_usage_beta"]["unknown_usage_calls"] == 2
    assert users["u_usage_idle"]["total_tokens"] is None

    models = {(row["provider"], row["model"]): row for row in report["models"]}
    assert models[("openrouter", "gpt-a")]["total_tokens"] == 155
    assert models[("anthropic", "claude-b")]["failed_turns"] == 1
    assert models[("unknown", "unknown")]["model_calls"] == 0

    lanes = {row["lane"]: row for row in report["lanes"]}
    assert lanes["chat"]["users"] == 2
    assert lanes["chat"]["model_calls"] == 5
    assert lanes["chat"]["total_tokens"] == 155
    assert lanes["chat"]["usage_coverage"] == pytest.approx(3 / 5)

    assert report["filters"] == {
        "lanes": ["chat", "heartbeat", "maintenance"],
        "providers": ["anthropic", "openrouter", "unknown"],
        "models": ["claude-b", "gpt-a", "unknown"],
    }
    assert report["coverage"]["usage_reported_calls"] == 4
    assert report["coverage"]["usage_coverage"] == pytest.approx(4 / 6)
    assert report["coverage"]["cache_reported_calls"] == 3
    assert report["coverage"]["cache_coverage"] == pytest.approx(0.5)
    assert report["coverage"]["cache_hit_ratio"] == pytest.approx(50 / 110)


def test_usage_snapshot_filters_every_usage_section_but_not_reference_cohorts(usage_rows):
    report = jobs_store.usage_report_snapshot(
        _usage_query(lane="chat", provider="openrouter", model="gpt-a", completeness="unknown")
    )

    assert report["overview"]["registered_accounts"] == 3
    assert report["overview"]["activated_users"] == 3
    assert report["overview"]["turns"] == 2
    assert report["overview"]["model_calls"] == 3
    assert report["overview"]["total_tokens"] == 35
    assert report["overview"]["unknown_usage_calls"] == 2
    assert report["averages"]["tokens_per_activated_user_day"] is None
    assert {row["user_id"] for row in report["users"]} == {"u_usage_beta"}
    assert {(row["provider"], row["model"]) for row in report["models"]} == {
        ("openrouter", "gpt-a")
    }
    assert sum(row["turns"] for row in report["daily"]) == 2


def test_usage_snapshot_preserves_unknown_token_and_cache_totals(usage_rows):
    report = jobs_store.usage_report_snapshot(
        _usage_query(user_id="u_usage_beta", completeness="unknown")
    )
    first = report["daily"][0]

    assert first["prompt_tokens"] is None
    assert first["completion_tokens"] is None
    assert first["total_tokens"] is None
    assert first["cache_read_tokens"] is None
    assert first["cache_hit_ratio"] is None
    assert first["usage_coverage"] == pytest.approx(0.0)


def test_usage_snapshot_metered_turn_average_excludes_legacy_unreported_tokens(
    usage_rows,
):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_turn_metrics "
            "(user_id,lane,provider,model,prompt_tokens,completion_tokens,"
            "usage_reported_calls,cache_reported_calls,model_calls,retries,failed,"
            "status,created_at) VALUES (%s,%s,%s,%s,%s,%s,0,0,1,0,false,%s,%s)",
            (
                "u_usage_beta",
                "chat",
                "openrouter",
                "legacy-model",
                700,
                70,
                "legacy-inconsistent-usage",
                "2026-07-01T13:00:00+00:00",
            ),
        )

    report = jobs_store.usage_report_snapshot(_usage_query())
    beta = next(row for row in report["users"] if row["user_id"] == "u_usage_beta")

    assert report["overview"]["total_tokens"] == 985
    assert report["averages"]["tokens_per_metered_turn"] == pytest.approx(215 / 3)
    assert beta["total_tokens"] == 805
    assert beta["tokens_per_metered_turn"] == pytest.approx(35)


def test_usage_snapshot_distinguishes_empty_days_from_unreported_metric_days(
    usage_rows,
):
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_turn_metrics SET created_at=%s "
            "WHERE user_id=%s AND status=%s",
            (
                "2026-07-03T11:00:00+00:00",
                "u_usage_beta",
                "usage-3",
            ),
        )

    report = jobs_store.usage_report_snapshot(
        _usage.UsageQuery(
            start_at_utc=datetime(2026, 7, 1, tzinfo=timezone.utc),
            end_at_utc=datetime(2026, 7, 4, tzinfo=timezone.utc),
            timezone="UTC",
            user_id="u_usage_beta",
        )
    )
    first, empty, third = report["daily"]

    assert first["local_day"] == "2026-07-01"
    assert first["turns"] == 1
    assert first["total_tokens"] is None
    assert first["cache_read_tokens"] is None
    assert empty["local_day"] == "2026-07-02"
    assert empty["turns"] == 0
    assert empty["prompt_tokens"] == 0
    assert empty["completion_tokens"] == 0
    assert empty["total_tokens"] == 0
    assert empty["cache_read_tokens"] == 0
    assert empty["cache_write_tokens"] == 0
    assert empty["cache_miss_tokens"] == 0
    assert third["local_day"] == "2026-07-03"
    assert third["total_tokens"] == 35


def test_usage_snapshot_uses_one_repeatable_read_only_connection(monkeypatch, usage_rows):
    real_pool = db.get_pool()
    settings = []
    connection_calls = 0

    class CursorProxy:
        def __init__(self, cursor):
            self._cursor = cursor

        def __getattr__(self, name):
            return getattr(self._cursor, name)

        def execute(self, query, params=None):
            result = self._cursor.execute(query, params) if params is not None else self._cursor.execute(query)
            if str(query).startswith("SET TRANSACTION"):
                settings.append(
                    self._cursor.connection.execute(
                        "SELECT current_setting('transaction_isolation'),"
                        "current_setting('transaction_read_only')"
                    ).fetchone()
                )
            return result

    class CursorContext:
        def __init__(self, context):
            self._context = context

        def __enter__(self):
            return CursorProxy(self._context.__enter__())

        def __exit__(self, *args):
            return self._context.__exit__(*args)

    class ConnectionProxy:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def cursor(self, *args, **kwargs):
            return CursorContext(self._connection.cursor(*args, **kwargs))

    class PoolProxy:
        def connection(self):
            nonlocal connection_calls
            connection_calls += 1
            context = real_pool.connection()

            class ConnectionContext:
                def __enter__(self):
                    return ConnectionProxy(context.__enter__())

                def __exit__(self, *args):
                    return context.__exit__(*args)

            return ConnectionContext()

    monkeypatch.setattr(jobs_store, "_pool", lambda: PoolProxy())

    jobs_store.usage_report_snapshot(_usage_query())

    assert connection_calls == 1
    assert settings == [("repeatable read", "on")]


def test_usage_snapshot_model_sql_failure_only_degrades_models(
    monkeypatch, usage_rows
):
    """A PostgreSQL error in one breakdown must roll back to its savepoint."""
    real_pool = db.get_pool()
    injected = False

    class CursorProxy:
        def __init__(self, cursor):
            self._cursor = cursor

        def __getattr__(self, name):
            return getattr(self._cursor, name)

        def execute(self, query, params=None):
            nonlocal injected
            text = str(query)
            if not injected and "FROM base GROUP BY provider,model" in text:
                injected = True
                return self._cursor.execute(
                    "SELECT missing_usage_breakdown_column"
                )
            if params is None:
                return self._cursor.execute(query)
            return self._cursor.execute(query, params)

    class CursorContext:
        def __init__(self, context):
            self._context = context

        def __enter__(self):
            return CursorProxy(self._context.__enter__())

        def __exit__(self, *args):
            return self._context.__exit__(*args)

    class ConnectionProxy:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def cursor(self, *args, **kwargs):
            return CursorContext(self._connection.cursor(*args, **kwargs))

    class ConnectionContext:
        def __init__(self, context):
            self._context = context

        def __enter__(self):
            return ConnectionProxy(self._context.__enter__())

        def __exit__(self, *args):
            return self._context.__exit__(*args)

    class PoolProxy:
        def connection(self):
            return ConnectionContext(real_pool.connection())

    monkeypatch.setattr(jobs_store, "_pool", lambda: PoolProxy())

    report = jobs_store.usage_report_snapshot(_usage_query())

    assert injected is True
    assert report["models"] is None
    assert report["overview"]["total_tokens"] == 215
    assert report["daily"] is not None
    assert report["users"] is not None
    assert report["lanes"] is not None
    with _admin_core.bind("view=usage&preset=30d"):
        body = _data_track._render_usage_page(report, _usage_query())
    assert "Provider / Model 暂时取不到" in body
    assert "2026-07-01" in body
    assert "u_usage_alpha" in body


def test_usage_snapshot_marks_unparseable_historical_cohort_rows_as_limited(usage_rows):
    seed_user("u_usage_bad_time", created_at="2026-99-99T99:99:99")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO memory_moments (user_id,moment_id,occurred_at,doc) "
            "VALUES (%s,%s,%s,%s)",
            (
                "u_usage_bad_time",
                "bad-time-memory",
                "2026-06-20T00:00:00+00:00",
                Jsonb({}),
            ),
        )
    try:
        report = jobs_store.usage_report_snapshot(_usage_query())
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", ("u_usage_bad_time",))

    cohort = report["coverage"]["reference_cohort"]
    assert cohort["basis"] == "parseable_utc_write_timestamps_at_end_at"
    assert cohort["unparseable_registered_rows"] == 1
    assert cohort["legacy_memory_rows_without_valid_created_at"] == 1
    assert "memory doc.created_at" in cohort["limitation"]


def test_usage_snapshot_treats_naive_registration_time_as_utc_in_every_session_timezone(
    monkeypatch,
):
    user_id = "u_usage_naive_registration"
    seed_user(user_id, created_at="2026-07-03T07:00:00")
    query = _usage.UsageQuery(
        start_at_utc=datetime(2026, 7, 2, 4, tzinfo=timezone.utc),
        end_at_utc=datetime(2026, 7, 3, 4, tzinfo=timezone.utc),
        timezone="UTC",
        user_id=user_id,
    )
    real_pool = db.get_pool()

    try:
        with real_pool.connection() as held_connection:
            class HeldConnectionContext:
                def __enter__(self):
                    return held_connection

                def __exit__(self, *_args):
                    return False

            class HeldPool:
                def connection(self):
                    return HeldConnectionContext()

            monkeypatch.setattr(jobs_store, "_pool", lambda: HeldPool())
            held_connection.execute("SET TIME ZONE 'UTC'")
            utc_report = jobs_store.usage_report_snapshot(query)
            held_connection.execute("SET TIME ZONE 'Asia/Shanghai'")
            shanghai_report = jobs_store.usage_report_snapshot(query)
            held_connection.execute("RESET TIME ZONE")
    finally:
        with real_pool.connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))

    assert utc_report["overview"]["registered_accounts"] == 0
    assert shanghai_report["overview"]["registered_accounts"] == 0


def test_usage_snapshot_activates_legacy_human_role_only_before_end_at():
    user_ids = ["u_usage_human_before", "u_usage_human_after"]
    for user_id in user_ids:
        seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    with db.get_pool().connection() as conn:
        for user_id, sent_at in (
            ("u_usage_human_before", datetime(2026, 7, 2, tzinfo=timezone.utc)),
            ("u_usage_human_after", datetime(2026, 7, 4, tzinfo=timezone.utc)),
        ):
            conn.execute(
                "INSERT INTO chat_messages (user_id,msg_id,ts,doc) "
                "VALUES (%s,%s,%s,%s)",
                (
                    user_id,
                    f"{user_id}-message",
                    sent_at.timestamp(),
                    Jsonb({"role": "human", "source": "chat"}),
                ),
            )
    try:
        report = jobs_store.usage_report_snapshot(_usage_query())
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id = ANY(%s)", (user_ids,))

    assert report["overview"]["registered_accounts"] == 2
    assert report["overview"]["activated_users"] == 1
