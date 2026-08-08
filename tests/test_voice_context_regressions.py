"""2026-08-08 语音回归的守卫:三条都在 prod 咬过人,且都只在一条 lane 上表现。

背景:08-07 合入、08-08 06:01 上 prod 的四个语音提交引入了三类问题。共同点是
**改动本身的意图都对**,但都只覆盖了作者最熟的那条路径:

1. 返回给 ElevenLabs 的 completion 可以没有正文 → 它判协议错误
   (`1002 custom_llm_error: LLM Cascade Error`)并**杀掉整通电话**;
2. V2 的解密视图不带 turn id → 通话中的每一轮在 prompt 尾巴里被判成"已被取代"
   而整体消失,同一处还让迟到回复抑制从未武装;
3. 通话卡被从尾巴/压缩/dream 里整个删掉 → 挂断后伴侣完全不知道刚才通过话。

这里测的是控制流与真实数据形状,不 inspect 源码。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from voice.message_filter import (  # noqa: E402
    VOICE_CALL_RECORD_ROLE,
    conversation_rows,
)


def _v2_view_row(**over):
    """严格照 serve_worker._decrypt_chat_rows 非 capture 分支的输出形状。

    ⚠️ 这个 fixture 就是契约本身。**别照记忆里的形状改它** —— 上一版回归正是
    因为测试喂的形状比生产多带了 turn id,BUG 才活着上了 prod。
    """
    row = {"id": "u1", "ts": 1.0, "role": "user", "content": "我最近在准备插画展",
           "seq": 1, "voice_call_id": "call_abc"}
    row.update(over)
    return row


# ── 1. V2 尾巴不能把通话轮次整体吞掉 ────────────────────────────────────


def test_v2_tail_keeps_voice_turns_that_carry_turn_metadata():
    """带 turn id 的通话行必须留在尾巴里 —— 连同它们的回复。

    没修之前 V2 的解密视图不带 `voice_logical_turn_id`,而"只保留最新 ASR 修订"
    的判据取不到 key → `None != row_id` → **每一轮**都被判成已被取代,
    用户话和回复一起消失。实测当时 V2 保留 `[]`、resident 保留全部 4 条。
    """
    rows = [
        _v2_view_row(id="u1", seq=1, voice_logical_turn_id="1"),
        {"id": "a1", "ts": 2.0, "role": "assistant", "content": "听起来很期待",
         "seq": 2, "voice_call_id": "call_abc", "reply_to_message_id": "u1"},
        _v2_view_row(id="u2", seq=3, content="展期定在下周三",
                     voice_logical_turn_id="2"),
        {"id": "a2", "ts": 4.0, "role": "assistant", "content": "我记下了",
         "seq": 4, "voice_call_id": "call_abc", "reply_to_message_id": "u2"},
    ]
    kept = [r["id"] for r in conversation_rows(rows)]
    assert kept == ["u1", "a1", "u2", "a2"], (
        f"通话轮次在尾巴里被吞掉了,只剩 {kept}"
    )


def test_only_the_newest_asr_revision_of_one_logical_turn_survives():
    """同一逻辑轮的旧 ASR 修订该被丢掉 —— 这是原改动要的正经行为,别修坏了。"""
    rows = [
        _v2_view_row(id="old", seq=1, content="我最近在准备插",
                     voice_logical_turn_id="1"),
        _v2_view_row(id="new", seq=2, content="我最近在准备插画展",
                     voice_logical_turn_id="1"),
    ]
    kept = [r["id"] for r in conversation_rows(rows)]
    assert kept == ["new"]


# ── 2. 通话卡必须留在上下文里,但不能冒充伴侣自己说的话 ──────────────────


def _card(**over):
    row = {
        "id": "card1", "ts": 100.0, "role": "openclaw",
        "source": "voice_call_transcript",
        "content": "我: 今天去看了插画展\n年年: 听起来很棒",
        "seq": 10, "voice_call_id": "c1",
        "voice_turn_count": 12, "voice_duration_sec": 372,
    }
    row.update(over)
    return row


def test_voice_card_stays_in_conversation_context():
    """卡不能被删掉。

    删掉的代价:挂断之后伴侣在普通聊天里完全不知道刚才通过话 —— 用户接着打字说
    「刚才电话里说的那个」,模型没有任何上下文。信息形状不对就把信息本身消掉,
    是这次明确否掉的做法。
    """
    kept = conversation_rows([_card()])
    assert len(kept) == 1, "通话卡被从对话上下文里删掉了"
    assert kept[0]["id"] == "card1"


def test_voice_card_is_not_replayed_as_the_companion_own_words():
    """卡的正文是**双方混合**的预览,绝不能以 assistant 身份 replay。

    原始 role 是 openclaw → 会被归一成 assistant,于是模型看到「我(助手)说了
    一段包含用户台词的话」。那正是 2026-07-17 字面 `user:` 标签事故的同族:
    它会学着写对话体,也会把对方做的事写成自己做的。
    """
    card = conversation_rows([_card()])[0]
    assert card["role"] == VOICE_CALL_RECORD_ROLE
    assert card["role"] not in {"openclaw", "assistant", "agent", "model"}


def test_voice_card_block_declares_who_is_who_and_that_it_is_not_its_own_speech():
    """抬头必须自带说话人对照 + 「这不是你说过的话」。

    抬头放在**正文**里而不是靠 role:六个调用点对 role 的处理各不相同,
    但都会渲染 content —— 信息放在那里才不会在某条 lane 上漏掉。
    """
    content = conversation_rows([_card()])[0]["content"]
    assert "不是你说过的话" in content
    assert "「我」" in content, "必须说清「我」指的是谁"
    assert "12 轮" in content and "6 分钟" in content, "通话规模要交代"
    assert "我: 今天去看了插画展" in content, "逐字记录片段本身必须还在"


def test_empty_card_is_dropped_rather_than_rendered_as_a_bare_header():
    """预览为空时不要留一个只有抬头、没有内容的空壳。"""
    assert conversation_rows([_card(content="   ")]) == []


# ── 3. 噪音行仍然要挡住(原改动的正经意图) ─────────────────────────────


@pytest.mark.parametrize("noise", ["……", "(inaudible)", "[music]"])
def test_transport_noise_rows_stay_out_of_context(noise):
    rows = [_v2_view_row(content=noise, voice_logical_turn_id="1")]
    assert conversation_rows(rows) == []


def test_a_short_real_utterance_is_not_mistaken_for_noise():
    """「嗯」「好」「对」是真实发言,不是噪音。"""
    for text in ("嗯", "好", "对"):
        rows = [_v2_view_row(content=text, voice_logical_turn_id="1")]
        assert conversation_rows(rows), f"{text!r} 被误判成噪音"


# ── 4. 返回给 ElevenLabs 的 completion 永远不能没有正文 ──────────────────


def test_a_silent_turn_still_carries_content_so_the_call_survives():
    """「这一轮不说话」也必须是带正文的 completion。

    2026-08-08 线上事故:噪音轮与"生命周期已结束"两条路径返回了零 content 的
    SSE 流(只有 role 块 + finish 块)。ElevenLabs 的 Custom LLM 拿到没有任何
    正文的 completion 会判协议错误 `1002 custom_llm_error: LLM Cascade Error`,
    **杀掉整通电话** —— 用户侧是「暂时无法通话」,客户端日志前一行正是
    `ignored control-only agent response`。

    保证放在 `_streaming_text_response` 内部而不是各调用点:调用点是开集
    (以后还会有别的"这一轮不说话"),漏一个就是又一次线上事故。
    """
    import asyncio
    import json

    from voice import routes_asgi

    async def content_of(text):
        response = routes_asgi._streaming_text_response("chatcmpl-test", text)
        body = b""
        async for chunk in response.body_iterator:
            body += chunk if isinstance(chunk, bytes) else chunk.encode()
        out = ""
        for line in body.decode().splitlines():
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            delta = json.loads(line[6:])["choices"][0]["delta"] or {}
            out += delta.get("content") or ""
        return out

    silent = asyncio.run(content_of(""))
    assert silent, (
        "空文本产生了零 content 的流 —— ElevenLabs 会判协议错误并杀掉整通电话"
    )
    # 必须用 ElevenLabs 自己文档里的缓冲串:线上每一通正常电话都以它开头,
    # 所以**已知**它不会被判成空。裸空格没有这个证据,可能被 trim 后仍判无文本。
    assert silent == routes_asgi._VOICE_BUFFER_TEXT
    # 但绝不能是真实语义内容
    assert not silent.strip(" ." ) or silent.strip() == "...", (
        f"静音轮不该包含实际话语,实际={silent!r}"
    )
    # 真实文案照常原样送达
    assert asyncio.run(content_of("模型暂时不可用")) == "模型暂时不可用"


# ── 5. 最终 prompt 里的标签(不是过滤器的输出) ─────────────────────────


def test_v1_final_prompt_does_not_label_the_call_record_as_companion_speech():
    """换了 role 还不够 —— **最终渲染层**也得认这个 role。

    codex 审出:`conversation_rows` 把 role 换成 voice_call_record 之后,
    resident 的 `_message_role_for_context` 仍把「所有非 user 的行」归成 agent,
    `_capture_message_role` 经 `transcript_speaker_label` 同样归给 AI ——
    于是记录块在最终 prompt 里又变回了「伴侣自己说的话」。

    **修了过滤层、漏了渲染层,正是这批改动本身在批评的那个错误。**
    所以这条断言必须打在**边界**上(最终标签),不能只打在过滤器输出上。
    """
    import os

    os.environ.setdefault("FEEDLING_API_URL", "http://localhost:5001")
    os.environ.setdefault("FEEDLING_API_KEY", "test_key_00000000")
    os.environ.setdefault("AGENT_MODE", "http")
    os.environ.setdefault("AGENT_HTTP_URL", "http://localhost:8080/chat")
    os.environ.setdefault(
        "CHECKPOINT_FILE", "/tmp/feedling_test_voice_regressions_checkpoint.json"
    )
    import tools.chat_resident_consumer as crc

    record = crc._conversation_rows([_card()])[0]

    for label in (
        crc._message_role_for_context(record),
        crc._capture_message_role(record, user_label="小雨", agent_label="年年"),
    ):
        assert label not in {"agent", "年年", "我"}, (
            f"通话记录被标成了伴侣自己的发言({label!r})"
        )
        assert label not in {"user", "小雨", "对方"}, (
            f"通话记录被标成了用户的发言({label!r})"
        )


def test_cancel_does_not_promise_an_archive_it_cannot_deliver():
    """cancel 不许声称"行留着等 finalize" —— 那个 finalize 到不了。

    `voice_call_cancel` 先把状态写成 cancelled;`voice_call_begin_finalize`
    见到 cancelled 会**永远**返回 cancelled,finalize 路由 409。所以任何
    "保留行等后续 finalize 归档"的说法都是假承诺(codex 审出,已撤回该守卫)。

    这条锁的是:要么别留、要么先设计出可恢复的中间态 —— 不能只留个安慰。
    """
    from pathlib import Path as _Path

    source = (
        _Path(__file__).parent.parent / "backend" / "voice" / "routes_asgi.py"
    ).read_text(encoding="utf-8")
    assert "rows_kept_for_finalize" not in source, (
        "cancel 又在承诺一个到不了的 finalize;"
        "要恢复这条路必须先有可恢复的生命周期状态"
    )
