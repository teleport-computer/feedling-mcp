from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from memory import timestamps as memory_timestamps  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-08-13T21:43:04Z", datetime(2026, 8, 13, 21, 43, 4, tzinfo=timezone.utc)),
        (
            "2026-08-13T20:00:00+08:00",
            datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc),
        ),
        (
            "2026-08-13T17:27:18.618643",
            datetime(2026, 8, 13, 17, 27, 18, 618643, tzinfo=timezone.utc),
        ),
        ("2026-06-18T00:00:00", datetime(2026, 6, 18, tzinfo=timezone.utc)),
        ("2026-08-13", datetime(2026, 8, 13, tzinfo=timezone.utc)),
    ],
)
def test_parse_ts_accepts_every_historical_memory_shape(raw, expected):
    assert memory_timestamps.parse_ts(raw) == expected


@pytest.mark.parametrize("raw", ["", "not-a-date", "2026-02-30", "2026-08-13 trailing"])
def test_parse_ts_never_turns_garbage_into_a_date(raw):
    assert memory_timestamps.parse_ts(raw) is None


def test_sort_key_orders_instants_and_leaves_garbage_last():
    values = [
        "garbage",
        "2026-08-13T20:00:00+08:00",  # 12:00Z despite the later-looking 20:00
        "2026-08-13T13:00:00Z",
    ]

    assert sorted(values, key=memory_timestamps.sort_key, reverse=True) == [
        "2026-08-13T13:00:00Z",
        "2026-08-13T20:00:00+08:00",
        "garbage",
    ]
