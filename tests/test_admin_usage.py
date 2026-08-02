"""Admin Usage query normalization and canonical link behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from psycopg.types.json import Jsonb


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track as _data_track  # noqa: E402
from admin import admin_core as _admin_core  # noqa: E402
from admin import usage as _usage  # noqa: E402
from accounts import registry  # noqa: E402
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
        "filters", "coverage"
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
