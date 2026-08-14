"""已搬至 ``memory_garden.text.card_guard`` —— 此处保留 re-export。

「模型原始输出泄漏」的字段级判据是纯函数，属于内核第 ④ 种活（解析并算 mutation）
里的校验环节。现有调用方以 ``from memory import card_guard`` 形式使用。
"""
from memory_garden.text.card_guard import *  # noqa: F401,F403
