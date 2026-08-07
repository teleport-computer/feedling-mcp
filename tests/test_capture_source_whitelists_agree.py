"""The two capture source whitelists must agree.

`capture_scheduler.CAPTURE_LIVE_SOURCES` decides which chat rows TRIGGER a
capture and count toward its window; `worker._CAPTURE_PROMPT_SOURCES` decides
which of those rows actually reach the model. When a source is in the first but
not the second, capture fires, renders an empty window, writes no cards, and
still advances the cursor past the rows — silent, permanent memory loss with a
green log line.

That is not hypothetical: `voice_call_summary` was added to the trigger set on
2026-08-05 and never to the prompt set, so on Runtime V2 every voice call's
memory was dropped until 2026-08-07.
"""
from model_api_runtime.v2 import worker
from proactive import capture_scheduler


def test_every_triggering_source_reaches_the_prompt():
    missing = sorted(
        capture_scheduler.CAPTURE_LIVE_SOURCES - worker._CAPTURE_PROMPT_SOURCES
    )
    assert not missing, (
        "这些 source 会触发 capture 但不会进 prompt，capture 将在空窗口上空转"
        f"并照常推进游标（= 静默丢记忆）：{missing}\n"
        "修法：把它们加进 worker._CAPTURE_PROMPT_SOURCES，或者先想清楚它们"
        "为什么该触发 capture。"
    )
