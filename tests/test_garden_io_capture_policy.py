"""io 的落卡档位（max_cards=50）必须经组件的公开入口真正生效，且不许再打内核补丁。

## 为什么有这个文件

``memory/garden_component.py`` 以前在模块加载时改写
``memgarden.component.build_capture_prompt``（memgarden 0.16.0 按对象 identity
选模板，``replace`` 出来的 io 同档 policy 会被误认）。0.20.1 起模板按
``policy.name`` / 标志位渲染，垫片已删。删掉之后要守住三件事：

1. io 的宿主配置（``IO_CONVERSATION_CAPTURE_POLICY``，max_cards=50）**真的生效**
   —— V1（组件自带循环）和 V2（宿主驱动的会话 + ``extract``）两条都要验；
   同时它不改变提示词字节（仍是钉版的对话档）。
2. 同一进程里默认档和 io 档的组件实例、不同的导入顺序，互不串味。
3. 内核的模块属性保持原函数对象 —— 谁再加 monkeypatch 这里就红。

⚠️ 上限的真实语义（memgarden ``prompts/capture.py`` 的 ``parse_capture_cards``）：
第一问**严格**，超限整批打回 ``too_many_cards:N>50``（不静默截断，也不是
可重问的格式错误）；只有进入重问之后的第二问才放宽为「保留前 50 张」。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import memgarden.component as kernel_component  # noqa: E402
import memgarden.prompts.capture as kernel_capture  # noqa: E402
import memgarden.prompts.dream as kernel_dream  # noqa: E402
from memgarden import CaptureRequest  # noqa: E402
from memgarden.policies import CONVERSATION_CAPTURE  # noqa: E402

from memory import garden_component  # noqa: E402
from memory.capture_prompt_v1 import IO_CONVERSATION_CAPTURE_POLICY  # noqa: E402
from model_api_runtime.v2 import extraction as v2_extraction  # noqa: E402

LOCALE = "zh-Hans"
WINDOW = "老王：我不吃辣，一吃就胃疼\n我：那以后点菜避开"


def _cards_reply(n: int) -> str:
    return json.dumps({"cards": [
        {"action": "add", "type": "fact", "bucket": "偏好与边界",
         "threads": ["饮食"], "summary": f"老王的第{i}条饮食偏好",
         "content": f"第{i}条：他明确说过这件事，点菜时要记得照顾到。"}
        for i in range(n)
    ]}, ensure_ascii=False)


#: 第一问吐占位符 → 内容闸打回（可重问的格式错误），第二问才放宽解析。
_DIRTY_REPLY = json.dumps({"cards": [
    {"action": "add", "summary": "[摘要]", "content": "...", "bucket": "工作"}
]}, ensure_ascii=False)


def _request(policy) -> CaptureRequest:
    return CaptureRequest(window=WINDOW, locale=LOCALE, ai_name="io",
                          user_name="老王", policy=policy)


class _Scripted:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]


def _v1_capture(policy, *replies: str):
    """V1 形状：组件自带循环（consumer 里 ``_garden.capture(...)``）。"""
    model = _Scripted(*replies)
    tracker = garden_component.BounceTracker()
    garden = garden_component.build_garden(
        garden_component.CallableModel(model), on_step=tracker)
    return garden.capture(_request(policy)), model, tracker


class _FakeProvider:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def __call__(self, config, messages, **kw):
        self.calls += 1
        return {"reply": self.replies[min(self.calls - 1, len(self.replies) - 1)],
                "stop_reason": "stop", "usage": {"completion_tokens": 10}}


def _v2_capture(monkeypatch, policy, *replies: str):
    """V2 形状：宿主驱动的会话，provider 那步走 ``extraction.extract``。"""
    provider = _FakeProvider(*replies)
    monkeypatch.setattr(
        v2_extraction.provider_client, "reliable_chat_completion_async", provider)
    session = garden_component.build_garden(
        garden_component.CallableModel(lambda _p: "")
    ).capture_session(_request(policy))
    cards, err = asyncio.run(v2_extraction.extract(
        provider_config={}, prompt="", parse=lambda r: ([], None), session=session))
    return cards, err, provider


# --------------------------------------------------------------------------- #
# 1. 上限经公开入口真正生效
# --------------------------------------------------------------------------- #

def test_io_policy_really_differs_from_the_pinned_default() -> None:
    """前提：两档只差 max_cards。否则下面的测试区分不出「io 档是否生效」。"""
    assert IO_CONVERSATION_CAPTURE_POLICY.max_cards == 50
    assert CONVERSATION_CAPTURE.max_cards == 2
    assert IO_CONVERSATION_CAPTURE_POLICY.name == CONVERSATION_CAPTURE.name


@pytest.mark.parametrize("n", [3, 50])
def test_v1_component_keeps_every_card_up_to_io_cap(n) -> None:
    result, model, _ = _v1_capture(IO_CONVERSATION_CAPTURE_POLICY, _cards_reply(n))
    assert result.error is None
    assert len(result.cards) == n
    assert len(model.prompts) == 1


def test_v1_component_rejects_first_reply_above_io_cap_with_io_limit() -> None:
    result, model, _ = _v1_capture(IO_CONVERSATION_CAPTURE_POLICY, _cards_reply(51))
    # 报的是 io 的 50，不是钉版默认的 2 —— 证明 parse 用的是请求里的 io 档。
    assert result.error == "too_many_cards:51>50"
    assert result.cards == []
    assert len(model.prompts) == 1


def test_v1_component_caps_retry_reply_at_io_limit() -> None:
    # 第一问吐占位符 → 可重问；第二问 60 张 → 放宽解析，保留前 50 张。
    result, model, tracker = _v1_capture(
        IO_CONVERSATION_CAPTURE_POLICY, _DIRTY_REPLY, _cards_reply(60))
    assert result.error is None
    assert len(model.prompts) == 2
    assert len(result.cards) == 50
    assert [c["summary"] for c in result.cards] == [
        f"老王的第{i}条饮食偏好" for i in range(50)]
    assert tracker.bounce(cards=result.cards, error=result.error) == "bounced_ok"


@pytest.mark.parametrize("n", [3, 50])
def test_v2_session_keeps_every_card_up_to_io_cap(monkeypatch, n) -> None:
    cards, err, provider = _v2_capture(
        monkeypatch, IO_CONVERSATION_CAPTURE_POLICY, _cards_reply(n))
    assert err is None
    assert len(cards) == n
    assert provider.calls == 1


def test_v2_session_rejects_first_reply_above_io_cap_with_io_limit(monkeypatch) -> None:
    cards, err, provider = _v2_capture(
        monkeypatch, IO_CONVERSATION_CAPTURE_POLICY, _cards_reply(51))
    assert err == "too_many_cards:51>50"
    assert cards is None
    assert provider.calls == 1


def test_v2_session_caps_retry_reply_at_io_limit(monkeypatch) -> None:
    cards, err, provider = _v2_capture(
        monkeypatch, IO_CONVERSATION_CAPTURE_POLICY, _DIRTY_REPLY, _cards_reply(60))
    assert err is None
    assert provider.calls == 2
    assert len(cards) == 50


def test_io_policy_does_not_change_capture_prompt_bytes() -> None:
    """宿主配置只动上限，不动提示词：io 档的提示词与钉版对话档逐字节相同。"""
    _, io_model, _ = _v1_capture(IO_CONVERSATION_CAPTURE_POLICY, _cards_reply(1))
    _, pinned_model, _ = _v1_capture(CONVERSATION_CAPTURE, _cards_reply(1))
    assert io_model.prompts == pinned_model.prompts


# --------------------------------------------------------------------------- #
# 2. 不串味
# --------------------------------------------------------------------------- #

def test_default_and_io_policy_instances_do_not_cross_contaminate() -> None:
    default_garden = garden_component.build_garden(
        garden_component.CallableModel(lambda _p: _cards_reply(3)))
    io_garden = garden_component.build_garden(
        garden_component.CallableModel(lambda _p: _cards_reply(3)))

    # 交替跑两遍：任何一方的档位若被另一方「记住」，第二轮就会变。
    for _ in range(2):
        default_result = default_garden.capture(_request(None))
        io_result = io_garden.capture(_request(IO_CONVERSATION_CAPTURE_POLICY))
        pinned_result = io_garden.capture(_request(CONVERSATION_CAPTURE))
        assert default_result.error == "too_many_cards:3>2"
        assert pinned_result.error == "too_many_cards:3>2"
        assert io_result.error is None and len(io_result.cards) == 3


_ORDER_PROBE = r"""
import json, sys
sys.path.insert(0, {backend!r})
order = {order!r}
for name in order:
    __import__(name)
from memgarden import CaptureRequest
from memgarden.policies import CONVERSATION_CAPTURE
from memory import garden_component
from memory.capture_prompt_v1 import IO_CONVERSATION_CAPTURE_POLICY
import memgarden.component as kc
import memgarden.prompts.capture as kp

reply = {reply!r}
def run(policy):
    g = garden_component.build_garden(garden_component.CallableModel(lambda _p: reply))
    r = g.capture(CaptureRequest(window="w", locale="zh-Hans", policy=policy))
    return [len(r.cards), r.error]

print(json.dumps({{
    "default": run(None),
    "pinned": run(CONVERSATION_CAPTURE),
    "io": run(IO_CONVERSATION_CAPTURE_POLICY),
    "prompt_is_original": kc.build_capture_prompt is kp.build_capture_prompt,
    "parse_is_original": kc.parse_capture_cards is kp.parse_capture_cards,
}}))
"""


@pytest.mark.parametrize("order", [
    ["memgarden.component", "memgarden.prompts.capture",
     "memory.capture_prompt_v1", "memory.garden_component"],
    ["memory.garden_component", "memory.capture_prompt_v1",
     "memgarden.component"],
    ["model_api_runtime.v2.extraction", "memory.dream_prompt_v1",
     "memory.capture_prompt_v1", "memory.garden_component"],
])
def test_import_order_does_not_change_policy_behaviour(order) -> None:
    code = _ORDER_PROBE.format(backend=str(BACKEND), order=order,
                               reply=_cards_reply(3))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, cwd=str(BACKEND), timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["default"] == [0, "too_many_cards:3>2"]
    assert out["pinned"] == [0, "too_many_cards:3>2"]
    assert out["io"] == [3, None]
    assert out["prompt_is_original"] is True
    assert out["parse_is_original"] is True


# --------------------------------------------------------------------------- #
# 3. 内核属性不许被 io 改写
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("attr,source", [
    ("build_capture_prompt", kernel_capture),
    ("parse_capture_cards", kernel_capture),
    ("build_capture_retry_prompt", kernel_capture),
    ("build_dream_prompt", kernel_dream),
    ("parse_dream_consolidations", kernel_dream),
    ("build_dream_retry_prompt", kernel_dream),
])
def test_kernel_component_functions_are_not_monkeypatched_by_io(attr, source) -> None:
    """导入 io 的记忆模块后，``memgarden.component`` 里的函数仍是内核原对象。

    io 的定制只能走公开入口（``CaptureRequest.policy`` / ``signals`` 等构造参数），
    不许改写内核模块属性 —— 那种改法对同进程里的所有实例生效，且升级内核后
    悄悄失效或反向生效。
    """
    import memory.capture_prompt_v1  # noqa: F401
    import memory.dream_prompt_v1  # noqa: F401
    import memory.garden_component  # noqa: F401

    assert getattr(kernel_component, attr) is getattr(source, attr)
