"""已搬至 ``memory_garden.prompts.dream`` —— 此处保留 re-export 与旧签名。

同 ``capture_prompt_v1``：内核不 import ``identity``，称呼规则在这层装配。

⚠️ ``naming_rule`` 用未 sanitize 的原始 ``user_name``，模板里的 ``user_name``
用 sanitize 后的值 —— 两者不同源，照搬原实现。
"""
from identity.user_naming import _naming_rule, sanitize_user_name  # noqa: F401

from memory_garden.prompts.dream import *  # noqa: F401,F403
from memory_garden.prompts import dream as _kernel


def build_dream_prompt(
    *,
    ai_name: str,
    user_name: str,
    cards: str,
    recent_conversations: str,
) -> str:
    """旧签名不变；称呼规则在这层装配后传给内核。"""
    return _kernel.build_dream_prompt(
        ai_name=ai_name,
        user_name=sanitize_user_name(user_name),
        naming_rule=_naming_rule(user_name),
        cards=cards,
        recent_conversations=recent_conversations,
    )
