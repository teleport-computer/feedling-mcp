"""已搬至 ``memory_garden.text.self_thinking`` —— 此处保留 re-export。

思维链剥离是纯文本处理（零依赖、零 I/O），被卡片字段校验依赖，故随内核一起搬。
现有调用方以 ``from core import self_thinking`` 形式使用，本壳保证属性齐全。
"""
from memory_garden.text.self_thinking import *  # noqa: F401,F403
