"""Shared resident/V2 workloads for the token rollback gate."""
from __future__ import annotations

from copy import deepcopy


SHARED_TURN_FIXTURES = [
    {
        "prompt": "今天过得怎么样",
        "summary": "",
        "tail": [{"role": "user", "content": "今天过得怎么样"}],
    },
    {
        "prompt": "我还是有点焦虑，面试没过",
        "summary": "",
        "tail": [{"role": "user", "content": "我还是有点焦虑，面试没过"}],
    },
    {
        "prompt": "帮我回忆一下上周说的那个计划",
        "summary": "- 用户上周提到过一个计划",
        "tail": [{"role": "user", "content": "帮我回忆一下上周说的那个计划"}],
        "planner_replies": [
            '{"plan":[{"type":"memory_search","payload":{"query":"上周的计划"}}]}',
            '{"plan":[{"type":"final_response","payload":{}}]}',
        ],
    },
]


def resident_prompts() -> list[str]:
    return [str(fixture["prompt"]) for fixture in SHARED_TURN_FIXTURES]


def v2_turn_fixtures() -> list[dict]:
    return [
        {key: deepcopy(value) for key, value in fixture.items() if key != "prompt"}
        for fixture in SHARED_TURN_FIXTURES
    ]
