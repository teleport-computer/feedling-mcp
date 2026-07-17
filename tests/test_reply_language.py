from __future__ import annotations

from datetime import datetime, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from chat.reply_language import (  # noqa: E402
    format_time_anchor,
    infer_reply_language_policy,
    reply_language_system_line,
)


def test_language_preference_overrides_text_evidence():
    policy = infer_reply_language_policy(
        {"language_preference": "en", "custom_persona_prompt": "你是一个中文角色，会一直用中文陪伴。"},
        [{"summary": "中文记忆", "content": "用户喜欢中文聊天。"}],
    )

    assert policy.language == "en"
    assert policy.source == "identity.language_preference"


def test_identity_and_memory_dominant_language_wins_as_combined_evidence():
    policy = infer_reply_language_policy(
        {
            "custom_persona_prompt": (
                "You are a warm companion who replies in a calm, concise, friendly English voice."
            )
        },
        [{"summary": "中文记忆", "content": "用户的很多记忆卡都是中文内容，包含生活和关系背景。"}],
    )

    assert policy.language == "en"
    assert policy.source == "identity_memory_dominant"


def test_reply_in_english_phrase_is_not_special_parsed():
    policy = infer_reply_language_policy(
        {"custom_persona_prompt": "用户写过 reply in English 这句话，但这只是短语，不是语言偏好字段。"},
        [],
    )

    assert policy.language == "zh-Hans"
    assert policy.source == "default"


def test_memory_dominant_language_used_when_identity_is_sparse():
    policy = infer_reply_language_policy(
        {"custom_persona_prompt": ""},
        [
            {"summary": "做饭", "content": "用户喜欢周末自己做饭，也经常记录新的菜谱。"},
            {"summary": "通勤", "content": "用户平时坐地铁上班，晚上回家会听播客。"},
        ],
    )

    assert policy.language == "zh-Hans"
    assert policy.source == "identity_memory_dominant"


def test_locale_then_default_when_evidence_is_insufficient():
    assert infer_reply_language_policy({}, [], locale="en-US").language == "en"
    fallback = infer_reply_language_policy({}, [], locale="", archive_language="")
    assert fallback.language == "zh-Hans"
    assert fallback.source == "default"


def test_reply_language_system_line_uses_default_policy_not_absolute_pin():
    line = reply_language_system_line(infer_reply_language_policy({}, [], locale="en-US"))

    assert "Default reply language: English" in line
    assert "latest message is clearly in another language" in line
    assert "memory cards, OCR, timestamps" in line


def test_time_anchor_formats_in_policy_language():
    dt = datetime(2026, 7, 17, 6, 30, tzinfo=timezone.utc)
    en = infer_reply_language_policy({}, [], locale="en-US")
    zh = infer_reply_language_policy({}, [], locale="zh-Hans-CN")

    en_line = format_time_anchor(dt, "Asia/Shanghai", en, since_sec=3600, timezone_default=True)
    zh_line = format_time_anchor(dt, "Asia/Shanghai", zh, since_sec=3600, timezone_default=True)

    assert "Friday" in en_line
    assert "afternoon" in en_line
    assert "default; device timezone unavailable" in en_line
    assert "last interaction 1 hour ago" in en_line
    assert "周五" in zh_line
    assert "下午" in zh_line
    assert "默认·未获取到设备时区" in zh_line
    assert "距上次互动 1 小时前" in zh_line
