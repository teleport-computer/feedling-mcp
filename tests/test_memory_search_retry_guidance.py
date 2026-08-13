"""memory_search 的工具描述必须和运行时的去重语义一致。

usr_7f30…(2026-08-13,prod/V2)让伴侣找记忆花园里的几张卡,伴侣回答
「我搜遍了 ElevenLabs、MiniMax、西双版纳这些词,一张卡都搜不到」,然后反过来求
用户把卡的正文复制粘贴给它。

真实经过是两件事叠在一起:

1. **匹配是字面子串**(`enclave/readside.py` 的 `memory_index_filter_items`:
   `if query not in haystack`,只做 casefold)。卡是中文写的("捏声音"),
   搜英文品牌名本来就该 0 命中 —— 代码完全按设计工作。
2. **一次未命中之后不许再搜**:当时的工具描述写着
   "do not repeat it after one discovery result",而当时线上的运行时去重键
   还是**按工具名**去重,两者一致地把模型钉死在"一轮只能搜一次"。

于是模型把「这个写法没命中」如实汇报成了「这张卡不存在」。

运行时那一半已经修好(去重键改成按 canonical args,不同 query 各算一次)。
**本文件钉住剩下的那一半**:描述不能再劝模型别搜第二次,而且描述里承诺的
"换个写法再搜一次" 必须真的能跑通 —— 否则又变成提示词和运行时各说各话。

判据不是文案好不好看,是**描述与去重键这两处永远不能互相打架**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from capabilities import tool_schema  # noqa: E402
from model_api_runtime.v2 import tool_loop  # noqa: E402
from provider_types import ToolCall  # noqa: E402


def _description() -> str:
    return tool_schema.DESCRIPTIONS["memory_search"].lower()


def _call(query: str, **extra) -> ToolCall:
    return ToolCall(id="c1", name="memory_search", args={"query": query, **extra})


def test_description_no_longer_forbids_searching_again_after_one_result():
    """这句正是 usr_7f30 那轮把模型钉死的半边。"""
    assert "do not repeat it after one discovery result" not in _description()


def test_description_warns_that_empty_is_not_absent():
    """0 命中必须被解释成「这个写法没有」,不是「这段记忆没有」。"""
    description = _description()
    assert "substring" in description
    assert "not that the memory is absent" in description


def test_description_tells_the_model_to_retry_instead_of_asking_the_user_to_paste():
    """截图里模型的收场动作(求用户粘贴原文)必须被显式挡掉。

    ⚠️ 断言必须锁**完整的否定小句**。第一版只断言 `"paste" in description`,
    codex2 审计时把 "do not ask them to paste" 反转成 "ask them to paste",
    测试照样绿 —— 那等于把「让用户粘贴」写进指令还没人红。
    """
    description = _description()
    assert "search again with different wording" in description
    assert "do not tell the user the memory does not exist" in description
    assert "do not ask them to paste the content back to you" in description


def test_description_states_the_actual_repeat_contract():
    """描述里的重搜契约必须和去重键一致,别把「同参数」说成「同 query」。

    去重键是整份 canonical args:同 query 换 limit/bucket/thread 是**允许**的;
    而 readside 会把大小写和首尾空白归一化,所以只差这两样的重搜是白烧一次调用。
    """
    description = _description()
    assert "identical repeat of the same arguments" in description
    assert "case and surrounding whitespace are normalized away" in description


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("ElevenLabs", "MiniMax"),          # 同一轮问的两个不同主题
        ("ElevenLabs", "捏声音"),            # 换成用户自己的说法
        ("西双版纳", "傣族园"),               # 换成更短的特征片段
    ],
)
def test_runtime_actually_allows_the_retry_the_description_promises(first, second):
    """描述让模型换写法再搜,去重键就必须把不同写法算成不同的调用。

    这是本文件的核心:两处不一致时,模型会照描述去搜,却被运行时静默吞掉
    (吞掉时返回的是 "already completed; use its prior result"),
    于是它得到的结论仍然是「搜不到」。
    """
    assert tool_loop._memory_discovery_call_key(_call(first)) != (
        tool_loop._memory_discovery_call_key(_call(second))
    )


def test_identical_arguments_are_still_deduplicated():
    """描述里唯一保留的禁令(别用完全相同的参数重复调)必须仍然成立。"""
    assert tool_loop._memory_discovery_call_key(_call("ElevenLabs")) == (
        tool_loop._memory_discovery_call_key(_call("ElevenLabs"))
    )


def test_same_query_narrowed_by_another_field_is_not_deduplicated():
    """描述承诺「同 query 换 limit/bucket/thread 算合法重搜」,去重键必须放行。"""
    assert tool_loop._memory_discovery_call_key(_call("ElevenLabs")) != (
        tool_loop._memory_discovery_call_key(_call("ElevenLabs", bucket="story"))
    )
    assert tool_loop._memory_discovery_call_key(_call("ElevenLabs")) != (
        tool_loop._memory_discovery_call_key(_call("ElevenLabs", limit=200))
    )


def test_memory_index_stays_once_per_turn():
    """放开的只有 search;总览仍然是一轮一次,别顺手把它也放开了。"""
    browse = ToolCall(id="c2", name="memory_index", args={"bucket": "about_me"})
    other_browse = ToolCall(id="c3", name="memory_index", args={"bucket": "story"})
    assert tool_loop._memory_discovery_call_key(browse) == (
        tool_loop._memory_discovery_call_key(other_browse)
    )
