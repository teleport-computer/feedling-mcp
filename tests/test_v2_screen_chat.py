from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import context, screen_chat


def test_uniform_sampler_only_uses_frames_after_cursor_and_keeps_latest():
    rows = [{"id": f"f{i}", "ts": float(i)} for i in range(12)]

    selected = screen_chat.uniformly_sample_new_frames(rows, max_frames=6)
    assert [row["id"] for row in selected] == ["f0", "f2", "f4", "f7", "f9", "f11"]

    after_cursor = screen_chat.uniformly_sample_new_frames(
        rows, last_pushed_frame_id="f8", max_frames=6
    )
    assert [row["id"] for row in after_cursor] == ["f9", "f10", "f11"]


def test_frame_message_is_tagged_untrusted_and_has_absolute_and_relative_time():
    message = screen_chat.build_untrusted_frame_message(
        [{"id": "f1", "ts": 100.0, "image_b64": "AAAA", "image_mime": "image/png"}],
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


def test_empty_screen_message_keeps_prompt_byte_identical():
    kwargs = dict(system_prompt="system", summary="", tail=[{"role": "user", "content": "hi"}])

    before = context.build_turn_messages(**kwargs)
    after = context.build_turn_messages(**kwargs, screen_frame_message=None)

    assert after == before
