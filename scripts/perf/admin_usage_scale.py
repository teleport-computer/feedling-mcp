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
import sys
import time
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo


DEFAULT_ROWS = 3_000_000
DEFAULT_USERS = 2_000
DEFAULT_RUNS = 5
DEFAULT_HISTORY_DAYS = 365
P95_BUDGET_MS = 2_000.0
ROLLUP_TABLES = (
    "v2_usage_daily_users",
    "v2_usage_daily_dimensions",
    "v2_usage_rollup_watermarks",
)
SHANGHAI = ZoneInfo("Asia/Shanghai")
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


def _production_window() -> tuple[datetime, datetime]:
    """Return 90 complete Shanghai days through 2026-08-01 inclusive."""

    end_local = datetime(2026, 8, 2, tzinfo=SHANGHAI)
    start_local = end_local - timedelta(days=90)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _validate_scale_database_url(database_url: str) -> dict[str, Any]:
    """Refuse remote/shared databases before any destructive scale-fixture work."""

    parsed = urlsplit(database_url)
    database = unquote(parsed.path.lstrip("/"))
    port = parsed.port or 5432
    safe = bool(
        parsed.scheme in {"postgres", "postgresql"}
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and port == 55432
        and database.startswith("feedling_usage_scale_")
        and database != "feedling_usage_scale_"
    )
    if not safe:
        raise SystemExit(
            "performance fixture requires a dedicated local PostgreSQL database: "
            "127.0.0.1:55432/feedling_usage_scale_<name>"
        )
    return {"database": database, "host": parsed.hostname, "port": port}


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


def _compact_plan_text(value: object, limit: int = 240) -> object:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _explain_statements(conn, statements):
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
                }
                or isinstance(index_name, str)
                and (
                    "v2_turn_metrics" in index_name
                    or "v2_usage_daily" in index_name
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
        key_nodes = key_nodes[:16]
        explained.append(
            {
                "label": _statement_label(sql),
                "execution_time_ms": round(float(document["Execution Time"]), 3),
                "planning_time_ms": round(float(document["Planning Time"]), 3),
                "root_node": root.get("Node Type"),
                "key_nodes": key_nodes,
            }
        )
    return sorted(explained, key=lambda item: item["execution_time_ms"], reverse=True)


def _seed_fixture(conn, *, prefix: str, rows: int, users: int, end_at, history_days: int):
    conn.execute(
        "INSERT INTO users (user_id,created_at,doc) "
        "SELECT %s || lpad(g::text, 6, '0'), %s, "
        "jsonb_build_object('scale_fixture', true) "
        "FROM generate_series(0, %s) AS g",
        (prefix, (end_at - timedelta(days=history_days)).isoformat(), users - 1),
    )
    insert_metrics_sql = (
        "INSERT INTO v2_turn_metrics "
        "(user_id,lane,provider,model,prompt_tokens,completion_tokens,latency_ms,"
        "model_calls,retries,failed,status,cache_read_tokens,cache_write_tokens,"
        "cache_miss_tokens,usage_reported_calls,cache_reported_calls,created_at) "
        "SELECT "
        "%s || lpad(((g-1) %% %s)::text, 6, '0'), "
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
    conn.execute("ANALYZE v2_turn_metrics")


def _delete_fixture(conn, prefix: str) -> None:
    conn.execute(
        "DELETE FROM users WHERE left(user_id, length(%s))=%s",
        (prefix, prefix),
    )
    conn.execute("ANALYZE v2_turn_metrics")


def _fixture_counts(conn, prefix: str) -> dict[str, int]:
    counts = {}
    for table in (
        "v2_turn_metrics",
        "v2_usage_daily_users",
        "v2_usage_daily_dimensions",
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
    counts["watermark"] = int(
        conn.execute(
            "SELECT count(*) FROM v2_usage_rollup_watermarks "
            "WHERE rollup_name='hosted_v2_usage'"
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
            "v2_usage_daily_users",
            "v2_usage_daily_dimensions",
            "v2_usage_rollup_watermarks",
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


def _assert_schema(conn) -> None:
    """Validate the pre-migrated test DB without mutating its Alembic state."""
    required_columns = {
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
    database_url = args.database_url.strip()
    if not database_url:
        raise SystemExit("pass an explicit --database-url for the dedicated scale DB")
    database_identity = _validate_scale_database_url(database_url)
    os.environ["DATABASE_URL"] = database_url
    os.environ["FEEDLING_V2_USAGE_ROLLUP_ENABLED"] = "1"
    repo = Path(__file__).resolve().parents[2]
    backend = str(repo / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)

    import db  # noqa: PLC0415
    from admin.usage import UsageQuery  # noqa: PLC0415
    from model_api_runtime.v2 import jobs_store, usage_rollup  # noqa: PLC0415

    pool = db.get_pool()
    with pool.connection() as conn:
        _assert_schema(conn)
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
    evidence: dict[str, Any] = {
        "database": database_identity,
        "fixture": {
            "rows": args.rows,
            "users": args.users,
            "history_days": args.history_days,
            "window_days": 90,
            "distribution": {
                "lanes": "chat 62%, heartbeat 15%, manual_wake 11%, maintenance 7%, scheduled 5%",
                "providers": "openrouter 68%, anthropic 20%, google 12%",
                "unknown_usage": "5%",
            },
        },
        "budget_ms": P95_BUDGET_MS,
        "query": {
            "timezone": "Asia/Shanghai",
            "start_at_utc": start_at,
            "end_at_utc": end_at,
            "half_open": True,
        },
        "cohorts": {},
    }
    try:
        with pool.connection() as conn:
            _seed_fixture(
                conn,
                prefix=prefix,
                rows=args.rows,
                users=args.users,
                end_at=end_at,
                history_days=args.history_days,
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
            }

        bootstrap_started = time.perf_counter()
        bootstrap_ticks = _bootstrap_rollups(usage_rollup)
        evidence["rollup_bootstrap"] = {
            "elapsed_ms": round((time.perf_counter() - bootstrap_started) * 1000, 3),
            "ticks": bootstrap_ticks,
        }
        with pool.connection() as conn:
            watermark = conn.execute(
                "SELECT bootstrap_complete,dirty_from_day,dirty_through_day,"
                "source_updated_at,source_id,source_observed_updated_at,"
                "source_lag_seconds,refreshed_at,last_success_at,last_error "
                "FROM v2_usage_rollup_watermarks "
                "WHERE rollup_name='hosted_v2_usage'"
            ).fetchone()
            if watermark is None or not watermark[0] or watermark[1] is not None:
                raise RuntimeError(f"production rollup watermark is not ready: {watermark}")
            evidence["rollup"] = {
                "tables": {
                    table: _relation_stats(conn, table)
                    for table in ("v2_usage_daily_users", "v2_usage_daily_dimensions")
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
            }

        for name, query in queries.items():
            report, statements = _capture_report(jobs_store, pool, query)
            coverage = report["coverage"]["rollup"]
            if coverage["mode"] != "hybrid-parallel" or not coverage["rollup_days"]:
                raise AssertionError(
                    f"report did not use production hybrid rollup path: {coverage}"
                )
            assert_content_free_metric_sql(statements)
            assert_metric_time_ranges(statements)
            timing = _measure_report(jobs_store, query, args.runs)
            with pool.connection() as conn:
                conn.execute("SET statement_timeout='120s'")
                explains = _explain_statements(conn, statements)
            if not explains:
                raise AssertionError("no production usage SQL was explained")
            exact_latency = next(
                (item for item in explains if item["label"] == "exact_latency"),
                None,
            )
            if exact_latency is None:
                raise AssertionError("exact latency SQL was not captured and explained")
            evidence["cohorts"][name] = {
                "timing": timing,
                "slowest_explain": explains[0],
                "exact_latency_explain": exact_latency,
                "all_explains": explains,
                "coverage": coverage,
                "content_free_metric_sql": True,
                "half_open_created_at_range": True,
            }
    finally:
        if not args.keep_data:
            with pool.connection() as conn:
                _delete_fixture(conn, prefix)
                after_cascade = _fixture_counts(conn, prefix)
                residual = {
                    key: value
                    for key, value in after_cascade.items()
                    if key != "watermark" and value
                }
                if residual:
                    raise RuntimeError(
                        f"fixture cleanup left source/rollup rows: {residual}"
                    )
                conn.execute(
                    "DELETE FROM v2_usage_rollup_watermarks "
                    "WHERE rollup_name='hosted_v2_usage'"
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

    evidence["passed"] = all(
        cohort["timing"]["p95_ms"] < P95_BUDGET_MS
        for cohort in evidence["cohorts"].values()
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
    parser.add_argument("--users", type=int, default=DEFAULT_USERS)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--history-days", type=int, default=DEFAULT_HISTORY_DAYS)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if args.self_test:
        return _self_test()
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
