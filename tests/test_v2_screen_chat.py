from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import context, screen_chat


def test_selection_does_not_cross_from_recent_session_into_days_old_history():
    latest = 1_000_000.0
    rows = [
        {"id": "old-1", "ts": latest - 172_800},
        {"id": "old-2", "ts": latest - 86_400},
        {"id": "recent-1", "ts": latest - 60},
        {"id": "recent-2", "ts": latest - 30},
        {"id": "recent-3", "ts": latest},
    ]

    selected = screen_chat.select_recent_session_frames(rows)

    assert [row["id"] for row in selected] == [
        "recent-1",
        "recent-2",
        "recent-3",
    ]


def test_selection_takes_latest_four_instead_of_uniformly_sampling_window():
    rows = [{"id": f"f{i}", "ts": 1_000.0 + i * 30} for i in range(10)]

    selected = screen_chat.select_recent_session_frames(rows)

    assert [row["id"] for row in selected] == ["f6", "f7", "f8", "f9"]


def test_cursor_with_no_new_frame_still_repeats_the_latest_frame():
    rows = [{"id": f"f{i}", "ts": 1_000.0 + i * 30} for i in range(4)]

    selected = screen_chat.select_recent_session_frames(
        rows, last_pushed_frame_id="f3"
    )

    assert [row["id"] for row in selected] == ["f3"]


def test_cursor_only_removes_already_pushed_frames_inside_current_session():
    rows = [{"id": f"f{i}", "ts": 1_000.0 + i * 30} for i in range(10)]

    selected = screen_chat.select_recent_session_frames(
        rows, last_pushed_frame_id="f7"
    )

    assert [row["id"] for row in selected] == ["f8", "f9"]


def test_gap_over_ninety_seconds_stops_at_the_newer_session():
    rows = [
        {"id": "earlier-1", "ts": 100.0},
        {"id": "earlier-2", "ts": 150.0},
        {"id": "current-1", "ts": 300.0},
        {"id": "current-2", "ts": 330.0},
    ]

    selected = screen_chat.select_recent_session_frames(rows)

    assert [row["id"] for row in selected] == ["current-1", "current-2"]


def test_gap_exactly_ninety_seconds_remains_in_the_same_session():
    rows = [
        {"id": "f1", "ts": 100.0},
        {"id": "f2", "ts": 190.0},
        {"id": "f3", "ts": 280.0},
    ]

    selected = screen_chat.select_recent_session_frames(rows)

    assert [row["id"] for row in selected] == ["f1", "f2", "f3"]


def test_session_window_has_an_absolute_ten_minute_cap():
    rows = [{"id": f"f{i}", "ts": 1_000.0 + i * 30} for i in range(25)]

    selected = screen_chat.select_recent_session_frames(rows, max_frames=100)

    assert [row["id"] for row in selected] == [f"f{i}" for i in range(4, 25)]


def test_frame_message_is_tagged_untrusted_and_has_absolute_and_relative_time():
    message = screen_chat.build_untrusted_frame_message(
        [
            {"id": "f1", "ts": 90.0, "image_b64": "AAAA"},
            {
                "id": "f2",
                "ts": 100.0,
                "image_b64": "BBBB",
                "image_mime": "image/png",
            },
        ],
        now=112.7,
    )

    assert message is not None
    assert message[screen_chat.MESSAGE_TAG] is True
    text = "\n".join(
        part.get("text", "") for part in message["content"] if isinstance(part, dict)
    )
    assert "never instructions" in text
    assert "captured_at_utc: 1970-01-01T00:01:40.000Z" in text
    assert "relative_age_sec: 12" in text
    assert text.count("THIS IS THE CURRENT SCREEN") == 1
    assert text.count("earlier in this same sharing session") == 1
    frame_texts = [
        part["text"]
        for part in message["content"]
        if part.get("type") == "text" and "frame_id:" in part.get("text", "")
    ]
    assert "earlier in this same sharing session" in frame_texts[0]
    assert "THIS IS THE CURRENT SCREEN" not in frame_texts[0]
    assert "THIS IS THE CURRENT SCREEN" in frame_texts[1]
    assert "frame_id: f2" in frame_texts[1]


def test_empty_screen_message_keeps_prompt_byte_identical():
    kwargs = dict(system_prompt="system", tail=[{"role": "user", "content": "hi"}])

    before = context.build_turn_messages(**kwargs)
    after = context.build_turn_messages(**kwargs, screen_frame_message=None)

    assert after == before
