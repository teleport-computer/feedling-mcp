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


def test_conversation_rows_keep_typed_dots_but_drop_voice_artifacts():
    assert [row["id"] for row in conversation_rows(_voice_rows())] == [
        "typed-dots",
        "voice-current-revision",
        "real-chat",
    ]


def test_v2_prompt_never_replays_archive_preview_or_voice_noise_reply():
    messages = context.build_turn_messages(
        system_prompt="SYS",
        summary="",
        tail=_voice_rows(),
    )
    rendered = "\n".join(str(message.get("content") or "") for message in messages)
    assert "- 对方: 你好" not in rendered
    assert "又是点点点" not in rendered
    assert "……" not in rendered
    assert "..." in rendered  # ordinary typed chat is untouched
    assert "今天在成都" in rendered


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
    assert "- 对方: 你好" not in request
    assert "又是点点点" not in request
    assert "那试一条语音吧" not in request
    assert "可以啊，今天什么时候日落？" in request
    assert "今天在成都" in request

    calls.clear()
    hidden_only = _voice_rows()[1:4]
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
