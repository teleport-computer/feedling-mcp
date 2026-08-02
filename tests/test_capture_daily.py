from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from proactive import capture_daily  # noqa: E402


def test_device_timezone_controls_calendar_day_before_proactive_fallback():
    shanghai = ZoneInfo("Asia/Shanghai")
    previous_at = datetime(2026, 8, 1, 23, 30, tzinfo=shanghai).timestamp()
    completed_at = datetime(2026, 8, 2, 0, 30, tzinfo=shanghai).timestamp()

    patch = capture_daily.daily_capture_patch(
        {
            "last_capture_cards_added_at": previous_at,
            "last_capture_cards_added": 7,
        },
        cards_added=2,
        completed_at=completed_at,
        timezone_name="UTC",
        device_timezone="Asia/Shanghai",
    )

    assert patch == {
        "last_capture_cards_added_at": completed_at,
        "last_capture_cards_added": 2,
    }


def test_noop_returns_no_banner_patch():
    assert capture_daily.daily_capture_patch(
        {
            "last_capture_cards_added_at": 1234.0,
            "last_capture_cards_added": 7,
        },
        cards_added=0,
        completed_at=5678.0,
        timezone_name="UTC",
    ) == {}
