#!/usr/bin/env python3
"""Reproducible 10x-scale proof for the Admin Hosted V2 Usage report.

This harness is intentionally opt-in: it inserts millions of content-free rows
into an explicitly supplied PostgreSQL test database, benchmarks the real
``usage_report_snapshot()`` entry point, explains every SQL statement that the
entry point executes, prints JSON evidence, and removes only its own rows.
It never runs as part of the default pytest suite.
"""

from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from psycopg.conninfo import conninfo_to_dict


DEFAULT_ROWS = 3_000_000
DEFAULT_ATTEMPT_ROWS = 3_000_000
FORMAL_ROWS = 3_000_000
FORMAL_ATTEMPT_ROWS = 3_000_000
DEFAULT_USERS = 2_000
DEFAULT_RUNS = 5
DEFAULT_HISTORY_DAYS = 365
P95_BUDGET_MS = 3_000.0
ROLLUP_TABLES = (
    "v2_usage_daily_users",
    "v2_usage_daily_dimensions",
    "v2_usage_rollup_watermarks",
    "llm_usage_daily_attempt_dimensions",
    "llm_usage_daily_call_memberships",
    "llm_usage_rollup_dirty_days",
)
SHANGHAI = ZoneInfo("Asia/Shanghai")
SCALE_NOW_UTC = datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc)
_LOCAL_SCALE_HOST = "127.0.0.1"
_SENSITIVE_COLUMNS = frozenset(
    {
        "body_ct",
        "nonce",
        "content_envelope",
        "tool_input",
        "tool_output",
        "assistant_reply",
        "prompt_text",
        "message_content",
        "user_prompt",
    }
)


def _percentile_nearest_rank(samples: list[float], percentile: float) -> float:
    if not samples:
        raise ValueError("at least one timing sample is required")
    if not 0.0 < percentile <= 1.0:
        raise ValueError("percentile must be in (0, 1]")
    ordered = sorted(float(value) for value in samples)
    rank = max(0, min(math.ceil(percentile * len(ordered)) - 1, len(ordered) - 1))
    return ordered[rank]


def timing_summary(samples_ms: list[float]) -> dict[str, Any]:
    """Return auditable warmed samples plus nearest-rank p50/p95."""
    return {
        "samples_ms": [round(value, 3) for value in samples_ms],
        "p50_ms": round(_percentile_nearest_rank(samples_ms, 0.50), 3),
        "p95_ms": round(_percentile_nearest_rank(samples_ms, 0.95), 3),
    }


def _formal_gate_passed(
    cohorts: dict[str, Any],
    cleanup: dict[str, Any],
    *,
    source: dict[str, Any],
    fixture: dict[str, Any],
    formal: bool,
) -> bool:
    required = {"unfiltered", "provider_model_filtered"}
    cleanup_passed = bool(
        cleanup.get("foreign_key_cascade_verified") is True
        and cleanup.get("watermark_removed") is True
        and cleanup.get("residual_counts")
        and not any(cleanup["residual_counts"].values())
    )
    cardinality_passed = bool(
        formal
        and fixture.get("rows") == FORMAL_ROWS
        and fixture.get("attempt_rows") == FORMAL_ATTEMPT_ROWS
        and source.get("total_rows") == FORMAL_ROWS
        and source.get("attempt_rows") == FORMAL_ATTEMPT_ROWS
    )
    return cardinality_passed and cleanup_passed and set(cohorts) == required and all(
        cohort["timing"]["p95_ms"] < P95_BUDGET_MS
        and cohort["attempt_ledger_statement_count"] == 1
        and cohort["attempt_runtime_job_index_used"] is True
        and cohort["attempt_rollup_relations_used"] is True
        and cohort["attempt_full_history_scan_absent"] is True
        and cohort["attempt_rate_card_probe_loops_absent"] is True
        and cohort["attempt_full_window_call_probe_loops_absent"] is True
        for cohort in cohorts.values()
    )


def _retention_index_evidence_passed(evidence: dict[str, Any]) -> bool:
    """Require inspectable size/write-maintenance evidence from a formal run."""

    maintenance = evidence.get("maintenance") or {}
    numeric = (
        evidence.get("index_bytes"),
        evidence.get("attempt_rows"),
        evidence.get("bytes_per_attempt"),
        evidence.get("attempt_table_total_bytes"),
        evidence.get("index_share_of_attempt_total"),
        maintenance.get("idx_scan"),
        maintenance.get("idx_tup_read"),
        maintenance.get("idx_tup_fetch"),
    )
    return bool(
        evidence.get("present") is True
        and evidence.get("valid") is True
        and evidence.get("definition_exact") is True
        and all(isinstance(value, (int, float)) and value >= 0 for value in numeric)
        and int(evidence["attempt_rows"]) > 0
    )


def _business_path_evidence_passed(
    evidence: dict[str, Any], *, expected_commit: str
) -> bool:
    """Delegate to the producer's strict provenance/raw-sample validator."""

    try:
        from scripts.perf.provider_attempt_business_path import (  # noqa: PLC0415
            validate_business_path_evidence,
        )

        validate_business_path_evidence(evidence, expected_commit=expected_commit)
    except (ImportError, TypeError, ValueError):
        return False
    return True


def _current_git_commit(repo: Path) -> str:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if len(commit) != 40:
        raise RuntimeError("formal gate requires a full Git commit")
    return commit


def assert_content_free_metric_sql(statements: list[tuple[str, tuple[Any, ...]]]) -> None:
    """Reject content-bearing columns without requiring a raw-table branch."""
    reporting_sql = [
        sql
        for sql, _params in statements
        if "v2_turn_metrics" in sql or "v2_usage_daily_" in sql
    ]
    if not reporting_sql:
        raise AssertionError("no usage reporting statement was captured")
    for sql in reporting_sql:
        lowered = sql.lower()
        found = sorted(
            column
            for column in _SENSITIVE_COLUMNS
            if re.search(rf"\b{re.escape(column)}\b", lowered)
        )
        if found:
            raise AssertionError(f"sensitive usage columns referenced: {found}")


def assert_metric_time_ranges(statements: list[tuple[str, tuple[Any, ...]]]) -> None:
    """Every captured raw metric branch must retain half-open time bounds."""
    metric_sql = [sql for sql, _params in statements if "v2_turn_metrics" in sql]
    for sql in metric_sql:
        normalized = " ".join(sql.lower().split())
        direct_bounds = (
            "m.created_at >= %s" in normalized
            and "m.created_at < %s" in normalized
        )
        range_join = (
            "m.created_at >= rr.start_at" in normalized
            and "m.created_at < rr.end_at" in normalized
        )
        if not direct_bounds and not range_join:
            raise AssertionError("v2 metric statement lost its half-open created_at range")


def _production_window(
    now_utc: datetime = SCALE_NOW_UTC,
) -> tuple[datetime, datetime]:
    """Return the reproducible rolling 90-day preset at a non-midnight now."""

    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("scale gate now must be timezone-aware")
    end_at = now_utc.astimezone(timezone.utc)
    return end_at - timedelta(days=90), end_at


def _raw_edge_bounds(partition, *, start_at, end_at):
    bounds = []
    for local_day in partition.raw_days:
        day_start = datetime.combine(
            local_day, datetime.min.time(), tzinfo=SHANGHAI
        ).astimezone(timezone.utc)
        day_end = datetime.combine(
            local_day + timedelta(days=1),
            datetime.min.time(),
            tzinfo=SHANGHAI,
        ).astimezone(timezone.utc)
        bounds.append((max(day_start, start_at), min(day_end, end_at)))
    return tuple(bounds)


def _raw_edge_source_counts(conn, *, prefix, partition, start_at, end_at):
    bounds = _raw_edge_bounds(
        partition, start_at=start_at, end_at=end_at
    )
    if not bounds:
        return {"turn_rows": 0, "attempt_rows": 0, "logical_calls": 0}
    starts = [item[0] for item in bounds]
    ends = [item[1] for item in bounds]
    row = conn.execute(
        "WITH raw_ranges(start_at,end_at) AS ("
        " SELECT * FROM unnest(%s::timestamptz[],%s::timestamptz[])"
        "), raw_turns AS MATERIALIZED ("
        " SELECT m.job_id FROM raw_ranges r JOIN v2_turn_metrics m"
        " ON m.created_at>=r.start_at AND m.created_at<r.end_at"
        " WHERE left(m.user_id,length(%s))=%s"
        "), raw_attempts AS MATERIALIZED ("
        " SELECT a.call_id FROM raw_turns t JOIN llm_provider_attempts a"
        " ON a.job_id=t.job_id WHERE a.source='runtime_recorder'"
        ") SELECT (SELECT count(*) FROM raw_turns),"
        "(SELECT count(*) FROM raw_attempts),"
        "(SELECT count(DISTINCT call_id) FROM raw_attempts)",
        (starts, ends, prefix, prefix),
    ).fetchone()
    return {
        "turn_rows": int(row[0]),
        "attempt_rows": int(row[1]),
        "logical_calls": int(row[2]),
    }


def _resolve_attempt_rows(rows: int, requested: int | None) -> int:
    """Keep the legacy metric-only default; formal P0-B runs opt in explicitly."""

    attempt_rows = DEFAULT_ATTEMPT_ROWS if requested is None else requested
    if attempt_rows < 0 or attempt_rows > rows:
        raise SystemExit("attempt-rows must be between 0 and rows")
    return attempt_rows


def _validate_scale_database_url(database_url: str) -> dict[str, Any]:
    """Refuse remote/shared databases before any destructive scale-fixture work."""

    try:
        conninfo = conninfo_to_dict(database_url)
    except Exception as exc:
        raise SystemExit(
            "performance fixture requires a dedicated local PostgreSQL database: "
            "127.0.0.1:55432/feedling_usage_scale_<name>"
        ) from exc
    database = str(conninfo.get("dbname") or "")
    host = str(conninfo.get("host") or "")
    hostaddr = str(conninfo.get("hostaddr") or "")
    port_text = str(conninfo.get("port") or "")
    hosts = host.split(",") if host else []
    hostaddrs = hostaddr.split(",") if hostaddr else []
    ports = port_text.split(",") if port_text else []
    try:
        port = int(ports[0]) if len(ports) == 1 else 0
    except ValueError:
        port = 0
    safe = bool(
        not conninfo.get("service")
        and not conninfo.get("servicefile")
        and len(hosts) == 1
        and hosts[0] == _LOCAL_SCALE_HOST
        and (
            not hostaddrs
            or len(hostaddrs) == 1
            and hostaddrs[0] == _LOCAL_SCALE_HOST
        )
        and len(ports) == 1
        and port == 55432
        and database.startswith("feedling_usage_scale_")
        and database != "feedling_usage_scale_"
    )
    if not safe:
        raise SystemExit(
            "performance fixture requires a dedicated local PostgreSQL database: "
            "127.0.0.1:55432/feedling_usage_scale_<name>"
        )
    return {"database": database, "host": hosts[0], "port": port}


def _validate_connected_scale_database(conn, expected: dict[str, Any]) -> dict[str, Any]:
    """Recheck libpq's connected endpoint before schema checks or fixture writes."""

    actual_identity = {
        "database": str(conn.info.dbname or ""),
        "host": str(conn.info.host or ""),
        "port": int(conn.info.port or 0),
    }
    actual_hostaddr = str(getattr(conn.info, "hostaddr", "") or "")
    if actual_identity != expected or actual_hostaddr != _LOCAL_SCALE_HOST:
        raise RuntimeError(
            "connected PostgreSQL identity does not match the validated dedicated "
            f"scale database: expected={expected}, actual={actual_identity}, "
            f"actual_hostaddr={actual_hostaddr!r}"
        )
    return actual_identity


class _ValidatedScaleConnection(AbstractContextManager):
    """Validate a checked-out connection before exposing it to the harness."""

    def __init__(self, context, expected: dict[str, Any]):
        self._context = context
        self._expected = expected

    def __enter__(self):
        conn = self._context.__enter__()
        try:
            _validate_connected_scale_database(conn, self._expected)
        except BaseException:
            self._context.__exit__(*sys.exc_info())
            raise
        return conn

    def __exit__(self, *args):
        return self._context.__exit__(*args)


class _ValidatedScalePool:
    """Apply the destructive-harness identity gate to every pool checkout."""

    def __init__(self, pool, expected: dict[str, Any]):
        self._pool = pool
        self._expected = expected

    def __getattr__(self, name):
        return getattr(self._pool, name)

    def connection(self, *args, **kwargs):
        return _ValidatedScaleConnection(
            self._pool.connection(*args, **kwargs), self._expected
        )


def _install_validated_scale_pool(database_module, expected: dict[str, Any]):
    """Make every production-module checkout use the validated scale pool."""

    pool = _ValidatedScalePool(database_module.get_pool(), expected)

    def get_validated_pool():
        return pool

    database_module.get_pool = get_validated_pool
    return pool


def _validate_rolling_partition(partition) -> dict[str, list[str]]:
    """Require the real 90d preset shape: full interior days, two raw edges."""

    if partition is None:
        raise AssertionError(
            "rolling 90d gate requires 89 full rollup days and 2 partial raw days; "
            "got no rollup partition"
        )
    rollup_days = [day.isoformat() for day in partition.rollup_days]
    raw_days = [day.isoformat() for day in partition.raw_days]
    valid = bool(
        len(rollup_days) == 89
        and rollup_days[0] == "2026-05-05"
        and rollup_days[-1] == "2026-08-01"
        and raw_days == ["2026-05-04", "2026-08-02"]
    )
    if not valid:
        raise AssertionError(
            "rolling 90d gate requires 89 full rollup days and 2 partial raw "
            f"days; got rollup={rollup_days}, raw={raw_days}"
        )
    return {"rollup_days": rollup_days, "raw_days": raw_days}


def _self_test() -> int:
    good = [
        (
            "SELECT m.prompt_tokens FROM v2_turn_metrics m "
            "WHERE m.created_at >= %s AND m.created_at < %s",
            ("start", "end"),
        )
    ]
    assert_content_free_metric_sql(good)
    assert_metric_time_ranges(good)
    try:
        assert_content_free_metric_sql(
            [(good[0][0].replace("m.prompt_tokens", "m.body_ct"), good[0][1])]
        )
    except AssertionError:
        sensitive_check = "passed"
    else:
        raise AssertionError("sensitive-column mutation was not detected")
    try:
        assert_metric_time_ranges(
            [(good[0][0].replace("m.created_at < %s", "true"), good[0][1])]
        )
    except AssertionError:
        time_range_check = "passed"
    else:
        raise AssertionError("time-range mutation was not detected")
    print(
        json.dumps(
            {
                "p50_ms": timing_summary([10, 20, 30, 40, 50])["p50_ms"],
                "p95_ms": timing_summary([10, 20, 30, 40, 50])["p95_ms"],
                "sensitive_column_check": sensitive_check,
                "time_range_check": time_range_check,
            },
            sort_keys=True,
        )
    )
    return 0


class _CursorProxy(AbstractContextManager):
    def __init__(self, context, statements: list[tuple[str, tuple[Any, ...]]]):
        self._context = context
        self._cursor = None
        self._statements = statements

    def __enter__(self):
        self._cursor = self._context.__enter__()
        return self

    def __exit__(self, *args):
        return self._context.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def execute(self, query, params=None):
        text_query = str(query)
        normalized = text_query.lstrip().upper()
        if normalized.startswith(("SELECT", "WITH")):
            self._statements.append((text_query, tuple(params or ())))
        if params is None:
            return self._cursor.execute(query)
        return self._cursor.execute(query, params)


class _ConnectionProxy(AbstractContextManager):
    def __init__(self, context, statements: list[tuple[str, tuple[Any, ...]]]):
        self._context = context
        self._connection = None
        self._statements = statements

    def __enter__(self):
        self._connection = self._context.__enter__()
        return self

    def __exit__(self, *args):
        return self._context.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def cursor(self, *args, **kwargs):
        return _CursorProxy(
            self._connection.cursor(*args, **kwargs), self._statements
        )


class _CapturePool:
    def __init__(self, pool, statements: list[tuple[str, tuple[Any, ...]]]):
        self._pool = pool
        self._statements = statements

    def connection(self, *args, **kwargs):
        return _ConnectionProxy(
            self._pool.connection(*args, **kwargs), self._statements
        )


def _statement_label(sql: str) -> str:
    normalized = " ".join(sql.lower().split())
    if "llm_provider_attempts" in normalized:
        return "attempt_ledger"
    if "cross join lateral unnest(s.latency_samples)" in normalized:
        return "exact_latency"
    if "array_agg(distinct lane" in normalized:
        return "filter_options"
    if "group by provider,model" in normalized:
        return "provider_model"
    if "group by lane" in normalized:
        return "lane"
    if "known_user_days" in normalized:
        return "user_day_percentiles"
    if "provider_model_rank" in normalized:
        return "per_user"
    if "generate_series(" in normalized:
        return "daily"
    if "provider_model_rank AS" in sql:
        return "per_user"
    if "FROM base GROUP BY provider,model" in sql:
        return "provider_model"
    if "FROM base GROUP BY lane" in sql:
        return "lane"
    if "generate_series(" in sql:
        return "daily"
    if "known_user_days AS" in sql:
        return "user_day_percentiles"
    if "array_agg(DISTINCT" in sql:
        return "filter_options"
    if "WITH user_times AS" in sql:
        return "reference_cohort"
    if "AS model_active_users" in sql:
        return "overview"
    return "statement"


def _walk_plan(node: dict[str, Any]):
    yield node
    for child in node.get("Plans", []):
        yield from _walk_plan(child)


def _attempt_plan_guards(
    root: dict[str, Any],
    *,
    total_attempt_rows: int,
    expected_raw_edge_attempt_rows: int,
    expected_raw_edge_logical_calls: int,
) -> dict[str, Any]:
    """Derive formal guards from every node in the untruncated JSON plan."""

    nodes = list(_walk_plan(root))
    attempt_nodes = [
        node
        for node in nodes
        if node.get("Relation Name") == "llm_provider_attempts"
        or node.get("Index Name") in {
            "ix_llm_provider_attempts_runtime_job",
            "ix_llm_provider_attempts_call",
        }
    ]
    rate_nodes = [
        node for node in nodes
        if node.get("Relation Name") == "llm_rate_cards"
    ]
    call_nodes = [
        node for node in nodes
        if node.get("Index Name") == "ix_llm_provider_attempts_call"
    ]
    attempt_scan_rows = [
        int(node.get("Actual Rows") or 0)
        * int(node.get("Actual Loops") or 0)
        for node in attempt_nodes
        if node.get("Relation Name") == "llm_provider_attempts"
    ]
    call_node_loops = [
        int(node.get("Actual Loops") or 0) for node in call_nodes
    ]
    examined_attempt_rows = sum(attempt_scan_rows)
    max_single_attempt_scan_rows = max(attempt_scan_rows, default=0)
    call_probe_loops = sum(call_node_loops)
    max_single_call_probe_loops = max(call_node_loops, default=0)
    rate_probe_loops = sum(
        int(node.get("Actual Loops") or 0) for node in rate_nodes
    )
    edge_scan_limit = max(
        100,
        int(expected_raw_edge_attempt_rows) * 3,
    )
    call_probe_limit = max(
        10,
        int(expected_raw_edge_logical_calls) * 2,
    )
    single_attempt_scan_limit = max(
        1,
        int(expected_raw_edge_attempt_rows) * 3,
    )
    single_call_probe_loop_limit = max(
        1,
        int(expected_raw_edge_logical_calls) * 2,
    )
    near_full_attempt_scan_threshold = max(
        1,
        int(total_attempt_rows) - int(expected_raw_edge_attempt_rows),
    )
    near_full_attempt_scan_absent = bool(
        expected_raw_edge_attempt_rows >= total_attempt_rows
        or max_single_attempt_scan_rows < near_full_attempt_scan_threshold
    )
    rollup_relations = {
        str(node.get("Relation Name"))
        for node in nodes
        if node.get("Relation Name") in {
            "llm_usage_daily_attempt_dimensions",
            "llm_usage_daily_call_memberships",
        }
    }
    return {
        "complete_plan_node_count": len(nodes),
        "attempt_relation_scan_nodes": len(attempt_nodes),
        "rate_card_scan_nodes": len(rate_nodes),
        "call_probe_nodes": len(call_nodes),
        "examined_attempt_rows": examined_attempt_rows,
        "expected_raw_edge_attempt_rows": int(expected_raw_edge_attempt_rows),
        "attempt_edge_scan_limit": edge_scan_limit,
        "max_single_attempt_scan_rows": max_single_attempt_scan_rows,
        "single_attempt_scan_limit": single_attempt_scan_limit,
        "near_full_attempt_scan_threshold": near_full_attempt_scan_threshold,
        "near_full_attempt_scan_absent": near_full_attempt_scan_absent,
        "call_probe_loops": call_probe_loops,
        "expected_raw_edge_logical_calls": int(expected_raw_edge_logical_calls),
        "call_probe_loop_limit": call_probe_limit,
        "max_single_call_probe_loops": max_single_call_probe_loops,
        "single_call_probe_loop_limit": single_call_probe_loop_limit,
        "rate_card_probe_loops": rate_probe_loops,
        "attempt_runtime_job_index_used": any(
            node.get("Index Name") == "ix_llm_provider_attempts_runtime_job"
            for node in nodes
        ),
        "attempt_rollup_relations_used": rollup_relations == {
            "llm_usage_daily_attempt_dimensions",
            "llm_usage_daily_call_memberships",
        },
        "attempt_full_history_scan_absent": bool(
            total_attempt_rows > 0
            and examined_attempt_rows <= edge_scan_limit
            and max_single_attempt_scan_rows <= single_attempt_scan_limit
            and near_full_attempt_scan_absent
            and not any(
                node.get("Node Type") == "Seq Scan"
                and node.get("Relation Name") == "llm_provider_attempts"
                for node in nodes
            )
        ),
        "attempt_rate_card_probe_loops_absent": (
            not rate_nodes or rate_probe_loops <= 1
        ),
        "attempt_full_window_call_probe_loops_absent": (
            not call_nodes
            or (
                call_probe_loops <= call_probe_limit
                and max_single_call_probe_loops
                <= single_call_probe_loop_limit
            )
        ),
    }


def _compact_plan_text(value: object, limit: int = 240) -> object:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _explain_statements(conn, statements, *, attempt_guard_context=None):
    explained = []
    seen: set[str] = set()
    for sql, params in statements:
        if "v2_usage_daily_" not in sql and "v2_turn_metrics" not in sql:
            continue
        signature = f"{sql}\0{params!r}"
        if signature in seen:
            continue
        seen.add(signature)
        row = conn.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
            params,
        ).fetchone()
        document = row[0][0]
        root = document["Plan"]
        label = _statement_label(sql)
        attempt_guards = None
        if label == "attempt_ledger" and attempt_guard_context is not None:
            attempt_guards = _attempt_plan_guards(
                root, **attempt_guard_context
            )
        key_nodes = []
        for node in _walk_plan(root):
            relation = node.get("Relation Name")
            index_name = node.get("Index Name")
            node_type = str(node.get("Node Type") or "")
            usage_relation = bool(
                relation in {
                    "v2_turn_metrics",
                    "v2_usage_daily_users",
                    "v2_usage_daily_dimensions",
                    "llm_provider_attempts",
                    "llm_provider_attempt_corrections",
                    "llm_rate_cards",
                    "llm_usage_daily_attempt_dimensions",
                    "llm_usage_daily_call_memberships",
                }
                or isinstance(index_name, str)
                and (
                    "v2_turn_metrics" in index_name
                    or "v2_usage_daily" in index_name
                    or "llm_provider_attempt" in index_name
                )
            )
            structural = any(
                marker in node_type for marker in ("Aggregate", "Sort", "Gather")
            )
            if usage_relation or structural:
                key_nodes.append(
                    {
                        "node_type": node_type,
                        "relation": relation,
                        "index_name": index_name,
                        "actual_rows": node.get("Actual Rows"),
                        "actual_loops": node.get("Actual Loops"),
                        "index_cond": _compact_plan_text(node.get("Index Cond")),
                        "filter": _compact_plan_text(node.get("Filter")),
                        "shared_hit_blocks": node.get("Shared Hit Blocks", 0),
                        "shared_read_blocks": node.get("Shared Read Blocks", 0),
                        "temp_read_blocks": node.get("Temp Read Blocks", 0),
                        "temp_written_blocks": node.get("Temp Written Blocks", 0),
                        "sort_method": node.get("Sort Method"),
                        "sort_space_kb": node.get("Sort Space Used"),
                    }
                )
        key_nodes = key_nodes[:128]
        explained.append(
            {
                "label": label,
                "execution_time_ms": round(float(document["Execution Time"]), 3),
                "planning_time_ms": round(float(document["Planning Time"]), 3),
                "root_node": root.get("Node Type"),
                "key_nodes": key_nodes,
                "attempt_plan_guards": attempt_guards,
            }
        )
    return sorted(explained, key=lambda item: item["execution_time_ms"], reverse=True)


def _seed_fixture(
    conn,
    *,
    prefix: str,
    rows: int,
    attempt_rows: int,
    users: int,
    end_at,
    history_days: int,
):
    conn.execute(
        "INSERT INTO users (user_id,created_at,doc) "
        "SELECT %s || lpad(g::text, 6, '0'), %s, "
        "jsonb_build_object('scale_fixture', true) "
        "FROM generate_series(0, %s) AS g",
        (prefix, (end_at - timedelta(days=history_days)).isoformat(), users - 1),
    )
    insert_metrics_sql = (
        "INSERT INTO v2_turn_metrics "
        "(job_id,user_id,lane,provider,model,prompt_tokens,completion_tokens,latency_ms,"
        "model_calls,retries,failed,status,cache_read_tokens,cache_write_tokens,"
        "cache_miss_tokens,usage_reported_calls,cache_reported_calls,created_at) "
        "SELECT "
        "g,%s || lpad(((g-1) %% %s)::text, 6, '0'), "
        "CASE WHEN g %% 100 < 62 THEN 'chat' WHEN g %% 100 < 77 THEN 'heartbeat' "
        "WHEN g %% 100 < 88 THEN 'manual_wake' WHEN g %% 100 < 95 THEN 'maintenance' "
        "ELSE 'scheduled' END, "
        "CASE WHEN g %% 100 < 68 THEN 'openrouter' WHEN g %% 100 < 88 THEN 'anthropic' "
        "ELSE 'google' END, "
        "CASE WHEN g %% 100 < 68 THEN 'openai/gpt-4o-mini' "
        "WHEN g %% 100 < 88 THEN 'claude-3-5-haiku' ELSE 'gemini-2.5-flash' END, "
        "CASE WHEN g %% 20 = 0 THEN NULL ELSE 400 + (g %% 1600) END, "
        "CASE WHEN g %% 20 = 0 THEN NULL ELSE 40 + (g %% 360) END, "
        "100 + (g %% 15000), 1 + (g %% 2), "
        "CASE WHEN g %% 20 = 0 THEN 1 ELSE 0 END, (g %% 97 = 0), 'scale-ok', "
        "CASE WHEN g %% 4 = 0 THEN 100 + (g %% 900) ELSE 0 END, "
        "CASE WHEN g %% 10 = 0 THEN 20 + (g %% 80) ELSE 0 END, "
        "CASE WHEN g %% 4 = 0 THEN 50 + (g %% 450) ELSE 0 END, "
        "CASE WHEN g %% 20 = 0 THEN 0 ELSE 1 + (g %% 2) END, "
        "CASE WHEN g %% 4 = 0 THEN 1 + (g %% 2) ELSE 0 END, "
        "%s::timestamptz - make_interval(secs => ((g::bigint * 104729) %% %s)::double precision) "
        "FROM generate_series(%s::bigint, %s::bigint) AS g"
    )
    # Bounded batches prevent the local PostgreSQL process and client from
    # holding a single 3M-row statement's executor state at once.  The fixture
    # remains deterministic because g is global across batches.
    for first_row in range(1, rows + 1, 100_000):
        last_row = min(first_row + 100_000 - 1, rows)
        conn.execute(
            insert_metrics_sql,
            (
                prefix,
                users,
                end_at,
                history_days * 86400,
                first_row,
                last_row,
            ),
        )
    insert_attempts_sql = (
        "WITH fixture AS (SELECT g,%s || lpad(((g-1) %% %s)::text,6,'0') "
        "AS user_id,%s::timestamptz-make_interval(secs => "
        "((g::bigint*104729) %% %s)::double precision) AS started_at "
        "FROM generate_series(%s::bigint,%s::bigint) AS g), shaped AS ("
        "SELECT *,md5(%s||g::text) AS digest FROM fixture) "
        "INSERT INTO llm_provider_attempts (attempt_id,user_id,lane,job_id,call_id,"
        "outer_attempt_ordinal,inner_attempt_ordinal,retry_kind,requested_provider,"
        "resolved_provider,requested_model,resolved_model,transport,started_at,"
        "finished_at,state,outcome,error_class,input_tokens,output_tokens,"
        "cache_read_tokens,cache_write_tokens,cache_miss_tokens,usage_known,"
        "possibly_billed,source,completeness,revision) SELECT "
        "substr(digest,1,8)||'-'||substr(digest,9,4)||'-'||substr(digest,13,4)||'-'||"
        "substr(digest,17,4)||'-'||substr(digest,21,12),user_id,"
        "CASE WHEN g %% 100 < 62 THEN 'chat' WHEN g %% 100 < 77 THEN 'heartbeat' "
        "WHEN g %% 100 < 88 THEN 'manual_wake' WHEN g %% 100 < 95 THEN 'maintenance' "
        "ELSE 'scheduled' END,g,%s||'call-'||g,1,1,'initial',"
        "CASE WHEN g %% 100 < 68 THEN 'openrouter' WHEN g %% 100 < 88 THEN 'anthropic' "
        "ELSE 'google' END,"
        "CASE WHEN g %% 100 < 68 THEN 'openrouter' WHEN g %% 100 < 88 THEN 'anthropic' "
        "ELSE 'google' END,"
        "CASE WHEN g %% 100 < 68 THEN 'openai/gpt-4o-mini' "
        "WHEN g %% 100 < 88 THEN 'claude-3-5-haiku' ELSE 'gemini-2.5-flash' END,"
        "CASE WHEN g %% 100 < 68 THEN 'openai/gpt-4o-mini' "
        "WHEN g %% 100 < 88 THEN 'claude-3-5-haiku' ELSE 'gemini-2.5-flash' END,"
        "'openai_responses',started_at,started_at+interval '100 milliseconds',"
        "'completed','succeeded','none',400+(g %% 1600),40+(g %% 360),"
        "CASE WHEN g %% 4=0 THEN 100+(g %% 900) ELSE 0 END,"
        "CASE WHEN g %% 10=0 THEN 20+(g %% 80) ELSE 0 END,"
        "CASE WHEN g %% 4=0 THEN 50+(g %% 450) ELSE 0 END,true,false,"
        "'runtime_recorder','complete',2 FROM shaped"
    )
    for first_row in range(1, attempt_rows + 1, 100_000):
        last_row = min(first_row + 100_000 - 1, attempt_rows)
        conn.execute(
            insert_attempts_sql,
            (
                prefix,
                users,
                end_at,
                history_days * 86400,
                first_row,
                last_row,
                prefix,
                prefix,
            ),
        )
    conn.execute("ANALYZE v2_turn_metrics")
    if attempt_rows:
        conn.execute("ANALYZE llm_provider_attempts")


def _delete_fixture(conn, prefix: str) -> None:
    conn.execute(
        "DELETE FROM users WHERE left(user_id, length(%s))=%s",
        (prefix, prefix),
    )
    conn.execute("ANALYZE v2_turn_metrics")
    conn.execute("ANALYZE llm_provider_attempts")


def _fixture_counts(conn, prefix: str) -> dict[str, int]:
    counts = {}
    for table in (
        "v2_turn_metrics",
        "llm_provider_attempts",
        "llm_provider_attempt_corrections",
        "v2_usage_daily_users",
        "v2_usage_daily_dimensions",
        "llm_usage_daily_attempt_dimensions",
        "llm_usage_daily_call_memberships",
    ):
        counts[table] = int(
            conn.execute(
                f"SELECT count(*) FROM {table} WHERE user_id LIKE %s",  # noqa: S608
                (prefix + "%",),
            ).fetchone()[0]
        )
    counts["users"] = int(
        conn.execute(
            "SELECT count(*) FROM users WHERE user_id LIKE %s", (prefix + "%",)
        ).fetchone()[0]
    )
    counts["turn_watermark"] = int(
        conn.execute(
            "SELECT count(*) FROM v2_usage_rollup_watermarks "
            "WHERE rollup_name='hosted_v2_usage'"
        ).fetchone()[0]
    )
    counts["attempt_watermark"] = int(
        conn.execute(
            "SELECT count(*) FROM llm_usage_rollup_watermarks "
            "WHERE rollup_name='hosted_v2_attempt_usage'"
        ).fetchone()[0]
    )
    counts["dirty_days"] = int(
        conn.execute(
            "SELECT count(*) FROM llm_usage_rollup_dirty_days "
            "WHERE rollup_name='hosted_v2_attempt_usage'"
        ).fetchone()[0]
    )
    return counts


def _assert_empty_dedicated_database(conn) -> None:
    occupied = {
        table: int(
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
        )
        for table in (
            "v2_turn_metrics",
            "llm_provider_attempts",
            "llm_provider_attempt_corrections",
            "v2_usage_daily_users",
            "v2_usage_daily_dimensions",
            "v2_usage_rollup_watermarks",
            "llm_usage_daily_attempt_dimensions",
            "llm_usage_daily_call_memberships",
            "llm_usage_rollup_dirty_days",
        )
    }
    if any(occupied.values()):
        raise RuntimeError(
            "dedicated scale database is not empty; refusing to disturb existing "
            f"source/rollup state: {occupied}"
        )


def _relation_stats(conn, table: str) -> dict[str, int]:
    row = conn.execute(
        "SELECT count(*)::bigint,pg_relation_size(%s::regclass)::bigint,"
        "pg_indexes_size(%s::regclass)::bigint,"
        "pg_total_relation_size(%s::regclass)::bigint FROM " + table,
        (table, table, table),
    ).fetchone()
    return {
        "rows": int(row[0]),
        "relation_bytes": int(row[1]),
        "index_bytes": int(row[2]),
        "total_bytes": int(row[3]),
    }


def _retention_index_evidence(conn, *, attempt_rows: int) -> dict[str, Any]:
    name = "ix_llm_provider_attempts_retention_started"
    row = conn.execute(
        "SELECT idx.indisvalid,pg_get_indexdef(idx.indexrelid),"
        "pg_get_expr(idx.indpred,idx.indrelid,true),"
        "pg_relation_size(idx.indexrelid)::bigint,"
        "pg_total_relation_size(idx.indrelid)::bigint,"
        "coalesce(stats.idx_scan,0)::bigint,"
        "coalesce(stats.idx_tup_read,0)::bigint,"
        "coalesce(stats.idx_tup_fetch,0)::bigint "
        "FROM pg_index idx LEFT JOIN pg_stat_user_indexes stats "
        "ON stats.indexrelid=idx.indexrelid "
        "WHERE idx.indexrelid=to_regclass(%s)",
        (name,),
    ).fetchone()
    if row is None:
        return {"present": False}
    index_bytes = int(row[3])
    table_total_bytes = int(row[4])
    definition_exact = bool(
        "USING btree (started_at, attempt_id) INCLUDE (job_id)" in row[1]
        and row[2] == "source = 'runtime_recorder'::text"
    )
    return {
        "name": name,
        "present": True,
        "valid": bool(row[0]),
        "definition_exact": definition_exact,
        "definition": row[1],
        "predicate": row[2],
        "index_bytes": index_bytes,
        "attempt_rows": int(attempt_rows),
        "bytes_per_attempt": index_bytes / attempt_rows if attempt_rows else None,
        "attempt_table_total_bytes": table_total_bytes,
        "index_share_of_attempt_total": (
            index_bytes / table_total_bytes if table_total_bytes else 0.0
        ),
        "maintenance": {
            "idx_scan": int(row[5]),
            "idx_tup_read": int(row[6]),
            "idx_tup_fetch": int(row[7]),
        },
    }


def _assert_schema(conn) -> dict[str, Any]:
    """Validate the pre-migrated test DB without mutating its Alembic state."""
    required_columns = {
        "job_id",
        "user_id",
        "lane",
        "provider",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
        "model_calls",
        "retries",
        "failed",
        "status",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_miss_tokens",
        "usage_reported_calls",
        "cache_reported_calls",
        "created_at",
    }
    columns = {
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='v2_turn_metrics'"
        ).fetchall()
    }
    missing = sorted(required_columns - columns)
    if missing:
        raise RuntimeError(f"test database is not migrated for P0-A: {missing}")
    created_at_index = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE schemaname=current_schema() "
        "AND tablename='v2_turn_metrics' AND indexdef ILIKE '%(created_at DESC)%'"
    ).fetchone()
    if created_at_index is None:
        raise RuntimeError("test database lacks the v2_turn_metrics created_at index")
    present = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_name=ANY(%s)",
            (list(ROLLUP_TABLES),),
        ).fetchall()
    }
    missing_tables = sorted(set(ROLLUP_TABLES) - present)
    if missing_tables:
        raise RuntimeError(
            f"test database lacks production rollup schema: {missing_tables}"
        )
    attempt_columns = {
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() "
            "AND table_name='llm_provider_attempts'"
        ).fetchall()
    }
    required_attempt_columns = {
        "attempt_id",
        "user_id",
        "job_id",
        "call_id",
        "resolved_provider",
        "resolved_model",
        "started_at",
        "source",
    }
    missing_attempt_columns = sorted(required_attempt_columns - attempt_columns)
    if missing_attempt_columns:
        raise RuntimeError(
            "test database lacks provider-attempt schema: "
            f"{missing_attempt_columns}"
        )
    runtime_job_index = conn.execute(
        "SELECT idx.indisvalid,pg_get_indexdef(idx.indexrelid),"
        "pg_get_expr(idx.indpred,idx.indrelid,true) FROM pg_index idx WHERE "
        "idx.indexrelid=to_regclass('ix_llm_provider_attempts_runtime_job')"
    ).fetchone()
    if (
        runtime_job_index is None
        or runtime_job_index[0] is not True
        or "USING btree (job_id)" not in runtime_job_index[1]
        or runtime_job_index[2]
        != "source = 'runtime_recorder'::text AND job_id IS NOT NULL"
    ):
        raise RuntimeError(
            "test database lacks the exact runtime provider-attempt job index"
        )
    retention_index = conn.execute(
        "SELECT idx.indisvalid,pg_get_indexdef(idx.indexrelid),"
        "pg_get_expr(idx.indpred,idx.indrelid,true) FROM pg_index idx WHERE "
        "idx.indexrelid=to_regclass('ix_llm_provider_attempts_retention_started')"
    ).fetchone()
    if (
        retention_index is None
        or retention_index[0] is not True
        or "USING btree (started_at, attempt_id) INCLUDE (job_id)"
        not in retention_index[1]
        or retention_index[2] != "source = 'runtime_recorder'::text"
    ):
        raise RuntimeError(
            "test database lacks the exact provider-attempt retention index"
        )
    return {
        "runtime_job_index_present": True,
        "runtime_job_index_valid": True,
        "runtime_job_index_definition": runtime_job_index[1],
        "retention_index_present": True,
        "retention_index_valid": True,
        "retention_index_definition": retention_index[1],
        "retention_index_predicate": retention_index[2],
    }


def _bootstrap_rollups(usage_rollup, *, max_ticks: int = 20) -> list[dict[str, Any]]:
    ticks = []
    for _ in range(max_ticks):
        result = usage_rollup.run_maintenance_tick(
            max_days=31,
            max_changed_rows=100_000,
            overlap_seconds=0,
            statement_timeout_ms=120_000,
            pool_timeout_seconds=5,
            now_utc=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        ticks.append(result)
        if result.get("status") != "ok":
            raise RuntimeError(f"production usage rollup bootstrap failed: {result}")
        if result.get("bootstrap_complete") and not result.get("dirty_from_day"):
            return ticks
    raise RuntimeError(f"production usage rollup bootstrap exceeded {max_ticks} ticks")


def _bootstrap_attempt_rollups(provider_attempt_rollup, *, max_ticks: int = 100):
    ticks = []
    for _ in range(max_ticks):
        result = provider_attempt_rollup.run_maintenance_tick(
            max_days=31,
            max_changed_rows=100_000,
            max_dirty_days=2_000,
            max_stale_rows=0,
            statement_timeout_ms=120_000,
            pool_timeout_seconds=5,
            now_utc=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        ticks.append(result)
        if result.get("status") != "ok":
            raise RuntimeError(
                f"production attempt rollup bootstrap failed: {result}"
            )
        if result.get("bootstrap_complete") and not result.get("dirty_pending"):
            return ticks
    raise RuntimeError(
        f"production attempt rollup bootstrap exceeded {max_ticks} ticks"
    )


def _capture_report(jobs_store, real_pool, query):
    statements: list[tuple[str, tuple[Any, ...]]] = []
    original_pool = jobs_store._pool
    jobs_store._pool = lambda: _CapturePool(real_pool, statements)
    try:
        report = jobs_store.usage_report_snapshot(query)
    finally:
        jobs_store._pool = original_pool
    return report, statements


def _measure_report(jobs_store, query, runs: int) -> dict[str, Any]:
    jobs_store.usage_report_snapshot(query)  # explicit warm-up, never measured
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        jobs_store.usage_report_snapshot(query)
        samples.append((time.perf_counter() - started) * 1000.0)
    return timing_summary(samples)


def _run(args) -> int:
    if args.rows < 1 or args.users < 1 or args.runs < 5 or args.history_days < 90:
        raise SystemExit("rows/users must be positive, runs >= 5, history-days >= 90")
    attempt_rows = _resolve_attempt_rows(args.rows, args.attempt_rows)
    formal = not args.non_formal
    if formal and (
        args.rows != FORMAL_ROWS or attempt_rows != FORMAL_ATTEMPT_ROWS
    ):
        raise SystemExit(
            "formal mode requires exactly --rows 3000000 --attempt-rows 3000000; "
            "use --non-formal for a probe that can never pass"
        )
    database_url = args.database_url.strip()
    if not database_url:
        raise SystemExit("pass an explicit --database-url for the dedicated scale DB")
    database_identity = _validate_scale_database_url(database_url)
    os.environ["DATABASE_URL"] = database_url
    os.environ["FEEDLING_V2_USAGE_ROLLUP_ENABLED"] = "1"
    repo = Path(__file__).resolve().parents[2]
    repo_path = str(repo)
    if repo_path not in sys.path:
        sys.path.insert(0, repo_path)
    backend = str(repo / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)

    from scripts.perf.provider_attempt_business_path import (  # noqa: PLC0415
        measure_pool_contention_evidence,
        produce_business_path_evidence,
    )

    import db  # noqa: PLC0415
    from admin.usage import UsageQuery  # noqa: PLC0415
    from model_api_runtime.v2 import (  # noqa: PLC0415
        jobs_store,
        provider_attempt_rollup,
        usage_reporting,
        usage_rollup,
    )

    pool = _install_validated_scale_pool(db, database_identity)
    with pool.connection() as conn:
        _validate_connected_scale_database(conn, database_identity)
        schema_evidence = _assert_schema(conn)
        _assert_empty_dedicated_database(conn)
    prefix = f"scale_usage_{uuid4().hex[:10]}_"
    start_at, end_at = _production_window()
    queries = {
        "unfiltered": UsageQuery(
            start_at_utc=start_at,
            end_at_utc=end_at,
            timezone="Asia/Shanghai",
            preset="90d",
        ),
        "provider_model_filtered": UsageQuery(
            start_at_utc=start_at,
            end_at_utc=end_at,
            timezone="Asia/Shanghai",
            provider="openrouter",
            model="openai/gpt-4o-mini",
            preset="90d",
        ),
    }
    pool_evidence = measure_pool_contention_evidence(
        real_pool=pool,
        db_module=db,
        jobs_store=jobs_store,
        provider_attempt_rollup=provider_attempt_rollup,
        usage_query=queries["unfiltered"],
    )
    business_path_evidence = produce_business_path_evidence(
        repo=repo, pool_evidence=pool_evidence
    )
    if args.business_path_output:
        Path(args.business_path_output).write_text(
            json.dumps(business_path_evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    expected_partition = _validate_rolling_partition(
        usage_reporting.rollup_partition(queries["unfiltered"])
    )
    evidence: dict[str, Any] = {
        "database": {
            **database_identity,
            "precondition": args.precondition_note or None,
        },
        "fixture": {
            "formal": formal,
            "prefix": prefix,
            "rows": args.rows,
            "attempt_rows": attempt_rows,
            "users": args.users,
            "history_days": args.history_days,
            "window_days": 90,
            "distribution": {
                "lanes": "chat 62%, heartbeat 15%, manual_wake 11%, maintenance 7%, scheduled 5%",
                "providers": "openrouter 68%, anthropic 20%, google 12%",
                "unknown_usage": "5%",
                "provider_attempts": (
                    "explicit deterministic one-to-one prefix of metric rows; "
                    "zero corrections"
                ),
            },
        },
        "schema": schema_evidence,
        "business_path": business_path_evidence,
        "budget_ms": P95_BUDGET_MS,
        "query": {
            "timezone": "Asia/Shanghai",
            "start_at_utc": start_at,
            "end_at_utc": end_at,
            "half_open": True,
            "preset": "90d",
            "fixed_now_at_utc": SCALE_NOW_UTC,
            "expected_partition": expected_partition,
        },
        "cohorts": {},
    }
    try:
        with pool.connection() as conn:
            _seed_fixture(
                conn,
                prefix=prefix,
                rows=args.rows,
                attempt_rows=attempt_rows,
                users=args.users,
                end_at=end_at,
                history_days=args.history_days,
            )
            raw_edge_counts = _raw_edge_source_counts(
                conn,
                prefix=prefix,
                partition=usage_reporting.rollup_partition(
                    queries["unfiltered"]
                ),
                start_at=start_at,
                end_at=end_at,
            )
            evidence["source"] = {
                "total_rows": int(
                    conn.execute(
                        "SELECT count(*) FROM v2_turn_metrics WHERE user_id LIKE %s",
                        (prefix + "%",),
                    ).fetchone()[0]
                ),
                "rows_in_90d": int(
                    conn.execute(
                        "SELECT count(*) FROM v2_turn_metrics WHERE user_id LIKE %s "
                        "AND created_at >= %s AND created_at < %s",
                        (prefix + "%", start_at, end_at),
                    ).fetchone()[0]
                ),
                "attempt_rows": int(
                    conn.execute(
                        "SELECT count(*) FROM llm_provider_attempts "
                        "WHERE user_id LIKE %s",
                        (prefix + "%",),
                    ).fetchone()[0]
                ),
                "attempt_rows_in_90d_job_cohort": int(
                    conn.execute(
                        "SELECT count(*) FROM llm_provider_attempts a "
                        "JOIN v2_turn_metrics m ON m.job_id=a.job_id "
                        "WHERE a.user_id LIKE %s AND a.source='runtime_recorder' "
                        "AND m.created_at>=%s AND m.created_at<%s",
                        (prefix + "%", start_at, end_at),
                    ).fetchone()[0]
                ),
                "expected_raw_edge_turn_rows": raw_edge_counts["turn_rows"],
                "expected_raw_edge_attempt_rows": raw_edge_counts["attempt_rows"],
                "expected_raw_edge_logical_calls": raw_edge_counts["logical_calls"],
            }

        bootstrap_started = time.perf_counter()
        bootstrap_ticks = _bootstrap_rollups(usage_rollup)
        evidence["rollup_bootstrap"] = {
            "elapsed_ms": round((time.perf_counter() - bootstrap_started) * 1000, 3),
            "ticks": bootstrap_ticks,
        }
        attempt_bootstrap_started = time.perf_counter()
        attempt_bootstrap_ticks = _bootstrap_attempt_rollups(
            provider_attempt_rollup
        )
        evidence["attempt_rollup_bootstrap"] = {
            "elapsed_ms": round(
                (time.perf_counter() - attempt_bootstrap_started) * 1000, 3
            ),
            "ticks": attempt_bootstrap_ticks,
        }
        with pool.connection() as conn:
            evidence["retention_index"] = _retention_index_evidence(
                conn, attempt_rows=evidence["source"]["attempt_rows"]
            )
            watermark = conn.execute(
                "SELECT bootstrap_complete,dirty_from_day,dirty_through_day,"
                "source_updated_at,source_id,source_observed_updated_at,"
                "source_lag_seconds,refreshed_at,last_success_at,last_error "
                "FROM v2_usage_rollup_watermarks "
                "WHERE rollup_name='hosted_v2_usage'"
            ).fetchone()
            if watermark is None or not watermark[0] or watermark[1] is not None:
                raise RuntimeError(f"production rollup watermark is not ready: {watermark}")
            attempt_watermark = conn.execute(
                "SELECT bootstrap_complete,completed_through_day,retained_from,"
                "retention_pending_from "
                "FROM llm_usage_rollup_watermarks "
                "WHERE rollup_name='hosted_v2_attempt_usage'"
            ).fetchone()
            if attempt_watermark is None or not attempt_watermark[0]:
                raise RuntimeError(
                    "production attempt rollup watermark is not ready: "
                    f"{attempt_watermark}"
                )
            evidence["rollup"] = {
                "tables": {
                    table: _relation_stats(conn, table)
                    for table in (
                        "v2_usage_daily_users",
                        "v2_usage_daily_dimensions",
                        "llm_provider_attempts",
                        "llm_usage_daily_attempt_dimensions",
                        "llm_usage_daily_call_memberships",
                    )
                },
                "watermark": {
                    "bootstrap_complete": bool(watermark[0]),
                    "dirty_from_day": watermark[1],
                    "dirty_through_day": watermark[2],
                    "source_updated_at": watermark[3],
                    "source_id": int(watermark[4]),
                    "source_observed_updated_at": watermark[5],
                    "source_lag_seconds": watermark[6],
                    "refreshed_at": watermark[7],
                    "last_success_at": watermark[8],
                    "last_error": watermark[9],
                },
                "attempt_watermark": {
                    "bootstrap_complete": bool(attempt_watermark[0]),
                    "completed_through_day": attempt_watermark[1],
                    "retained_from": attempt_watermark[2],
                    "retention_pending_from": attempt_watermark[3],
                },
            }

        for name, query in queries.items():
            report, statements = _capture_report(jobs_store, pool, query)
            coverage = report["coverage"]["rollup"]
            if coverage["mode"] != "hybrid-parallel" or not coverage["rollup_days"]:
                raise AssertionError(
                    f"report did not use production hybrid rollup path: {coverage}"
                )
            raw_sql_omitted = not any(
                "v2_turn_metrics" in sql and "llm_provider_attempts" not in sql
                for sql, _params in statements
            )
            actual_partition = {
                "rollup_days": coverage["rollup_days"],
                "raw_days": coverage["raw_days"],
            }
            if actual_partition != expected_partition:
                raise AssertionError(
                    "production report did not use the expected rolling partition: "
                    f"expected={expected_partition}, actual={actual_partition}"
                )
            raw_sql_present = not raw_sql_omitted
            if raw_sql_present != bool(expected_partition["raw_days"]):
                raise AssertionError(
                    "raw metric SQL presence did not match rolling partial days: "
                    f"raw_sql_present={raw_sql_present}, partition={actual_partition}"
                )
            assert_content_free_metric_sql(statements)
            assert_metric_time_ranges(statements)
            attempt_statements = [
                sql for sql, _params in statements
                if "llm_provider_attempts" in sql
            ]
            if len(attempt_statements) != 1:
                raise AssertionError(
                    "usage report must execute exactly one attempt-ledger statement; "
                    f"captured={len(attempt_statements)}"
                )
            timing = _measure_report(jobs_store, query, args.runs)
            with pool.connection() as conn:
                conn.execute("SET statement_timeout='120s'")
                explains = _explain_statements(
                    conn,
                    statements,
                    attempt_guard_context={
                        "total_attempt_rows": evidence["source"]["attempt_rows"],
                        "expected_raw_edge_attempt_rows": evidence["source"][
                            "expected_raw_edge_attempt_rows"
                        ],
                        "expected_raw_edge_logical_calls": evidence["source"][
                            "expected_raw_edge_logical_calls"
                        ],
                    },
                )
            if not explains:
                raise AssertionError("no production usage SQL was explained")
            exact_latency = next(
                (item for item in explains if item["label"] == "exact_latency"),
                None,
            )
            if exact_latency is None:
                raise AssertionError("exact latency SQL was not captured and explained")
            attempt_explain = next(
                (item for item in explains if item["label"] == "attempt_ledger"),
                None,
            )
            if attempt_explain is None:
                raise AssertionError(
                    "provider-attempt ledger SQL was not captured and explained"
                )
            attempt_guards = attempt_explain.get("attempt_plan_guards")
            if attempt_guards is None:
                raise AssertionError(
                    "attempt EXPLAIN did not produce complete-plan guards"
                )
            evidence["cohorts"][name] = {
                "timing": timing,
                "slowest_explain": explains[0],
                "exact_latency_explain": exact_latency,
                "attempt_ledger_explain": attempt_explain,
                "all_explains": explains,
                "coverage": coverage,
                "content_free_metric_sql": True,
                "half_open_created_at_range": True,
                "rollup_only_omits_raw_table": raw_sql_omitted,
                "raw_table_sql_present": raw_sql_present,
                "raw_table_branch_matches_partition": True,
                "attempt_ledger_statement_count": len(attempt_statements),
                "attempt_runtime_job_index_used": attempt_guards[
                    "attempt_runtime_job_index_used"
                ],
                "attempt_rollup_relations_used": attempt_guards[
                    "attempt_rollup_relations_used"
                ],
                "attempt_full_history_scan_absent": attempt_guards[
                    "attempt_full_history_scan_absent"
                ],
                "attempt_rate_card_probe_loops_absent": attempt_guards[
                    "attempt_rate_card_probe_loops_absent"
                ],
                "attempt_full_window_call_probe_loops_absent": attempt_guards[
                    "attempt_full_window_call_probe_loops_absent"
                ],
                "attempt_plan_guards": attempt_guards,
                "attempt_relation_scan_nodes": [
                    {
                        "node_type": node.get("node_type"),
                        "index_name": node.get("index_name"),
                    }
                    for node in attempt_explain["key_nodes"]
                    if node.get("relation") == "llm_provider_attempts"
                ],
            }
    finally:
        if not args.keep_data:
            with pool.connection() as conn:
                _delete_fixture(conn, prefix)
                after_cascade = _fixture_counts(conn, prefix)
                residual = {
                    key: value
                    for key, value in after_cascade.items()
                    if key not in {"turn_watermark", "attempt_watermark"} and value
                }
                if residual:
                    raise RuntimeError(
                        f"fixture cleanup left source/rollup rows: {residual}"
                    )
                conn.execute(
                    "DELETE FROM v2_usage_rollup_watermarks "
                    "WHERE rollup_name='hosted_v2_usage'"
                )
                conn.execute(
                    "DELETE FROM llm_usage_rollup_watermarks "
                    "WHERE rollup_name='hosted_v2_attempt_usage'"
                )
                conn.execute(
                    "DELETE FROM llm_usage_rollup_dirty_days "
                    "WHERE rollup_name='hosted_v2_attempt_usage'"
                )
                after_cleanup = _fixture_counts(conn, prefix)
                if any(after_cleanup.values()):
                    raise RuntimeError(
                        f"fixture cleanup left residual state: {after_cleanup}"
                    )
                evidence["cleanup"] = {
                    "foreign_key_cascade_verified": True,
                    "watermark_removed": True,
                    "residual_counts": after_cleanup,
                }

    evidence["passed"] = _formal_gate_passed(
        evidence["cohorts"],
        evidence["cleanup"],
        source=evidence.get("source") or {},
        fixture=evidence["fixture"],
        formal=formal,
    ) and _retention_index_evidence_passed(
        evidence["retention_index"]
    ) and _business_path_evidence_passed(
        evidence["business_path"], expected_commit=_current_git_commit(repo)
    )
    rendered = json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if evidence["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--attempt-rows", type=int, default=None)
    parser.add_argument("--users", type=int, default=DEFAULT_USERS)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--history-days", type=int, default=DEFAULT_HISTORY_DAYS)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument("--precondition-note", default="")
    parser.add_argument(
        "--non-formal",
        action="store_true",
        help="run a small probe; evidence is permanently ineligible to pass",
    )
    parser.add_argument("--business-path-output", default="")
    args = parser.parse_args()
    if args.self_test:
        return _self_test()
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
