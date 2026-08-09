"""Voice archive cards and ASR artifacts are not conversational history."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import compaction, context, serve_worker, worker  # noqa: E402
from voice.message_filter import conversation_rows  # noqa: E402


def _voice_rows() -> list[dict]:
    return [
        {
            "id": "typed-dots",
            "role": "user",
            "source": "chat",
            "content": "...",
        },
        {
            "id": "voice-noise",
            "role": "user",
            "source": "chat",
            "voice_call_id": "vcall_old",
            "content": "……",
        },
        {
            "id": "noise-reply",
            "role": "assistant",
            "source": "model_api",
            "reply_to_message_id": "voice-noise",
            "content": "又是点点点。",
        },
        {
            "id": "archive-card",
            "role": "assistant",
            "source": "voice_call_transcript",
            "voice_call_id": "vcall_old",
            "content": "- 对方: 你好\n- 我: 你好呀",
        },
        {
            "id": "voice-old-revision",
            "role": "user",
            "source": "chat",
            "voice_call_id": "vcall_live",
            "voice_turn_id": "2.old",
            "voice_logical_turn_id": "2",
            # Simulate the brief primary-to-TEE mirror lag: ordering still
            # makes the later revision authoritative before this flips.
            "voice_turn_status": "current",
            "content": "可以啊",
        },
        {
            "id": "voice-old-reply",
            "role": "assistant",
            "source": "model_api",
            "reply_to_message_id": "voice-old-revision",
            "content": "那试一条语音吧",
        },
        {
            "id": "voice-current-revision",
            "role": "user",
            "source": "chat",
            "voice_call_id": "vcall_live",
            "voice_turn_id": "2.current",
            "voice_logical_turn_id": "2",
            "voice_turn_status": "current",
            "content": "可以啊，今天什么时候日落？",
        },
        {
            "id": "real-chat",
            "role": "user",
            "source": "chat",
            "content": "今天在成都",
        },
    ]


def test_conversation_rows_keep_typed_dots_and_the_call_record_but_drop_artifacts():
    """噪音行与旧 ASR 修订仍然要挡住;**通话卡要留下**。

    这条原本断言卡也被丢掉。2026-08-08 定案:卡被整个删掉的代价是挂断之后伴侣
    在普通聊天里完全不知道刚才通过话 —— 用户接着打字说「刚才电话里说的那个」,
    模型没有任何上下文。卡改成换身份保留(见 test_voice_context_regressions)。
    """
    assert [row["id"] for row in conversation_rows(_voice_rows())] == [
        "typed-dots",
        "archive-card",
        "voice-current-revision",
        "real-chat",
    ]


def test_v2_prompt_never_replays_voice_noise_and_labels_the_call_record():
    messages = context.build_turn_messages(
        system_prompt="SYS",
        summary="",
        tail=_voice_rows(),
    )
    rendered = "\n".join(str(message.get("content") or "") for message in messages)
    # 噪音行与它的回复照旧挡住
    assert "又是点点点" not in rendered
    assert "……" not in rendered
    assert "..." in rendered  # ordinary typed chat is untouched
    assert "今天在成都" in rendered

    # 通话记录进 prompt,但**必须带抬头**,而且不能以伴侣自己的身份出现。
    # 卡的正文是双方混合的预览,原样 replay 会让模型把用户的话当成自己说的
    # —— 与 2026-07-17 字面 `user:` 标签事故同族。
    assert "- 对方: 你好" in rendered, "通话记录不该从 prompt 里整个消失"
    card_messages = [
        m for m in messages if "- 对方: 你好" in str(m.get("content") or "")
    ]
    assert len(card_messages) == 1
    block = str(card_messages[0]["content"])
    assert "不是你说过的话" in block, "必须声明这不是伴侣自己的发言"
    assert block.index("不是你说过的话") < block.index("- 对方: 你好"), (
        "抬头必须在逐字记录之前,否则模型先读到对话体再读到说明"
    )


def test_future_compaction_omits_voice_artifact_content_but_keeps_coverage():
    calls = []

    async def llm(_cfg, messages, **_kwargs):
        calls.append(messages)
        return {"reply": "- 用户今天在成都"}

    out = asyncio.run(
        compaction.compact_segment(
            provider_config=object(),
            old_messages=_voice_rows(),
            llm=llm,
            verbatim_max_chars=0,
        )
    )
    request = str(calls)
    assert out == "- 用户今天在成都"
    assert "又是点点点" not in request
    # 通话记录也要进滚动摘要:否则压缩之后那通电话就彻底不存在了。
    assert "- 对方: 你好" in request, "通话记录不该被排除在压缩输入之外"
    assert "那试一条语音吧" not in request
    assert "可以啊，今天什么时候日落？" in request
    assert "今天在成都" in request

    calls.clear()
    # 只含**真正不可见**的行:噪音用户行 + 挂在它下面的回复。
    # 原来这里切的是 [1:4](多含一张通话卡),那时卡也被丢弃所以整段确实为空;
    # 现在卡是**可见内容**(它代表真实发生过的一通电话),含卡的段落理应正常
    # 走摘要而不是被确定性折叠掉 —— 那才是「整段不可见」这条捷径的本意。
    hidden_only = _voice_rows()[1:3]
    deterministic = asyncio.run(
        compaction.compact_segment(
            provider_config=object(),
            old_messages=hidden_only,
            llm=llm,
        )
    )
    assert calls == []
    assert deterministic == compaction.deterministic_fold(
        source_message_count=len(hidden_only)
    )


def test_capture_still_expands_structured_archive_instead_of_card_preview():
    card = {
        "role": "assistant",
        "source": "voice_call_transcript",
        "voice_call_id": "vcall_full",
        "voice_turn_count": 2,
        "content": "CARD PREVIEW ONLY",
    }
    archived = "- 对方: 全文第一句\n- 我: 全文第二句"

    rendered = worker._render_capture_line(
        card,
        {"vcall_full": archived},
        user_name="小雨",
        ai_name="小舟",
    )

    assert "CARD PREVIEW ONLY" not in rendered
    assert archived in rendered
    assert "共 2 轮" in rendered


def test_v2_decrypt_preserves_voice_routing_metadata_for_each_lane(monkeypatch):
    row = {
        "id": "archive-card",
        "ts": 1.0,
        "seq": 9,
        "role": "openclaw",
        "source": "voice_call_transcript",
        "voice_call_id": "vcall_full",
        "voice_turn_count": 8,
        "voice_duration_sec": 75,
        "body_ct": "ciphertext",
        "K_enclave": "wrapped",
    }
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "rt")
    monkeypatch.setattr(
        serve_worker.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"preview",
    )

    normal = serve_worker._decrypt_chat_rows(
        "user", [row], user_only=False
    )[0]
    assert normal["source"] == "voice_call_transcript"
    assert normal["voice_call_id"] == "vcall_full"
    assert "voice_turn_count" not in normal

    capture = serve_worker._decrypt_chat_rows(
        "user", [row], user_only=False, include_capture_metadata=True
    )[0]
    assert capture["voice_turn_count"] == 8
    assert capture["voice_duration_sec"] == 75
    assert capture["capture_eligible"] is True
