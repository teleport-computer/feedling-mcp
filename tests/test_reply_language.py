from __future__ import annotations

import ast
from datetime import datetime, timezone
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from chat.reply_language import (  # noqa: E402
    DEFAULT_FAILURE_FALLBACK_EN,
    DEFAULT_FAILURE_FALLBACK_ZH,
    _language_from_hint,
    failure_fallback_reply,
    format_time_anchor,
    garden_language_decision,
    infer_reply_language,
    reply_language_system_line,
)


@pytest.mark.parametrize(
    ("hint", "language"),
    [
        ("zh-Hant-TW", "zh-Hans"),
        ("zh-TW", "zh-Hans"),
        ("yue-HK", "zh-Hans"),
        ("en-GB", "en"),
        ("en-AU", "en"),
    ],
)
def test_machine_language_hint_accepts_supported_tags(
    hint,
    language,
):
    policy = infer_reply_language(locale=hint)

    assert policy.language == language


def test_invalid_cn_locale_falls_through_to_archive_language():
    policy = infer_reply_language(
        locale="cn",
        archive_language="en-US",
    )

    assert policy.language == "en"


@pytest.mark.parametrize(
    ("hint", "language", "opposite_archive"),
    [
        ("英语", "en", "zh-CN"),
        ("英文", "en", "zh-CN"),
        ("粤语", "zh-Hans", "en-US"),
    ],
)
def test_machine_language_hint_accepts_exact_chinese_language_names(
    hint,
    language,
    opposite_archive,
):
    policy = infer_reply_language(
        locale=hint,
        archive_language=opposite_archive,
    )

    assert policy.language == language


@pytest.mark.parametrize(
    "hint",
    [
        "no English, 中文 only",
        "中文, not English",
        "en français",
        "不要英文",
    ],
)
def test_machine_language_hint_has_an_explicit_unparseable_result(hint):
    parsed = _language_from_hint(hint)

    assert parsed.status == "unparseable"
    assert parsed.language == ""


@pytest.mark.parametrize(
    "locale",
    [
        "no English, 中文 only",
        "中文, not English",
        "en français",
    ],
)
def test_unparseable_locale_falls_through_instead_of_forcing_opposite_language(locale):
    policy = infer_reply_language(
        locale=locale,
        archive_language="zh-Hant-TW",
    )

    assert policy.language == "zh-Hans"


def test_machine_language_hint_result_distinguishes_all_three_states():
    absent = _language_from_hint("")
    unparseable = _language_from_hint("不要英文")
    parsed = _language_from_hint("en-GB")

    assert (absent.status, absent.language) == ("absent", "")
    assert (unparseable.status, unparseable.language) == ("unparseable", "")
    assert (parsed.status, parsed.language) == ("parsed", "en")


def test_locale_then_default_when_evidence_is_insufficient():
    assert infer_reply_language(locale="en-US").language == "en"
    fallback = infer_reply_language(locale="", archive_language="")
    assert fallback.language == "zh-Hans"


def test_reply_language_system_line_uses_default_policy_not_absolute_pin():
    line = reply_language_system_line(infer_reply_language(locale="en-US"))

    assert "Default reply language: English" in line
    assert "latest message is clearly in another language" in line
    assert "memory cards, OCR, timestamps" in line


def test_failure_fallback_reply_selects_paired_shared_copy():
    en = infer_reply_language(locale="en-US")
    zh = infer_reply_language(locale="zh-Hans-CN")

    assert failure_fallback_reply(en) == DEFAULT_FAILURE_FALLBACK_EN
    assert failure_fallback_reply(zh) == DEFAULT_FAILURE_FALLBACK_ZH


def test_time_anchor_formats_in_policy_language():
    dt = datetime(2026, 7, 17, 6, 30, tzinfo=timezone.utc)
    en = infer_reply_language(locale="en-US")
    zh = infer_reply_language(locale="zh-Hans-CN")

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


def _loaded_names(function_name: str) -> set[str]:
    source = Path(__file__).parent.parent / "backend" / "chat" / "reply_language.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    return {
        node.id
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _direct_call_names(function_name: str) -> set[str]:
    source = Path(__file__).parent.parent / "backend" / "chat" / "reply_language.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    return {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_garden_adapter_keeps_shared_identity_and_hint_dependency_edges():
    """Proves Garden's direct static edges; it cannot see dynamic indirection."""
    garden_calls = _direct_call_names("garden_language_decision")
    assert {"_identity_texts", "_language_from_hint"} <= garden_calls
    assert "IDENTITY_LANGUAGE_FIELDS" in _loaded_names("_identity_texts")


def test_garden_adapter_normalizes_explicit_hint_and_reads_identity_text() -> None:
    """Proves both Garden evidence paths behave; it cannot locate their AST edges."""
    explicit = garden_language_decision(
        {"language_preference": "English"},
        written="今天一直在处理工作和家里的事情，晚上终于有时间坐下来休息一会儿。",
    )
    assert (explicit["locale"], explicit["basis"]) == (
        "en",
        "explicit_preference",
    )

    identity_text = garden_language_decision(
        {
            "self_introduction": (
                "I prefer thoughtful conversations about daily life, relationships, "
                "creative work, and the small details that make each day meaningful."
            )
        },
        written="",
    )
    assert (identity_text["locale"], identity_text["basis"]) == (
        "en",
        "writing_language",
    )
