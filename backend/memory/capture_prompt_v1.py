"""已搬至 ``memgarden.prompts.capture`` —— 此处保留 re-export 与旧签名。

内核不 import ``identity``（那是宿主的身份体系），所以称呼规则的装配留在这层壳里，
现有调用方（V1 consumer、V2 worker、genesis）无需改动。

⚠️ 保留一处易丢的语义：``naming_rule`` 用的是**未 sanitize** 的原始 ``user_name``，
而模板里的 ``user_name`` 用的是 sanitize 后的值。两者不同源，照搬原实现。
"""
from dataclasses import replace

from identity.user_naming import _naming_rule, sanitize_user_name  # noqa: F401
from memory.card_leak_signals import IO_LEAK_SIGNALS

from memgarden.prompts.capture import *  # noqa: F401,F403
from memgarden.prompts import capture as _kernel
from memgarden.policies import CONVERSATION_CAPTURE


# 提示词继续要求「少而厚」；硬上限只防失控批次，不再把模型判断截在 2 张。
# 从钉版 policy replace，确保 rubric 与其余行为逐字段保持原样。
IO_CONVERSATION_CAPTURE_POLICY = replace(CONVERSATION_CAPTURE, max_cards=50)


def build_capture_prompt(
    *,
    ai_name: str,
    user_name: str,
    buckets: str,
    threads: str,
    identity: str,
    window: str,
    cards: str = "",
    locale: str,
) -> str:
    """称呼规则在这层装配后传给内核。

    ``locale``（"zh-Hans" / "en"）**必填、不给默认**：给了默认，漏改的调用点会
    安静地按错语言落卡；必填则当场炸出来。调用方从 ``chat.reply_language``
    的判断里取 —— 那个判断读了身份卡、历史记忆和已有桶名，是 io 里唯一一处
    「这个人说什么语言」的事实源，桶名跟着它走才不会和 io 的回复语言打架。
    """
    return _kernel.build_capture_prompt(
        ai_name=ai_name,
        user_name=sanitize_user_name(user_name),
        naming_rule=_naming_rule(user_name, locale=locale),
        locale=locale,
        buckets=buckets,
        threads=threads,
        identity=identity,
        window=window,
        cards=cards,
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
_kernel_parse_capture_cards = _kernel.parse_capture_cards


def parse_capture_cards(*args, **kwargs):
    """内核 parser + io 的识别器。调用方不需要、也不应该自己传 signals。"""
    kwargs.setdefault("signals", IO_LEAK_SIGNALS)
    kwargs.setdefault("policy", IO_CONVERSATION_CAPTURE_POLICY)
    return _kernel_parse_capture_cards(*args, **kwargs)
