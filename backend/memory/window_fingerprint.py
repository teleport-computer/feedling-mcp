"""落卡窗口的**内容无关指纹** —— 用来定位「模型为什么吐出坏 JSON」。

## 为什么要有它（2026-09-12 事故的最后一块拼图）

事故复现出来的机制是：模型引用用户原话时把半角双引号写进 JSON 字符串却不
转义，整批卡解析失败；而落卡失败不推进游标，于是同一条消息被反复重放，
那个用户从此不再有任何新记忆。

但**毒引号从哪来**一直没定位到。候选一堆，看日志分不出来：

    用户自己打的（换了输入法？）
    语音转写（ASR 输出常带半角标点）
    图片 / 附件的 caption
    代码把消息正文 json.dumps 出来（V1 的 dict 分支会）
    助手自己的回复引用了用户原话

诊断里刻意不存对话原文，所以不能靠读原文查。这个模块给出**足够定位、
又不含任何内容**的指纹：

    ascii_double_quotes   窗口里半角双引号的**个数**
    roles                 窗口里出现过哪些 role（枚举集合）
    sources               出现过哪些 source（枚举集合）

失败窗口的 ``ascii_double_quotes`` 显著 >0、成功窗口 =0 → 引号假说坐实。
再看是哪个 role/source 在场时才爆 → 源头就指出来了。

🔴 **这个模块的输出会进诊断面，所以绝不能包含任何用户内容。**
只有计数和白名单枚举 —— 有测试守着这一条。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: 允许出现在指纹里的 role / source。白名单之外一律归成 "other" ——
#: 直接回传原值等于让任意字符串从这个口子漏进诊断面。
_KNOWN_ROLES = frozenset({
    "user", "assistant", "agent", "system", "tool",
    "voice", "openclaw", "screen", "image", "file",
})
_KNOWN_SOURCES = frozenset({
    "chat", "voice", "wake", "heartbeat", "screen", "openclaw",
    "image", "file", "caption", "import", "genesis", "perception",
})

#: 半角双引号。这是**唯一**会让模型写出的 JSON 失效的引号形态 ——
#: 全角引号（“ ” 「 」）在 JSON 字符串里完全合法，不需要转义。
_ASCII_DQ = '"'


def fingerprint(window_text: str = "", messages: Iterable[Mapping] = ()) -> dict[str, Any]:
    """窗口的内容无关指纹。

    ``window_text`` 是渲染好的窗口文本（拿它数引号）；``messages`` 是这一批
    原始消息（拿它取 role/source）。两个都可以省，缺哪个就少哪部分字段。
    """
    out: dict[str, Any] = {}

    text = str(window_text or "")
    if text:
        out["window_chars"] = len(text)
        # 🔴 只出个数，不出位置、不出上下文。
        out["ascii_double_quotes"] = text.count(_ASCII_DQ)

    roles: set[str] = set()
    sources: set[str] = set()
    count = 0
    for msg in messages or ():
        if not isinstance(msg, Mapping):
            continue
        count += 1
        role = str(msg.get("role") or "").strip().lower()
        roles.add(role if role in _KNOWN_ROLES else "other")
        src = str(msg.get("source") or msg.get("wake_kind") or "").strip().lower()
        if src:
            sources.add(src if src in _KNOWN_SOURCES else "other")
    if count:
        out["message_count"] = count
        out["roles"] = sorted(roles)
        if sources:
            out["sources"] = sorted(sources)
    return out


def quote_pressure(window_text: str) -> dict[str, Any]:
    """只数引号的轻量版，给拿不到原始消息列表的调用点用。

    额外给一个 ``per_kchars``（每千字符多少个半角引号）—— 绝对个数会被窗口
    长度带偏：四千字的窗口有 3 个引号和四百字的窗口有 3 个，风险完全不同。
    """
    text = str(window_text or "")
    n = text.count(_ASCII_DQ)
    out: dict[str, Any] = {"ascii_double_quotes": n, "window_chars": len(text)}
    if text:
        out["per_kchars"] = round(n * 1000 / len(text), 2)
    return out
