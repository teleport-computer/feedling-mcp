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

_TAG_WORDS = ("thinking", "reasoning", "thought", "think")  # longest-first
_TAG_ALT = "|".join(_TAG_WORDS)
# Invisible leading chars (BOM, zero-width) that must not hide a truncated opener.
_INVISIBLE = "﻿​‌‍⁠⁦⁧⁨⁩"
_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏")
# Any of our protocol tags (open or close) — used only to detect nesting INSIDE a
# resolved thinking block, never to scrub the reply.
_ANY_TAG = re.compile(rf"<\s*/?\s*(?:{_TAG_ALT})\b", re.IGNORECASE)

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
    " 想多写就多写，一句带过也行，不用凑字数也不用收着。\n"
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
    rf"<\s*(?P<tag>{_TAG_ALT}){_NAME_END}\s*>(?P<body>.*?)<\s*/\s*(?P=tag){_NAME_END}\s*>",
    re.IGNORECASE | re.DOTALL,
)
# 剥完之后判定「还有没有残留」。任何开或闭标签都算。
_RESIDUE = re.compile(rf"<\s*/?\s*(?:{_TAG_ALT}){_NAME_END}", re.IGNORECASE)
# 孤立闭标签：按本协议思考永远写在最前面，所以一个配不上对的 </think> 说明它
# 前面的全是思考（开标签在上游某处被吃掉了）。
_LONE_CLOSE = re.compile(rf"<\s*/\s*(?:{_TAG_ALT}){_NAME_END}\s*>", re.IGNORECASE)


def gate_enabled() -> bool:
    """泄漏闸的 kill switch。默认开——关掉只用于线上出问题时立刻止血，
    不是灰度门。关掉后调用方必须逐字回到本次改动前的行为。"""
    return os.environ.get(_GATE_ENV_FLAG, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


# 只删标签壳、保留文字。给「历史入口」的 FAILED 兜底用：那种行结构已经乱到
# 分不清哪段是思考，但它的文字本来就已经发到用户眼前过了，删掉整行会平白打断
# 对话连贯性。入口真正要断的是「模型看到可抄的格式」，删掉标签壳就够了。
_TAG_MARKER = re.compile(rf"<\s*/?\s*(?:{_TAG_ALT})(?![\w:.-])\s*>", re.IGNORECASE)


def strip_tag_markers(text: str) -> str:
    """删掉 think 类标签本身，保留标签之间的文字。"""
    return re.sub(r"\n{3,}", "\n\n", _TAG_MARKER.sub("", str(text or ""))).strip()


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
