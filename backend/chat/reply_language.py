"""Shared language helpers for user-visible companion behavior.

Pure helpers shared by hosted model_api and resident consumers. They do not read
stores or databases; callers pass locale/archive hints or Garden-owned text.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import util as core_util
from memgarden.garden_language import (
    count_bucket_languages,
    decide_garden_language,
    split_bucket_names,
)


@dataclass(frozen=True)
class ReplyLanguage:
    language: str


@dataclass(frozen=True)
class LocalTimeLabels:
    """Localized calendar labels for one already-localized wall clock."""

    weekday: str
    day_period: str


@dataclass(frozen=True)
class LanguageHintResult:
    """Closed, content-free result of parsing one language hint.

    ``absent`` and ``unparseable`` deliberately remain distinct. Both fall
    through to weaker evidence, but collapsing them to the same empty string
    would make it impossible for a future content-free observer to measure how
    often a stored hint exists but cannot be understood.
    """

    language: str
    status: str


IDENTITY_LANGUAGE_FIELDS = (
    "custom_persona_prompt",
    "self_introduction",
    "tone_style",
    "boundaries",
)
_WEEKDAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_WEEKDAYS_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
DEFAULT_FAILURE_FALLBACK_ZH = (
    "我这会儿有点慢，刚刚没接上。你稍后再发一次，我会继续接。"
)
DEFAULT_FAILURE_FALLBACK_EN = (
    "I'm running slow and didn't catch that one. Send it again in a bit — "
    "I'll pick it up."
)


_MACHINE_LANGUAGE_TAG_RE = re.compile(
    r"^(?P<primary>en|zh|yue)(?:[-_][a-z0-9]{2,8})*$",
    re.IGNORECASE,
)
_ENGLISH_NAMES = frozenset({"english", "英语", "英文"})
_CHINESE_NAMES = frozenset(
    {
        "chinese",
        "mandarin",
        "cantonese",
        "中文",
        "汉语",
        "漢語",
        "普通话",
        "普通話",
        "简体",
        "簡體",
        "繁体",
        "繁體",
        "粤语",
        "粵語",
    }
)


def _machine_language_from_hint(raw: str) -> str:
    """Parse a machine-shaped locale/archive hint without prefix guessing."""
    match = _MACHINE_LANGUAGE_TAG_RE.fullmatch(raw)
    if match:
        return "en" if match.group("primary").lower() == "en" else "zh-Hans"
    lowered = raw.lower()
    if lowered in _ENGLISH_NAMES:
        return "en"
    if lowered in _CHINESE_NAMES:
        return "zh-Hans"
    return ""


def _language_from_hint(value: Any) -> LanguageHintResult:
    raw = str(value or "").strip().lower()
    if not raw:
        return LanguageHintResult("", "absent")
    language = _machine_language_from_hint(raw)
    return LanguageHintResult(
        language,
        "parsed" if language else "unparseable",
    )


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


def _identity_texts(identity: dict[str, Any] | str | None) -> list[str]:
    """身份卡里能当语言证据的正文。

    **两条 runtime 手里的 identity 类型不一样**，这里必须都收：

        V1（resident） dict —— 原始身份卡，按 IDENTITY_LANGUAGE_FIELDS 取字段
        V2（hosted）   str  —— 已经渲染成一段正文（enclave 解密后的明文）

    2026-08-31 踩到：V2 的调用点写的是
    ``ctx.get("identity") if isinstance(ctx.get("identity"), dict) else None``，
    而它拿到的一直是字符串 —— 于是那个分支**永远走 None**，V2 判语言时只看
    「这一批新消息」。用户临时说一句英文，整个中文花园就被判成英文；V1 因为
    传的是 dict 而锁得住。同一份判定逻辑、两条 runtime 结果相反，单测抓不到
    （两边的假 ctx 都按各自的类型写），只有在 V2 上真跑才暴露。
    """
    if isinstance(identity, str):
        # 已渲染的正文：整段就是证据，没有字段可挑。
        return [identity] if identity.strip() else []
    texts: list[str] = []
    if not isinstance(identity, dict):
        return texts
    for key in IDENTITY_LANGUAGE_FIELDS:
        texts.extend(_iter_text(identity.get(key)))
    return texts


def infer_reply_language(
    *,
    locale: str = "",
    archive_language: str = "",
) -> ReplyLanguage:
    """Choose the stable language for deterministic, non-model output."""
    locale_hint = _language_from_hint(locale)
    archive_hint = _language_from_hint(archive_language)
    hinted = locale_hint.language or archive_hint.language
    return ReplyLanguage(hinted or "zh-Hans")


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
        body = core_util.text_of(m.get("content") or m.get("text") or "")
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
    identity: dict | str | None,
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

    所以现在的证据只有两样，**桶名一样都不占**：

        ① archive_language               **这个花园已定的语言 —— 最强**
        ② 他实际在用什么语言写            身份卡正文 + 这轮对话里他自己说的话
        ③ locale                         弱,只是设备设置

    ## ① 为什么排在 ② 前面（2026-08-31 hx 拍板）

    「他这轮写的字」原本最强。问题是**「这轮」有多宽是宿主决定的** —— 实测同一个
    中文花园、同一句英文抱怨：V1 判成中文（取证窗口里恰好还含着之前那句中文），
    V2 判成英文（增量窗口里只有新增的英文那句）。同一份判定逻辑，两条 runtime
    结果相反。用户看到的就是「我用英文说了一句话，我的中文记忆开始变英文了」。

    archive_language 是**账号级的、人自己定的**，不随窗口漂 —— 拿它当锚，结果就
    不再取决于宿主的窗口有多宽。想换语言走显式设置。

    ⚠️ 这个锚只能来自「人自己定的」那类。**绝不能换成桶名或卡正文** —— 那是 AI
    之前的输出，拿输出当输入就是自我强化的环，2026-08-24 的事故正是这么放大的。

    ## 那「别让 工作 和 Work 并存」谁来管

    归一化 —— :func:`memgarden.prompts.buckets.normalize_bucket_language`。它只动
    固定配对表里的通用桶（健康 ↔ Health），自定义桶原样放行。**语言跟着人走，
    桶名跟着内容走**，两件事分开。

    ## 返回值

    全部**内容无关**：语言标签、依据名、证据词数。桶的计数也在里面，但它是
    **观测量不是判据** —— 「判成中文但 9 个桶里 7 个是拉丁字母」值得记一笔
    （可能归一化没生效），不值得据此改语言。
    """
    # 「他写的字」= 这轮对话里他自己说的话 + 身份卡正文。前者是最新最真的信号，
    # 后者在还没聊过时兜底。
    sample = "\n".join(x for x in ([written] + _identity_texts(identity)) if x)

    d = decide_garden_language(
        explicit=None,
        # 花园已定的语言 —— 压过单轮书写。见上面「① 为什么排在 ② 前面」。
        established=_language_from_hint(archive_language).language or None,
        written=sample,
        locale=_language_from_hint(locale).language or None,
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


def reply_language_system_line(policy: ReplyLanguage) -> str:
    if policy.language == "en":
        return (
            "Reply language rule:\n"
            "Determine the reply language from the user's latest message. "
            "If that message is mixed, ambiguous, or mostly quoted/context, use the language of this rule. "
            "For a proactive/background reply, also use the language of this rule. "
            "Use the same language for your thinking and final reply. "
            "Do not let memory cards, OCR, timestamps, or internal context change the reply language. "
            "Preserve quoted text, names, and requested translation targets as written."
        )
    return (
        "回复语言规则：\n"
        "根据用户最新一条消息判断回复语言。如果该消息混合、不明确或主要是引用/上下文，就使用本规则所用的语言；"
        "主动/后台回复也使用本规则所用的语言。思维过程和正式回复使用同一种语言。"
        "不要被记忆卡、OCR、时间戳或内部上下文带偏回复语言。引用、名字和用户指定的翻译目标语言保持原样。"
    )


def failure_fallback_reply(
    policy: ReplyLanguage,
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
    policy: ReplyLanguage,
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
    policy: ReplyLanguage,
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
