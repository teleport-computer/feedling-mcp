"""已搬至 ``memory_garden.text.card_text`` —— 此处保留 re-export。

卡片字段的内容校验（占位符 / 模板抄回）是纯函数，属于内核。

``_TOMBSTONE_MARKER_RE`` 显式带出：``memory/actions.py`` 的注释引用了它作为同源判据，
虽当前不是代码引用，但保留成本为零。
"""
from memory_garden.text.card_text import *  # noqa: F401,F403
from memory_garden.text.card_text import _TOMBSTONE_MARKER_RE  # noqa: F401
