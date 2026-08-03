#!/usr/bin/env python3
"""Non-persistent feasibility probe for exact ranked provider-call flags."""

from __future__ import annotations

import argparse
from datetime import datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo

import psycopg


SELECTOR_PARTITIONS = {
    "all": (),
    "provider": ("resolved_provider",),
    "model": ("resolved_model",),
    "provider_model": ("resolved_provider", "resolved_model"),
}
COMPLETENESS_PARTITIONS = {
    "all": (),
    "effective": ("effective_usage_known",),
}
_METRICS = (
    "logical_calls_cohort",
    "logical_calls_requested",
    "missing_outer_ordinals",
    "missing_inner_ordinals",
)
FLAG_COLUMN_NAMES = tuple(
    f"{metric}_{selector}_{completeness}"
    for selector in SELECTOR_PARTITIONS
    for completeness in COMPLETENESS_PARTITIONS
    for metric in _METRICS
)
_RELATION = re.compile(r"^[a-z_][a-z0-9_]*$")
P95_BUDGET_MS = 3_000.0
MAINTENANCE_TIMEOUT_MS = 120_000
EXPECTED_TEMP_ROWS = 731_199
EXPECTED_RAW_EDGE_ROWS = 8_208
DIAGNOSTIC_TIMEOUT_MS = 180_000
SHANGHAI = ZoneInfo("Asia/Shanghai")
FIXED_NOW_UTC = datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc)
FORMAL_START_UTC = FIXED_NOW_UTC - timedelta(days=90)
FORMAL_END_UTC = FIXED_NOW_UTC
DEFAULT_PREFIX = "scale_usage_42e02f444a_"
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_miss_tokens",
)
_IDENTITY_FIELDS = (
    "user_id",
    "cohort_lane",
    "requested_provider",
    "requested_model",
    "resolved_provider",
    "resolved_model",
    "effective_usage_known",
    "cost_kind",
    "currency",
)
_PERSISTENT_COUNT_TABLES = (
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
)


def _rank_alias(projection: str, selector: str, completeness: str) -> str:
    return f"rn_{projection}_{selector}_{completeness}"


def _ranked_rows_and_outputs(*, priced_relation: str, gap_relation: str):
    """Return the shared ranked rows SQL and its exact flag projections."""
    for relation in (priced_relation, gap_relation):
        if not _RELATION.fullmatch(relation):
            raise ValueError(f"unsafe diagnostic relation name: {relation!r}")

    ranks = []
    outputs = []
    for selector, selector_fields in SELECTOR_PARTITIONS.items():
        for completeness, completeness_fields in COMPLETENESS_PARTITIONS.items():
            cohort_alias = _rank_alias("cohort", selector, completeness)
            requested_alias = _rank_alias("requested", selector, completeness)
            base_partition = (
                "call_id",
                *selector_fields,
                *completeness_fields,
            )
            cohort_partition = ",".join(f"p.{field}" for field in base_partition)
            requested_partition = ",".join(
                f"p.{field}"
                for field in (
                    *base_partition,
                    "requested_provider",
                    "requested_model",
                )
            )
            ranks.extend(
                (
                    "row_number() OVER (PARTITION BY "
                    f"{cohort_partition} ORDER BY p.attempt_id) AS {cohort_alias}",
                    "row_number() OVER (PARTITION BY "
                    f"{requested_partition} ORDER BY p.attempt_id) AS {requested_alias}",
                )
            )
            outputs.extend(
                (
                    f"({cohort_alias}=1)::int::bigint AS "
                    f"logical_calls_cohort_{selector}_{completeness}",
                    f"({requested_alias}=1)::int::bigint AS "
                    f"logical_calls_requested_{selector}_{completeness}",
                    f"CASE WHEN {cohort_alias}=1 THEN missing_outer_ordinals "
                    f"ELSE 0 END::bigint AS missing_outer_ordinals_{selector}_{completeness}",
                    f"CASE WHEN {cohort_alias}=1 THEN missing_inner_ordinals "
                    f"ELSE 0 END::bigint AS missing_inner_ordinals_{selector}_{completeness}",
                )
            )

    ranked_rows = (
        "SELECT p.*,"
        "coalesce(g.missing_outer_ordinals,0)::bigint AS missing_outer_ordinals,"
        "coalesce(g.missing_inner_ordinals,0)::bigint AS missing_inner_ordinals,"
        + ",".join(ranks)
        + f" FROM {priced_relation} p LEFT JOIN {gap_relation} g USING(call_id)"
    )
    return ranked_rows, outputs


def _ranked_flag_select(*, priced_relation: str, gap_relation: str) -> str:
    """Return one SELECT assigning exact flags to stable attempt representatives."""

    ranked_rows, outputs = _ranked_rows_and_outputs(
        priced_relation=priced_relation, gap_relation=gap_relation
    )
    return (
        "SELECT " + ",".join(outputs) + " FROM (" + ranked_rows + ") ranked"
    )


def _ranked_flag_ctes(*, priced_relation: str, gap_relation: str) -> str:
    """Compatibility boundary used by the scale checkpoint and SQL guards."""

    return _ranked_flag_select(
        priced_relation=priced_relation, gap_relation=gap_relation
    )


def _ranked_dimension_select(*, priced_relation: str, gap_relation: str) -> str:
    """Aggregate diagnostic flags onto the proposed existing dimension grain."""

    identity_fields = (
        "user_id",
        "cohort_lane",
        "requested_provider",
        "requested_model",
        "resolved_provider",
        "resolved_model",
        "effective_usage_known",
        "cost_kind",
        "currency",
    )
    ranked_rows, outputs = _ranked_rows_and_outputs(
        priced_relation=priced_relation, gap_relation=gap_relation
    )
    flags = (
        "SELECT "
        + ",".join(identity_fields)
        + ","
        + ",".join(outputs)
        + " FROM ("
        + ranked_rows
        + ") ranked"
    )
    return (
        "SELECT "
        + ",".join(identity_fields)
        + ",count(*)::bigint AS attempts,"
        + ",".join(
            f"sum({column})::bigint AS {column}" for column in FLAG_COLUMN_NAMES
        )
        + " FROM ("
        + flags
        + ") ranked_flags GROUP BY "
        + ",".join(identity_fields)
    )


def _attempt_scope_select(
    *, scope: str, keys: tuple[tuple[str, str], ...], logical_column: str
) -> str:
    key_expressions = dict(keys)
    group_expressions = tuple(key_expressions.values())
    token_outputs = ",".join(
        f"CASE WHEN sum({field}_known_count)>0 THEN sum({field}_sum)::bigint END "
        f"AS {field}"
        for field in _TOKEN_FIELDS
    )
    group_by = (
        " GROUP BY " + ",".join(group_expressions) if group_expressions else ""
    )
    return (
        f"SELECT '{scope}'::text AS scope,"
        + ",".join(
            f"{key_expressions.get(name, 'NULL::text')} AS {name}"
            for name in ("user_id", "lane", "provider", "model")
        )
        + ",sum(attempts)::bigint AS attempts,"
        f"sum({logical_column})::bigint AS logical_calls,"
        "sum(retry_attempts)::bigint AS retry_attempts,"
        "sum(failover_attempts)::bigint AS failover_attempts,"
        "sum(failed_attempts)::bigint AS failed_attempts,"
        "coalesce(sum(attempts) FILTER (WHERE effective_usage_known),0)::bigint "
        "AS usage_known_attempts,"
        "coalesce(sum(attempts) FILTER (WHERE NOT effective_usage_known),0)::bigint "
        "AS usage_unknown_attempts,"
        "sum(possibly_billed_attempts)::bigint AS possibly_billed_attempts,"
        + token_outputs
        + " FROM selected_dimensions"
        + group_by
    )


def _ttft_scope_select(*, scope: str, keys: tuple[tuple[str, str], ...]) -> str:
    key_expressions = dict(keys)
    group_expressions = tuple(key_expressions.values())
    group_by = (
        " GROUP BY " + ",".join(group_expressions) if group_expressions else ""
    )
    return (
        f"SELECT '{scope}'::text AS scope,"
        + ",".join(
            f"{key_expressions.get(name, 'NULL::text')} AS {name}"
            for name in ("user_id", "lane", "provider", "model")
        )
        + ",percentile_cont(.50) WITHIN GROUP (ORDER BY sample) AS ttft_ms_p50,"
        "percentile_cont(.95) WITHIN GROUP (ORDER BY sample) AS ttft_ms_p95 "
        "FROM selected_dimensions CROSS JOIN LATERAL unnest(ttft_samples) sample"
        + group_by
    )


def _candidate_query_sql(*, selector: str, completeness: str) -> str:
    """Return the rollup-only ranked candidate used by the hard checkpoint."""

    if selector not in SELECTOR_PARTITIONS:
        raise ValueError(f"unknown selector: {selector}")
    if completeness not in COMPLETENESS_PARTITIONS:
        raise ValueError(f"unknown completeness mode: {completeness}")
    cohort = f"logical_calls_cohort_{selector}_{completeness}"
    requested = f"logical_calls_requested_{selector}_{completeness}"
    resolved = f"logical_calls_cohort_provider_model_{completeness}"
    outer = f"missing_outer_ordinals_{selector}_{completeness}"
    inner = f"missing_inner_ordinals_{selector}_{completeness}"
    selected_clauses = ["TRUE"]
    if completeness == "effective":
        selected_clauses.append("effective_usage_known=%s")
    if selector in {"provider", "provider_model"}:
        selected_clauses.append("resolved_provider=%s")
    if selector in {"model", "provider_model"}:
        selected_clauses.append("resolved_model=%s")
    selected_where = " AND ".join(selected_clauses)
    scopes = (
        ("overview", (), cohort),
        ("user", (("user_id", "user_id"),), cohort),
        ("lane", (("lane", "cohort_lane"),), cohort),
        (
            "requested_model",
            (("provider", "requested_provider"), ("model", "requested_model")),
            requested,
        ),
        (
            "resolved_model",
            (("provider", "resolved_provider"), ("model", "resolved_model")),
            resolved,
        ),
    )
    attempt_scopes = " UNION ALL ".join(
        _attempt_scope_select(scope=scope, keys=keys, logical_column=logical)
        for scope, keys, logical in scopes
    )
    ttft_scopes = " UNION ALL ".join(
        _ttft_scope_select(scope=scope, keys=keys) for scope, keys, _ in scopes
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
        "a.attempts,a.logical_calls,a.retry_attempts,a.failover_attempts,"
        "a.failed_attempts,a.usage_known_attempts,a.usage_unknown_attempts,"
        "a.possibly_billed_attempts,"
        + ",".join(f"a.{field}" for field in _TOKEN_FIELDS)
        + ",t.ttft_ms_p50,t.ttft_ms_p95,NULL::numeric AS authoritative_cost,"
        "NULL::numeric AS estimated_cost,NULL::bigint AS authoritative_attempts,"
        "NULL::bigint AS estimated_attempts,NULL::bigint AS unknown_cost_attempts,"
        "NULL::bigint AS outer_gaps,NULL::bigint AS inner_gaps,"
        "NULL::bigint AS whole_turn_model_calls FROM attempt_scope_rows a "
        "LEFT JOIN ttft_scope_rows t ON t.scope=a.scope "
        "AND t.user_id IS NOT DISTINCT FROM a.user_id "
        "AND t.lane IS NOT DISTINCT FROM a.lane "
        "AND t.provider IS NOT DISTINCT FROM a.provider "
        "AND t.model IS NOT DISTINCT FROM a.model"
    )
    return (
        "WITH dimension_source AS MATERIALIZED ("
        "SELECT * FROM pg_temp.admin_usage_ranked_dimensions "
        "WHERE local_day>=%s AND local_day<=%s UNION ALL "
        "SELECT * FROM pg_temp.admin_usage_ranked_raw_dimensions),"
        "selected_dimensions AS MATERIALIZED (SELECT * FROM dimension_source WHERE "
        + selected_where
        + "),attempt_scope_rows AS MATERIALIZED ("
        + attempt_scopes
        + "),ttft_scope_rows AS MATERIALIZED ("
        + ttft_scopes
        + ") SELECT "
        + metric_select
        + " UNION ALL SELECT 'cost',NULL,NULL,NULL,NULL,currency,"
        + null_metrics
        + ",sum(cost_amount) FILTER (WHERE cost_kind='authoritative'),"
        "sum(cost_amount) FILTER (WHERE cost_kind='estimated'),"
        "sum(authoritative_cost_attempts)::bigint,"
        "sum(estimated_cost_attempts)::bigint,sum(unknown_cost_attempts)::bigint,"
        "NULL,NULL,NULL FROM selected_dimensions GROUP BY currency "
        "UNION ALL SELECT 'gaps',NULL,NULL,NULL,NULL,NULL,"
        + null_metrics
        + ",NULL::numeric,NULL::numeric,NULL::bigint,NULL::bigint,NULL::bigint,"
        f"coalesce(sum({outer}),0)::bigint,coalesce(sum({inner}),0)::bigint,"
        "NULL::bigint FROM selected_dimensions "
        "UNION ALL SELECT 'filter_option',NULL,NULL,resolved_provider,resolved_model,"
        "NULL,"
        + null_metrics
        + ",NULL::numeric,NULL::numeric,NULL::bigint,NULL::bigint,NULL::bigint,"
        "NULL,NULL,NULL FROM dimension_source GROUP BY resolved_provider,resolved_model"
    )


def _probe_passed(evidence) -> bool:
    temp = evidence.get("temp_relation", {})
    build = evidence.get("build", {})
    raw = evidence.get("raw_edge", {})
    cohorts = evidence.get("cohorts", {})
    required = {"unfiltered", "provider_model_filtered"}
    return bool(
        temp.get("rows") == EXPECTED_TEMP_ROWS
        and build.get("days", 0) > 0
        and float(build.get("max_ms", MAINTENANCE_TIMEOUT_MS))
        < MAINTENANCE_TIMEOUT_MS
        and raw.get("attempts") == EXPECTED_RAW_EDGE_ROWS
        and raw.get("logical_calls") == EXPECTED_RAW_EDGE_ROWS
        and set(cohorts) == required
        and all(
            item.get("exact") is True
            and item.get("candidate_forbidden_path_absent") is True
            and float(item.get("ordinary_ms", P95_BUDGET_MS)) < P95_BUDGET_MS
            and all(
                float(item.get(sample, {}).get("execution_ms", P95_BUDGET_MS))
                < P95_BUDGET_MS
                and item.get(sample, {}).get("temp_read_blocks") == 0
                and item.get(sample, {}).get("temp_written_blocks") == 0
                for sample in ("cold", "warm")
            )
            for item in cohorts.values()
        )
        and evidence.get("persistent_state", {}).get("unchanged") is True
        and evidence.get("session_close", {}).get("persistent_probe_objects") == []
    )


def _percentile_nearest_rank(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile + 0.999999) - 1))
    return ordered[index]


def _cost_projection(alias: str = "p") -> str:
    return (
        f"{alias}.*,CASE WHEN {alias}.authoritative_cost IS NOT NULL THEN "
        "'authoritative' WHEN "
        f"{alias}.estimated_cost IS NOT NULL THEN 'estimated' ELSE 'unknown' END "
        "AS cost_kind,CASE WHEN "
        f"{alias}.authoritative_cost IS NOT NULL THEN {alias}.authoritative_currency "
        f"WHEN {alias}.estimated_cost IS NOT NULL THEN {alias}.rate_currency END "
        "AS currency"
    )


def _gap_ctes() -> str:
    return """
,cohort_calls AS MATERIALIZED (
  SELECT DISTINCT call_id FROM corrected
),global_attempts AS MATERIALIZED (
  SELECT a.call_id,a.outer_attempt_ordinal,a.inner_attempt_ordinal
  FROM cohort_calls c JOIN llm_provider_attempts a USING(call_id)
  WHERE a.source='runtime_recorder'
),outer_gaps AS (
  SELECT call_id,greatest(
    coalesce(max(outer_attempt_ordinal) FILTER (WHERE outer_attempt_ordinal>=1),0)
    -count(DISTINCT outer_attempt_ordinal)
      FILTER (WHERE outer_attempt_ordinal>=1),0
  )::bigint AS missing_outer_ordinals
  FROM global_attempts GROUP BY call_id
),inner_gap_groups AS (
  SELECT call_id,outer_attempt_ordinal,greatest(
    coalesce(max(inner_attempt_ordinal) FILTER (WHERE inner_attempt_ordinal>=1),0)
    -count(DISTINCT inner_attempt_ordinal)
      FILTER (WHERE inner_attempt_ordinal>=1),0
  )::bigint AS missing_inner_ordinals
  FROM global_attempts GROUP BY call_id,outer_attempt_ordinal
),call_gaps AS (
  SELECT o.call_id,o.missing_outer_ordinals,
    coalesce(sum(i.missing_inner_ordinals),0)::bigint AS missing_inner_ordinals
  FROM outer_gaps o LEFT JOIN inner_gap_groups i USING(call_id)
  GROUP BY o.call_id,o.missing_outer_ordinals
)
"""


def _day_rank_update_sql(effective_ctes: str) -> str:
    flags = _ranked_dimension_select(
        priced_relation="priced_probe", gap_relation="call_gaps"
    )
    identity_match = " AND ".join(
        (
            f"d.{field} IS NOT DISTINCT FROM r.{field}"
            if field == "currency"
            else f"d.{field}=r.{field}"
        )
        for field in _IDENTITY_FIELDS
    )
    assignments = ",".join(f"{name}=r.{name}" for name in FLAG_COLUMN_NAMES)
    return (
        effective_ctes
        + _gap_ctes()
        + ",priced_probe AS MATERIALIZED (SELECT "
        + _cost_projection()
        + " FROM priced p),ranked_flags AS MATERIALIZED ("
        + flags
        + ") UPDATE pg_temp.admin_usage_ranked_dimensions d SET "
        + assignments
        + " FROM ranked_flags r WHERE d.local_day=%s AND "
        + identity_match
    )


def _raw_ranked_insert_sql(effective_ctes: str) -> str:
    ranked_rows, flag_outputs = _ranked_rows_and_outputs(
        priced_relation="priced_probe", gap_relation="call_gaps"
    )
    token_selects = ",".join(
        item
        for field in _TOKEN_FIELDS
        for item in (
            f"coalesce(sum({field}) FILTER (WHERE {field} IS NOT NULL),0)::bigint",
            f"count({field})::bigint",
        )
    )
    flag_sums = ",".join(
        f"sum({name})::bigint AS {name}" for name in FLAG_COLUMN_NAMES
    )
    columns = (
        "local_day,"
        + ",".join(_IDENTITY_FIELDS)
        + ",attempts,retry_attempts,failover_attempts,failed_attempts,"
        "possibly_billed_attempts,"
        + ",".join(
            item
            for field in _TOKEN_FIELDS
            for item in (f"{field}_sum", f"{field}_known_count")
        )
        + ",authoritative_cost_attempts,estimated_cost_attempts,"
        "unknown_cost_attempts,cost_amount,ttft_samples,refreshed_at,"
        + ",".join(FLAG_COLUMN_NAMES)
    )
    return (
        effective_ctes
        + _gap_ctes()
        + ",priced_probe AS MATERIALIZED (SELECT timezone('Asia/Shanghai',m.created_at)"
        "::date AS local_day,"
        + _cost_projection()
        + " FROM priced p JOIN llm_provider_attempts source_attempt USING(attempt_id) "
        "JOIN v2_turn_metrics m ON m.job_id=source_attempt.job_id "
        "AND m.user_id=source_attempt.user_id),ranked AS MATERIALIZED (SELECT "
        "local_day,user_id,cohort_lane,requested_provider,requested_model,"
        "resolved_provider,resolved_model,effective_usage_known,cost_kind,currency,"
        "attempt_id,retry_kind,outcome,possibly_billed,"
        + ",".join(_TOKEN_FIELDS)
        + ",authoritative_cost,estimated_cost,ttft_ms,"
        + ",".join(flag_outputs)
        + " FROM ("
        + ranked_rows
        + ") ranked_source) INSERT INTO "
        "pg_temp.admin_usage_ranked_raw_dimensions ("
        + columns
        + ") SELECT local_day,"
        + ",".join(_IDENTITY_FIELDS)
        + ",count(*)::bigint,count(*) FILTER (WHERE retry_kind<>'initial')::bigint,"
        "count(*) FILTER (WHERE retry_kind='failover')::bigint,"
        "count(*) FILTER (WHERE outcome IN ('failed','timed_out','cancelled'))::bigint,"
        "count(*) FILTER (WHERE possibly_billed)::bigint,"
        + token_selects
        + ",count(*) FILTER (WHERE authoritative_cost IS NOT NULL)::bigint,"
        "count(*) FILTER (WHERE authoritative_cost IS NULL AND estimated_cost IS NOT NULL)::bigint,"
        "count(*) FILTER (WHERE authoritative_cost IS NULL AND estimated_cost IS NULL)::bigint,"
        "coalesce(sum(CASE WHEN authoritative_cost IS NOT NULL THEN authoritative_cost "
        "WHEN estimated_cost IS NOT NULL THEN estimated_cost ELSE 0 END),0),"
        "coalesce(array_agg(ttft_ms ORDER BY ttft_ms,attempt_id) FILTER "
        "(WHERE ttft_ms IS NOT NULL),'{}'::double precision[]),now(),"
        + flag_sums
        + " FROM ranked GROUP BY local_day,"
        + ",".join(_IDENTITY_FIELDS)
    )


def _persistent_snapshot(conn, *, prefix: str) -> dict[str, Any]:
    counts = {
        table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608
        for table in _PERSISTENT_COUNT_TABLES
    }
    checksums = {}
    witnesses = {
        "users": (
            "SELECT count(*)::bigint,coalesce(sum(hashtextextended(user_id,0)::numeric),0)::text "
            "FROM users WHERE left(user_id,length(%s))=%s",
            (prefix, prefix),
        ),
        "turns": (
            "SELECT count(*)::bigint,coalesce(sum(hashtextextended(concat_ws(E'\\x1f',"
            "job_id,user_id,created_at::text,updated_at::text),0)::numeric),0)::text "
            "FROM v2_turn_metrics WHERE left(user_id,length(%s))=%s",
            (prefix, prefix),
        ),
        "attempts": (
            "SELECT count(*)::bigint,coalesce(sum(hashtextextended(concat_ws(E'\\x1f',"
            "attempt_id::text,user_id,job_id,call_id,updated_at::text),0)::numeric),0)::text "
            "FROM llm_provider_attempts WHERE left(user_id,length(%s))=%s",
            (prefix, prefix),
        ),
        "corrections": (
            "SELECT count(*)::bigint,coalesce(sum(hashtextextended(id::text,0)"
            "::numeric),0)::text FROM llm_provider_attempt_corrections "
            "WHERE left(user_id,length(%s))=%s",
            (prefix, prefix),
        ),
        "rate_cards": (
            "SELECT count(*)::bigint,coalesce(sum(hashtextextended(concat_ws(E'\\x1f',"
            "provider,model,version,effective_at::text,created_at::text),0)::numeric),0)::text "
            "FROM llm_rate_cards",
            (),
        ),
    }
    for name, (statement, params) in witnesses.items():
        row = conn.execute(statement, params).fetchone()
        checksums[name] = {"rows": int(row[0]), "hash_sum": row[1]}
    watermarks = {}
    for table in ("v2_usage_rollup_watermarks", "llm_usage_rollup_watermarks"):
        watermarks[table] = conn.execute(
            f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb)::text FROM {table} t"  # noqa: S608
        ).fetchone()[0]
    return {"counts": counts, "source_checksums": checksums, "watermarks": watermarks}


def _canonical_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, list):
        return tuple(_canonical_value(item) for item in value)
    return value


def _canonical_rows(rows) -> tuple[tuple, ...]:
    canonical = [tuple(_canonical_value(value) for value in row) for row in rows]
    return tuple(sorted(canonical, key=repr))


def _rows_witness(rows: tuple[tuple, ...]) -> dict[str, Any]:
    encoded = repr(rows).encode("utf-8")
    return {"rows": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}


def _plan_blocks(node) -> tuple[int, int]:
    read = int(node.get("Temp Read Blocks", 0) or 0)
    written = int(node.get("Temp Written Blocks", 0) or 0)
    for child in node.get("Plans", ()):
        child_read, child_written = _plan_blocks(child)
        read += child_read
        written += child_written
    return read, written


def _explain_candidate(conn, statement: str, params: tuple) -> dict[str, Any]:
    row = conn.execute(
        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON,TIMING OFF) " + statement, params
    ).fetchone()
    document = row[0][0]
    read, written = _plan_blocks(document["Plan"])
    return {
        "execution_ms": float(document["Execution Time"]),
        "planning_ms": float(document["Planning Time"]),
        "temp_read_blocks": read,
        "temp_written_blocks": written,
        "plan": document,
    }


def _write_json_atomic(output: Path, evidence: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)


def _set_statement_timeout(conn, timeout_ms: int) -> None:
    conn.execute(
        "SELECT set_config('statement_timeout',%s,false)", (str(timeout_ms),)
    )


def _run_probe(
    database_url: str, *, output: Path, prefix: str = DEFAULT_PREFIX
) -> dict[str, Any]:
    """Run the retained-fixture checkpoint without creating persistent state."""

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
        "cohorts": {},
        "phase": "starting",
    }
    pre_snapshot = None
    try:
        with psycopg.connect(database_url, autocommit=True) as conn:
            _set_statement_timeout(conn, DIAGNOSTIC_TIMEOUT_MS)
            pre_snapshot = _persistent_snapshot(conn, prefix=prefix)
            evidence["persistent_state"] = {"pre": pre_snapshot}
            source_counts = pre_snapshot["counts"]
            if (
                source_counts["llm_provider_attempts"] != 3_000_000
                or source_counts["llm_usage_daily_attempt_dimensions"]
                != EXPECTED_TEMP_ROWS
                or source_counts["llm_usage_daily_call_memberships"] != 3_000_000
            ):
                raise RuntimeError(
                    "retained fixture cardinality mismatch; refusing TEMP experiment"
                )

            evidence["phase"] = "temp_clone"
            conn.execute("SET temp_buffers='8MB'")
            zero_flags = ",".join(
                f"0::bigint AS {name}" for name in FLAG_COLUMN_NAMES
            )
            conn.execute(
                "CREATE TEMP TABLE admin_usage_ranked_dimensions "
                "ON COMMIT PRESERVE ROWS AS SELECT d.*,"
                + zero_flags
                + " FROM llm_usage_daily_attempt_dimensions d "
                "WHERE left(user_id,length(%s))=%s",
                (prefix, prefix),
            )
            conn.execute(
                "CREATE TEMP TABLE admin_usage_ranked_raw_dimensions "
                "(LIKE pg_temp.admin_usage_ranked_dimensions INCLUDING DEFAULTS) "
                "ON COMMIT PRESERVE ROWS"
            )
            conn.execute(
                "CREATE INDEX admin_usage_ranked_dimensions_user_day_idx ON "
                "pg_temp.admin_usage_ranked_dimensions(user_id,local_day)"
            )
            conn.execute(
                "CREATE INDEX admin_usage_ranked_dimensions_resolved_day_idx ON "
                "pg_temp.admin_usage_ranked_dimensions"
                "(local_day,resolved_provider,resolved_model)"
            )

            day_rows = conn.execute(
                "SELECT local_day,count(*)::bigint FROM "
                "pg_temp.admin_usage_ranked_dimensions GROUP BY local_day ORDER BY local_day"
            ).fetchall()
            effective = provider_attempt_rollup._effective_attempt_ctes()
            update_sql = _day_rank_update_sql(effective)
            build_samples = []
            built_rows = 0
            evidence["phase"] = "ranked_day_build"
            _set_statement_timeout(conn, MAINTENANCE_TIMEOUT_MS)
            for local_day, expected_rows in day_rows:
                local_start = datetime.combine(
                    local_day, datetime_time.min, tzinfo=SHANGHAI
                ).astimezone(timezone.utc)
                local_end = datetime.combine(
                    local_day + timedelta(days=1), datetime_time.min, tzinfo=SHANGHAI
                ).astimezone(timezone.utc)
                started = time.perf_counter()
                cursor = conn.execute(update_sql, (local_start, local_end, local_day))
                elapsed = (time.perf_counter() - started) * 1000
                if cursor.rowcount != int(expected_rows):
                    raise RuntimeError(
                        f"ranked day {local_day} updated {cursor.rowcount}, "
                        f"expected {expected_rows}"
                    )
                build_samples.append(elapsed)
                built_rows += cursor.rowcount
            conn.execute("ANALYZE pg_temp.admin_usage_ranked_dimensions")
            relation_row = conn.execute(
                "SELECT count(*)::bigint,"
                "pg_relation_size('pg_temp.admin_usage_ranked_dimensions'::regclass)::bigint,"
                "pg_indexes_size('pg_temp.admin_usage_ranked_dimensions'::regclass)::bigint,"
                "pg_total_relation_size('pg_temp.admin_usage_ranked_dimensions'::regclass)::bigint "
                "FROM pg_temp.admin_usage_ranked_dimensions"
            ).fetchone()
            evidence["temp_relation"] = {
                "rows": int(relation_row[0]),
                "heap_bytes": int(relation_row[1]),
                "index_bytes": int(relation_row[2]),
                "total_bytes": int(relation_row[3]),
                "temp_buffers": "8MB",
            }
            evidence["build"] = {
                "days": len(build_samples),
                "updated_rows": built_rows,
                "total_ms": sum(build_samples),
                "p50_ms": statistics.median(build_samples),
                "p95_ms": _percentile_nearest_rank(build_samples, 0.95),
                "max_ms": max(build_samples),
            }

            evidence["phase"] = "raw_edge_build"
            start_local_day = FORMAL_START_UTC.astimezone(SHANGHAI).date()
            end_local_day = FORMAL_END_UTC.astimezone(SHANGHAI).date()
            first_full_start = datetime.combine(
                start_local_day + timedelta(days=1), datetime_time.min, tzinfo=SHANGHAI
            ).astimezone(timezone.utc)
            last_full_end = datetime.combine(
                end_local_day, datetime_time.min, tzinfo=SHANGHAI
            ).astimezone(timezone.utc)
            raw_predicate = (
                "(m.created_at >= %s AND m.created_at < %s) OR "
                "(m.created_at >= %s AND m.created_at < %s)"
            )
            raw_effective = provider_attempt_rollup._effective_attempt_ctes(
                cohort_where=raw_predicate
            )
            conn.execute(
                _raw_ranked_insert_sql(raw_effective),
                (FORMAL_START_UTC, first_full_start, last_full_end, FORMAL_END_UTC),
            )
            conn.execute("ANALYZE pg_temp.admin_usage_ranked_raw_dimensions")
            raw_row = conn.execute(
                "SELECT coalesce(sum(attempts),0)::bigint,"
                "coalesce(sum(logical_calls_cohort_all_all),0)::bigint "
                "FROM pg_temp.admin_usage_ranked_raw_dimensions"
            ).fetchone()
            evidence["raw_edge"] = {
                "attempts": int(raw_row[0]), "logical_calls": int(raw_row[1])
            }

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
            rollup_from, rollup_through = min(partition.rollup_days), max(
                partition.rollup_days
            )
            for name, query in queries.items():
                evidence["phase"] = f"cohort_{name}"
                old_sql, old_params = jobs_store._usage_attempt_query(query, partition)
                _set_statement_timeout(conn, DIAGNOSTIC_TIMEOUT_MS)
                old_rows = _canonical_rows(conn.execute(old_sql, old_params).fetchall())
                selector = "provider_model" if query.provider and query.model else "all"
                candidate_sql = _candidate_query_sql(
                    selector=selector, completeness="all"
                )
                candidate_params: tuple[Any, ...] = (rollup_from, rollup_through)
                if query.provider:
                    candidate_params += (query.provider,)
                if query.model:
                    candidate_params += (query.model,)
                _set_statement_timeout(conn, int(P95_BUDGET_MS))
                started = time.perf_counter()
                candidate_rows = _canonical_rows(
                    conn.execute(candidate_sql, candidate_params).fetchall()
                )
                ordinary_ms = (time.perf_counter() - started) * 1000
                conn.execute("DISCARD PLANS")
                cold = _explain_candidate(conn, candidate_sql, candidate_params)
                warm = _explain_candidate(conn, candidate_sql, candidate_params)
                evidence["cohorts"][name] = {
                    "exact": candidate_rows == old_rows,
                    "old": _rows_witness(old_rows),
                    "candidate": _rows_witness(candidate_rows),
                    "ordinary_ms": ordinary_ms,
                    "cold": cold,
                    "warm": warm,
                    "candidate_forbidden_path_absent": all(
                        forbidden not in candidate_sql.lower()
                        for forbidden in (
                            "llm_usage_daily_call_memberships",
                            "call_id",
                            "count(distinct",
                        )
                    ),
                }

            evidence["phase"] = "persistent_post_witness"
            _set_statement_timeout(conn, DIAGNOSTIC_TIMEOUT_MS)
            post_snapshot = _persistent_snapshot(conn, prefix=prefix)
            evidence["persistent_state"].update(
                {"post": post_snapshot, "unchanged": post_snapshot == pre_snapshot}
            )

        with psycopg.connect(database_url, autocommit=True) as verify_conn:
            objects = [
                row[0]
                for row in verify_conn.execute(
                    "SELECT n.nspname||'.'||c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE c.relname LIKE 'admin_usage_ranked_%' "
                    "AND c.relpersistence<>'t' ORDER BY 1"
                ).fetchall()
            ]
            evidence["session_close"] = {"persistent_probe_objects": objects}
        evidence["phase"] = "complete"
        evidence["passed"] = _probe_passed(evidence)
    except Exception as exc:  # noqa: BLE001 - evidence must survive any gate failure
        evidence["failure"] = {
            "type": type(exc).__name__, "message": str(exc), "phase": evidence["phase"]
        }
    _write_json_atomic(output, evidence)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the non-persistent 3M ranked-flags checkpoint"
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
