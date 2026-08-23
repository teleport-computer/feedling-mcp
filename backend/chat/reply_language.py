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


@dataclass(frozen=True)
class LocalTimeLabels:
    """Localized calendar labels for one already-localized wall clock."""

    weekday: str
    day_period: str


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


def infer_garden_language(
    identity: dict | None,
    *,
    existing_buckets: str = "",
    locale: str = "",
    archive_language: str = "",
) -> str:
    """这个花园的分类语言（"zh-Hans" / "en"）—— 桶名、线索、卡片正文共用它。

    为什么不直接用回复语言：**回复语言可以每轮变，花园语言不该变。** 这个人今天
    用英文问一句，不该让他的花园里冒出一个 Work 桶，跟已有的「工作」并存 ——
    桶是分类键，裂开等于同一类记忆被拆成两堆、检索时互相看不见。

    所以证据里显式带上**已有的桶名**：花园现在是中文桶，判断就会继续给中文，
    哪怕这一轮对话是英文的。第一次落卡（还没有任何桶）时才退回 identity /
    locale / archive_language 那几级。

    与 io 回复语言同源（``infer_reply_language_policy``），所以不会出现
    「io 用英文跟你说话、却给你一个中文桶」这种自相矛盾。
    """
    return garden_language_decision(
        identity,
        existing_buckets=existing_buckets,
        locale=locale,
        archive_language=archive_language,
    )["locale"]


def garden_language_decision(
    identity: dict | None,
    *,
    existing_buckets: str = "",
    locale: str = "",
    archive_language: str = "",
) -> dict:
    """同 :func:`infer_garden_language`，但把**判定依据**一并返回，供落库观测。

    为什么需要它：落卡语言错了是**看得见症状、看不见原因**的一类问题 ——
    用户只会说「怎么变英文了」，而我们查不到当时算出的是什么语言、凭什么算的。
    2026-08-23 提示词英文化之后，这条路径的风险显著上升，必须能观测。

    返回的字段全部**内容无关**：语言标签、依据名、以及桶名里的字符计数 ——
    桶名本身是用户内容，绝不落库。
    """
    # 已有桶优先，且**不走 infer_reply_language_policy 的证据门槛**。
    # 那个门槛（32 字符）是给散文调的；桶名天生就短 —— "Work / Health / Pets"
    # 只有 14 个拉丁字母，喂进去会被判成证据不足、回落成中文，
    # 于是一个英文花园会突然开始长中文桶。实测踩到过。
    buckets = str(existing_buckets or "")
    if buckets.strip():
        cjk = len(_CJK_RE.findall(buckets))
        latin = len(_LATIN_RE.findall(buckets))
        if cjk or latin:
            return {
                "locale": "zh-Hans" if cjk >= latin else "en",
                "basis": "existing_buckets",
                "bucket_cjk": cjk,
                "bucket_latin": latin,
            }

    # 还没有任何桶（新花园的第一张卡）才看身份卡 / locale / 归档语言。
    policy = infer_reply_language_policy(
        identity or {}, [], locale=locale, archive_language=archive_language
    )
    return {
        "locale": policy.language,
        "basis": f"reply_language:{policy.source}",
        "bucket_cjk": 0,
        "bucket_latin": 0,
        "confidence": round(float(policy.confidence or 0.0), 2),
    }


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


def local_time_labels(
    local: datetime,
    policy: ReplyLanguagePolicy,
) -> LocalTimeLabels:
    """Return the shared V1/V2 weekday and day-period labels.

    ``local`` must already be expressed in the user's timezone. Keeping timezone
    conversion outside this helper lets each runtime preserve its existing
    fallback semantics while sharing the language and boundary rules exactly.
    """
    hour = local.hour
    if policy.language == "en":
        day_period = (
            "late night" if hour < 6
            else "morning" if hour < 12
            else "midday" if hour < 14
            else "afternoon" if hour < 18
            else "evening"
        )
        return LocalTimeLabels(_WEEKDAYS_EN[local.weekday()], day_period)

    day_period = (
        "凌晨" if hour < 6
        else "上午" if hour < 12
        else "中午" if hour < 14
        else "下午" if hour < 18
        else "晚上"
    )
    return LocalTimeLabels(_WEEKDAYS_ZH[local.weekday()], day_period)


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
    labels = local_time_labels(local, policy)
    if policy.language == "en":
        body = f"{local.strftime('%Y-%m-%d')} {labels.weekday} {local.strftime('%H:%M')} {labels.day_period}"
        body += f" {zone_name}" + (" (default; device timezone unavailable)" if timezone_default else "")
        line = f"current_time: {body}"
        if since_sec is not None and since_sec >= 1800:
            line += f" (last interaction {_age_text(float(since_sec), policy.language)})"
        return line

    body = f"{local.strftime('%Y-%m-%d')} {labels.weekday} {local.strftime('%H:%M')} {labels.day_period}"
    body += f" {zone_name}" + ("（默认·未获取到设备时区）" if timezone_default else "")
    line = f"current_time: {body}"
    if since_sec is not None and since_sec >= 1800:
        line += f" (距上次互动 {_age_text(float(since_sec), policy.language)})"
    return line
