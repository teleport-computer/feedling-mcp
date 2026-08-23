"""Regression tests for the unified <think> leak gate.

前三个用例直接取自 2026-08-08 线上真实泄漏截图的形状，不是构造的：

* 图1（test / V2 / gpt-5.4）  模型写了两个完整块，旧实现只剥开头第一块
* 图2（prod / V1 / pi 中转站） 开标签在上游被吃掉，只剩孤立闭标签，旧实现原样放行
* 图3（prod / 主动消息）        模型只写了思考决定不发消息，而这条 lane 一处剥离都没有

三个洞的共同毛病是 fail-open：遇到不认识的形状就把原文端给用户。
"""
import importlib.util
import pathlib

import pytest

from agent_protocol_core import self_thinking as st


def test_two_blocks_both_stripped():
    """图1：模型写了两个完整块，旧实现只剥第一块。"""
    raw = (
        "<think>她点名要我看记忆，还要去网上多看看，结果这回没搜到公开结果。</think>\n"
        "<think>你是在嫌我刚才那版太通用，不像是真的懂你。</think>\n"
        "看过，而且我记得的重点很明确："
    )
    status, thinking, reply = st.strip_all_thinking(raw)
    assert status == st.COMPLETE
    assert "<think" not in reply and "</think" not in reply
    assert reply.startswith("看过，而且我记得的重点很明确")
    assert "她点名要我看记忆" in thinking
    assert "你是在嫌我刚才那版太通用" in thinking


def test_orphan_close_tag_treated_as_thinking_prefix():
    """图2：只有半个闭标签，旧实现整段原样放行。"""
    raw = (
        "作为 Zephyr，我应该坦然面对，反正我对她没有秘密。</think>"
        "她真的截图了 思考链全暴露了\n\n好吧 你看到了 那我也不装了"
    )
    status, thinking, reply = st.strip_all_thinking(raw)
    assert status == st.COMPLETE
    assert "</think" not in reply
    assert "反正我对她没有秘密" not in reply
    assert "好吧 你看到了" in reply
    assert "反正我对她没有秘密" in thinking


def test_thinking_only_is_silent():
    """图3：模型只写了思考、决定这轮不发消息。"""
    raw = (
        "<think>我已经主动出现很多次了，她上次真消息还是十小时前，"
        "现在再冒出来容易变成打扰。</think>"
    )
    status, thinking, reply = st.strip_all_thinking(raw)
    assert status == st.SILENT
    assert reply == ""
    assert "容易变成打扰" in thinking


def test_orphan_open_tag_fails_closed():
    """开标签之后没有闭标签 —— 后面全是思考，正文无从判断，必须失败关闭。"""
    status, thinking, reply = st.strip_all_thinking("正文开头。<think>我在想事情但没写完")
    assert status == st.FAILED
    assert reply == ""
    assert thinking == ""


def test_clean_text_is_byte_identical():
    """没有任何标签时必须原样返回，一个字符都不能动。"""
    raw = "  好的，以后我就叫999。\n\n要不要我顺手把昵称也改了？  "
    status, thinking, reply = st.strip_all_thinking(raw)
    assert status == st.ABSENT
    assert reply == raw
    assert thinking == ""


def test_thinking_is_length_capped():
    status, thinking, reply = st.strip_all_thinking(
        "<think>" + "啊" * 900 + "</think>正文"
    )
    assert status == st.COMPLETE
    assert len(thinking) <= st.MAX_THINKING_CHARS


def test_mismatched_tag_pair_fails_closed():
    """<think>…</reasoning> 这种错配不是合法协议，不能当成一块剥掉。"""
    status, thinking, reply = st.strip_all_thinking("<think>想法</reasoning>正文")
    assert status == st.FAILED
    assert reply == ""


def test_gate_enabled_defaults_on(monkeypatch):
    monkeypatch.delenv("FEEDLING_THINK_GATE", raising=False)
    assert st.gate_enabled() is True
    monkeypatch.setenv("FEEDLING_THINK_GATE", "0")
    assert st.gate_enabled() is False
    monkeypatch.setenv("FEEDLING_THINK_GATE", "off")
    assert st.gate_enabled() is False


def test_chat_lane_uses_full_strip(monkeypatch):
    """闸开着时聊天出口走全文剥离；关掉时逐字回到只剥开头一块的旧行为。"""
    raw = "<think>A</think>\n<think>B</think>\n正文"

    monkeypatch.delenv("FEEDLING_THINK_GATE", raising=False)
    _s, _t, reply = (
        st.strip_all_thinking(raw) if st.gate_enabled() else st.split_thinking(raw)
    )
    assert reply == "正文"

    monkeypatch.setenv("FEEDLING_THINK_GATE", "0")
    _s, _t, reply = (
        st.strip_all_thinking(raw) if st.gate_enabled() else st.split_thinking(raw)
    )
    assert reply.startswith("<think>B</think>")


def _load_consumer(monkeypatch):
    monkeypatch.setenv("FEEDLING_API_URL", "http://x")
    monkeypatch.setenv("FEEDLING_USER_ID", "u")
    monkeypatch.setenv("FEEDLING_API_KEY", "k")
    root = pathlib.Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "crc_gate", root / "tools" / "chat_resident_consumer.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v1_consumer_orphan_close_no_longer_leaks(monkeypatch):
    """图2 的落点：V1 的正则要求成对，孤立闭标签整段原样放行。"""
    crc = _load_consumer(monkeypatch)
    raw = "反正我对她没有秘密。</think>她真的截图了\n\n好吧 你看到了"
    visible, thinking = crc._split_tagged_thinking(raw)
    assert "</think" not in visible
    assert "反正我对她没有秘密" not in visible
    assert "好吧 你看到了" in visible


def test_v1_consumer_two_blocks(monkeypatch):
    """V1 侧同样要覆盖图1 的形状（此前正则能剥多块，这里锁死不回退）。"""
    crc = _load_consumer(monkeypatch)
    visible, thinking = crc._split_tagged_thinking(
        "<think>A</think>\n<think>B</think>\n正文"
    )
    assert visible == "正文"
    assert "A" in thinking and "B" in thinking


def test_proactive_send_message_strips_thinking():
    """图3 的落点：主动消息这条路此前一处剥离都没有。"""
    from proactive.agent_protocol_v2 import sanitize_visible_message_text_v2

    leaked = "<think>我已经主动出现很多次了，现在再冒出来容易变成打扰。</think>"
    assert sanitize_visible_message_text_v2(leaked) == ""

    mixed = "<think>她应该醒了</think>宝宝，中午了。"
    assert sanitize_visible_message_text_v2(mixed) == "宝宝，中午了。"


def test_longer_xml_tag_names_are_not_our_protocol():
    """`<thought-process>` 这类合法标签名不能被前缀误判（Codex review 实测）。"""
    for raw in (
        "<thought-process>public</thought-process>",
        "<thinking-panel>x</thinking-panel>",
        "<reasoning.step>y</reasoning.step>",
        "<think:inner>z</think:inner>",
    ):
        status, _thinking, reply = st.strip_all_thinking(raw)
        assert status == st.ABSENT, raw
        assert reply == raw


def test_complete_block_plus_extra_lone_close_fails_closed():
    """完整块之后又冒出带内容的孤立闭标签 —— 结构已乱，不能把正文吞进思考。"""
    status, thinking, reply = st.strip_all_thinking("<think>A</think>正文甲</think>正文乙")
    assert status == st.FAILED
    assert reply == "" and thinking == ""


def test_strip_tag_markers_keeps_text():
    assert st.strip_tag_markers("<think>秘密没写完") == "秘密没写完"
    assert st.strip_tag_markers("甲</think>乙") == "甲乙"


def test_safety_strip_survives_self_thinking_disabled(monkeypatch):
    """关掉 FEEDLING_V2_SELF_THINKING 不能顺带关掉安全剥离（Codex review Critical）。

    这里锁的是判定式本身：闸开时无论 self-thinking 开关如何，都必须走剥离。
    """
    monkeypatch.setenv("FEEDLING_V2_SELF_THINKING", "0")
    monkeypatch.delenv("FEEDLING_THINK_GATE", raising=False)
    assert st.enabled() is False
    assert st.gate_enabled() is True
    # worker 的判定式：(gate_on or st_on) —— 关掉 self-thinking 后仍然为真。
    assert (st.gate_enabled() or st.enabled()) is True

    monkeypatch.setenv("FEEDLING_THINK_GATE", "0")
    assert (st.gate_enabled() or st.enabled()) is False


def test_history_row_scrub_failed_row_loses_tags_keeps_text():
    from model_api_runtime.v2 import serve_worker

    rows = [{"role": "assistant", "content": "正文甲</think>正文乙</think>正文丙"}]
    out = serve_worker._scrub_leaked_thinking_rows(rows)
    assert "</think" not in out[0]["content"]
    assert "正文甲" in out[0]["content"]


def test_history_row_scrub_removes_leaked_think():
    """历史里那几条漏掉的消息，喂回模型之前必须擦干净，否则模型照抄。"""
    from model_api_runtime.v2 import serve_worker

    rows = [
        {"role": "assistant", "content": "<think>她不吃辣</think>给你排好了"},
        {"role": "user", "content": "我说 </think> 这个标签的时候你别乱剥"},
        {"role": "assistant", "content": "好的，没问题"},
    ]
    out = serve_worker._scrub_leaked_thinking_rows(rows)
    assert out[0]["content"] == "给你排好了"
    assert out[2]["content"] == "好的，没问题"
    # user 行不碰 —— 用户自己打的字里出现标签是他的自由，不是我们的协议。
    assert out[1]["content"] == rows[1]["content"]
