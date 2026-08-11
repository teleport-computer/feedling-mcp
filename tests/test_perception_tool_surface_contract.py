"""模型不知道自己能读健康数据 —— 工具面没告诉它。

usr_f10e9fd5ebd63ea0(2026-08-10,test/V2,openrouter openai/gpt-5.4)问
「今天走了多少步」,AI 回「能查,但这次查出来是空的」。**AI 没说错,也没偷懒**:

`perception_snapshot` 不传 `signals` 时只返回 FAST 那五个
(now/location/weather/motion/calendar)—— **步数不在里面**。健康类 13 个信号
必须显式点名,而模型无从知道这些名字:参数是自由字符串没有 enum,描述里从不
列举信号,猜错还撞 400 `unknown_signals`。猜是被惩罚的。

考古:FAST/SLOW 两档是 2026-06-23 `3644e73c` 给 **CLI 端点**设计的,那里调用方
知道信号名,合理。断裂点是这个默认被 V2 聊天工具面继承,而那边的调用方是个
没有词表的模型。

本文件钉住工具面的**可发现性契约**:模型看得见的东西,必须足以让它问对问题。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from agent import perception_core  # noqa: E402
from capabilities import tool_schema  # noqa: E402
from perception import agent_fields  # noqa: E402


_SIGNAL_PARAM_TOOLS = {
    "perception_snapshot": "signals",
    "perception_history": "signal",
    "perception_trend": "signal",
}


def _param_schema(tool: str, param: str) -> dict:
    return (tool_schema.PARAMS[tool].get("properties") or {}).get(param) or {}


def _enum_of(tool: str, param: str) -> set[str]:
    """取该参数的 enum(数组参数取 items 的 enum)。"""
    schema = _param_schema(tool, param)
    if schema.get("type") == "array":
        schema = schema.get("items") or {}
    return set(schema.get("enum") or [])


# --------------------------------------------------------------------------- #
# 信号名必须暴露给模型,而且与后端同源
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("tool,param", sorted(_SIGNAL_PARAM_TOOLS.items()))
def test_the_signal_parameter_enumerates_the_valid_names(tool, param):
    """自由字符串 = 模型只能猜,而猜错是 400。"""
    enum = _enum_of(tool, param)

    assert enum, (
        f"{tool}.{param} 没有 enum —— 模型无从知道有哪些信号可用,"
        "这正是它问不出步数的原因"
    )


@pytest.mark.parametrize("tool,param", sorted(_SIGNAL_PARAM_TOOLS.items()))
def test_the_enum_is_exactly_what_the_backend_accepts(tool, param):
    """enum 与后端的受理集必须**完全一致**。

    少一个 = 模型看不到某个能力;多一个 = 模型照着 schema 调却撞 400。
    两边都不许手抄:加信号时要么一起动,要么这条红。
    """
    assert _enum_of(tool, param) == set(perception_core.AGENT_PERCEPTION_SIGNALS)


def test_every_enumerated_signal_actually_projects_fields():
    """enum 里不许有死值 —— 列出来却取不到字段等于骗模型。"""
    dead = [
        s for s in perception_core.AGENT_PERCEPTION_SIGNALS
        if not agent_fields.AGENT_SIGNAL_FIELDS.get(s)
    ]

    assert dead == [], f"这些信号列在受理集里却没有任何字段:{dead}"


# --------------------------------------------------------------------------- #
# 默认值那个坑必须写在模型看得见的地方
# --------------------------------------------------------------------------- #

def test_the_fast_default_really_does_omit_health():
    """钉住事故形状本身。这条一旦变红,说明默认集被改过 —— 那是好事,

    但下面那条「描述必须讲清默认」的断言也要跟着重新审,别留一句过时的话。
    """
    fast = set(agent_fields.FAST_AGENT_PERCEPTION_SIGNALS)

    assert "steps" not in fast, "默认已含步数 —— 请同步修订描述里的那句提醒"
    assert {"vitals", "sleep", "workout"}.isdisjoint(fast)


def test_the_description_warns_that_omitting_signals_skips_health():
    """模型必须能读到「不点名就拿不到健康数据」这件事。"""
    desc = tool_schema.DESCRIPTIONS["perception_snapshot"]

    assert "steps" in desc, "描述里没出现 steps —— 模型不知道能读步数"
    mentions_default = any(
        w in desc.lower() for w in ("default", "omit", "without", "explicit")
    )
    assert mentions_default, (
        "描述没讲清「不传 signals 时会漏掉健康数据」,"
        f"模型只会调一次空参数然后如实说查不到:{desc!r}"
    )


@pytest.mark.parametrize("signal", ["steps", "sleep", "vitals", "workout"])
def test_the_health_signals_are_named_in_the_description(signal):
    """能读什么要写出来 —— 「读取最新感知快照」等于什么都没说。"""
    assert signal in tool_schema.DESCRIPTIONS["perception_snapshot"]


# --------------------------------------------------------------------------- #
# 既有行为不许被这次改动带坏
# --------------------------------------------------------------------------- #

def test_an_unknown_signal_still_fails_loudly():
    """400 本身是对的(静默返回空更糟),enum 是为了让模型不必猜。"""
    with pytest.raises(perception_core.AgentRouteError) as caught:
        perception_core.agent_perception_payload(
            _FakeStore(), signals_raw="step_count",   # 像模像样但不存在的名字
        )

    assert caught.value.status_code == 400
    assert caught.value.body.get("error") == "unknown_signals"
    assert "available" in caught.value.body, (
        "报错里必须带可用列表,否则模型第二次还是瞎猜"
    )


def test_steps_maps_to_the_step_count_field():
    """信号名与字段名不同(steps -> step_count),这层映射别被改掉。"""
    assert agent_fields.AGENT_SIGNAL_FIELDS["steps"] == ("step_count",)


class _FakeStore:
    user_id = "usr_contract_test"

    def load_proactive_settings(self):
        return {}
