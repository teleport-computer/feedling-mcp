#!/usr/bin/env python3
"""Non-persistent feasibility probe for narrow daily call dimensions."""

from __future__ import annotations

import argparse
from datetime import datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import statistics
import sys
import time
from typing import Any

import psycopg

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.perf.admin_usage_ranked_flags_temp_probe import (  # noqa: E402
    DEFAULT_PREFIX,
    DIAGNOSTIC_TIMEOUT_MS,
    FORMAL_END_UTC,
    FORMAL_START_UTC,
    FLAG_COLUMN_NAMES,
    MAINTENANCE_TIMEOUT_MS,
    P95_BUDGET_MS,
    SHANGHAI,
    _TOKEN_FIELDS,
    _attempt_scope_select,
    _canonical_rows,
    _explain_candidate,
    _gap_ctes,
    _percentile_nearest_rank,
    _persistent_snapshot,
    _ranked_rows_and_outputs,
    _raw_ranked_insert_sql,
    _rows_witness,
    _set_statement_timeout,
    _ttft_scope_select,
    _write_json_atomic,
)


NARROW_IDENTITY_COLUMNS = (
    "local_day",
    "user_id",
    "cohort_lane",
    "requested_provider",
    "requested_model",
    "resolved_provider",
    "resolved_model",
    "effective_usage_known",
)
MAX_NARROW_TOTAL_BYTES = 700_000_000
MAX_MEMBERSHIP_RATIO = Decimal("0.25")
_RELATION = re.compile(r"^[a-z_][a-z0-9_]*$")

__all__ = (
    "FLAG_COLUMN_NAMES",
    "NARROW_IDENTITY_COLUMNS",
    "_candidate_query",
    "_narrow_day_insert_sql",
    "_narrow_dimension_select",
    "_narrow_raw_insert_sql",
    "_narrow_storage_passed",
    "_narrow_table_ddl",
    "_probe_passed",
    "_run_probe",
    "_ranked_rows_and_outputs",
    "_select_shape",
    "_shape_eligible",
    "_shape_order",
)

_FACT_SOURCE_COLUMNS = (
    "user_id",
    "cohort_lane",
    "requested_provider",
    "requested_model",
    "resolved_provider",
    "resolved_model",
    "effective_usage_known",
    "cost_kind",
    "currency",
    "attempts",
    "retry_attempts",
    "failover_attempts",
    "failed_attempts",
    "possibly_billed_attempts",
    *(item for field in _TOKEN_FIELDS for item in (f"{field}_sum", f"{field}_known_count")),
    "authoritative_cost_attempts",
    "estimated_cost_attempts",
    "unknown_cost_attempts",
    "cost_amount",
    "ttft_samples",
)
_SCOPE_KEYS = (
    ("overview", (), "cohort"),
    ("user", (("user_id", "user_id"),), "cohort"),
    ("lane", (("lane", "cohort_lane"),), "cohort"),
    (
        "requested_model",
        (("provider", "requested_provider"), ("model", "requested_model")),
        "requested",
    ),
    (
        "resolved_model",
        (("provider", "resolved_provider"), ("model", "resolved_model")),
        "resolved",
    ),
)
_GROUPING_FIELDS = (
    "user_id",
    "cohort_lane",
    "requested_provider",
    "requested_model",
    "resolved_provider",
    "resolved_model",
)
_GROUPING_SETS = (
    "()",
    "(user_id)",
    "(cohort_lane)",
    "(requested_provider,requested_model)",
    "(resolved_provider,resolved_model)",
)
_GROUPING_SCOPE = {63: "overview", 31: "user", 47: "lane", 51: "requested_model", 60: "resolved_model"}


def _probe_passed(evidence: dict) -> bool:
    temp = evidence.get("temp_relation", {})
    build = evidence.get("build", {})
    raw = evidence.get("raw_edge", {})
    shapes = evidence.get("shapes", {})
    selected = evidence.get("selected_shape")
    return bool(
        temp.get("rows") == 731_199
        and _narrow_storage_passed(
            temp,
            membership_total_bytes=int(temp.get("membership_total_bytes", 0)),
        )
        and build.get("days") == 366
        and float(build.get("max_ms", 120_000.0)) < 120_000.0
        and raw.get("attempts") == 8_208
        and raw.get("logical_calls") == 8_208
        and evidence.get("adversarial", {}).get("exact") is True
        and set(shapes) == {"bounded_unions", "grouping_sets"}
        and selected == _select_shape(shapes)
        and selected is not None
        and evidence.get("plan_guards", {}).get("forbidden_paths_absent") is True
        and evidence.get("persistent_state", {}).get("unchanged") is True
        and evidence.get("session_close", {}).get("persistent_probe_objects") == []
    )


def _shape_order(cohort: str) -> tuple[str, str]:
    if cohort == "unfiltered":
        return "bounded_unions", "grouping_sets"
    if cohort == "provider_model_filtered":
        return "grouping_sets", "bounded_unions"
    raise ValueError(f"unknown formal cohort: {cohort!r}")


def _shape_maximum(shape_evidence: dict) -> float:
    return max(
        float(sample["elapsed_ms"])
        for cohort in shape_evidence["cohorts"].values()
        for sample in cohort["samples"].values()
    )


def _shape_eligible(shape_evidence: dict) -> bool:
    cohorts = shape_evidence.get("cohorts", {})
    if set(cohorts) != {"unfiltered", "provider_model_filtered"}:
        return False
    return all(
        cohort.get("exact") is True
        and set(cohort.get("samples", {}))
        == {"first_execution_after_build_analyze", "warm", "ordinary"}
        and all(
            float(sample.get("elapsed_ms", 3_000.0)) < 3_000.0
            and sample.get("temp_read_blocks") == 0
            and sample.get("temp_written_blocks") == 0
            for sample in cohort["samples"].values()
        )
        for cohort in cohorts.values()
    )


def _select_shape(shapes: dict[str, dict]) -> str | None:
    eligible = tuple(
        name for name, evidence in shapes.items() if _shape_eligible(evidence)
    )
    if not eligible:
        return None
    return min(eligible, key=lambda name: (_shape_maximum(shapes[name]), name))


def _selected_where(*, selector: str, completeness: str) -> str:
    if selector not in {"all", "provider", "model", "provider_model"}:
        raise ValueError(f"unknown selector: {selector!r}")
    if completeness not in {"all", "effective"}:
        raise ValueError(f"unknown completeness: {completeness!r}")
    clauses = ["TRUE"]
    if completeness == "effective":
        clauses.append("effective_usage_known=%s")
    if selector in {"provider", "provider_model"}:
        clauses.append("resolved_provider=%s")
    if selector in {"model", "provider_model"}:
        clauses.append("resolved_model=%s")
    return " AND ".join(clauses)


def _call_scope_select(
    *,
    scope: str,
    keys: tuple[tuple[str, str], ...],
    logical_column: str,
) -> str:
    expressions = dict(keys)
    groups = tuple(expressions.values())
    group_by = " GROUP BY " + ",".join(groups) if groups else ""
    return (
        f"SELECT '{scope}'::text AS scope,"
        + ",".join(
            f"{expressions.get(name, 'NULL::text')} AS {name}"
            for name in ("user_id", "lane", "provider", "model")
        )
        + f",sum({logical_column})::bigint AS logical_calls "
        "FROM selected_call_dimensions"
        + group_by
    )


def _grouping_scope_projection() -> str:
    mask = "grouping_mask"
    scope = "CASE " + mask + " " + " ".join(
        f"WHEN {value} THEN '{name}'::text"
        for value, name in _GROUPING_SCOPE.items()
    ) + " END AS scope"
    return ",".join(
        (
            scope,
            f"CASE WHEN {mask}=31 THEN user_id END AS user_id",
            f"CASE WHEN {mask}=47 THEN cohort_lane END AS lane",
            f"CASE WHEN {mask}=51 THEN requested_provider "
            f"WHEN {mask}=60 THEN resolved_provider END AS provider",
            f"CASE WHEN {mask}=51 THEN requested_model "
            f"WHEN {mask}=60 THEN resolved_model END AS model",
        )
    )


def _fact_aggregate_outputs() -> str:
    token_outputs = ",".join(
        f"CASE WHEN sum({field}_known_count)>0 THEN "
        f"sum({field}_sum)::bigint END AS {field}"
        for field in _TOKEN_FIELDS
    )
    return (
        "sum(attempts)::bigint AS attempts,"
        "sum(retry_attempts)::bigint AS retry_attempts,"
        "sum(failover_attempts)::bigint AS failover_attempts,"
        "sum(failed_attempts)::bigint AS failed_attempts,"
        "coalesce(sum(attempts) FILTER (WHERE effective_usage_known),0)::bigint "
        "AS usage_known_attempts,"
        "coalesce(sum(attempts) FILTER (WHERE NOT effective_usage_known),0)::bigint "
        "AS usage_unknown_attempts,"
        "sum(possibly_billed_attempts)::bigint AS possibly_billed_attempts,"
        + token_outputs
    )


def _grouping_sets_ctes(
    *, cohort_column: str, requested_column: str, resolved_column: str
) -> tuple[str, str, str]:
    grouping = "grouping(" + ",".join(_GROUPING_FIELDS) + ")"
    fields = ",".join(_GROUPING_FIELDS)
    sets = ",".join(_GROUPING_SETS)
    projection = _grouping_scope_projection()
    fact = (
        "fact_grouped AS MATERIALIZED (SELECT "
        + grouping
        + " AS grouping_mask,"
        + fields
        + ","
        + _fact_aggregate_outputs()
        + " FROM selected_dimensions GROUP BY GROUPING SETS ("
        + sets
        + ")),attempt_scope_rows AS MATERIALIZED (SELECT "
        + projection
        + ",attempts,retry_attempts,failover_attempts,failed_attempts,"
        "usage_known_attempts,usage_unknown_attempts,possibly_billed_attempts,"
        + ",".join(_TOKEN_FIELDS)
        + " FROM fact_grouped)"
    )
    ttft = (
        "ttft_grouped AS MATERIALIZED (SELECT "
        + grouping
        + " AS grouping_mask,"
        + fields
        + ",percentile_cont(.50) WITHIN GROUP (ORDER BY sample) AS ttft_ms_p50,"
        "percentile_cont(.95) WITHIN GROUP (ORDER BY sample) AS ttft_ms_p95 "
        "FROM selected_dimensions,unnest(ttft_samples) sample "
        "GROUP BY GROUPING SETS ("
        + sets
        + ")),ttft_scope_rows AS MATERIALIZED (SELECT "
        + projection
        + ",ttft_ms_p50,ttft_ms_p95 FROM ttft_grouped)"
    )
    calls = (
        "call_grouped AS MATERIALIZED (SELECT "
        + grouping
        + " AS grouping_mask,"
        + fields
        + f",sum({cohort_column})::bigint AS cohort_calls,"
        f"sum({requested_column})::bigint AS requested_calls,"
        f"sum({resolved_column})::bigint AS resolved_calls "
        "FROM selected_call_dimensions GROUP BY GROUPING SETS ("
        + sets
        + ")),call_scope_rows AS MATERIALIZED (SELECT "
        + projection
        + ",CASE WHEN grouping_mask IN (63,31,47) THEN cohort_calls "
        "WHEN grouping_mask=51 THEN requested_calls ELSE resolved_calls END "
        "AS logical_calls FROM call_grouped)"
    )
    return fact, ttft, calls


def _bounded_union_ctes(
    *, cohort_column: str, requested_column: str, resolved_column: str
) -> tuple[str, str, str]:
    logical = {
        "cohort": cohort_column,
        "requested": requested_column,
        "resolved": resolved_column,
    }
    fact = "attempt_scope_rows AS MATERIALIZED (" + " UNION ALL ".join(
        _attempt_scope_select(scope=scope, keys=keys, logical_column="unused")
        .replace(" FROM selected_dimensions", " FROM selected_dimensions")
        .replace(",sum(unused)::bigint AS logical_calls", "")
        for scope, keys, _kind in _SCOPE_KEYS
    ) + ")"
    ttft = "ttft_scope_rows AS MATERIALIZED (" + " UNION ALL ".join(
        _ttft_scope_select(scope=scope, keys=keys).replace(
            "CROSS JOIN LATERAL", "CROSS JOIN"
        )
        for scope, keys, _kind in _SCOPE_KEYS
    ) + ")"
    calls = "call_scope_rows AS MATERIALIZED (" + " UNION ALL ".join(
        _call_scope_select(
            scope=scope, keys=keys, logical_column=logical[kind]
        )
        for scope, keys, kind in _SCOPE_KEYS
    ) + ")"
    return fact, ttft, calls


def _candidate_query(*, shape: str, selector: str, completeness: str) -> str:
    selected_where = _selected_where(
        selector=selector, completeness=completeness
    )
    cohort_column = f"logical_calls_cohort_{selector}_{completeness}"
    requested_column = f"logical_calls_requested_{selector}_{completeness}"
    resolved_column = f"logical_calls_cohort_provider_model_{completeness}"
    outer_column = f"missing_outer_ordinals_{selector}_{completeness}"
    inner_column = f"missing_inner_ordinals_{selector}_{completeness}"
    if shape == "bounded_unions":
        scope_ctes = _bounded_union_ctes(
            cohort_column=cohort_column,
            requested_column=requested_column,
            resolved_column=resolved_column,
        )
    elif shape == "grouping_sets":
        scope_ctes = _grouping_sets_ctes(
            cohort_column=cohort_column,
            requested_column=requested_column,
            resolved_column=resolved_column,
        )
    else:
        raise ValueError(f"unknown candidate shape: {shape!r}")

    fact_columns = ",".join(_FACT_SOURCE_COLUMNS)
    call_columns = ",".join(
        (*NARROW_IDENTITY_COLUMNS[1:], *FLAG_COLUMN_NAMES)
    )
    null_metrics = ",".join(
        (
            "NULL::bigint AS attempts",
            "NULL::bigint AS logical_calls",
            "NULL::bigint AS retry_attempts",
            "NULL::bigint AS failover_attempts",
            "NULL::bigint AS failed_attempts",
            "NULL::bigint AS usage_known_attempts",
            "NULL::bigint AS usage_unknown_attempts",
            "NULL::bigint AS possibly_billed_attempts",
            *(f"NULL::bigint AS {field}" for field in _TOKEN_FIELDS),
            "NULL::double precision AS ttft_ms_p50",
            "NULL::double precision AS ttft_ms_p95",
        )
    )
    metric_select = (
        "a.scope,a.user_id,a.lane,a.provider,a.model,NULL::text AS currency,"
        "a.attempts,coalesce(c.logical_calls,0)::bigint AS logical_calls,"
        "a.retry_attempts,a.failover_attempts,a.failed_attempts,"
        "a.usage_known_attempts,a.usage_unknown_attempts,"
        "a.possibly_billed_attempts,"
        + ",".join(f"a.{field}" for field in _TOKEN_FIELDS)
        + ",t.ttft_ms_p50,t.ttft_ms_p95,"
        "NULL::numeric AS authoritative_cost,"
        "NULL::numeric AS estimated_cost,"
        "NULL::bigint AS authoritative_attempts,"
        "NULL::bigint AS estimated_attempts,"
        "NULL::bigint AS unknown_cost_attempts,"
        "NULL::bigint AS outer_gaps,NULL::bigint AS inner_gaps,"
        "NULL::bigint AS whole_turn_model_calls FROM attempt_scope_rows a "
        "LEFT JOIN call_scope_rows c ON c.scope=a.scope "
        "AND c.user_id IS NOT DISTINCT FROM a.user_id "
        "AND c.lane IS NOT DISTINCT FROM a.lane "
        "AND c.provider IS NOT DISTINCT FROM a.provider "
        "AND c.model IS NOT DISTINCT FROM a.model "
        "LEFT JOIN ttft_scope_rows t ON t.scope=a.scope "
        "AND t.user_id IS NOT DISTINCT FROM a.user_id "
        "AND t.lane IS NOT DISTINCT FROM a.lane "
        "AND t.provider IS NOT DISTINCT FROM a.provider "
        "AND t.model IS NOT DISTINCT FROM a.model"
    )
    ctes = (
        "fact_dimension_source AS MATERIALIZED (SELECT "
        + fact_columns
        + " FROM llm_usage_daily_attempt_dimensions "
        "WHERE local_day>=%s AND local_day<=%s UNION ALL SELECT "
        + fact_columns
        + " FROM pg_temp.admin_usage_daily_attempt_raw_dimensions)",
        "call_dimension_source AS MATERIALIZED (SELECT "
        + call_columns
        + " FROM pg_temp.admin_usage_daily_call_dimensions "
        "WHERE local_day>=%s AND local_day<=%s UNION ALL SELECT "
        + call_columns
        + " FROM pg_temp.admin_usage_daily_call_raw_dimensions)",
        "selected_dimensions AS MATERIALIZED (SELECT * FROM fact_dimension_source "
        "WHERE "
        + selected_where
        + ")",
        "selected_call_dimensions AS MATERIALIZED (SELECT * FROM call_dimension_source "
        "WHERE "
        + selected_where
        + ")",
        *scope_ctes,
    )
    return (
        "WITH "
        + ",".join(ctes)
        + " SELECT "
        + metric_select
        + " UNION ALL SELECT 'cost',NULL,NULL,NULL,NULL,currency,"
        + null_metrics
        + ",sum(cost_amount) FILTER (WHERE cost_kind='authoritative'),"
        "sum(cost_amount) FILTER (WHERE cost_kind='estimated'),"
        "sum(authoritative_cost_attempts)::bigint,"
        "sum(estimated_cost_attempts)::bigint,"
        "sum(unknown_cost_attempts)::bigint,NULL,NULL,NULL "
        "FROM selected_dimensions GROUP BY currency "
        "UNION ALL SELECT 'gaps',NULL,NULL,NULL,NULL,NULL,"
        + null_metrics
        + ",NULL::numeric,NULL::numeric,NULL::bigint,NULL::bigint,NULL::bigint,"
        f"coalesce(sum({outer_column}),0)::bigint,"
        f"coalesce(sum({inner_column}),0)::bigint,NULL::bigint "
        "FROM selected_call_dimensions "
        "UNION ALL SELECT 'filter_option',NULL,NULL,resolved_provider,"
        "resolved_model,NULL,"
        + null_metrics
        + ",NULL::numeric,NULL::numeric,NULL::bigint,NULL::bigint,NULL::bigint,"
        "NULL,NULL,NULL FROM fact_dimension_source "
        "GROUP BY resolved_provider,resolved_model"
    )
def _narrow_day_insert_sql(effective_ctes: str) -> str:
    dimensions = _narrow_dimension_select(
        priced_relation="priced_probe", gap_relation="call_gaps"
    )
    columns = ",".join((*NARROW_IDENTITY_COLUMNS, *FLAG_COLUMN_NAMES))
    return (
        effective_ctes
        + _gap_ctes()
        + ",priced_probe AS MATERIALIZED (SELECT p.* FROM priced p) "
        "INSERT INTO pg_temp.admin_usage_daily_call_dimensions ("
        + columns
        + ") SELECT %s,n.* FROM ("
        + dimensions
        + ") n"
    )


def _narrow_raw_insert_sql(effective_ctes: str) -> str:
    dimensions = _narrow_dimension_select(
        priced_relation="priced_probe",
        gap_relation="call_gaps",
        include_local_day=True,
    )
    columns = ",".join((*NARROW_IDENTITY_COLUMNS, *FLAG_COLUMN_NAMES))
    return (
        effective_ctes
        + _gap_ctes()
        + ",priced_probe AS MATERIALIZED (SELECT "
        "timezone('Asia/Shanghai',m.created_at)::date AS local_day,p.* "
        "FROM priced p JOIN llm_provider_attempts source_attempt "
        "USING(attempt_id) JOIN v2_turn_metrics m "
        "ON m.job_id=source_attempt.job_id "
        "AND m.user_id=source_attempt.user_id) "
        "INSERT INTO pg_temp.admin_usage_daily_call_raw_dimensions ("
        + columns
        + ") "
        + dimensions
    )


def _narrow_dimension_select(
    *,
    priced_relation: str,
    gap_relation: str,
    include_local_day: bool = False,
) -> str:
    ranked_rows, flag_outputs = _ranked_rows_and_outputs(
        priced_relation=priced_relation, gap_relation=gap_relation
    )
    identities = (
        NARROW_IDENTITY_COLUMNS
        if include_local_day
        else NARROW_IDENTITY_COLUMNS[1:]
    )
    projection = (
        "SELECT "
        + ",".join(identities)
        + ","
        + ",".join(flag_outputs)
        + " FROM ("
        + ranked_rows
        + ") ranked"
    )
    return (
        "SELECT "
        + ",".join(identities)
        + ","
        + ",".join(
            f"sum({name})::bigint AS {name}" for name in FLAG_COLUMN_NAMES
        )
        + " FROM ("
        + projection
        + ") narrow_flags GROUP BY "
        + ",".join(identities)
    )


def _narrow_storage_passed(
    stats: dict[str, int], *, membership_total_bytes: int
) -> bool:
    total = int(stats["total_bytes"])
    return (
        total <= MAX_NARROW_TOTAL_BYTES
        and Decimal(total)
        < Decimal(membership_total_bytes) * MAX_MEMBERSHIP_RATIO
    )


def _narrow_table_ddl(*, relation: str) -> tuple[str, ...]:
    if not _RELATION.fullmatch(relation):
        raise ValueError(f"unsafe diagnostic relation name: {relation!r}")

    identity_definitions = (
        "local_day DATE NOT NULL",
        "user_id TEXT NOT NULL",
        "cohort_lane TEXT NOT NULL",
        "requested_provider TEXT NOT NULL",
        "requested_model TEXT NOT NULL",
        "resolved_provider TEXT NOT NULL",
        "resolved_model TEXT NOT NULL",
        "effective_usage_known BOOLEAN NOT NULL",
    )
    flag_definitions = tuple(
        f"{name} BIGINT NOT NULL DEFAULT 0 CHECK ({name} >= 0)"
        for name in FLAG_COLUMN_NAMES
    )
    grain = ",".join(NARROW_IDENTITY_COLUMNS)
    return (
        "CREATE TEMP TABLE "
        + relation
        + " ("
        + ",".join((*identity_definitions, *flag_definitions))
        + ") ON COMMIT PRESERVE ROWS",
        f"CREATE UNIQUE INDEX {relation}_grain_idx ON {relation} ({grain})",
        f"CREATE INDEX {relation}_user_day_idx ON {relation} "
        "(user_id,local_day)",
        f"CREATE INDEX {relation}_resolved_day_idx ON {relation} "
        "(local_day,resolved_provider,resolved_model,user_id,cohort_lane) "
        "INCLUDE (requested_provider,requested_model,effective_usage_known)",
    )


def _adversarial_witness(conn) -> dict[str, Any]:
    rows = conn.execute(
        "WITH priced(attempt_id,call_id,user_id,cohort_lane,"
        "requested_provider,requested_model,resolved_provider,resolved_model,"
        "effective_usage_known,cost_kind,currency) AS (VALUES "
        "('00000000-0000-5000-8000-000000000001'::uuid,'call-a','u1','chat','req','a','p1','shared',true,'unknown',NULL),"
        "('00000000-0000-5000-8000-000000000002'::uuid,'call-a','u1','chat','req','b','p2','shared',false,'unknown',NULL),"
        "('00000000-0000-5000-8000-000000000003'::uuid,'call-b','u1','maintenance','req','a','p1','m1',true,'authoritative','USD'),"
        "('00000000-0000-5000-8000-000000000004'::uuid,'call-b','u1','maintenance','req','a','p1','m2',true,'estimated','USD'),"
        "('00000000-0000-5000-8000-000000000005'::uuid,'call-c','u2','chat','req','c','p2','m1',false,'unknown',NULL),"
        "('00000000-0000-5000-8000-000000000006'::uuid,'call-c','u2','chat','req','c','p2','m1',false,'estimated','USD')"
        "),call_gaps(call_id,missing_outer_ordinals,missing_inner_ordinals) "
        "AS (VALUES ('call-a',1::bigint,0::bigint),"
        "('call-b',0::bigint,1::bigint),('call-c',0::bigint,0::bigint)),"
        "dimensions AS ("
        + _narrow_dimension_select(
            priced_relation="priced", gap_relation="call_gaps"
        )
        + ") SELECT count(*)::bigint,"
        "sum(logical_calls_cohort_all_all)::bigint,"
        "sum(logical_calls_requested_all_all)::bigint,"
        "sum(missing_outer_ordinals_all_all)::bigint,"
        "sum(missing_inner_ordinals_all_all)::bigint FROM dimensions"
    ).fetchone()
    actual = tuple(int(value) for value in rows)
    expected = (5, 3, 4, 1, 1)
    return {"exact": actual == expected, "actual": actual, "expected": expected}


def _sample_candidate(conn, statement: str, params: tuple) -> dict[str, Any]:
    plan = _explain_candidate(conn, statement, params)
    return {
        "elapsed_ms": plan["execution_ms"],
        "planning_ms": plan["planning_ms"],
        "temp_read_blocks": plan["temp_read_blocks"],
        "temp_written_blocks": plan["temp_written_blocks"],
        "plan": plan["plan"],
    }


def _samples_passed(cohort: dict[str, Any]) -> bool:
    samples = cohort.get("samples", {})
    return bool(
        set(samples)
        == {"first_execution_after_build_analyze", "warm", "ordinary"}
        and all(
            float(sample.get("elapsed_ms", P95_BUDGET_MS)) < P95_BUDGET_MS
            and sample.get("temp_read_blocks") == 0
            and sample.get("temp_written_blocks") == 0
            for sample in samples.values()
        )
    )


def _run_probe(
    database_url: str, *, output: Path, prefix: str = DEFAULT_PREFIX
) -> dict[str, Any]:
    """Run the one-session narrow checkpoint and always persist failure evidence."""

    repo = Path(__file__).resolve().parents[2]
    for candidate in (repo, repo / "backend"):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    from admin.usage import UsageQuery  # noqa: PLC0415
    from model_api_runtime.v2 import (  # noqa: PLC0415
        jobs_store,
        provider_attempt_rollup,
        usage_reporting,
    )

    if not re.fullmatch(r"scale_usage_[0-9a-f]{10}_", prefix):
        raise ValueError("probe prefix must match the retained formal fixture shape")
    evidence: dict[str, Any] = {
        "passed": False,
        "nonpersistent": True,
        "prefix": prefix,
        "budget_ms": P95_BUDGET_MS,
        "maintenance_timeout_ms": MAINTENANCE_TIMEOUT_MS,
        "phase": "starting",
        "shapes": {
            shape: {"cohorts": {}}
            for shape in ("bounded_unions", "grouping_sets")
        },
    }
    pre_snapshot = None
    try:
        with psycopg.connect(database_url, autocommit=True) as conn:
            try:
                evidence["phase"] = "pre_witness"
                _set_statement_timeout(conn, DIAGNOSTIC_TIMEOUT_MS)
                pre_snapshot = _persistent_snapshot(conn, prefix=prefix)
                evidence["persistent_state"] = {"pre": pre_snapshot}
                counts = pre_snapshot["counts"]
                expected_counts = {
                    "users": 2_000,
                    "v2_turn_metrics": 3_000_000,
                    "llm_provider_attempts": 3_000_000,
                    "llm_provider_attempt_corrections": 0,
                    "llm_rate_cards": 0,
                    "llm_usage_daily_attempt_dimensions": 731_199,
                    "llm_usage_daily_call_memberships": 3_000_000,
                    "llm_usage_rollup_dirty_days": 0,
                    "v2_usage_rollup_watermarks": 1,
                    "llm_usage_rollup_watermarks": 1,
                }
                if any(counts.get(name) != value for name, value in expected_counts.items()):
                    raise RuntimeError(
                        "retained fixture cardinality mismatch; refusing TEMP experiment"
                    )

                evidence["phase"] = "temp_ddl"
                conn.execute("SET temp_buffers='8MB'")
                for relation in (
                    "admin_usage_daily_call_dimensions",
                    "admin_usage_daily_call_raw_dimensions",
                ):
                    for statement in _narrow_table_ddl(relation=relation):
                        conn.execute(statement)
                zero_flags = ",".join(
                    f"0::bigint AS {name}" for name in FLAG_COLUMN_NAMES
                )
                conn.execute(
                    "CREATE TEMP TABLE admin_usage_ranked_raw_dimensions "
                    "ON COMMIT PRESERVE ROWS AS SELECT d.*," + zero_flags
                    + " FROM llm_usage_daily_attempt_dimensions d WITH NO DATA"
                )
                conn.execute(
                    "CREATE TEMP TABLE admin_usage_daily_attempt_raw_dimensions "
                    "(LIKE llm_usage_daily_attempt_dimensions INCLUDING DEFAULTS) "
                    "ON COMMIT PRESERVE ROWS"
                )

                evidence["phase"] = "day_build"
                day_rows = conn.execute(
                    "SELECT local_day FROM llm_usage_daily_attempt_dimensions "
                    "WHERE left(user_id,length(%s))=%s GROUP BY local_day "
                    "ORDER BY local_day",
                    (prefix, prefix),
                ).fetchall()
                effective = provider_attempt_rollup._effective_attempt_ctes()
                insert_day = _narrow_day_insert_sql(effective)
                build_samples: list[float] = []
                inserted_rows = 0
                _set_statement_timeout(conn, MAINTENANCE_TIMEOUT_MS)
                for (local_day,) in day_rows:
                    local_start = datetime.combine(
                        local_day, datetime_time.min, tzinfo=SHANGHAI
                    ).astimezone(timezone.utc)
                    local_end = datetime.combine(
                        local_day + timedelta(days=1),
                        datetime_time.min,
                        tzinfo=SHANGHAI,
                    ).astimezone(timezone.utc)
                    started = time.perf_counter()
                    cursor = conn.execute(
                        insert_day, (local_start, local_end, local_day)
                    )
                    build_samples.append((time.perf_counter() - started) * 1000)
                    inserted_rows += cursor.rowcount
                conn.execute("ANALYZE pg_temp.admin_usage_daily_call_dimensions")
                evidence["build"] = {
                    "days": len(build_samples),
                    "inserted_rows": inserted_rows,
                    "total_ms": sum(build_samples),
                    "p50_ms": statistics.median(build_samples),
                    "p95_ms": _percentile_nearest_rank(build_samples, 0.95),
                    "max_ms": max(build_samples),
                }
                relation = conn.execute(
                    "SELECT count(*)::bigint,"
                    "pg_relation_size('pg_temp.admin_usage_daily_call_dimensions'::regclass)::bigint,"
                    "pg_indexes_size('pg_temp.admin_usage_daily_call_dimensions'::regclass)::bigint,"
                    "pg_total_relation_size('pg_temp.admin_usage_daily_call_dimensions'::regclass)::bigint "
                    "FROM pg_temp.admin_usage_daily_call_dimensions"
                ).fetchone()
                membership_bytes = int(
                    conn.execute(
                        "SELECT pg_total_relation_size("
                        "'llm_usage_daily_call_memberships'::regclass)::bigint"
                    ).fetchone()[0]
                )
                evidence["temp_relation"] = {
                    "rows": int(relation[0]),
                    "heap_bytes": int(relation[1]),
                    "index_bytes": int(relation[2]),
                    "total_bytes": int(relation[3]),
                    "membership_total_bytes": membership_bytes,
                    "temp_buffers": "8MB",
                }

                evidence["phase"] = "raw_edge_build"
                start_day = FORMAL_START_UTC.astimezone(SHANGHAI).date()
                end_day = FORMAL_END_UTC.astimezone(SHANGHAI).date()
                first_full = datetime.combine(
                    start_day + timedelta(days=1), datetime_time.min, tzinfo=SHANGHAI
                ).astimezone(timezone.utc)
                last_full = datetime.combine(
                    end_day, datetime_time.min, tzinfo=SHANGHAI
                ).astimezone(timezone.utc)
                raw_effective = provider_attempt_rollup._effective_attempt_ctes(
                    cohort_where="(m.created_at >= %s AND m.created_at < %s) OR "
                    "(m.created_at >= %s AND m.created_at < %s)"
                )
                conn.execute(
                    _raw_ranked_insert_sql(raw_effective),
                    (FORMAL_START_UTC, first_full, last_full, FORMAL_END_UTC),
                )
                fact_columns = ",".join(_FACT_SOURCE_COLUMNS)
                conn.execute(
                    "INSERT INTO pg_temp.admin_usage_daily_attempt_raw_dimensions "
                    "(local_day," + fact_columns + ",refreshed_at) SELECT local_day,"
                    + fact_columns
                    + ",refreshed_at FROM pg_temp.admin_usage_ranked_raw_dimensions"
                )
                narrow_columns = ",".join(
                    (*NARROW_IDENTITY_COLUMNS, *FLAG_COLUMN_NAMES)
                )
                conn.execute(
                    "INSERT INTO pg_temp.admin_usage_daily_call_raw_dimensions ("
                    + narrow_columns
                    + ") SELECT "
                    + ",".join(NARROW_IDENTITY_COLUMNS)
                    + ","
                    + ",".join(
                        f"sum({name})::bigint AS {name}"
                        for name in FLAG_COLUMN_NAMES
                    )
                    + " FROM pg_temp.admin_usage_ranked_raw_dimensions GROUP BY "
                    + ",".join(NARROW_IDENTITY_COLUMNS)
                )
                conn.execute("ANALYZE pg_temp.admin_usage_daily_attempt_raw_dimensions")
                conn.execute("ANALYZE pg_temp.admin_usage_daily_call_raw_dimensions")
                raw = conn.execute(
                    "SELECT coalesce(sum(attempts),0)::bigint,"
                    "coalesce(sum(logical_calls_cohort_all_all),0)::bigint "
                    "FROM pg_temp.admin_usage_ranked_raw_dimensions"
                ).fetchone()
                evidence["raw_edge"] = {
                    "attempts": int(raw[0]), "logical_calls": int(raw[1])
                }
                evidence["adversarial"] = _adversarial_witness(conn)

                queries = {
                    "unfiltered": UsageQuery(
                        start_at_utc=FORMAL_START_UTC,
                        end_at_utc=FORMAL_END_UTC,
                        timezone="Asia/Shanghai",
                        preset="90d",
                    ),
                    "provider_model_filtered": UsageQuery(
                        start_at_utc=FORMAL_START_UTC,
                        end_at_utc=FORMAL_END_UTC,
                        timezone="Asia/Shanghai",
                        preset="90d",
                        provider="openrouter",
                        model="openai/gpt-4o-mini",
                    ),
                }
                partition = usage_reporting.rollup_partition(queries["unfiltered"])
                rollup_from = min(partition.rollup_days)
                rollup_through = max(partition.rollup_days)
                evidence["plan_guards"] = {"forbidden_paths_absent": True}
                for cohort_name, query in queries.items():
                    selector = (
                        "provider_model" if query.provider and query.model else "all"
                    )
                    filters = tuple(
                        value for value in (query.provider, query.model) if value
                    )
                    params = (
                        rollup_from,
                        rollup_through,
                        rollup_from,
                        rollup_through,
                        *filters,
                        *filters,
                    )
                    ordinary_rows: dict[str, tuple[tuple, ...]] = {}
                    for shape in _shape_order(cohort_name):
                        evidence["phase"] = f"{cohort_name}_{shape}"
                        statement = _candidate_query(
                            shape=shape,
                            selector=selector,
                            completeness="all",
                        )
                        guards = all(
                            forbidden not in statement.lower()
                            for forbidden in (
                                "llm_usage_daily_call_memberships",
                                "count(distinct",
                                "cross join lateral",
                            )
                        )
                        evidence["plan_guards"]["forbidden_paths_absent"] &= guards
                        cohort_evidence: dict[str, Any] = {
                            "exact": False,
                            "samples": {},
                        }
                        evidence["shapes"][shape]["cohorts"][
                            cohort_name
                        ] = cohort_evidence
                        try:
                            _set_statement_timeout(conn, int(P95_BUDGET_MS))
                            conn.execute("DISCARD PLANS")
                            for sample_name in (
                                "first_execution_after_build_analyze",
                                "warm",
                                "ordinary",
                            ):
                                cohort_evidence["samples"][sample_name] = (
                                    _sample_candidate(conn, statement, params)
                                )
                            started = time.perf_counter()
                            ordinary_rows[shape] = _canonical_rows(
                                conn.execute(statement, params).fetchall()
                            )
                            cohort_evidence["result_fetch_ms"] = (
                                time.perf_counter() - started
                            ) * 1000
                        except Exception as shape_exc:  # noqa: BLE001
                            cohort_evidence["failure"] = {
                                "type": type(shape_exc).__name__,
                                "message": str(shape_exc),
                            }

                    viable = tuple(
                        shape
                        for shape in ("bounded_unions", "grouping_sets")
                        if _samples_passed(
                            evidence["shapes"][shape]["cohorts"].get(
                                cohort_name, {}
                            )
                        )
                    )
                    if not viable:
                        raise RuntimeError(
                            f"neither shape can pass cohort {cohort_name}"
                        )
                    evidence["phase"] = f"{cohort_name}_reference"
                    _set_statement_timeout(conn, DIAGNOSTIC_TIMEOUT_MS)
                    old_sql, old_params = jobs_store._usage_attempt_query(
                        query, partition
                    )
                    old_rows = _canonical_rows(
                        conn.execute(old_sql, old_params).fetchall()
                    )
                    for shape in viable:
                        cohort_evidence = evidence["shapes"][shape]["cohorts"][
                            cohort_name
                        ]
                        cohort_evidence["exact"] = ordinary_rows[shape] == old_rows
                        cohort_evidence["old"] = _rows_witness(old_rows)
                        cohort_evidence["candidate"] = _rows_witness(
                            ordinary_rows[shape]
                        )

                evidence["selected_shape"] = _select_shape(evidence["shapes"])
                if evidence["selected_shape"] is not None:
                    maxima = {
                        shape: _shape_maximum(shape_evidence)
                        for shape, shape_evidence in evidence["shapes"].items()
                        if _shape_eligible(shape_evidence)
                    }
                    evidence["selection_reason"] = {
                        "rule": "eligible shape with lower six-sample maximum",
                        "eligible_max_ms": maxima,
                    }
            except Exception as exc:  # noqa: BLE001
                evidence["failure"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "phase": evidence["phase"],
                }
            finally:
                if pre_snapshot is not None:
                    try:
                        evidence["phase"] = "persistent_post_witness"
                        _set_statement_timeout(conn, DIAGNOSTIC_TIMEOUT_MS)
                        post = _persistent_snapshot(conn, prefix=prefix)
                        evidence["persistent_state"].update(
                            {"post": post, "unchanged": post == pre_snapshot}
                        )
                    except Exception as post_exc:  # noqa: BLE001
                        evidence["persistent_state"]["post_failure"] = {
                            "type": type(post_exc).__name__,
                            "message": str(post_exc),
                        }
    except Exception as connection_exc:  # noqa: BLE001
        evidence.setdefault(
            "failure",
            {
                "type": type(connection_exc).__name__,
                "message": str(connection_exc),
                "phase": evidence["phase"],
            },
        )
    try:
        with psycopg.connect(database_url, autocommit=True) as verify_conn:
            objects = [
                row[0]
                for row in verify_conn.execute(
                    "SELECT n.nspname||'.'||c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE (c.relname LIKE 'admin_usage_narrow_%' "
                    "OR c.relname IN ('admin_usage_daily_call_dimensions',"
                    "'admin_usage_daily_call_raw_dimensions',"
                    "'admin_usage_daily_attempt_raw_dimensions',"
                    "'admin_usage_ranked_raw_dimensions')) "
                    "AND c.relpersistence<>'t' ORDER BY 1"
                ).fetchall()
            ]
            evidence["session_close"] = {"persistent_probe_objects": objects}
    except Exception as verify_exc:  # noqa: BLE001
        evidence["session_close"] = {
            "persistent_probe_objects": None,
            "failure": {
                "type": type(verify_exc).__name__,
                "message": str(verify_exc),
            },
        }
    evidence["passed"] = _probe_passed(evidence)
    evidence["phase"] = "complete"
    _write_json_atomic(output, evidence)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the non-persistent 3M narrow-call checkpoint"
    )
    parser.add_argument(
        "--database-url", default=os.environ.get("SCALE_PROBE_DSN", "")
    )
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.database_url.strip():
        parser.error("set SCALE_PROBE_DSN or pass --database-url")
    evidence = _run_probe(
        args.database_url.strip(), output=args.output, prefix=args.prefix
    )
    print(json.dumps(evidence, indent=2, sort_keys=True, default=str))
    return 0 if evidence.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
