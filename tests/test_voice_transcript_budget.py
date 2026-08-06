"""Capture 的转写预算必须跟得上通话时长上限。

采样(头尾各取、中间省略)是**兜底**,不是正常路径:它会永久丢掉通话中段,
而中段恰恰是"确实蒸了全文"的唯一证据。只要预算 > 时长上限能产出的字数,
这条路径就不可达。

这个测试存在的意义是防漂移:有人把通话时长调大(那是个 iOS 常量,离这里很远)
却没调预算,采样就会静默生效,记忆开始丢中段而没有人知道。
"""
import os
import re
from pathlib import Path

# 中文口语双向对话的保守估计:150 字/分钟。宁可高估,预算是廉价的。
_CHARS_PER_MINUTE = 150
_REPO = Path(__file__).resolve().parent.parent


def _ios_call_duration_seconds() -> int:
    """iOS 建 ElevenLabs agent 时设的 max_duration_seconds —— 通话能有多长的唯一真源。"""
    for candidate in (
        _REPO.parent / "feedling-mcp-ios" / "App" / "FeedlingTest" / "API" / "ElevenLabsAgentClient.swift",
        _REPO.parent / "feedling-mcp-ios-voice" / "App" / "FeedlingTest" / "API" / "ElevenLabsAgentClient.swift",
    ):
        if candidate.is_file():
            m = re.search(r'"max_duration_seconds":\s*(\d+)', candidate.read_text())
            if m:
                return int(m.group(1))
    return 3600  # iOS 仓库不在旁边时用当前已知值,仍然锁住后端两个预算


def test_capture_budgets_cover_the_longest_possible_call():
    from model_api_runtime.v2 import worker

    duration = _ios_call_duration_seconds()
    needed = (duration // 60) * _CHARS_PER_MINUTE
    v2_budget = worker._VOICE_TRANSCRIPT_PROMPT_CHARS
    assert v2_budget >= needed, (
        f"V2 转写预算 {v2_budget} 装不下 {duration}s 的通话(约需 {needed} 字):"
        "超出部分会被头尾采样永久丢掉。调大 "
        "FEEDLING_V2_VOICE_TRANSCRIPT_PROMPT_CHARS，或调小通话时长上限。"
    )

    resident = _REPO / "tools" / "chat_resident_consumer.py"
    text = resident.read_text()
    tb = int(re.search(r'FEEDLING_CAPTURE_VOICE_TRANSCRIPT_MAX_CHARS",\s*"(\d+)"', text).group(1))
    wb = int(re.search(r'FEEDLING_CAPTURE_WINDOW_MAX_CHARS",\s*"(\d+)"', text).group(1))
    assert tb >= needed, (
        f"resident 转写预算 {tb} 装不下 {duration}s 的通话(约需 {needed} 字)。"
    )
    assert wb > tb, (
        f"resident 整窗上限 {wb} 必须大于单条转写预算 {tb}：那道截断是 text[-N:]，"
        "否则一通电话会把同窗口的文字聊天从头部静默砍掉。"
    )
