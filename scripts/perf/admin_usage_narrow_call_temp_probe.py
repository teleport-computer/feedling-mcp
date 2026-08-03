#!/usr/bin/env python3
"""Non-persistent feasibility probe for narrow daily call dimensions."""

from __future__ import annotations

from decimal import Decimal
import re

from scripts.perf.admin_usage_ranked_flags_temp_probe import (
    FLAG_COLUMN_NAMES,
    _TOKEN_FIELDS,
    _attempt_scope_select,
    _gap_ctes,
    _ranked_rows_and_outputs,
    _ttft_scope_select,
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
