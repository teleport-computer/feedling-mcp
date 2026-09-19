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


# 命名空间前缀。第二个是 2026-09-06 线上截图里真实出现的那一个，
# 拼接书写：以字面量形式经过某些工具链时会被改写掉。
# 第四个是 2026-09-06 线上原文逐字节的前缀：数学斜体字母（U+1D44E…），不是 ASCII。
_NS_PREFIXES = ("", "ns:", "a" "ntml:", "\U0001D44E\U0001D45B\U0001D461\U0001D45A\U0001D459:")


def test_namespace_prefixed_tags_are_recognized_like_tool_markup():
    """图4（prod 2026-09-06 用户报障截图）：闭标签带 XML 命名空间前缀。

    旧实现要求协议词紧跟在 ``</`` 之后，于是 ``</<ns>:thinking>`` 在 ``_RESIDUE``
    上零命中 —— 走 fast-path 直接 ABSENT，整段心里话连同标签逐字节进了用户气泡。

    兄弟守卫 ``backend/core/tool_markup_leak.py`` 处理的是**同一个驱动的同一套
    内部 XML 方言**（``<invoke>`` / ``<parameter>``），它早就显式接受一个可选命名
    空间前缀、并且只按 local name 配对（见其 docstring）。本闸 2026-08-08 新建时
    没采纳同一条规则，于是同一族标记里「工具那一半」认得、「思考那一半」不认得。

    词表与前缀集都从常量派生：新增一个协议词而漏掉前缀形态，这条会红。
    """
    assert st._TAG_WORDS, "protocol tag vocabulary must not be empty"
    assert any(p for p in _NS_PREFIXES), "prefix matrix must exercise a real prefix"
    for word in st._TAG_WORDS:
        for prefix in _NS_PREFIXES:
            tag = prefix + word
            lone = f"这句是心里话</{tag}>这句是要说的话"
            status, thinking, reply = st.strip_all_thinking(lone)
            assert status == st.COMPLETE, (tag, status)
            assert reply == "这句是要说的话", (tag, reply)
            assert "这句是心里话" in thinking

            paired = f"<{tag}>这句是心里话</{tag}>这句是要说的话"
            status, thinking, reply = st.strip_all_thinking(paired)
            assert status == st.COMPLETE, (tag, status)
            assert reply == "这句是要说的话", (tag, reply)


def test_namespace_prefixed_open_without_close_fails_closed():
    """前缀形态的截断开标签也必须失败关闭，而不是原样放行。"""
    for prefix in _NS_PREFIXES:
        raw = f"<{prefix}thinking>她在提醒我修思考链 我没办法从技术层面修这个"
        status, thinking, reply = st.strip_all_thinking(raw)
        assert status == st.FAILED, (prefix, status)
        assert reply == "" and thinking == ""


def test_prefix_support_does_not_widen_to_glued_names():
    """前缀必须是**带冒号的**命名空间段，不能顺手把别人的标签也吞了。"""
    for raw in (
        "<mythinking>这是别人的标签</mythinking>",
        "<thought-process>hi</thought-process>",
        "<ns:answer>hi</ns:answer>",
        "chain of thought 是什么意思呀",
    ):
        status, _thinking, reply = st.strip_all_thinking(raw)
        assert status == st.ABSENT, (raw, status)
        assert reply == raw


# ---------------------------------------------------------------------------
# 打捞层（T656，2026-09-19）。判据不放宽：上面每一条 FAILED 的用例照旧红/绿；
# 只是 FAILED 之后再扫一遍，分得出正文就发正文、思考全丢。形状取自 T655 线上
# trace（MiniMax-M3 + openai_compatible + pi：一条回复开 2～3 个 <think>
# 只关 1～2 个，尾块常被截断），正文用占位符。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,reply,reason", [
    # T655 主形状：open 2 / close 1（思考里再开一次）
    ("<think>A<think>B</think>正文", "正文", "nested_open"),
    # open 3 / close 1
    ("<think>A<think>B<think>C</think>正文", "正文", "nested_open"),
    # 成对块 + 正文 + 末尾未闭的第二块（被 token / 输出上限截断）
    ("<think>A</think>正文1<think>B", "正文1", "trailing_unclosed"),
    # open 3 / close 2：块、正文、再一块未闭
    ("<think>A</think>正文1<think>B<think>C</think>正文2<think>D",
     "正文1\n正文2", "nested_open+trailing_unclosed"),
    # 2026-08-08 反例的反向：完整块之后的孤立闭标签只丢标签，正文甲不再被吞
    ("<think>A</think>正文甲</think>正文乙", "正文甲\n正文乙", "stray_close_after_block"),
    # 开闭错配：第一个闭标签就是闭
    ("<think>A</reasoning>正文", "正文", "mismatched_close"),
    # 两个孤立闭、没有任何块：第一个前面是思考，第二个只是噪音
    ("A</think>B</think>C", "B\nC", "lone_close_head+stray_close_after_block"),
    # codex4 复审 2026-09-19 反例：完整配平的嵌套块——外层块的尾巴 C 是思考，
    # 第一版在内层闭标签就出了思考态、把 C 当正文发了出去。
    ("<think>PRIVATE_OUTER_A<think>PRIVATE_INNER</think>PRIVATE_OUTER_B</think>PUBLIC_REPLY",
     "PUBLIC_REPLY", "nested_balanced"),
    # 同形状，命名空间前缀 / aside 标签
    ("<ns:think>A<ns:think>B</ns:think>C</ns:think>R", "R", "nested_balanced"),
    ("<aside>A<aside>B</aside>C</aside>R", "R", "nested_balanced"),
    # 配平嵌套块 + 正文 + 末尾未闭块：正文只有 R
    ("<think>A<think>B</think>C</think>R<think>D", "R", "nested_balanced+trailing_unclosed"),
    # 配平嵌套块 + 正文 + 不配平的再开块：G 是再开块闭掉之后的正文
    ("<think>A<think>B</think>C</think>R<think>E<think>F</think>G", "R\nG", "nested_balanced+nested_open"),
    # codex4 r2 反例：外层不配平、里面却有一个配平的子块（三开两闭）——
    # B_TAIL 在配平的子块里，是思考；r2 的「第一个闭标签结束」把它发了出去。
    ("<think>PRIVATE_A<think>PRIVATE_B<think>PRIVATE_C</think>PRIVATE_B_TAIL</think>PUBLIC_REPLY",
     "PUBLIC_REPLY", "nested_balanced+nested_open"),
    ("<aside>PRIVATE_A<aside>PRIVATE_B<aside>PRIVATE_C</aside>PRIVATE_B_TAIL</aside>PUBLIC_REPLY",
     "PUBLIC_REPLY", "nested_balanced+nested_open"),
    # 两段各带再开的思考：两段正文都要留下（再开的父块只借第一个子块的闭）
    ("<think>A<think>B</think>正文1<think>C<think>D</think>正文2", "正文1\n正文2", "nested_open"),
    # codex4 r3 反例①：再开块借来的闭标签**紧挨着**下一个兄弟块（match.end 是开区间）
    ("<think>A<think>B</think><think>C</think>PUBLIC_REPLY", "PUBLIC_REPLY", "nested_open"),
    ("<think>A<think>B</think> <think>C</think>PUBLIC_REPLY", "PUBLIC_REPLY", "nested_open"),
    # codex4 r3 反例②：借闭的第一个子块自己也是再开块，它借闭之后的兄弟 D 藏在它下面
    ("<think>A<think>B<think>C</think>PUBLIC_1<think>D</think>PUBLIC_2", "PUBLIC_1\nPUBLIC_2", "nested_open"),
    ("<think>A<think>B<think>C</think> PUBLIC_1 <think>D</think> PUBLIC_2", "PUBLIC_1 \n PUBLIC_2", "nested_open"),
    # 再深一层：四开、借闭链三级
    ("<think>A<think>B<think>C<think>D</think>P1<think>E</think>P2", "P1\nP2", "nested_open"),
    # 对照：同样四开但第三个块自己配平了（包住 P1/P2）——那它们是思考，只剩 P3
    ("<think>A<think>B<think>C<think>D</think>P1<think>E</think>P2</think>P3", "P3", "nested_balanced+nested_open"),
])
def test_salvage_keeps_reply_text_and_drops_every_thinking_block(raw, reply, reason):
    # 严格判据仍然拒绝——这是打捞层存在的前提，不是被放宽了。
    assert st.strip_all_thinking(raw)[0] == st.FAILED
    status, thinking, visible = st.strip_all_thinking_or_salvage(raw)
    assert status == st.SALVAGED
    assert visible == reply
    assert thinking == ""            # 打捞出的思考不可信，永远不展示
    assert "<" not in visible        # 没有半个标签出门
    assert st.salvage_thinking(raw)[2] == reason


@pytest.mark.parametrize("raw", [
    "<think>A<think>B",              # 全是思考，没有正文
    "<think>A<think>B<think>C",      # 三开零闭：借闭链到底都没有闭，整段是被截断的思考
    "<think>A<think>B</think>C</think>",  # 配平嵌套、没有正文（codex4 反例的无正文版）
    "<think>A</think>",              # 只有一个完整块（严格判据本来就 SILENT）
    "<thin",                         # 截断的协议开标签头
])
def test_salvage_still_fails_closed_when_no_reply_text_can_be_told_apart(raw):
    status, thinking, visible = st.strip_all_thinking_or_salvage(raw)
    assert status in {st.FAILED, st.SILENT}
    assert visible == ""


def test_salvage_wrapper_is_transparent_for_clean_shapes():
    for raw, want_status in [
        ("正文", st.ABSENT),
        ("<think>A</think>正文", st.COMPLETE),
        ("<think>A</think>\n<think>B</think>\n正文", st.COMPLETE),
        ("<think>A</think>", st.SILENT),
    ]:
        assert st.strip_all_thinking_or_salvage(raw) == st.strip_all_thinking(raw), raw
        assert st.strip_all_thinking_or_salvage(raw)[0] == want_status


def test_salvage_never_publishes_the_outer_tail_of_a_balanced_nested_block():
    """独立于参数表再钉一次 codex4 的反例：三个 PRIVATE 都不许出门。"""
    raw = "<think>PRIVATE_OUTER_A<think>PRIVATE_INNER</think>PRIVATE_OUTER_B</think>PUBLIC_REPLY"
    status, thinking, visible = st.strip_all_thinking_or_salvage(raw)
    assert (status, visible, thinking) == (st.SALVAGED, "PUBLIC_REPLY", "")
    assert "PRIVATE" not in visible


def test_salvage_keeps_balanced_child_private_under_unbalanced_parent():
    """codex4 r2 反例单独钉一次：四个 PRIVATE 一个都不出门。"""
    raw = "<think>PRIVATE_A<think>PRIVATE_B<think>PRIVATE_C</think>PRIVATE_B_TAIL</think>PUBLIC_REPLY"
    status, thinking, visible = st.strip_all_thinking_or_salvage(raw)
    assert (status, visible, thinking) == (st.SALVAGED, "PUBLIC_REPLY", "")
    assert "PRIVATE" not in visible


def test_salvage_is_linear_in_repeated_malformed_blocks():
    """codex4 r2：r2 的实现对每个不配平块重扫后缀，2000 次重复要 0.5 s；
    树形单遍后 8000 次 < 0.05 s。这里钉一个宽松上界，红了说明又变二次方了。"""
    import time
    for unit, per_unit in (
        ("<think>A<think>B</think>PUBLIC\n", 1),                       # 再开链
        ("<think>A<think>B</think><think>C</think>PUBLIC\n", 1),       # 紧挨兄弟
        ("<think>A<think>B<think>C</think>PUBLIC<think>D</think>PUBLIC\n", 2),  # 借闭链
    ):
        raw = unit * 4000
        started = time.perf_counter()
        visible, _thinking, _reason = st.salvage_thinking(raw)
        assert visible.count("PUBLIC") == 4000 * per_unit, unit
        assert time.perf_counter() - started < 1.0, unit


def test_salvage_reasons_are_a_closed_set():
    raw = "<think>A</think>正文1<think>B<think>C</think>正文2<think>D</reasoning>x</think>y"
    for part in st.salvage_thinking(raw)[2].split("+"):
        assert part in st.SALVAGE_REASONS


def test_v1_consumer_salvages_multi_open_think_instead_of_dropping_the_turn(monkeypatch):
    """T655 的落点：以前 thinking_gate_failed 整轮作废；现在正文照发、思考丢掉、
    记 thinking_gate_salvaged + 打捞理由（日报/巡检靠它数接管了多少）。"""
    crc = _load_consumer(monkeypatch)
    turn = crc.AgentTurn()
    raw = "<think>她在生气<think>要不要先道歉</think>先别急，我在呢。"
    visible, thinking = crc._split_tagged_thinking(raw, diagnostics=turn)
    assert visible == "先别急，我在呢。"
    assert thinking == ""
    assert turn.sanitizer_reason == "thinking_gate_salvaged"
    assert turn.raw_reply_diagnostics["salvage_reason"] == "nested_open"
    assert turn.raw_reply_diagnostics["think_open_count"] == 2
    assert turn.raw_reply_diagnostics["think_close_count"] == 1
    # 严格判据仍然会拒：这条不是靠放宽过的。
    assert st.strip_all_thinking(raw, sanitize=False)[0] == st.FAILED


def test_v1_consumer_balanced_nested_block_keeps_outer_tail_private(monkeypatch):
    """codex4 反例打在真出口上：V1 consumer 只发 PUBLIC_REPLY。"""
    crc = _load_consumer(monkeypatch)
    turn = crc.AgentTurn()
    raw = "<think>PRIVATE_OUTER_A<think>PRIVATE_INNER</think>PRIVATE_OUTER_B</think>PUBLIC_REPLY"
    visible, thinking = crc._split_tagged_thinking(raw, diagnostics=turn)
    assert visible == "PUBLIC_REPLY" and thinking == ""
    assert turn.sanitizer_reason == "thinking_gate_salvaged"
    assert turn.raw_reply_diagnostics["salvage_reason"] == "nested_balanced"


def test_v1_consumer_unbalanced_parent_with_balanced_child_keeps_child_private(monkeypatch):
    """codex4 r2 反例打在真出口上。"""
    crc = _load_consumer(monkeypatch)
    turn = crc.AgentTurn()
    raw = "<think>PRIVATE_A<think>PRIVATE_B<think>PRIVATE_C</think>PRIVATE_B_TAIL</think>PUBLIC_REPLY"
    visible, thinking = crc._split_tagged_thinking(raw, diagnostics=turn)
    assert visible == "PUBLIC_REPLY" and thinking == ""
    assert "PRIVATE" not in visible
    assert turn.sanitizer_reason == "thinking_gate_salvaged"


@pytest.mark.parametrize("raw,reply", [
    ("<think>A<think>B</think><think>C</think>PUBLIC_REPLY", "PUBLIC_REPLY"),
    ("<think>A<think>B<think>C</think>PUBLIC_1<think>D</think>PUBLIC_2", "PUBLIC_1\nPUBLIC_2"),
])
def test_v1_consumer_adjacent_and_deep_promoted_shapes_deliver_reply(monkeypatch, raw, reply):
    """codex4 r3 两个反例打在真出口上：r3 之前这两条整轮 FAILED、用户拿到空。"""
    crc = _load_consumer(monkeypatch)
    turn = crc.AgentTurn()
    visible, thinking = crc._split_tagged_thinking(raw, diagnostics=turn)
    assert visible == reply and thinking == ""
    assert not any(ch in visible for ch in "<>")
    assert turn.sanitizer_reason == "thinking_gate_salvaged"


def test_v1_consumer_still_fails_closed_when_nothing_is_reply_text(monkeypatch):
    crc = _load_consumer(monkeypatch)
    turn = crc.AgentTurn()
    visible, thinking = crc._split_tagged_thinking("<think>只有思考<think>没有正文", diagnostics=turn)
    assert visible == ""
    assert turn.sanitizer_reason == "thinking_gate_failed"


def test_salvaged_is_a_registered_resident_sanitizer_reason():
    from notices import error_contract
    assert "thinking_gate_salvaged" in error_contract.RESIDENT_SANITIZER_REASONS
    assert "thinking_gate_failed" in error_contract.RESIDENT_SANITIZER_REASONS
