"""感知 prompt 的基线快照 —— 守「行为逐字节不变」。

fixture 在 origin/test 基线的 checkout 上跑 scripts/dump_perception_baseline.py 得到。
一旦红，说明某段说明书被改动了 —— 那是行为变更，不能混在重构批次里悄悄发生。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

_FIXTURE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "perception_kernel" / "prompt_baseline.json"
)

# Self-contained sys.path bootstrap (mirrors other _PURE_UNIT files such as
# tests/test_v2_history_search_unit.py): conftest.py only adds backend/ to
# sys.path inside its DB-provisioning try-block, so on a no-Postgres machine
# this file must add both backend/ and tools/ itself. chat_resident_consumer.py
# also reads FEEDLING_API_URL/FEEDLING_API_KEY at module scope, so those must
# be set before the first `import chat_resident_consumer` (pattern mirrors
# tests/test_chat_resident_consumer_file.py).
os.environ.setdefault("FEEDLING_API_URL", "http://localhost:5001")
os.environ.setdefault("FEEDLING_API_KEY", "test_key_00000000")
_BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
_TOOLS = pathlib.Path(__file__).resolve().parent.parent / "tools"
for _p in (_BACKEND, _TOOLS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _baseline() -> dict[str, str]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_v2_wake_system_prompt_unchanged():
    from model_api_runtime.v2 import worker

    assert worker._WAKE_SYSTEM_PROMPT == _baseline()["v2_wake_system"]


def test_v2_scheduled_wake_system_prompt_unchanged():
    from model_api_runtime.v2 import worker

    assert worker._SCHEDULED_WAKE_SYSTEM_PROMPT == _baseline()["v2_scheduled_wake_system"]


def test_v2_runtime_perception_policy_unchanged():
    from model_api_runtime.v2 import context

    assert context._RUNTIME_PERCEPTION_POLICY == _baseline()["v2_runtime_perception_policy"]


def test_v1_reachout_context_unchanged():
    import chat_resident_consumer as consumer

    got = consumer._native_reachout_perception_context(
        {"place_label": "office", "motion_state": "walking"},
        [{"signal": "health_sleep", "field": "asleep_minutes", "direction": "down"}],
        {"location": {"label": "office"}, "media": {"new_artist": True}},
    )
    assert got == _baseline()["v1_reachout_context"]


def test_v1_reachout_context_empty_unchanged():
    import chat_resident_consumer as consumer

    assert consumer._native_reachout_perception_context({}, [], None) == \
        _baseline()["v1_reachout_context_empty"]


def test_v1_reachout_context_change_only_unchanged():
    """Exercises the `elif change:` back-compat branch (domains falsy, change
    non-empty) — neither of the two tests above ever reaches it: one has
    domains truthy, the other has both change and domains empty."""
    import chat_resident_consumer as consumer

    got = consumer._native_reachout_perception_context(
        {"place_label": "office", "motion_state": "walking"},
        [{"signal": "health_sleep", "field": "asleep_minutes", "direction": "down"}],
        None,
    )
    assert got == _baseline()["v1_reachout_context_change_only"]


def test_v2_tool_schema_perception_descriptions_unchanged():
    """Task 5 will move these out of capabilities.tool_schema.DESCRIPTIONS into
    the kernel package; pin the current text of each one individually so a
    later diff can tell exactly which tool's wording moved/changed."""
    from capabilities import tool_schema

    baseline = _baseline()["v2_tool_schema_perception"]
    for name, expected in baseline.items():
        assert tool_schema.DESCRIPTIONS[name] == expected
