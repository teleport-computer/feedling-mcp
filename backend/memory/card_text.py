"""卡片字段的内容校验 —— capture / dream 共用的「这是真内容,还是把模板抄回来了?」闸。

背景(2026-07-26):一个跑 minimax-M3 的用户,记忆花园里出现了两类垃圾卡:
  1. 标题很长、正文只有 `...` —— 模型把叙述塞进 summary,把示例里的 `...` 抄进 content;
  2. summary/content 都是 `[thickened summary]` 这类方括号占位 —— 模型根本没填,
     只是把输出示例的骨架复述了一遍。

两类都能通过原来的解析:JSON 合法、字段存在、非空。原来的唯一内容判据是
「summary 和 content **全空**才丢」,于是占位符一路写进信封、落进花园,用户能亲眼看到。

这里把判据从「有没有字段」升级成「字段里是不是真内容」。**硬字段**(summary/content)
不合格 = 整行打回;**软字段**(bucket/threads)不合格 = 就地清洗(丢掉那一条),
但在 strict 模式下同样会触发一次重试 —— 软字段抄模板是「模型在复述骨架」的强信号。

模块保持纯函数、零 I/O、只依赖 stdlib,以便 resident 与 V2 两条运行时共用一份判据。
"""
from __future__ import annotations

import re
import unicodedata

# 输出示例里出现过的骨架文字。弱模型常原样抄回来。
_TEMPLATE_FRAGMENTS = (
    "一段厚的正文",
    "被并/被厚化/被取代的卡",
    "merge/supersede 时填被并",
    "拿不准的矛盾",
    "merge | thicken | supersede",
    "add | merge | supersede",
    "event | fact | quote | moment",
    "thickened summary",
    "thickened content",
)

# 方括号/尖括号/花括号占位:[thickened summary]、<summary>、{content}、【正文】。
# 刻意不含圆括号 —— 「(他终于答应去看医生)」是合法写法。
_BRACKET_PLACEHOLDER_RE = re.compile(r"^[\[<{【]\s*[^\]>}】]{0,120}\s*[\]>}】]$")

# 整段就是这么一个占位词时才算(不做子串匹配,避免误伤正常句子)。
_PLACEHOLDER_WORD_RE = re.compile(
    r"^(?:tbd|todo|n/?a|none|null|nil|undefined|placeholder|example|summary|content|"
    r"bucket|thread|threads|string|text|xxx+|待填|待补|占位|示例|同上|略|无)$",
    re.IGNORECASE,
)

# 下限刻意压到只能拦「明显没写」(单字残留),不评判写得好不好 —— 卡片写得薄是
# 模型能力问题,不该由这道闸来判死;它只负责拦占位符和空壳。
MIN_SUMMARY_CHARS = 2
MIN_CONTENT_CHARS = 2

_FORMAT_ERROR_PREFIX = "invalid_card_content"
_AFTER_RETRY_ERROR_PREFIX = "invalid_card_content_after_retry"


def _is_substantive_char(ch: str) -> bool:
    """字母或数字(任何语种)。标点、省略号、空白、emoji、组合符号都不算。

    ⚠️ 用 unicodedata 而不是字符区间白名单:早先那版只列了 ASCII/CJK/假名/谚文,
    阿拉伯文、西里尔文、希伯来文的整张卡实义字数会算成 0 → 整语种被误判成
    「只有标点」而全部打回(codex 07-26 review P1-1)。判「有没有字」这件事必须
    语种中立,否则这道闸会变成对非拉丁非 CJK 用户的静默封杀。
    """
    return unicodedata.category(ch)[0] in {"L", "N"}


def substantive_len(text: str) -> int:
    """有效字符数(不含标点/空白/emoji)。"""
    return sum(1 for ch in (text or "") if _is_substantive_char(ch))


def placeholder_reason(text: str) -> str | None:
    """``text`` 是模板残留而非真内容时返回一个短码,否则 None。"""
    s = (text or "").strip()
    if not s:
        return "empty"
    if _BRACKET_PLACEHOLDER_RE.match(s):
        return "bracket_placeholder"
    if _PLACEHOLDER_WORD_RE.match(s):
        return "placeholder_word"
    for fragment in _TEMPLATE_FRAGMENTS:
        if fragment in s:
            return "template_fragment"
    if substantive_len(s) == 0:
        # 纯标点/省略号/emoji:"..."、"…"、"。"、"— —"
        return "no_substantive_chars"
    return None


def card_text_rejection(*, summary: str, content: str) -> str | None:
    """硬字段体检。返回 ``None``=可以落库,否则 ``"<字段>_<原因>"``。

    与旧判据的差别:旧的是「两个都空才拦」,现在是**两个都必须是真内容**。
    「长标题 + 空正文」正是用户看到的第一类垃圾卡。
    """
    reason = placeholder_reason(summary)
    if reason:
        return f"summary_{reason}"
    reason = placeholder_reason(content)
    if reason:
        return f"content_{reason}"
    if substantive_len(summary) < MIN_SUMMARY_CHARS:
        return "summary_too_short"
    if substantive_len(content) < MIN_CONTENT_CHARS:
        return "content_too_short"
    return None


def sanitize_card_labels(
    *, bucket: str, threads: list[str]
) -> tuple[str, list[str], list[str]]:
    """清洗软字段。返回 ``(bucket, threads, reasons)``;``reasons`` 非空表示确实洗掉了东西。

    ⚠️ ``reasons`` **不参与**打回判定 —— 它只是「洗掉了什么」的记录。硬内容
    (summary/content)完全正常、只是 ``bucket="无"`` 的卡不值得为它多烧一次
    BYOK 调用;软字段的定义就是「能就地修好」(codex 07-26 review P1-2)。
    """
    reasons: list[str] = []
    clean_bucket = (bucket or "").strip()
    reason = placeholder_reason(clean_bucket) if clean_bucket else None
    if reason:
        reasons.append(f"bucket_{reason}")
        clean_bucket = ""
    clean_threads: list[str] = []
    for thread in threads or []:
        text = str(thread or "").strip()
        if not text:
            continue
        reason = placeholder_reason(text)
        if reason:
            reasons.append(f"threads_{reason}")
            continue
        clean_threads.append(text)
    return clean_bucket, clean_threads, reasons


def format_error(reasons: list[str], *, after_retry: bool = False) -> str:
    """把逐行的拒绝码收敛成一个可进 job status 的短 reason。

    ``after_retry=True`` 换一个前缀:那是「打回重问之后还是全脏」的终局,
    调用方必须让 job **失败**(而不是伪装成 noop 推进 frontier),
    data-track 上也要能和第一问的打回分开看。
    """
    prefix = _AFTER_RETRY_ERROR_PREFIX if after_retry else _FORMAT_ERROR_PREFIX
    seen: list[str] = []
    for reason in reasons:
        if reason and reason not in seen:
            seen.append(reason)
    if not seen:
        return prefix
    return f"{prefix}:" + ",".join(seen[:3])


def is_card_format_error(err: str | None) -> bool:
    """这个 reason 是不是「值得原样打回去重来一次」的格式问题。

    只认内容闸**第一问**发的码 —— provider 挂了、JSON 根本没出来(``no_json_object``)
    这类问题重问一遍也没用,不在此列;它们各有自己的重试/退避路径。
    ``invalid_card_content_after_retry`` 也刻意不在此列:那已经是第二问的终局,
    再打回就成了死循环。
    """
    return bool(err) and str(err).split(":", 1)[0] == _FORMAT_ERROR_PREFIX


_REASON_TEXT = {
    "summary_empty": "summary 是空的",
    "summary_bracket_placeholder": "summary 还是方括号占位(例如 [thickened summary])",
    "summary_placeholder_word": "summary 只填了一个占位词",
    "summary_template_fragment": "summary 抄了输出示例里的说明文字",
    "summary_no_substantive_chars": "summary 只有标点/省略号,没有字",
    "summary_too_short": "summary 太短,看不出这张卡是什么",
    "content_empty": "content 是空的",
    "content_bracket_placeholder": "content 还是方括号占位",
    "content_placeholder_word": "content 只填了一个占位词",
    "content_template_fragment": "content 抄了输出示例里的说明文字(例如「一段厚的正文」)",
    "content_no_substantive_chars": "content 只有省略号/标点,没有正文",
    "content_too_short": "content 太短,不是一段正文",
    "bucket_bracket_placeholder": "bucket 是占位符",
    "bucket_placeholder_word": "bucket 只填了一个占位词",
    "bucket_no_substantive_chars": "bucket 只有标点",
    "bucket_template_fragment": "bucket 抄了示例",
    "threads_bracket_placeholder": "threads 里有占位符",
    "threads_placeholder_word": "threads 里有占位词",
    "threads_no_substantive_chars": "threads 里有只剩省略号的条目",
    "threads_template_fragment": "threads 抄了示例",
}


def _problem_text(err: str | None) -> str:
    codes = str(err or "").split(":", 1)[-1].split(",") if err else []
    parts = [_REASON_TEXT.get(code.strip()) for code in codes if code.strip()]
    named = [p for p in parts if p]
    return "；".join(named) if named else "有字段没有真正填写"


def build_format_retry_prompt(prompt: str, err: str, *, empty_example: str) -> str:
    """在原 prompt 后面追加一段「打回重做」的说明,用于同一 lane 的第二次尝试。

    刻意保留原 prompt 全文(而不是只发一段纠错):模型这一轮是无状态的,
    上下文(现有的卡/这段对话)必须再给一次,否则它只能凭空编。
    """
    return (
        f"{prompt}\n\n"
        "【上一次的输出被打回,请重做】\n"
        f"问题:{_problem_text(err)}。\n"
        "这不是 JSON 语法错误 —— 是内容没写:你把上面输出示例里的占位符"
        "(`...`、方括号里的说明、「一段厚的正文」之类)原样抄了回来,或者留了空。\n"
        "这些卡是本人会亲眼看到的记忆,占位符会直接显示成空白卡片。\n"
        "\n"
        "再来一次,仍然只输出 JSON,并且:\n"
        "· summary:一句真实的话,一眼看出这张卡是什么。不能是 `...`、`[...]`、空字符串。\n"
        "· content:完整的一段正文(至少一两句),写清楚发生了什么。不能只有省略号,"
        "也不能只是把 summary 重复一遍。\n"
        "· bucket / threads:写具体的词,不要照抄示例里的 `...`。\n"
        f"· 如果其实没有值得写的,就输出 {empty_example} —— 空结果完全可以接受,"
        "比填占位符好得多。\n"
    )
