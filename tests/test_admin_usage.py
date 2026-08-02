"""Admin Usage query normalization and canonical link behavior."""

from __future__ import annotations

from datetime import datetime, timezone
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from psycopg.types.json import Jsonb


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track as _data_track  # noqa: E402
from admin import usage as _usage  # noqa: E402
from core import reqctx  # noqa: E402
import db  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402

from conftest import seed_user  # noqa: E402


NOW_UTC = datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc)


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


def test_usage_snapshot_reconciles_users_days_models_failures_and_unknowns(usage_rows):
    report = jobs_store.usage_report_snapshot(_usage_query())

    assert set(report) == {
        "overview", "averages", "daily", "users", "models", "filters", "coverage"
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
