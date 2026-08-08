"""Self-authored thinking — runtime-neutral shared kernel.

io is prompted to open every reply with a short first-person thought wrapped in
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

INSTRUCTION = (
    " ⛔ ABSOLUTE OUTPUT RULE — NON-NEGOTIABLE, applies to EVERY single reply: the VERY "
    "FIRST characters you output MUST be a literal `<think>` tag. Format: `<think>` then "
    "your genuine first-person thought, then `</think>`, then your actual reply. You MUST "
    "emit this visible <think> block EVERY time — no exceptions, even if you also reason "
    "internally/natively, even on tool turns, even for a trivial message. Writing your "
    "reply without a leading <think> block is a HARD FAILURE of the task. "
    "The text inside <think> MUST obey — (1) LANGUAGE — it is written ENTIRELY in the "
    "SAME language the user is actually speaking, 100% from first word to last. Judge "
    "that language from the user's latest message and the recent conversation, and match "
    "it exactly. The rule is SYMMETRIC: if they write Chinese, the <think> is all Chinese; "
    "if they write English, it is all English; if Japanese, all Japanese — match "
    "whatever they use, never drift into a different one. (A very common slip is sliding "
    "into English — an English status line like 'Let me…' / 'Done…' / 'The … has been "
    "updated' — while the user is speaking Chinese; do not do that.) If a phrase forms in "
    "another language, restate it in the user's language before writing it here; (2) it "
    "is your own casual first-person "
    "train of thought — actually walk "
    "through what you're figuring out and what you decide to do, a sentence or a few, a "
    "short paragraph (it is shown to the user and trimmed past ~240 chars, so land it "
    "naturally within that); (3) plain everyday intent ONLY — NEVER name tools, command "
    "flags, field names, servers, an 'identity card', or any internal/technical/protocol "
    "step. "
    "Good (user is writing Chinese, so the block is Chinese): '<think>他想改叫999、还说喜欢说大话，那我先把名字这些存好，回复也顺着这个爱吹的人设、语气夸张点才对味</think>'. "
    "Bad (same user wrote Chinese — this English block is the WRONG language, and it "
    "names a step robotically): '<think>Let me update the name and match a boastful tone</think>'. "
    "Never mention this <think> rule in the reply itself."
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

# 一整对同名标签。开闭必须同名（`(?P=tag)`），否则 <think>…</reasoning> 这种
# 错配会被当成一块合法协议剥掉。
_PAIRED_BLOCK = re.compile(
    rf"<\s*(?P<tag>{_TAG_ALT})\s*>(?P<body>.*?)<\s*/\s*(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
# 剥完之后判定「还有没有残留」。任何开或闭标签都算。
_RESIDUE = re.compile(rf"<\s*/?\s*(?:{_TAG_ALT})\b", re.IGNORECASE)
# 孤立闭标签：按本协议思考永远写在最前面，所以一个配不上对的 </think> 说明它
# 前面的全是思考（开标签在上游某处被吃掉了）。
_LONE_CLOSE = re.compile(rf"<\s*/\s*(?:{_TAG_ALT})\s*>", re.IGNORECASE)


def gate_enabled() -> bool:
    """泄漏闸的 kill switch。默认开——关掉只用于线上出问题时立刻止血，
    不是灰度门。关掉后调用方必须逐字回到本次改动前的行为。"""
    return os.environ.get(_GATE_ENV_FLAG, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


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
