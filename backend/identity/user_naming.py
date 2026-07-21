"""Shared rules for referring to the person in user-visible memory text.

This module is deliberately pure stdlib.  Identity validation, hosted import,
and the standalone resident consumer all need the exact same placeholder and
fallback semantics.
"""
from __future__ import annotations


_RESERVED_USER_NAMES = frozenset({"ta", "user", "用户"})


def sanitize_user_name(user_name: str) -> str:
    """Return a real preferred name, or the internal ``TA`` unknown marker."""
    name = " ".join(str(user_name or "").split())
    name = name.strip(" `\"'“”‘’「」『』。，,.;；:：!！?？")
    if not name or name.casefold() in _RESERVED_USER_NAMES:
        return "TA"
    return name


def _naming_rule(user_name: str) -> str:
    """Render the canonical rule for user-visible memory prose."""
    name = sanitize_user_name(user_name)
    if name != "TA":
        return (
            f"提到 {name} 就用「{name}」这个名字。"
            "不要用「用户」/\"user\"、指代本人的「TA」、猜测性别的他/她，"
            "也不要用第二人称「你」来指代本人。"
        )
    return (
        "如果材料里明确出现了本人希望被称呼的名字，就用那个名字；"
        "否则优先省略主语（例如「常在深夜写代码，累了会突然沉默」），"
        "确实需要主语时只用中性的「对方」。"
        "不要用「用户」/\"user\"、指代本人的「TA」、猜测性别的他/她，"
        "也不要用第二人称「你」来指代本人。"
    )
