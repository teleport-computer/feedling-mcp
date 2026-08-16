"""Pure-unit tests for hangup bookkeeping: the transcript card (no DB).

Replaces test_voice_summary.py — the model-written summary was removed on
2026-08-07 in favour of archiving the full transcript and letting Capture read
it. What is still worth locking here is the card that stands in for the call in
the chat stream, because two of its properties are load-bearing:

- it is **bounded** (an oversized chat row makes V2 compaction raise
  ``compaction_message_exceeds_char_budget``, taking the user's ordinary text
  chat down with it), and
- it carries ``voice_call_id`` (without it Capture cannot find the archive, and
  would silently distil the preview instead of the whole call).
"""

from __future__ import annotations

from voice import cleanup
from voice import transcript_store


def test_card_message_id_is_deterministic_per_call():
    a = cleanup.transcript_card_message_id("vcall_abc")
    assert a == cleanup.transcript_card_message_id("vcall_abc")
    assert a != cleanup.transcript_card_message_id("vcall_def")


def test_persisted_card_carries_call_id_and_counts(monkeypatch):
    captured = {}

    class Store:
        user_id = "u_voice_card"

        def append_chat(self, role, source, envelope, **kwargs):
            captured.update(role=role, source=source, envelope=envelope, **kwargs)

    monkeypatch.setattr(cleanup.db, "chat_get_strict", lambda *_args: None)
    monkeypatch.setattr(
        cleanup.core_envelope,
        "_build_shared_envelope_for_store",
        lambda *_args, **_kwargs: ({"id": "card-id"}, None),
    )

    assert cleanup.persist_transcript_card(
        Store(), "预览文本", "card-id", "vcall_abc",
        turn_count=12, duration_sec=340,
    )
    assert captured["role"] == "openclaw"
    assert captured["source"] == "voice_call_transcript"
    assert captured["extra"] == {
        "voice_call_id": "vcall_abc",
        "voice_turn_count": 12,
        "voice_duration_sec": 340,
    }


def test_blank_preview_is_refused(monkeypatch):
    monkeypatch.setattr(cleanup.db, "chat_get_strict", lambda *_args: None)
    assert not cleanup.persist_transcript_card(object(), "   ", "mid", "vcall_x")


def test_preview_is_bounded_and_keeps_both_ends():
    """The card is what the prompt tail carries for a whole call — it must stay
    small, and it must show how the call ENDED (decisions/todos land last), not
    only how it opened."""
    text = "开头很重要" + ("中" * 5000) + "结尾也很重要"
    preview = transcript_store.build_preview(text)
    assert len(preview) <= transcript_store.PREVIEW_MAX_CHARS + 4  # 加上 "\n…\n"
    assert preview.startswith("开头很重要")
    assert preview.endswith("结尾也很重要")
    assert "…" in preview


def test_short_transcript_is_previewed_verbatim():
    text = "- 对方: 在吗\n- 我: 在的"
    assert transcript_store.build_preview(text) == text


def test_rendered_transcript_never_leaks_the_literal_role():
    """`user:` in a transcript taught models to write 「用户」 into user-visible
    cards (usr_fee1, 2026-07-17). Rendering goes through the shared speaker
    labeller precisely so this cannot regress."""
    rendered = transcript_store.render_transcript([
        {"role": "user", "text": "明天提醒我"},
        {"role": "assistant", "text": "好"},
    ])
    assert "user:" not in rendered.lower()
    assert "assistant:" not in rendered.lower()
    assert "明天提醒我" in rendered


def test_transcript_uses_first_person_for_user_and_name_for_companion():
    """用户侧「我」(这份记录首先是给用户看的),伴侣侧用它自己的名字。"""
    rendered = transcript_store.render_transcript(
        [{"role": "user", "text": "今天封面定稿了"},
         {"role": "assistant", "text": "恭喜"}],
        ai_name="小满",
    )
    assert "- 我: 今天封面定稿了" in rendered
    assert "- 小满: 恭喜" in rendered


def test_companion_falls_back_to_TA_like_the_identity_card():
    """身份卡没名字时默认就是「TA」,这里保持一致,别自造第三种叫法。"""
    rendered = transcript_store.render_transcript(
        [{"role": "user", "text": "在吗"}, {"role": "assistant", "text": "在"}])
    assert "- 我: 在吗" in rendered
    assert "- TA: 在" in rendered


def test_capture_header_spells_out_who_is_who():
    """标签出现几十次、说明只有一次,所以说明必须毫不含糊地指出「我」不是模型
    自己 —— 否则模型会把用户做的事写成自己做的。"""
    header = transcript_store.capture_window_header(turn_count=24, ai_name="小满")
    assert "「我」是" in header and "不是你" in header
    assert "「小满」才是你自己" in header
    assert "这个人明确讲出来的每一件事" in header
    assert "TA" not in header
    # 「宁少勿多」是为闲聊窗口写的;不显式推翻它,一通电话只会留下一两张卡。
    assert "不适用于这里" in header


def test_both_lanes_share_one_header_implementation():
    """V2 与 resident 各写一份标签,正是当年 "user:" 事故漏掉托管路径的原因。"""
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    for path in ("backend/model_api_runtime/v2/worker.py",
                 "tools/chat_resident_consumer.py"):
        text = (repo / path).read_text()
        assert "capture_window_header(" in text, f"{path} 没有调共享抬头"
        assert "【语音通话逐字记录" not in text, (
            f"{path} 自己拼了抬头字面量 —— 必须调 transcript_store.capture_window_header"
        )
