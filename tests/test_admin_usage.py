"""Admin Usage query normalization and canonical link behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
import importlib.util
import json
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
from psycopg.types.json import Jsonb


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track as _data_track  # noqa: E402
from admin import admin_core as _admin_core  # noqa: E402
from admin import usage as _usage  # noqa: E402
from accounts import registry  # noqa: E402
from core import reqctx  # noqa: E402
import db  # noqa: E402
from model_api_runtime.v2 import jobs_store, usage_reporting, usage_rollup  # noqa: E402

from conftest import seed_user  # noqa: E402


NOW_UTC = datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc)


def _load_usage_scale_harness():
    path = Path(__file__).parent.parent / "scripts/perf/admin_usage_scale.py"
    spec = importlib.util.spec_from_file_location("admin_usage_scale_harness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_admin_usage_scale_uses_approved_three_second_p95_budget():
    harness = _load_usage_scale_harness()

    assert harness.P95_BUDGET_MS == 3_000.0
    assert 2_999.999 < harness.P95_BUDGET_MS
    assert not 3_000.0 < harness.P95_BUDGET_MS


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
            "INSERT INTO v2_runtime_state (user_id,hosted_runtime_state) "
            "VALUES (%s,'v2'),(%s,'v2') "
            "ON CONFLICT (user_id) DO UPDATE SET hosted_runtime_state='v2'",
            ("u_usage_alpha", "u_usage_beta"),
        )
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
                "(user_id,lane,provider,model,prompt_tokens,completion_tokens,"
                "usage_reported_calls,cache_reported_calls,model_calls,retries,failed,"
                "status,latency_ms,created_at) VALUES "
                "(%s,'chat','openrouter','late-model',999,1,1,0,1,0,false,%s,9,%s)",
                ("u_usage_alpha", "parallel-after-export", "2026-07-01T12:30:00+00:00"),
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

    assert settings == [str(jobs_store._USAGE_REPORT_STATEMENT_TIMEOUT_MS)]


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
            "current_app_users": 12,
            "current_hosted_v2_users": 7,
            "current_hosted_v2_coverage": 7 / 12,
            "model_active_users": 5,
            "token_users": 4,
            "token_user_tokens": 1_545,
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
            "tokens_per_token_user": 386.25,
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
            "token_users": 4,
            "token_user_tokens": 1_545,
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
            "tokens_per_token_user": 386.25,
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
        }, {
            "lane": "heartbeat",
            "users": 2,
            "turns": 5,
            "model_calls": 6,
            "retries": 1,
            "failed_turns": 1,
            "metered_turns": 4,
            "prompt_tokens": 300,
            "completion_tokens": 145,
            "total_tokens": 445,
            "cache_read_tokens": 100,
            "cache_write_tokens": 10,
            "cache_miss_tokens": 100,
            "unknown_usage_calls": 1,
            "usage_reported_calls": 5,
            "cache_reported_calls": 3,
            "usage_coverage": 5 / 6,
            "cache_coverage": 0.5,
            "cache_hit_ratio": 0.5,
            "tokens_per_call": 445 / 6,
            "latency_ms_p50": 2_000,
            "latency_ms_p95": 6_000,
            "failure_rate": 0.2,
            "retry_rate": 1 / 6,
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
        "用户覆盖与窗口活动",
        "当前账号覆盖",
        "所选窗口活动 · 跟随筛选",
        "全部 App 用户",
        "当前 Hosted V2",
        "窗口内 Token 用户",
        "Fleet Overview",
        "Token 用量与成本结构",
        "平均值",
        "每日趋势",
        "Per-user Usage",
        "Provider / Model",
        "Lane breakdown · 成本拆分",
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
    assert "Resident V1、Hosted V1 与 self-host token 均未计入" in body
    assert "Background known tokens" in body
    assert "两组不是漏斗，不能直接相减" in body
    assert "筛选 lane=chat 时 Background 为 0，不代表全站后台用量为 0" in body
    assert "窗口内 input / output 两列各至少有一次已知值" in body
    assert "Complete-cohort tokens" in body
    assert "Known total 会保留 partial-only 用户已知的 token" in body
    assert "445" in body
    assert "28.8%" in body
    assert "provider<&" not in body
    assert "model<script>" not in body
    assert "provider&lt;&amp;" in body
    assert "model&lt;script&gt;" in body
    assert (
        "/admin/data-track/users/usr_0123456789abcdef?"
        in body
    )
    assert "admin_key=query-admin-token" in body


def test_usage_page_renders_unknown_v2_coverage_as_dash(monkeypatch):
    report = _usage_render_report()
    report["overview"].update({
        "current_app_users": 0,
        "current_hosted_v2_users": 0,
        "current_hosted_v2_coverage": None,
    })
    monkeypatch.setattr(_data_track, "_usage_report", lambda _query: report)

    body = _admin_core.page_html("view=usage&preset=7d")
    start = body.index("当前 Hosted V2")
    current_v2_card = body[start:body.index("</div></div>", start)]

    assert "coverage-value'>0</div>" in current_v2_card
    assert "占全部 App 用户 —" in current_v2_card


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
        "filters", "coverage"
    }
    assert report["overview"] == {
        "registered_accounts": 3,
        "activated_users": 3,
        "current_app_users": 4,
        "current_hosted_v2_users": 2,
        "current_hosted_v2_coverage": 0.5,
        "model_active_users": 2,
        "token_users": 2,
        "token_user_tokens": 215,
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
    assert averages["tokens_per_token_user"] == pytest.approx(107.5)
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
    assert first["token_users"] == 1
    assert first["tokens_per_token_user"] == pytest.approx(120)
    assert first["tokens_per_active_user_day"] == pytest.approx(60)
    assert first["model_calls"] == 3
    assert first["failed_turns"] == 1
    assert first["unknown_usage_calls"] == 1
    assert second["total_tokens"] == 95
    assert second["token_users"] == 2
    assert second["tokens_per_token_user"] == pytest.approx(47.5)
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
    assert report["overview"]["current_app_users"] == 4
    assert report["overview"]["current_hosted_v2_users"] == 2
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


def test_usage_snapshot_user_drilldown_keeps_global_current_population(usage_rows):
    report = jobs_store.usage_report_snapshot(
        _usage_query(user_id="u_usage_beta")
    )

    assert report["overview"]["registered_accounts"] == 1
    assert report["overview"]["current_app_users"] == 4
    assert report["overview"]["current_hosted_v2_users"] == 2
    assert report["overview"]["current_hosted_v2_coverage"] == pytest.approx(0.5)


def test_usage_current_v2_population_excludes_resident_and_draining_states(
    usage_rows,
):
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET hosted_runtime_state='draining' "
            "WHERE user_id='u_usage_beta'"
        )
        conn.execute(
            "INSERT INTO v2_runtime_state (user_id,hosted_runtime_state) "
            "VALUES ('u_usage_idle','resident') "
            "ON CONFLICT (user_id) DO UPDATE "
            "SET hosted_runtime_state='resident'"
        )

    report = jobs_store.usage_report_snapshot(_usage_query())

    assert report["overview"]["current_app_users"] == 4
    assert report["overview"]["current_hosted_v2_users"] == 1
    assert report["overview"]["current_hosted_v2_coverage"] == pytest.approx(
        0.25
    )


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


def test_usage_token_user_average_excludes_partial_only_and_matches_all_paths(
    usage_rows,
):
    rows = [
        ("u_usage_idle", "split-prompt", "split-a", 40, None),
        ("u_usage_idle", "split-completion", "split-b", None, 10),
        ("u_usage_future", "partial-only", "partial", 90, None),
    ]
    with db.get_pool().connection() as conn:
        for user_id, status, model, prompt, completion in rows:
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(user_id,lane,provider,model,prompt_tokens,completion_tokens,"
                "usage_reported_calls,cache_reported_calls,model_calls,retries,"
                "failed,status,created_at) "
                "VALUES (%s,'chat','openrouter',%s,%s,%s,1,0,1,0,false,%s,%s)",
                (
                    user_id,
                    model,
                    prompt,
                    completion,
                    status,
                    "2026-07-01T13:00:00+00:00",
                ),
            )

    query = _shanghai_usage_query(provider="openrouter")
    raw = jobs_store._usage_report_snapshot_raw(query)
    _enable_usage_rollup()
    try:
        parallel = jobs_store.usage_report_snapshot(query)
        serial = jobs_store._usage_report_snapshot_hybrid_serial(query)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM v2_usage_rollup_watermarks")

    assert serial is not None
    for report in (raw, parallel, serial):
        assert report["overview"]["total_tokens"] == 295
        assert report["overview"]["token_users"] == 3
        assert report["overview"]["token_user_tokens"] == 205
        assert report["averages"]["tokens_per_token_user"] == pytest.approx(
            205 / 3
        )
        first, second = report["daily"]
        assert first["total_tokens"] == 260
        assert first["token_users"] == 2
        assert first["token_user_tokens"] == 170
        assert first["tokens_per_token_user"] == pytest.approx(85)
        assert second["total_tokens"] == 35
        assert second["token_users"] == 1
        assert second["token_user_tokens"] == 35
        assert second["tokens_per_token_user"] == pytest.approx(35)


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
