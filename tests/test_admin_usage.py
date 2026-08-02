"""Admin Usage query normalization and canonical link behavior."""

from __future__ import annotations

from datetime import datetime, timezone
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track as _data_track  # noqa: E402
from admin import usage as _usage  # noqa: E402
from core import reqctx  # noqa: E402


NOW_UTC = datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc)


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
        "&offset=200&admin_key=must-not-survive"
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
