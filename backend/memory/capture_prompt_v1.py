"""已搬至 ``memgarden.prompts.capture`` —— 此处保留 re-export 与旧签名。

内核不 import ``identity``（那是宿主的身份体系），所以称呼规则的装配留在这层壳里，
现有调用方（V1 consumer、V2 worker、genesis）无需改动。

⚠️ 保留一处易丢的语义：``naming_rule`` 用的是**未 sanitize** 的原始 ``user_name``，
而模板里的 ``user_name`` 用的是 sanitize 后的值。两者不同源，照搬原实现。
"""
from identity.user_naming import _naming_rule, sanitize_user_name  # noqa: F401

from memgarden.prompts.capture import *  # noqa: F401,F403
from memgarden.prompts import capture as _kernel


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
