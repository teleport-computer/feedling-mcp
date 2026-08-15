from __future__ import annotations

import sys
import os
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
os.environ.setdefault("FEEDLING_API_URL", "http://127.0.0.1:9")
os.environ.setdefault("FEEDLING_API_KEY", "test_key")

import hosted_runtime as runtime  # noqa: E402
import chat_resident_consumer as consumer  # noqa: E402
from hosted import turn as hosted_turn  # noqa: E402


def test_route_b_memory_create_coerces_to_clean_v1_add():
    coerced = runtime.coerce_runtime_action(
        {
            "type": "memory.create",
            "payload": {
                "summary": "用户喜欢先看地图再看路线。",
                "content": "记忆: 用户喜欢先看地图再看路线。\n上下文: 用户多次提出。\n使用提示: 复杂问题先给地图。",
                "bucket": "协作方式",
                "threads": ["解释偏好"],
                "importance": 0.7,
                "pulse": 0.4,
            },
            "reason": "Durable collaboration preference.",
        },
        [],
        direct_confidence=0.9,
    )

    assert coerced is not None
    occurred_at = coerced["executor_action"]["memory"]["occurred_at"]
    assert "T" in occurred_at
    assert occurred_at.endswith("Z")
    assert coerced["executor_action"] == {
        "type": "memory.add",
        "memory": {
            "summary": "用户喜欢先看地图再看路线。",
            "content": "记忆: 用户喜欢先看地图再看路线。\n上下文: 用户多次提出。\n使用提示: 复杂问题先给地图。",
            "bucket": "协作方式",
            "threads": ["解释偏好"],
            "importance": 0.7,
            "pulse": 0.4,
            "occurred_at": coerced["executor_action"]["memory"]["occurred_at"],
            "source": "hosted_runtime_state",
        },
        "reason": "Durable collaboration preference.",
        "capture_mode": "state",
    }


def test_route_b_memory_patch_coerces_to_supersede_not_content_patch():
    coerced = runtime.coerce_runtime_action(
        {
            "type": "memory.patch",
            "target": {"memory_id": "mem_old"},
            "payload": {
                "patch": {
                    "summary": "蛋子是一只比熊。",
                    "content": "记忆: 蛋子是一只比熊。\n上下文: 用户纠正。\n使用提示: 问到蛋子时用新事实。",
                }
            },
            "reason": "User corrected old pet fact.",
        },
        [{"id": "mem_old", "summary": "用户有只狗叫蛋子。"}],
        direct_confidence=0.9,
    )

    assert coerced is not None
    assert coerced["executor_action"]["type"] == "memory.supersede"
    assert coerced["executor_action"]["supersedes"] == "mem_old"
    assert coerced["executor_action"]["memory"]["summary"] == "蛋子是一只比熊。"


@pytest.mark.parametrize(
    "action,candidates",
    [
        (
            {
                "type": "memory.create",
                "payload": {
                    "summary": "New fact",
                    "content": "New fact content",
                    "occurred_at": "2026-08-15T20:00:00+08:00",
                },
            },
            [],
        ),
        (
            {
                "type": "memory.supersede",
                "target": {"memory_id": "mem_old"},
                "payload": {
                    "memory": {
                        "summary": "Replacement",
                        "content": "Replacement content",
                        "occurred_at": "2026-08-15T20:00:00+08:00",
                    }
                },
            },
            [{"id": "mem_old"}],
        ),
        (
            {
                "type": "memory.patch",
                "target": {"memory_id": "mem_old"},
                "payload": {
                    "patch": {
                        "summary": "Patch",
                        "content": "Patch content",
                        "occurred_at": "2026-08-15T20:00:00+08:00",
                    }
                },
            },
            [{"id": "mem_old"}],
        ),
    ],
)
def test_route_b_memory_actions_normalize_caller_timestamp(action, candidates):
    coerced = runtime.coerce_runtime_action(
        action, candidates, direct_confidence=0.9
    )

    assert coerced is not None
    assert (
        coerced["executor_action"]["memory"]["occurred_at"]
        == "2026-08-15T12:00:00Z"
    )


def test_route_b_memory_action_preserves_explicit_empty_occurred_at():
    coerced = runtime.coerce_runtime_action(
        {
            "type": "memory.create",
            "payload": {
                "summary": "Undated fact",
                "content": "The source does not provide an event date.",
                "occurred_at": "",
            },
        },
        [],
        direct_confidence=0.9,
    )

    assert coerced is not None
    assert coerced["executor_action"]["memory"]["occurred_at"] == ""


def test_route_a_v2_normalizes_legacy_memory_action_names():
    assert consumer._normalize_v2_action_type({"type": "memory.create"})["type"] == "memory.add"
    assert consumer._normalize_v2_action_type({"type": "memory.add_correction"})["type"] == "memory.add"
    patched = consumer._normalize_v2_action_type({"type": "memory.patch", "memory_id": "mem_old"})
    assert patched["type"] == "memory.supersede"
    assert patched["supersedes"] == "mem_old"
