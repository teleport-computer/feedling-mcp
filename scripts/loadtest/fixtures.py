"""Shared resident/V2 workloads for the token rollback gate."""
from __future__ import annotations

from copy import deepcopy


SHARED_TURN_FIXTURES = [
    {
        "prompt": "今天过得怎么样",
        "agent_memory": "",
        "user_profile": "",
        "tail": [{"role": "user", "content": "今天过得怎么样"}],
    },
    {
        "prompt": "我还是有点焦虑，面试没过",
        "agent_memory": "",
        "user_profile": "",
        "tail": [{"role": "user", "content": "我还是有点焦虑，面试没过"}],
    },
    {
        "prompt": "帮我回忆一下上周说的那个计划",
        "agent_memory": "- 用户上周提到过一个计划",
        "user_profile": "- 用户希望延续之前讨论的计划",
        "tail": [{"role": "user", "content": "帮我回忆一下上周说的那个计划"}],
        # The production V2 loop is provider-native: the mock emits this
        # function call while tools are enabled, then plain final text on the
        # loop's reserved tools-disabled request.
        "tool_call": {
            "id": "shared-memory-search",
            "name": "memory_search",
            "args": {"query": "上周的计划"},
        },
    },
]


def resident_prompts() -> list[str]:
    return [str(fixture["prompt"]) for fixture in SHARED_TURN_FIXTURES]


def v2_turn_fixtures() -> list[dict]:
    return [
        {key: deepcopy(value) for key, value in fixture.items() if key != "prompt"}
        for fixture in SHARED_TURN_FIXTURES
    ]
