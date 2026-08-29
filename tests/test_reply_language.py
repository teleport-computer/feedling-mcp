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


_REPO_ROOT = Path(__file__).parent.parent
_EXPECTED_REPLY_LANGUAGE_RULE_EN = (
    "Reply language rule:\n"
    "Determine the reply language from the user's latest message. "
    "If that message is mixed, ambiguous, or mostly quoted/context, use the language of this rule. "
    "For a proactive/background reply, also use the language of this rule. "
    "Use the same language for your thinking and final reply. "
    "Do not let memory cards, OCR, timestamps, or internal context change the reply language. "
    "Preserve quoted text, names, and requested translation targets as written."
)
_EXPECTED_REPLY_LANGUAGE_RULE_ZH = (
    "回复语言规则：\n"
    "根据用户最新一条消息判断回复语言。如果该消息混合、不明确或主要是引用/上下文，就使用本规则所用的语言；"
    "主动/后台回复也使用本规则所用的语言。思维过程和正式回复使用同一种语言。"
    "不要被记忆卡、OCR、时间戳或内部上下文带偏回复语言。引用、名字和用户指定的翻译目标语言保持原样。"
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


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en-US", _EXPECTED_REPLY_LANGUAGE_RULE_EN),
        ("zh-Hans-CN", _EXPECTED_REPLY_LANGUAGE_RULE_ZH),
    ],
)
def test_reply_language_system_line_has_exact_static_bilingual_renderings(
    locale,
    expected,
):
    assert reply_language_system_line(infer_reply_language(locale=locale)) == expected


def test_reply_language_system_line_signature_and_production_call_sites_are_closed():
    source = _REPO_ROOT / "backend" / "chat" / "reply_language.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "reply_language_system_line"
    )
    assert [arg.arg for arg in function.args.posonlyargs + function.args.args] == [
        "policy"
    ]
    assert function.args.kwonlyargs == []
    assert function.args.vararg is None
    assert function.args.kwarg is None

    calls_by_file: dict[str, int] = {}
    for root_name in ("backend", "tools"):
        for candidate in (_REPO_ROOT / root_name).rglob("*.py"):
            candidate_tree = ast.parse(
                candidate.read_text(encoding="utf-8"),
                filename=str(candidate),
            )
            for node in ast.walk(candidate_tree):
                if not isinstance(node, ast.Call):
                    continue
                is_reply_language_call = (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "reply_language_system_line"
                ) or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "reply_language_system_line"
                )
                if not is_reply_language_call:
                    continue
                relative = candidate.relative_to(_REPO_ROOT).as_posix()
                calls_by_file[relative] = calls_by_file.get(relative, 0) + 1
                assert len(node.args) == 1
                assert node.keywords == []

    assert calls_by_file == {
        "backend/model_api_runtime/v2/worker.py": 2,
        "tools/chat_resident_consumer.py": 1,
    }


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
