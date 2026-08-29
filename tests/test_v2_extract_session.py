"""V2 的 extract() 用 GardenComponent 的会话驱动时，行为要和原路径一致。

## 为什么两种模式都留着

``extract()`` 管着一整套 provider 机制：截断检测（要看 finish_reason）、
用量统计、失败分类、退避、轨迹。直接调 ``garden.acapture()`` 会把 provider
调用抢过去 —— 等于放弃这些能力，那是净退步。

所以组件只回答「下一步问什么」，provider 那步仍走 ``extract`` 里同一个
``_call``。两种模式共用它，provider 那一侧不可能分家。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from memgarden import CaptureRequest  # noqa: E402
from memory.garden_component import CallableModel, build_garden  # noqa: E402
from model_api_runtime.v2 import extraction as v2_extraction  # noqa: E402

WINDOW = "老王：我不吃辣，一吃就胃疼\n我：那以后点菜避开"


def _reply(*cards: dict) -> str:
    return json.dumps({"cards": list(cards)}, ensure_ascii=False)


GOOD = {"action": "add", "bucket": "偏好与边界", "threads": ["饮食"],
        "summary": "他不吃辣", "content": "一吃辣就胃疼，点菜会主动避开辣的。"}
DIRTY = {"action": "add", "summary": "[摘要]", "content": "...", "bucket": "工作"}


class FakeProvider:
    """假 provider —— 按剧本回，并可以标记「这次被截断了」。"""

    def __init__(self, *replies, truncate_on=()):
        self.replies = list(replies)
        self.truncate_on = set(truncate_on)
        self.calls = 0

    async def __call__(self, config, messages, **kw):
        self.calls += 1
        text = self.replies[min(self.calls - 1, len(self.replies) - 1)]
        return {
            "reply": text,
            "stop_reason": "length" if self.calls in self.truncate_on else "stop",
            "usage": {"completion_tokens": 10},
        }


def _run(coro):
    return asyncio.run(coro)


def _session(replies):
    """组件的会话 —— 模型端口不会被用到（provider 走 extract 那边）。"""
    garden = build_garden(CallableModel(lambda p: ""))
    return garden.capture_session(CaptureRequest(
        window=WINDOW, locale="zh-Hans", ai_name="io", user_name="老王"))


@pytest.mark.parametrize("name,replies,want_cards,want_err", [
    ("一次就干净", [_reply(GOOD)], 1, None),
    ("吐脏后重问救回", [_reply(DIRTY), _reply(GOOD)], 1, None),
    ("本来就没什么可记", [_reply()], 0, None),
])
def test_session_mode_produces_the_expected_outcome(
    monkeypatch, name, replies, want_cards, want_err
) -> None:
    provider = FakeProvider(*replies)
    monkeypatch.setattr(
        v2_extraction.provider_client, "reliable_chat_completion_async", provider
    )
    cards, err = _run(v2_extraction.extract(
        provider_config={}, prompt="", parse=lambda r: ([], None),
        session=_session(replies),
    ))
    assert err == want_err, f"「{name}」错误码不对"
    assert len(cards or []) == want_cards, f"「{name}」卡数不对"


def test_session_mode_still_reports_truncation(monkeypatch) -> None:
    """截断是 provider 层的事 —— 换了驱动方式，这条不能丢。

    第一次被截 → 换一版更简短的提示词；还被截 → 如实报 output_truncated，
    不能当成解析失败（那会让人以为是模型输出格式的问题）。
    """
    provider = FakeProvider("半截 JSON{", "还是半截{", truncate_on=(1, 2))
    monkeypatch.setattr(
        v2_extraction.provider_client, "reliable_chat_completion_async", provider
    )
    cards, err = _run(v2_extraction.extract(
        provider_config={}, prompt="", parse=lambda r: ([], None),
        session=_session([]),
    ))
    assert err == "output_truncated"
    assert cards is None
    assert provider.calls == 2, "第一次截断后应该换一版提示词再试一次"


def test_session_mode_reports_provider_failure_faithfully(monkeypatch) -> None:
    """provider 挂了要如实报，不能伪装成格式问题 —— 两者的重试策略完全不同。"""
    async def boom(config, messages, **kw):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(
        v2_extraction.provider_client, "reliable_chat_completion_async", boom
    )
    cards, err = _run(v2_extraction.extract(
        provider_config={}, prompt="", parse=lambda r: ([], None),
        session=_session([]),
    ))
    assert cards is None
    assert err and err.startswith("provider_call_failed:")


def test_the_legacy_path_is_untouched(monkeypatch) -> None:
    """不给 session 时走原路径，一个字节没改。"""
    provider = FakeProvider(_reply(GOOD))
    monkeypatch.setattr(
        v2_extraction.provider_client, "reliable_chat_completion_async", provider
    )
    from memgarden.prompts.capture import build_capture_prompt, parse_capture_cards

    cards, err = _run(v2_extraction.extract(
        provider_config={},
        prompt=build_capture_prompt(
            ai_name="io", user_name="老王", buckets="", threads="",
            identity="", window=WINDOW, locale="zh-Hans"),
        parse=lambda r: parse_capture_cards(r, strict=True),
    ))
    assert err is None and len(cards) == 1
