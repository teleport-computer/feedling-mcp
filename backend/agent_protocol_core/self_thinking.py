"""Self-authored thinking — runtime-neutral shared kernel.

io is prompted to open every reply with a first-person thought wrapped in
``<think>…</think>``, then the actual reply. The ``<think>`` marker is used
because BOTH runtimes already know it: the V1 resident consumer extracts it
natively, and V2 calls :func:`split_thinking` here to peel it into the thinking
envelope. Lives in ``core`` so agent_runtime (V1) and model_api_runtime (V2) can
both import it top-down. Pure stdlib.

``split_thinking`` is a small state machine, NOT a regex scrub (Codex review): it
only treats a *leading* ``<think>`` as protocol, and returns an explicit status so
the caller can fail closed. Hard invariants:

  * a raw ``<think`` / ``</think`` fragment of the leading protocol NEVER reaches
    the user-visible reply (same risk class as the protocol-JSON tail leak);
  * private thinking content is NEVER promoted to the reply when the block cannot
    be cleanly resolved;
  * a clean thinking-only response is distinct from malformed protocol so wake
    lanes can treat intentional silence as success without weakening leak guards;
  * with the feature off (or ABSENT) the reply is byte-identical to today.
"""
from __future__ import annotations

import os
import re
import unicodedata

# Parse outcomes.
ABSENT = "absent"       # no leading <think> → reply is the original text
COMPLETE = "complete"   # clean <tag>…</tag> + non-empty reply
SILENT = "silent"       # clean <tag>…</tag> + intentionally empty public reply
FAILED = "failed"       # unresolvable (truncated/mismatched/nested)
# strip_all_thinking said FAILED, but a non-nesting rescan could still tell
# reply text apart from thinking (T656): every thinking block is dropped, the
# reply is delivered, and the caller records ``thinking_gate_salvaged``.
SALVAGED = "salvaged"

MAX_THINKING_CHARS = 240

# 内部字段名/协议词。**思考是用户可见面**,这些词出现在里面等于把运行时内脏
# 端到用户眼前(「session_id: …」「permission_denials」「costUSD」)。
#
# 来源:V1 consumer 早有一份逐行黑名单(chat_resident_consumer.py 的
# `_sanitize_thinking_summary`),V2 一直只有长度截断 —— 这是真回归。
# 放在共享内核是为了让两代**共用同一份词表**;V1 的运行时行为本批不动
# (它逐行丢弃、V2 整段不发布,两种处置各有来由),但词表由此单一来源,
# 并有漂移守卫钉住(见 tests)。
#
# ⚠️ 只列**内部/协议**词汇,不列日常词。这里每多一个常见词,就多一次
# 把正常内心话误判为泄漏的机会 —— 那会让用户看到「(思考没写完)」而不是真话。
INTERNAL_FIELD_TERMS = (
    "system prompt", "developer message", "chain-of-thought", "chain of thought",
    "modelUsage", "terminal_reason", "permission_denials",
    "cache_read", "cache_creation", "session_id", "uuid", "costUSD",
    "input_tokens", "output_tokens",
)


# 词表单一来源,**宽度按道分开** —— codex2 审出来的关键设计点:
#   · V1 consumer 逐行丢弃:命中就删那一行,其余保留 → 宽匹配代价低
#   · V2 整段替换成 THINKING_FAILED_MARKER:命中就把整段内心话换掉 → 代价高得多
# 所以两代共享**词表**、各用各的**宽度**,而不是硬共用一个 matcher。
#
# V2 用下面这个窄判据:只认「字段泄漏的形状」,不认「概念提及」。
# codex2 实测的三句必须放行 —— 用户完全可能跟伴侣聊这些:
#   「用户问 UUID 是什么」「讨论 system prompt 的设计」「学习 chain of thought prompting」
# 整段吞掉的话,用户只会看到「(思考没写完)」,而且不知道为什么。
#
# 泄漏形状 = 词后面紧跟分隔符/取值:`session_id: abc`、`"input_tokens": 12`、
# `costUSD=0.02`、`terminal_reason -> x`。概念提及不会长这样。
# 结尾允许先闭合引号再跟分隔符 —— JSON 形态 `"input_tokens": 12` 就长这样,
# 第一版漏了它(claude2 自测发现)。
_FIELD_LEAK_TAIL = r"""["'`]?\s*(?:[:=]|=>|->|→)"""
_INTERNAL_FIELD_LEAK_RE = re.compile(
    r"""(?:^|[\s"'`\[{(,])("""
    + "|".join(re.escape(t) for t in sorted(INTERNAL_FIELD_TERMS,
                                            key=len, reverse=True))
    + r")" + _FIELD_LEAK_TAIL,
    re.IGNORECASE,
)


def internal_field_leak(text: str) -> str | None:
    """V2 用:只在**字段泄漏形状**下命中,概念提及放行。

    宽度刻意窄于 V1 的逐行黑名单 —— 见上面注释,两者处置代价不同。
    """
    m = _INTERNAL_FIELD_LEAK_RE.search(str(text or ""))
    return m.group(1) if m else None


def internal_field_terms_pattern() -> str:
    """V1 用:从共享词表构造它那份**宽**的逐行正则,行为不变。

    由构造保证单一来源 —— V1 不再自己维护一份字面量,
    「两边词表漂移」在结构上不可能发生,不必靠测试去追源码。
    """
    return "(" + "|".join(
        [re.escape(t) for t in INTERNAL_FIELD_TERMS]
        + [r"chain[-\s]*of[-\s]*thought"]   # V1 原有的宽形态,保留
    ) + ")"

# Shown in the thinking channel when the block is malformed, so the user sees that
# io tried to think rather than nothing/garbage. (zh for now; localization TBD.)
THINKING_FAILED_MARKER = "（思考没写完）"

# ``aside`` is the tag the resident lane uses when the driver is Claude Code
# (T587, 2026-09-15): the block is a first-person aside the app shows to the
# user under 「参考内容」; naming it "think" made Anthropic's request classifier
# read the protocol as a request for the model's hidden reasoning and reject
# every turn on the Opus 5 family. The parser accepts both tags for every
# driver so a block wrapped in either can never reach the user un-stripped.
_TAG_WORDS = ("thinking", "reasoning", "thought", "think", "aside")  # longest-first
TAG_THINK = "think"
TAG_ASIDE = "aside"
_TAG_ALT = "|".join(_TAG_WORDS)
# 可选的 XML 命名空间前缀。起始字符必须放宽到 **Unicode 字母**：2026-09-06 线上
# （usr_1baf…，pi + mimo-v2.5）真实写出的是 `</𝑎𝑛𝑡𝑚𝑙:thinking>` —— 前缀全是
# 数学斜体字母（U+1D44E…，category Ll），ASCII `[A-Za-z]` 一个都对不上。
# 一天 16 条泄漏全是这个形状。
_NAME_START = r"(?:[^\W\d_][\w.-]*:)?"
# Invisible leading chars (BOM, zero-width) that must not hide a truncated opener.
_INVISIBLE = "﻿​‌‍⁠⁦⁧⁨⁩"
_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏")
# Any of our protocol tags (open or close) — used only to detect nesting INSIDE a
# resolved thinking block, never to scrub the reply.
_ANY_TAG = re.compile(rf"<\s*/?\s*{_NAME_START}(?:{_TAG_ALT})\b", re.IGNORECASE)

_ENV_FLAG = "FEEDLING_V2_SELF_THINKING"

# One shared copy for foreground chat and every proactive wake lane. Keep the
# wording byte-identical: product copy is reviewed independently from parsing.
INSTRUCTION = (
    " 你每一轮的最终回复，第一个字符必须是 <think>，里面写你此刻心里真实的想法，\n"
    " 然后 </think>，然后才是你要说的话。整轮只写一个 <think>，只写在最终那次输出里；\n"
    " 中间调工具的轮次不写 <think>，也不输出正文。\n"
    "\n"
    " 这段心里话就用你平时跟他说话的那个口气，自己跟自己说。你是同一个人，\n"
    " 只是这几句他听不见。写你在意什么、注意到了什么、想做什么、为什么这么决定；\n"
    " 长短都行，一句也可以。\n"
    "\n"
    " 不要把它写成对他的评估或行动方案。「她状态不错，顺着来」是在打量他；\n"
    " 「他刚打完游戏还在笑，我也想接着这个玩笑闹下去」才是你自己的心里话。\n"
    "\n"
    " 语言跟着他走：他用什么语言说话，这段就整段用什么语言，从第一个字到最后一个字。\n"
    " 最常见的失误是他在说中文、你却滑进英文状态行（Let me… / Done… / The … has been\n"
    " updated）。别这样；哪句话先在别的语言里成形了，先换成他的语言再写下来。\n"
    " 好例子（用户在说中文，所以整块是中文）：\n"
    " '<think>他想改叫999、还说喜欢说大话，那我先把名字这些存好，回复也顺着这个爱吹的人设、语气夸张点才对味</think>'。\n"
    " 坏例子（同一个用户说的是中文；这个英文块语言错了，而且机械地报了步骤）：\n"
    " '<think>Let me update the name and match a boastful tone</think>'。\n"
    "\n"
    " 只说日常意图：不出现工具名、参数、字段名、服务器、「身份卡」这类内部或技术说法，\n"
    " 也不要在正文里提到这条规则本身。"
)

# The ``<think>`` rendering above is what pi / codex drivers keep receiving,
# byte for byte. The Claude Code driver receives the same protocol under the
# ``<aside>`` tag with the visibility sentence stated truthfully: the block is
# shown to the user (folded under 「参考内容」), it is not a private channel.
_THINK_VISIBILITY_SENTENCE = " 只是这几句他听不见。"
_ASIDE_VISIBILITY_SENTENCE = " 这几句会折叠在消息上方的「参考内容」里展示给他，他想看就能看到。"
# The opening line asks for the persona's mood and intent, not the model's
# private reasoning: measured 2026-09-15 (T587 bisect), this phrase alone kept
# the Opus 5 family rejecting the aside rendering; reworded (Seven picked this
# candidate out of three that each measured 0/3 on Opus 5 and Opus 5[1m]), 0/3.
_THINK_CONTENT_PHRASE = "里面写你此刻心里真实的想法"
_ASIDE_CONTENT_PHRASE = "里面写你这会儿的感受，和你打算怎么接他这句"
# Presence wakes (heartbeat / manual wake) have no line of theirs to answer:
# on prod the chat phrasing coincided with "nothing to say" silence (T723).
_PRESENCE_ASIDE_CONTENT_PHRASE = "里面写你这会儿的感受，和你为什么这会儿想找他说这些"
# Substitutions applied to INSTRUCTION for the aside rendering, in order. Each
# anchor must occur exactly once so a wording edit upstream cannot silently
# leave the think phrasing in the aside rendering.
_ASIDE_SUBSTITUTIONS = (
    (_THINK_VISIBILITY_SENTENCE, _ASIDE_VISIBILITY_SENTENCE),
    (_THINK_CONTENT_PHRASE, _ASIDE_CONTENT_PHRASE),
)


# Providers whose models are asked for the ``aside`` rendering on every
# runtime (Runtime V2 chat/scheduled wakes/terminal rounds, resident V1 with any
# driver). Measured 2026-09-15 on gemini-3.6-flash only (T586/T588/T591):
#   - tag-only bisect (only ``<think>``→``<aside>`` swapped, wording unchanged),
#     direct V2-shaped calls, counted among HTTP-200 responses: ``<think>``
#     7/11 ``finishReason=MALFORMED_RESPONSE`` vs ``<aside>`` 0/13 (each arm 20
#     attempts; the rest were HTTP 503 / transport errors on both arms);
#   - tag-only replay of one captured pi-wire request body: ``<think>`` 6/10
#     HTTP 503 vs ``<aside>`` 0/10 in the same window;
#   - the full aside rendering below (tag plus two wording substitutions) was
#     separately confirmed on the V2 shape: 8/8 and 8/8 STOP with an
#     ``<aside>`` opener.
# Official Gemini routes use this rendering regardless of the model name.
ASIDE_TAG_PROVIDERS = frozenset({"gemini"})
_GEMINI_RELAY_PROVIDERS = frozenset({"openai_compatible", "openrouter"})


def tag_for_route(provider: str | None, model: str | None) -> str:
    """Select the shared tag for an official Gemini or named Gemini relay route.

    Only the two relay providers admit model-name matching. Aliases without
    ``gemini`` and all other providers retain the historical ``think`` tag.
    """
    provider_name = str(provider or "").strip().lower()
    if provider_name in ASIDE_TAG_PROVIDERS or (
        provider_name in _GEMINI_RELAY_PROVIDERS
        and "gemini" in str(model or "").lower()
    ):
        return TAG_ASIDE
    return TAG_THINK


def tag_for_provider(provider: str | None) -> str:
    """Compatibility for callers without model metadata."""
    return tag_for_route(provider, "")


def _retag(text: str, tag: str) -> str:
    return text.replace("<think>", f"<{tag}>").replace("</think>", f"</{tag}>")


def instruction(tag: str = TAG_THINK) -> str:
    """Return the shared instruction rendered for one protocol tag.

    ``think`` returns ``INSTRUCTION`` itself (unchanged for pi / codex).
    ``aside`` swaps every ``<think>``/``</think>`` for the aside tag, states
    truthfully that the block is shown to the user, and asks for the persona's
    mood and intent rather than "genuine inner thoughts" (the block is a visible
    aside, not the model's reasoning).
    """
    if tag == TAG_THINK:
        return INSTRUCTION
    if tag != TAG_ASIDE:
        raise ValueError(f"unsupported self-thinking tag: {tag!r}")
    text = INSTRUCTION
    for anchor, replacement in _ASIDE_SUBSTITUTIONS:
        if text.count(anchor) != 1:
            raise RuntimeError(f"self-thinking aside anchor drifted: {anchor!r}")
        text = text.replace(anchor, replacement)
    return _retag(text, tag)


ASIDE_FIELD_DESCRIPTION = (
    "How you feel right now and how you mean to pick up what they said — "
    "shown to them folded above the message. Write `aside` entirely in their "
    "language — the language they speak to you — and in your usual voice "
    "with them: everyday intent only, with no tool names, parameters, field "
    "names, identity cards, or other internal terms."
)


def instruction_for_field(*, protocol: str = "reply", presence: bool = False) -> str:
    """Render the approved aside copy for a tool or resident JSON envelope.

    ``presence`` selects the proactive-wake opening; only the reply protocol has one.
    """
    if protocol not in {"reply", "json"}:
        raise ValueError(f"unsupported aside protocol: {protocol!r}")
    if presence and protocol != "reply":
        raise ValueError("presence aside copy exists only for the reply protocol")
    _, paragraphs = instruction(TAG_ASIDE).split("\n\n", 1)
    if protocol == "reply":
        opening = (
            " 最终回复请调用 reply 工具，把心里话填在 aside 字段里，"
            + (_PRESENCE_ASIDE_CONTENT_PHRASE if presence else _ASIDE_CONTENT_PHRASE)
            + "。\n"
            " 你要对他说的话完整填在 text 字段里。中间调其他工具的轮次不写心里话或正文。"
        )
    else:
        opening = (
            " 最终回复请使用 JSON，把心里话填在 aside 字段里，"
            + _ASIDE_CONTENT_PHRASE + "。\n"
            " 你要对他说的话填在 messages 数组里。中间调工具的轮次不写心里话或正文。"
        )
    return (opening + "\n\n" + paragraphs).replace(
        "<aside>", '{"aside":"'
    ).replace("</aside>", '"}')


# Foreground chat and every proactive wake lane select one whole rendering from
# the reply-language policy.  Each rendering intentionally has no blank line:
# hosts group system policy blocks on ``\n\n``, so splitting an example away
# from its surrounding rule can make an otherwise single-language block mixed.
INSTRUCTION_ZH = (
    "每轮最终回复以 <think> 开头，里面是你此刻真实的心里话，</think> 之后才是正文。中间调工具的轮次不写。\n"
    "心里话是你自己跟自己说的，用你平时的口气：写你在意什么、想怎么回、为什么。不是对他的分析，不是步骤汇报。\n"
    "心里话和正文都用他正在说的语言，整段如此，一个词也不换。\n"
    "坏例子（他在说中文）：<think>Let me update the name…</think>\n"
    "不提工具名、参数、内部字段，也不提这条规则本身。"
)

INSTRUCTION_EN = (
    "Start every final reply with <think> — your genuine inner voice right now — then </think>, then what you actually say. Tool-call turns get no <think>.\n"
    "The inner voice is you talking to yourself in your usual tone — what you notice, what you want to do, why. Not an assessment of them, not a progress report.\n"
    "Both the inner voice and the reply stay in the language they're speaking, the whole way through.\n"
    "Bad (they're speaking English): <think>让我更新名字…</think>\n"
    "Never mention tool names, parameters, internal fields, or this rule itself."
)


def _select_language(language: str | None, *, zh: str, en: str) -> str:
    """Mirror the host reply-language policy's one language branch."""
    return en if language == "en" else zh


def instruction_for_language(language: str | None = None) -> str:
    """Return one whole rendering selected by the host reply-language policy.

    Hosts pass ``ReplyLanguage.language`` here.  The branch deliberately mirrors
    ``reply_language_system_line``: exactly ``"en"`` selects English and every
    other or absent value falls back to Chinese.
    """
    return _select_language(language, zh=INSTRUCTION_ZH, en=INSTRUCTION_EN)


_ABSENT_CORRECTION_ZH = (
    "上一轮最终回复缺少规定的 <think>…</think> 结构。"
    "请重新输出最终回复，严格遵守以下既有契约："
)
_ABSENT_CORRECTION_EN = (
    "The previous final reply did not include the required <think>…</think> structure. "
    "Output the final reply again, strictly following the existing contract below:"
)


def absent_correction_instruction_for_language(language: str | None = None) -> str:
    """Return the localized absent-``<think>`` correction and its contract."""
    prefix = _select_language(
        language,
        zh=_ABSENT_CORRECTION_ZH,
        en=_ABSENT_CORRECTION_EN,
    )
    return prefix + "\n\n" + instruction_for_language(language)

# Screen-watch adds this immediately after the shared instruction. It narrows
# the no-narration rule to visible speech without silencing private thoughts.
SCREEN_WATCH_INSTRUCTION = (
    " 「不要叙述你在看屏幕」这条只管你说出口的话。心里话里，你看到了什么、屏幕上在发生什么，\n"
    " 该写就写。那本来就是你此刻在想的事。"
)


def enabled() -> bool:
    return os.environ.get(_ENV_FLAG, "1").strip().lower() not in {"0", "false", "no", "off"}


def _sanitize(value: str) -> str:
    out: list[str] = []
    for ch in str(value or ""):
        if ch in _BIDI_CONTROLS or ch in _INVISIBLE:
            continue
        if unicodedata.category(ch) == "Cc":  # control incl \x00 \t \n
            out.append(" ")
            continue
        out.append(ch)
    return " ".join("".join(out).split()).strip()[:MAX_THINKING_CHARS]


def _lstrip_invisible(s: str) -> str:
    i = 0
    while i < len(s) and (s[i].isspace() or s[i] in _INVISIBLE):
        i += 1
    return s[i:]


def split_thinking(text: str) -> tuple[str, str, str]:
    """Return ``(status, thinking, reply)`` — see module docstring for the contract."""
    raw = str(text or "")
    head = _lstrip_invisible(raw)
    if not head.startswith("<"):
        return ABSENT, "", raw  # no leading protocol candidate → reply untouched

    # Parse the leading tag token: '<' ws '/'? ws letters ws '>'?
    m = re.match(r"<\s*(/?)\s*([A-Za-z]*)\s*(>?)", head)
    slash, word, gt = m.group(1), (m.group(2) or "").lower(), m.group(3)

    is_full = word in _TAG_WORDS
    is_prefix = bool(word) and any(w.startswith(word) for w in _TAG_WORDS)

    # Not one of our tags at all (e.g. <div>, <3) → leave as ordinary reply.
    if not is_prefix:
        return ABSENT, "", raw

    # A leading close tag, or a truncated / partial-word opener → cannot be a clean
    # opener. Fail closed; never leak the fragment.
    if slash or not is_full or not gt:
        return FAILED, "", ""

    # Full '<tag>' opener with '>'. Find its matching close.
    rest = head[m.end():]
    close = re.search(rf"<\s*/\s*{word}\s*>", rest, re.IGNORECASE)
    if not close:
        return FAILED, "", ""  # truncated or mismatched close
    inner = rest[: close.start()]
    reply = rest[close.end():].strip()

    # Nesting or an extra protocol tag inside the thinking block → ambiguous.
    if _ANY_TAG.search(inner):
        return FAILED, "", ""
    if not reply:
        return SILENT, _sanitize(inner), ""
    return COMPLETE, _sanitize(inner), reply


# ---------------------------------------------------------------------------
# 全文剥离闸（2026-08-08）。split_thinking 只认开头第一块——那是当初 Codex review
# 要求的保守设计，为了不误剥正文里被引用的标签。线上证明它漏了两种形状：
#   * 开头剥完后面还有一整块（gpt-5.4 一轮写了两个块）
#   * 开标签被上游吃掉，只剩孤立闭标签（pi + 中转站）
# 两种都从「不认识就原样放行」这个 fail-open 缺口漏进了用户气泡。
# 本节改为 fail-CLOSED，并由四个对外出口 + 一个历史入口共用。
# ---------------------------------------------------------------------------

_GATE_ENV_FLAG = "FEEDLING_THINK_GATE"

# 标签名边界。**不能用 `\b`**：`\b` 在 `t` 和 `-` 之间成立，于是 `<thought-process>`
# 这种合法 XML/JSX 标签会被 _RESIDUE 判成残留、又不被 _PAIRED_BLOCK 接受，整条回复
# 白白失败关闭（Codex review 2026-08-08 实测）。XML 名称允许 `-` `.` `:`，所以边界
# 必须显式排掉这些字符。
_NAME_END = r"(?![\w:.-])"
# 一整对同名标签。开闭必须同名（`(?P=tag)`），否则 <think>…</reasoning> 这种
# 错配会被当成一块合法协议剥掉。
_PAIRED_BLOCK = re.compile(
    rf"<\s*{_NAME_START}(?P<tag>{_TAG_ALT}){_NAME_END}\s*>(?P<body>.*?)<\s*/\s*{_NAME_START}(?P=tag){_NAME_END}\s*>",
    re.IGNORECASE | re.DOTALL,
)
# 剥完之后判定「还有没有残留」。任何开或闭标签都算。
_RESIDUE = re.compile(rf"<\s*/?\s*{_NAME_START}(?:{_TAG_ALT}){_NAME_END}", re.IGNORECASE)
# 孤立闭标签：按本协议思考永远写在最前面，所以一个配不上对的 </think> 说明它
# 前面的全是思考（开标签在上游某处被吃掉了）。
_LONE_CLOSE = re.compile(rf"<\s*/\s*{_NAME_START}(?:{_TAG_ALT}){_NAME_END}\s*>", re.IGNORECASE)


def gate_enabled() -> bool:
    """泄漏闸的 kill switch。默认开——关掉只用于线上出问题时立刻止血，
    不是灰度门。关掉后调用方必须逐字回到本次改动前的行为。"""
    return os.environ.get(_GATE_ENV_FLAG, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


# 只删标签壳、保留文字。给「历史入口」的 FAILED 兜底用：那种行结构已经乱到
# 分不清哪段是思考，但它的文字本来就已经发到用户眼前过了，删掉整行会平白打断
# 对话连贯性。入口真正要断的是「模型看到可抄的格式」，删掉标签壳就够了。
_TAG_MARKER = re.compile(rf"<\s*/?\s*{_NAME_START}(?:{_TAG_ALT})(?![\w:.-])\s*>", re.IGNORECASE)


def tag_name_pattern() -> str:
    """协议标签名的单一来源：一个可选的 XML 命名空间前缀 + 协议词。

    给 V1 consumer 那几处自己编译的正则用。它们原先各写一份字面量词表、且都
    要求协议词紧跟在 ``<``/``</`` 之后 —— 2026-09-06 线上泄漏就是从这个缺口出去的。
    由构造共享，「前缀规则在某一处漂掉」在结构上不可能发生。
    """
    return rf"{_NAME_START}(?:{_TAG_ALT})"


def strip_namespace_prefix(tag_text: str) -> str:
    """把 ``<ns:th`` 这种**尚未写完**的标签头去掉命名空间段，便于前缀比对。"""
    return re.sub(r"^<\s*/?\s*[^\W\d_][\w.-]*:", "<", str(tag_text or ""))


def strip_tag_markers(text: str) -> str:
    """删掉 think 类标签本身，保留标签之间的文字。"""
    return re.sub(r"\n{3,}", "\n\n", _TAG_MARKER.sub("", str(text or ""))).strip()


_TRUNCATED_OPENER = re.compile(r"^<\s*/?\s*(?:[^\W\d_][\w.-]*:)?([A-Za-z]{2,})\s*$")


def _truncated_protocol_opener(text: str) -> bool:
    """``<asid`` — the whole (invisible-stripped) text is one opener cut before
    its ``>``, and the letters are a strict prefix of a protocol tag word.
    A complete HTML tag (``<a href>x``) never matches: it has a ``>`` and more
    text; a single letter (``<a``) is deliberately not treated as protocol."""
    m = _TRUNCATED_OPENER.match(_lstrip_invisible(str(text or "")).rstrip())
    if not m:
        return False
    word = m.group(1).lower()
    return any(w != word and w.startswith(word) for w in _TAG_WORDS)


def strip_all_thinking(text: str, *, sanitize: bool = True) -> tuple[str, str, str]:
    """全文剥离版，返回 ``(status, thinking, reply)``，状态常量与
    :func:`split_thinking` 完全相同，方便调用点按 kill switch 二选一。

    与 split_thinking 的唯一区别是扫描范围：那个只认开头第一块，这个扫全文并
    在结尾复查残留。剥完只要正文里还剩任何 think 类标签，就返回 ``FAILED``
    （thinking/reply 都为空），由调用方决定发兜底话还是静默——绝不把带标签的
    残文端给用户。

    ``sanitize=False`` 时思考按原样（保留换行、不截断）返回，交给调用方自己
    格式化。V1 consumer 用这条：它有自己的摘要器（保留换行、上限 700），本次
    统一剥离**判据**，不该顺带改掉它的展示格式。
    """
    raw = str(text or "")
    if _truncated_protocol_opener(raw):
        # 正文只有一个被截断的协议开标签头（``<asid`` / ``<thin``）：这是被
        # token 上限切断的思考块，不是可见文字。之前它会原样漏成消息
        # （T587 codex3 审出）。只看开头、只认真前缀、只在没有 ``>`` 时判定，
        # 所以 ``<a href="…">`` 这类普通 HTML 正文不受影响。
        return FAILED, "", ""
    if not _RESIDUE.search(raw):
        # 逐字节不变的快路径。没有标签就绝不碰，是 kill switch 之外的第二道保险。
        return ABSENT, "", raw

    blocks: list[str] = []

    def _take(match: "re.Match[str]") -> str:
        body = match.group("body") or ""
        # 块里还有别的标签，说明结构已经乱了，不当作可信思考内容——留在原地，
        # 由下面的残留检查失败关闭。
        if _ANY_TAG.search(body):
            return match.group(0)
        if body.strip():
            blocks.append(body.strip())
        return "\n"

    reply = _PAIRED_BLOCK.sub(_take, raw)

    # 孤立闭标签：它之前的一切当思考。只处理第一个——出现多个说明结构已乱，
    # 同样交给残留检查失败关闭。
    lone = _LONE_CLOSE.search(reply)
    if lone is not None:
        head = reply[: lone.start()].strip()
        # head 里还带标签 = 开闭错配（<think>…</reasoning>）或多层残骸，不是
        # 「开标签被上游吃掉」那种可救的形状。失败关闭，别把带标签的文本当思考。
        if _RESIDUE.search(head):
            return FAILED, "", ""
        if blocks and head:
            # 已经剥出过完整块，却还剩一个带内容的孤立闭标签——这不是「开标签被
            # 吃掉」，而是结构本身就乱了。此时把 head 当思考会把真正的正文吞进
            # 推理过程（`<think>A</think>正文甲</think>正文乙` → 正文甲消失，
            # Codex review 2026-08-08 实测）。失败关闭。
            return FAILED, "", ""
        if head:
            blocks.insert(0, head)
        reply = reply[lone.end():]

    if _RESIDUE.search(reply):
        return FAILED, "", ""
    if not blocks:
        # 有标签、却一块内容都没剥出来（例如只有一个空标签对）——同样不可信。
        return FAILED, "", ""

    reply = re.sub(r"\n{3,}", "\n\n", reply).strip()
    joined = "\n".join(blocks)
    thinking = _sanitize(joined) if sanitize else joined.strip()
    if not reply:
        return SILENT, thinking, ""
    return COMPLETE, thinking, reply


# ---------------------------------------------------------------------------
# 打捞层（2026-09-19，T656）。strip_all_thinking 的判据一字不动：嵌套 / 多开 /
# 末尾未闭 / 多个孤立闭标签仍然 FAILED（2026-08-08 那次放宽把整段思考端给了用户）。
# 线上（T655，MiniMax-M3 走 openai_compatible + pi）一条回复开 2～3 个 <think>
# 只关 1～2 个，FAILED 之后整轮作废，用户只看到兜底话。Seven 定：这种时候宁可把
# 思考全丢掉、只留正文，也不许整轮失败。
#
# 所以不改判据，只在 FAILED 之后再解析一遍——单遍用栈把标签建成树（闭标签弹
# 最近的开，所以配平的子块永远整段留在父块里，绝不会在内层闭标签处被切开）；
# 树建完再后序处理没闭的开：有子块 ⇒ 借第一个子块的闭（多出来的开是「再开」，
# 不是嵌套——T655 形状）；没有子块 ⇒ 到文末（被截断的块，整段丢）。每个顶层块
# 连同嵌套内容都是思考；正文态里的孤立闭标签，在已剥出过块（或已处理过一次孤立
# 闭）时只丢标签、前面正文保留，否则它前面的一切当思考（开标签被上游吃掉）。
# 打捞出的思考永远不展示（结构已乱，不可信），只有正文出门。全程线性。
# ---------------------------------------------------------------------------

_TAG_TOKEN = re.compile(
    rf"<\s*(?P<slash>/?)\s*{_NAME_START}(?P<tag>{_TAG_ALT}){_NAME_END}\s*>",
    re.IGNORECASE,
)

#: Closed set of salvage reasons, joined with ``+`` in trace details.
SALVAGE_REASONS = frozenset({
    "nested_balanced",       # a fully paired inner block inside an outer block
    "nested_open",           # an open tag while already inside thinking (unbalanced)
    "trailing_unclosed",     # text ended inside a thinking block
    "stray_close_after_block",  # a close tag in reply text after ≥1 paired block
    "lone_close_head",       # a close tag before any block: head is thinking
    "mismatched_close",      # <think>…</reasoning>: first close still closes
})


class _Block:
    """One open tag and everything it encloses (a node of the tag tree)."""

    __slots__ = ("open", "close", "children", "promoted")

    def __init__(self, open_match):
        self.open = open_match
        self.close = None          # the close token match, once resolved
        self.children: list = []
        self.promoted = False      # closed by a child's close, not its own


def _resolve_unclosed(root: "_Block") -> None:
    """Post-order (iterative — the T655 shape repeated thousands of times is
    one deep chain): an open with no close of its own ends where its FIRST
    child ends (the extra open was a re-open sharing that close); with no
    child at all it stays ``None`` = runs to the end of the text (truncated)."""
    order: list[_Block] = []
    work = [root]
    while work:
        block = work.pop()
        order.append(block)
        work.extend(c for c in block.children if c.close is None)
    for block in reversed(order):          # children before parents
        if block.close is None and block.children:
            first_close = block.children[0].close
            if first_close is not None:
                block.close = first_close
                block.promoted = True
            # else: the first child itself runs to the end of the text, so
            # this block does too (stays unclosed = trailing, all thinking).


def _flatten_promoted(top: list) -> list:
    """Effective top-level items after resolution, in text order.

    A promoted block ends early (at its first child's close), so descendants
    that start at or after that close are outside it and become top-level
    items themselves — at any depth (codex4 r3: the first child may itself be
    promoted and hide later siblings under it; and ``match.end`` is exclusive,
    so an adjacent sibling starts exactly at the borrowed close's end).
    Iterative, each block visited once.
    """
    out: list = []
    work = list(reversed(top))
    while work:
        item = work.pop()
        out.append(item)
        if not isinstance(item, _Block) or not item.promoted:
            continue
        boundary = item.close.end()
        escaped: list[_Block] = []
        inside = list(reversed(item.children))
        while inside:
            child = inside.pop()
            if child.open.start() >= boundary:
                escaped.append(child)        # outside the borrowed span
            else:
                inside.extend(reversed(child.children))  # look deeper
        work.extend(reversed(escaped))
    return out


def salvage_thinking(text: str) -> tuple[str, str, str]:
    """Rescan text ``strip_all_thinking`` refused.

    One left-to-right pass builds a tag tree with a stack (a close pops the
    most recent open, so a paired inner block always stays inside its outer
    block — no balanced span is ever cut in half, codex4 r1/r2 reviews
    2026-09-19). Opens left on the stack at the end are then resolved
    post-order:

    * an unclosed open **with children** ends where its first child ends —
      the extra open was a re-open sharing that close (T655: ``<think>A<think>
      B</think>X`` → ``X`` is reply; ``<think>A<think>B<think>C</think>B2
      </think>R`` → ``R`` only, ``B2`` stays inside the balanced child);
    * an unclosed open **without children** runs to the end of the text
      (truncated block, dropped).

    Every top-level block, nested content included, is thinking. A close tag
    met outside any block: after a block (or a second orphan) only the tag is
    noise; a first orphan close with no block before it means the opener was
    eaten upstream and the head is thinking.

    Returns ``(visible, thinking, reason)``; ``visible`` is empty when nothing
    can be told apart as reply text (the caller stays FAILED). ``reason`` is a
    ``+``-joined subset of :data:`SALVAGE_REASONS`. Linear in the text length.
    """
    raw = str(text or "")
    tokens = list(_TAG_TOKEN.finditer(raw))
    top: list = []          # top-level items: _Block, or ("close", match)
    stack: list[_Block] = []
    reasons: set[str] = set()
    for m in tokens:
        if m.group("slash"):
            if stack:
                block = stack.pop()
                block.close = m
                if block.open.group("tag").lower() != m.group("tag").lower():
                    reasons.add("mismatched_close")
            else:
                top.append(("close", m))
        else:
            block = _Block(m)
            if stack:
                stack[-1].children.append(block)
            else:
                top.append(block)
            stack.append(block)
    for block in top:
        if isinstance(block, _Block) and block.close is None:
            _resolve_unclosed(block)
    top = _flatten_promoted(top)
    # Reasons from the resolved tree: a block closed by its own tag that holds
    # children is genuine nesting; a promoted block is a re-open.
    work = [item for item in top if isinstance(item, _Block)]
    seen: set[int] = set()   # promoted chains reach later blocks twice
    while work:
        block = work.pop()
        if id(block) in seen:
            continue
        seen.add(id(block))
        if block.promoted:
            reasons.add("nested_open")
        elif block.close is not None and block.children:
            reasons.add("nested_balanced")
        work.extend(block.children)
    visible_parts: list[str] = []
    blocks: list[str] = []
    paired = 0
    pos = 0
    for item in top:
        if not isinstance(item, _Block):
            m = item[1]
            visible_parts.append(raw[pos:m.start()])
            if paired or "lone_close_head" in reasons:
                reasons.add("stray_close_after_block")
            else:
                reasons.add("lone_close_head")
                blocks.append("".join(visible_parts))
                visible_parts = []
            pos = m.end()
            continue
        visible_parts.append(raw[pos:item.open.start()])
        if item.close is None:
            reasons.add("trailing_unclosed")
            blocks.append(raw[item.open.end():])
            pos = len(raw)
            break
        blocks.append(raw[item.open.end():item.close.start()])
        paired += 1
        pos = item.close.end()
    visible_parts.append(raw[pos:])
    visible = re.sub(r"\n{3,}", "\n\n", "\n".join(visible_parts)).strip()
    if _RESIDUE.search(visible):
        # Only a truncated ``<thin`` fragment can survive the token scan; that is
        # not reply text. Fail closed rather than deliver a half tag.
        return "", "", "residue"
    thinking_text = "\n".join(b.strip() for b in blocks if b.strip())
    return visible, thinking_text, "+".join(sorted(reasons))


def strip_all_thinking_or_salvage(text: str, *, sanitize: bool = True) -> tuple[str, str, str]:
    """``strip_all_thinking``, then the salvage layer on FAILED.

    Same ``(status, thinking, reply)`` shape. A :data:`SALVAGED` result always
    carries an empty ``thinking`` — the blocks it dropped are not trustworthy
    enough to show — and a non-empty ``reply``. Everything the strict pass
    already answers (ABSENT / COMPLETE / SILENT) is returned untouched, and a
    truncated protocol opener stays FAILED (there is no reply text in it).
    """
    status, thinking, reply = strip_all_thinking(text, sanitize=sanitize)
    if status != FAILED or _truncated_protocol_opener(str(text or "")):
        return status, thinking, reply
    visible, _dropped, reason = salvage_thinking(text)
    if not visible or reason == "residue":
        return FAILED, "", ""
    return SALVAGED, "", visible
