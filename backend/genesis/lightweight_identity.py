"""非 LLM 的身份兜底:仅在 provider 派生失败时用,从上传的人物卡/档案原文里抓显式名字。
绝不调 LLM。质量有限(启发式),只保证'有内容不至于无名空过'(见 spec §2.3)。"""
from __future__ import annotations

import re

_NAME_PATTERNS = [
    r"名字[:：]\s*([^\s（(，,。\n]{1,20})",
    r"叫\s*([^\s（(，,。\n]{1,20})\s*[，,。]",
    r"#\s*([^\s·|]{1,20})\s*[·|]\s*角色卡",
]


def _extract_name(text: str) -> str:
    for pat in _NAME_PATTERNS:
        m = re.search(pat, text or "")
        if m:
            name = m.group(1).strip(" ·|、`\"'“”")
            if name and name.lower() not in {"claude", "gpt", "gemini", "chatgpt", "hermes"}:
                return name[:40]
    return ""


def derive_from_support(support_texts: list[str], *, days_with_user: int, language: str) -> dict:
    name = ""
    for t in support_texts or []:
        name = _extract_name(str(t or ""))
        if name:
            break
    return {"agent_name": name, "dimensions": [], "self_introduction": "",
            "category": "", "signature": [], "days_with_user": max(0, int(days_with_user or 0))}


def has_signal(payload: dict | None) -> bool:
    p = payload if isinstance(payload, dict) else {}
    if str(p.get("agent_name") or "").strip():
        return True
    return bool(isinstance(p.get("dimensions"), list) and p["dimensions"])
