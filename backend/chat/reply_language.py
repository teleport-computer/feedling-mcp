"""Reply-language policy for user-visible companion replies.

Pure helper shared by hosted model_api and resident consumers. It does not read
stores or databases; callers pass identity, durable memory text, and locale
hints.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class ReplyLanguagePolicy:
    language: str
    label: str
    source: str
    confidence: float = 0.0
    evidence_chars: int = 0


IDENTITY_LANGUAGE_FIELDS = (
    "custom_persona_prompt",
    "self_introduction",
    "tone_style",
    "boundaries",
)
MEMORY_LANGUAGE_FIELDS = (
    "title",
    "summary",
    "content",
    "description",
    "her_quote",
    "context",
    "bucket",
    "threads",
)

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_WEEKDAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_WEEKDAYS_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _policy(language: str, source: str, *, confidence: float = 0.0, evidence_chars: int = 0) -> ReplyLanguagePolicy:
    if language == "en":
        return ReplyLanguagePolicy("en", "English", source, confidence, evidence_chars)
    return ReplyLanguagePolicy("zh-Hans", "简体中文", source, confidence, evidence_chars)


def _language_from_hint(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("en") or "english" in raw:
        return "en"
    if raw.startswith(("zh", "cn")) or "chinese" in raw or "中文" in raw or "简体" in raw or "繁體" in raw:
        return "zh-Hans"
    return ""


def _iter_text(value: Any):
    if isinstance(value, str):
        text = value.strip()
        if text:
            yield text
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_text(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_text(item)


def _identity_texts(identity: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    if not isinstance(identity, dict):
        return texts
    for key in IDENTITY_LANGUAGE_FIELDS:
        texts.extend(_iter_text(identity.get(key)))
    return texts


def _memory_texts(memories: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    if not isinstance(memories, list):
        return texts
    for memory in memories:
        if not isinstance(memory, dict):
            continue
        source = memory.get("inner") if isinstance(memory.get("inner"), dict) else memory
        if not isinstance(source, dict):
            continue
        for key in MEMORY_LANGUAGE_FIELDS:
            texts.extend(_iter_text(source.get(key)))
    return texts


def _counts(texts: list[str]) -> tuple[int, int]:
    joined = "\n".join(texts)
    return len(_CJK_RE.findall(joined)), len(_LATIN_RE.findall(joined))


def _dominant_language(cjk: int, latin: int, *, min_evidence: int, min_confidence: float) -> tuple[str, float, int]:
    total = cjk + latin
    if total < min_evidence:
        return "", 0.0, total
    confidence = max(cjk, latin) / total if total else 0.0
    if confidence < min_confidence:
        return "", confidence, total
    return ("zh-Hans" if cjk > latin else "en"), confidence, total


def infer_reply_language_policy(
    identity: dict,
    memories: list[dict],
    *,
    locale: str = "",
    archive_language: str = "",
) -> ReplyLanguagePolicy:
    explicit = _language_from_hint((identity or {}).get("language_preference") if isinstance(identity, dict) else "")
    if explicit:
        return _policy(explicit, "identity.language_preference", confidence=1.0)

    identity_cjk, identity_latin = _counts(_identity_texts(identity or {}))
    memory_cjk, memory_latin = _counts(_memory_texts(memories or []))
    lang, confidence, total = _dominant_language(
        identity_cjk + memory_cjk,
        identity_latin + memory_latin,
        min_evidence=32,
        min_confidence=0.62,
    )
    if lang:
        return _policy(lang, "identity_memory_dominant", confidence=confidence, evidence_chars=total)

    hinted = _language_from_hint(locale) or _language_from_hint(archive_language)
    if hinted:
        return _policy(hinted, "locale" if _language_from_hint(locale) else "archive_language", confidence=1.0)
    return _policy("zh-Hans", "default", confidence=0.0)


def reply_language_system_line(policy: ReplyLanguagePolicy, *, proactive: bool = False) -> str:
    if policy.language == "en":
        return (
            "Reply language policy:\n"
            "Default reply language: English.\n"
            "For this user-visible reply, if the user's latest message is clearly in another language, reply in that language. "
            "If the latest message is mixed, ambiguous, mostly quoted/context, or this is a proactive/background reply, use English. "
            "Do not let memory cards, OCR, timestamps, or internal context change the reply language. "
            "Preserve quoted text, names, and requested translation targets as written."
        )
    return (
        "回复语言规则：\n"
        "默认回复语言：简体中文。\n"
        "本轮用户最新消息如果明显使用另一种语言，就用那种语言回复；如果最新消息混合、不明确、主要是引用/上下文，或这是主动/后台回复，就使用简体中文。"
        "不要被记忆卡、OCR、时间戳或内部上下文带偏回复语言。引用、名字和用户指定的翻译目标语言保持原样。"
    )


def _age_text(seconds: float, language: str) -> str:
    sec = max(0, int(seconds))
    if sec < 3600:
        minutes = max(1, round(sec / 60))
        return f"{minutes} 分钟前" if language != "en" else f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if sec < 86400:
        hours = max(1, round(sec / 3600))
        return f"{hours} 小时前" if language != "en" else f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = max(1, round(sec / 86400))
    return f"{days} 天前" if language != "en" else f"{days} day{'s' if days != 1 else ''} ago"


def format_time_anchor(
    dt,
    tz_name,
    policy: ReplyLanguagePolicy,
    *,
    since_sec=None,
    timezone_default: bool = False,
) -> str:
    zone_name = str(tz_name or "UTC").strip() or "UTC"
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        zone_name = "UTC"
        zone = ZoneInfo("UTC")
    current = dt if isinstance(dt, datetime) else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(zone)
    if policy.language == "en":
        h = local.hour
        segment = "late night" if h < 6 else "morning" if h < 12 else "midday" if h < 14 else "afternoon" if h < 18 else "evening"
        body = f"{local.strftime('%Y-%m-%d')} {_WEEKDAYS_EN[local.weekday()]} {local.strftime('%H:%M')} {segment}"
        body += f" {zone_name}" + (" (default; device timezone unavailable)" if timezone_default else "")
        line = f"current_time: {body}"
        if since_sec is not None and since_sec >= 1800:
            line += f" (last interaction {_age_text(float(since_sec), policy.language)})"
        return line

    h = local.hour
    segment = "凌晨" if h < 6 else "上午" if h < 12 else "中午" if h < 14 else "下午" if h < 18 else "晚上"
    body = f"{local.strftime('%Y-%m-%d')} {_WEEKDAYS_ZH[local.weekday()]} {local.strftime('%H:%M')} {segment}"
    body += f" {zone_name}" + ("（默认·未获取到设备时区）" if timezone_default else "")
    line = f"current_time: {body}"
    if since_sec is not None and since_sec >= 1800:
        line += f" (距上次互动 {_age_text(float(since_sec), policy.language)})"
    return line
