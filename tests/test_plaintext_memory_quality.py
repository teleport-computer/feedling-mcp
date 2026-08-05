"""Regression coverage for v1 plaintext Memory Garden quality scanning."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import turn


def test_v1_summary_content_card_is_not_misclassified_as_empty_noise():
    issues = turn._memory_quality_card_issues(
        {"type": "fact"},
        {
            "summary": "对方想做番茄炒蛋",
            "content": "已经列过番茄、鸡蛋和葱的食材清单。",
            "bucket": "饮食",
            "threads": ["做饭"],
        },
        archive_language="zh-Hans-CN",
    )

    assert issues == []


def test_typed_memory_quality_fields_remain_supported():
    issues = turn._memory_quality_card_issues(
        {"type": "fact"},
        {
            "title": "工作偏好",
            "description": "处理数据库问题时喜欢直接写 SQL，并先确认真实执行计划。",
        },
        archive_language="zh-Hans-CN",
    )

    assert issues == []
