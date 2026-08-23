"""已搬至 ``memgarden.prompts.dream`` —— 此处保留 re-export 与旧签名。

同 ``capture_prompt_v1``：内核不 import ``identity``，称呼规则在这层装配。

⚠️ ``naming_rule`` 用未 sanitize 的原始 ``user_name``，模板里的 ``user_name``
用 sanitize 后的值 —— 两者不同源，照搬原实现。
"""
from identity.user_naming import _naming_rule, sanitize_user_name  # noqa: F401
from memory.card_leak_signals import IO_LEAK_SIGNALS

from memgarden.prompts.dream import *  # noqa: F401,F403
from memgarden.prompts import dream as _kernel


def build_dream_prompt(
    *,
    ai_name: str,
    user_name: str,
    cards: str,
    recent_conversations: str,
    locale: str,
) -> str:
    """称呼规则在这层装配后传给内核。

    ``locale`` 必填、无默认 —— 同 capture：给了默认，漏改的调用点会安静地按错语言
    整理记忆，必填则当场炸出来。取自 ``chat.reply_language.infer_garden_language``。
    """
    return _kernel.build_dream_prompt(
        ai_name=ai_name,
        user_name=sanitize_user_name(user_name),
        naming_rule=_naming_rule(user_name, locale=locale),
        cards=cards,
        recent_conversations=recent_conversations,
        locale=locale,
    )


# --------------------------------------------------------------------------- #
# parser：在这层把 io 的识别器绑死，不让 runtime 调用点自己传
# --------------------------------------------------------------------------- #
#
# ⚠️ 这是 codex code_review 2026-08-23 抓到的一个**静默失效**：内核的 parser 内部
# 会调文本闸，signals 不传就退回通用集 —— io 的模型残片（harmony token / 工具路由
# / 协议头）一个都拦不住，而且不报错。事故串会直接落成卡的摘要，解析完立刻封加密
# 信封，下游再也拦不住。
#
# 为什么绑在这层而不是让调用点各自传：调用点会增加，漏一个就是一条无声失防的路，
# 而这类漏传**测不出来**（管道通、参数有默认）。绑在唯一入口是结构上的保证。
_kernel_parse_dream_consolidations = _kernel.parse_dream_consolidations


def parse_dream_consolidations(*args, **kwargs):
    """内核 parser + io 的识别器。调用方不需要、也不应该自己传 signals。"""
    kwargs.setdefault("signals", IO_LEAK_SIGNALS)
    return _kernel_parse_dream_consolidations(*args, **kwargs)
