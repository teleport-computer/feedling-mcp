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

from memgarden.garden_language import (
    count_bucket_languages,
    decide_garden_language,
    split_bucket_names,
)


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
DEFAULT_FAILURE_FALLBACK_ZH = (
    "我这会儿有点慢，刚刚没接上。你稍后再发一次，我会继续接。"
)
DEFAULT_FAILURE_FALLBACK_EN = (
    "I'm running slow and didn't catch that one. Send it again in a bit — "
    "I'll pick it up."
)


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


#: 一条消息算「本人写的」要 role 是这个。别用 in / startswith 之类的模糊匹配 ——
#: 助手侧的 role 也可能含 "user" 子串（"user_proxy"）。
_USER_ROLE = "user"

#: 取多少条。只要最近的：语言是会变的（有人换了工作开始用英文写），
#: 拿三年前的历史投票会把当下的信号淹掉。
USER_WRITING_SAMPLE_MESSAGES = 40


def user_written_text(messages, *, limit: int = USER_WRITING_SAMPLE_MESSAGES) -> str:
    """从消息列表里抽出**本人写的字**，拼成一段供语言判定用的样本。

    为什么只取本人的：要判的是「这个人用什么语言」。把 AI 的回复也算进去，等于让
    上一轮的输出参与决定下一轮 —— 那正是 2026-08-24 事故里桶名扮演的角色，
    换个字段重演一遍而已。

    为什么放在这里：**两条 runtime 共用一份实现。** 同一个文件里 ``user_naming``
    的注释记着一次教训 —— 说话人标签各写了一份，V1 修了、V2 漏了半年。取证逻辑
    同理，别再各写一份。
    """
    out = []
    for m in list(messages or [])[-limit:]:
        if not isinstance(m, dict):
            continue
        if str(m.get("role") or "").strip().lower() != _USER_ROLE:
            continue
        body = str(m.get("content") or m.get("text") or "").strip()
        if body:
            out.append(body)
    return "\n".join(out)


def infer_garden_language(
    identity: dict | None,
    *,
    written: str = "",
    existing_buckets: str = "",
    locale: str = "",
    archive_language: str = "",
) -> str:
    """这个花园的分类语言（"zh-Hans" / "en"）—— 桶名、线索、卡片正文共用它。

    为什么不直接用回复语言：**回复语言可以每轮变，花园语言不该变。** 一个人今天用
    英文问一句，不该让他整个花园换语言。

    ⚠️ ``existing_buckets`` **不参与判定**，只用来落一条观测。见
    :func:`garden_language_decision` 里的理由。
    """
    return garden_language_decision(
        identity,
        written=written,
        existing_buckets=existing_buckets,
        locale=locale,
        archive_language=archive_language,
    )["locale"]


def garden_language_decision(
    identity: dict | None,
    *,
    written: str = "",
    existing_buckets: str = "",
    locale: str = "",
    archive_language: str = "",
) -> dict:
    """定这个花园的语言，并把**判定依据**一并返回，供落库观测。

    ## 🔴 桶名不是证据

    最容易想到的做法是「看这个花园现在的桶名是中文还是英文」。曾经就是这么做的，
    **两次都出了事**：

    2026-08-24 线上事故：更早一个 bug 让约 1/3 中文记忆贴上了英文公共桶，这些残留
    被读成「这是英文花园」→ 新卡全用英文桶 → 英文桶更多。**桶是 AI 的输出，拿输出
    当输入就是一个自我强化的环**，真实用户的中文花园两天内整个翻成英文。

    当天的第一版修复只换了数票方式（按桶投票、不按字符），环还在。hx 随即指出更
    致命的一点：**桶名里根本没有语言信息**。

        工作、健康、James、Sarah、Mike        ← 中文用户,给三个朋友各建了个桶
        工作、James、OpenAI、GitHub、Figma    ← 中文用户,在记几个项目

    人名、公司名、项目名全是拉丁字母，跟这个人说什么语言毫无关系。怎么数都救不了 ——
    问题不在怎么数，在于压根不该数它。

    所以现在的证据只有三样，**桶名一样都不占**：

        ① identity.language_preference   用户明说的,任何东西不该盖过它
        ② 他实际在用什么语言写            身份卡正文 + 这轮对话里他自己说的话
        ③ locale / archive_language      弱,只是设备设置

    ## 那「别让 工作 和 Work 并存」谁来管

    归一化 —— :func:`memgarden.prompts.buckets.normalize_bucket_language`。它只动
    固定配对表里的通用桶（健康 ↔ Health），自定义桶原样放行。**语言跟着人走，
    桶名跟着内容走**，两件事分开。

    ## 返回值

    全部**内容无关**：语言标签、依据名、证据词数。桶的计数也在里面，但它是
    **观测量不是判据** —— 「判成中文但 9 个桶里 7 个是拉丁字母」值得记一笔
    （可能归一化没生效），不值得据此改语言。
    """
    explicit = _language_from_hint(
        (identity or {}).get("language_preference") if isinstance(identity, dict) else ""
    )
    # 「他写的字」= 这轮对话里他自己说的话 + 身份卡正文。前者是最新最真的信号，
    # 后者在还没聊过时兜底。
    sample = "\n".join(x for x in ([written] + _identity_texts(identity or {})) if x)

    d = decide_garden_language(
        explicit=explicit or None,
        written=sample,
        locale=_language_from_hint(locale) or _language_from_hint(archive_language) or None,
    )
    zh, en = count_bucket_languages(existing_buckets or "")
    names = split_bucket_names(existing_buckets or "")
    return {
        **d,
        # ↓ 观测用，**没有参与上面的判定**。留着是为了能发现「语言判成中文，
        #   但桶几乎全是拉丁字母」这类归一化失效的情况。
        "bucket_zh": zh,
        "bucket_en": en,
        "bucket_total": len(names),
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


def failure_fallback_reply(
    policy: ReplyLanguagePolicy,
    *,
    zh: str = DEFAULT_FAILURE_FALLBACK_ZH,
    en: str = DEFAULT_FAILURE_FALLBACK_EN,
) -> str:
    """Choose the visible failure fallback from the same reply policy.

    Callers may preserve their existing environment overrides by passing both
    rendered strings.  Language selection remains shared with the system-prompt
    policy so a failed turn cannot silently switch language.
    """

    return en if policy.language == "en" else zh


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
