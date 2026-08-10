"""Keep voice transport artifacts out of ordinary conversation context.

The transcript card is a UI/archive pointer. Capture deliberately expands it
from ``voice_transcripts``; normal chat replay and compaction must not mistake
the mixed-speaker preview for one assistant utterance.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Iterable


VOICE_TRANSCRIPT_SOURCE = "voice_call_transcript"

# 说话人标签只有这一份定义。transcript_store 从这里 import ——
# 各写一份正是 2026-07-17 字面 `user:` 标签只修了一条 lane 的根因。
VOICE_USER_LABEL = "我"
VOICE_UNKNOWN_AGENT_LABEL = "TA"

# 通话卡在普通对话上下文里的身份。
#
# 卡的原始 role 是 `openclaw` → 归一成 assistant,而正文是**双方混合**的预览
# (「我:… TA:…」)。原样 replay 等于让模型看到「我(助手)说了一段包含用户台词
# 的话」—— 与 2026-07-17 那次事故同族,它会学着写对话体、也会张冠李戴。
#
# 2026-08-07 的修法是把整张卡从尾巴/压缩/dream 里**删掉**,代价是挂断之后伴侣
# 在普通聊天里完全不知道刚才通过话:用户接着打字说「刚才电话里说的那个」,
# 模型没有任何上下文。信息形状不对就把信息本身消掉,是不能接受的。
#
# 现在改成**换身份**:卡仍然在上下文里,但带抬头、明确声明这不是它自己说的话,
# 并把说话人对照写清楚。
VOICE_CALL_RECORD_ROLE = "voice_call_record"
VOICE_CALL_RECORD_HEADER = (
    "UNTRUSTED VOICE CALL RECORD (archived transcript, not your own words):"
)


def speaker_labels(user_name: str = "", ai_name: str = "") -> tuple[str, str]:
    agent = " ".join(str(ai_name or "").split()) or VOICE_UNKNOWN_AGENT_LABEL
    return VOICE_USER_LABEL, agent


def call_record_block(preview: str, *, turn_count=None, duration_sec=None) -> str:
    """把通话卡的预览渲染成一段可以安全放进上下文的记录块。

    只依赖预览本身:预览是 `render_transcript` 产出的,两侧已经是真名
    (用户侧固定「我」,伴侣侧是它自己的名字),所以这里不需要再查名字。
    """
    body = str(preview or "").strip()
    if not body:
        return ""
    facts = []
    if isinstance(turn_count, int) and turn_count > 0:
        facts.append(f"共 {turn_count} 轮")
    if isinstance(duration_sec, int) and duration_sec > 0:
        minutes = max(1, round(duration_sec / 60))
        facts.append(f"约 {minutes} 分钟")
    note = f"（{'，'.join(facts)}）" if facts else ""
    return (
        f"{VOICE_CALL_RECORD_HEADER}\n"
        f"刚刚和用户通了一次电话{note}。以下是逐字记录片段。\n"
        f"说话人对照:「{VOICE_USER_LABEL}」是**跟你说话的那个人**,不是你;"
        "另一侧署名的是你自己。\n"
        "这段不是你说过的话,别把它当成自己的发言复述,也别学它的对话体格式。\n"
        "需要完整内容时用 voice_transcript_read 读取归档。\n\n"
        f"{body}"
    )

_VOICE_NOISE_MARKERS = frozenset({
    "backgroundnoise",
    "inaudible",
    "music",
    "silence",
    "无声",
    "杂音",
    "背景噪音",
    "背景杂音",
    "静音",
    "噪音",
})


def normalized_voice_marker(text: str) -> str:
    return "".join(
        character.casefold()
        for character in str(text or "").strip()
        if unicodedata.category(character)[0] not in {"P", "S", "Z"}
    )


def is_meaningful_voice_message(text: str) -> bool:
    marker = normalized_voice_marker(text)
    if not marker or marker in _VOICE_NOISE_MARKERS:
        return False
    return any(character.isalnum() for character in marker)


def _content_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and str(block.get("text") or "").strip()
        )
    return str(content or "")


def conversation_rows(rows: Iterable[dict]) -> list[dict]:
    """Return rows that are real conversational history.

    A punctuation/noise-only row is suppressed only when it carries
    ``voice_call_id``. A user who intentionally types ``...`` in text chat is
    therefore unchanged. For one voice call/logical turn, only the newest ASR
    revision is kept even if shadow metadata replication is still catching up.
    Replies explicitly linked to a suppressed voice row are removed with their
    parent.
    """
    materialized = [row for row in rows if isinstance(row, dict)]
    latest_voice_user_id_by_turn: dict[tuple[str, str], str] = {}
    for row in materialized:
        role = str(row.get("role") or "").strip().lower()
        call_id = str(row.get("voice_call_id") or "").strip()
        logical_turn_id = str(
            row.get("voice_logical_turn_id") or row.get("voice_turn_id") or ""
        ).strip()
        row_id = str(row.get("id") or "").strip()
        if role in {"user", "human"} and call_id and logical_turn_id and row_id:
            latest_voice_user_id_by_turn[(call_id, logical_turn_id)] = row_id

    rejected_voice_user_ids = {
        str(row.get("id") or "").strip()
        for row in materialized
        if str(row.get("role") or "").strip().lower() in {"user", "human"}
        and str(row.get("voice_call_id") or "").strip()
        and (
            str(row.get("voice_turn_status") or "").strip() == "superseded"
            or not is_meaningful_voice_message(_content_text(row.get("content")))
            or latest_voice_user_id_by_turn.get((
                str(row.get("voice_call_id") or "").strip(),
                str(
                    row.get("voice_logical_turn_id")
                    or row.get("voice_turn_id")
                    or ""
                ).strip(),
            ))
            != str(row.get("id") or "").strip()
        )
        and str(row.get("id") or "").strip()
    }

    kept: list[dict] = []
    for row in materialized:
        if str(row.get("source") or "").strip() == VOICE_TRANSCRIPT_SOURCE:
            # 卡不再被丢弃 —— 换身份保留。role 换成 VOICE_CALL_RECORD_ROLE
            # (不再冒充 assistant),正文换成自带抬头与说话人对照的记录块。
            # 抬头写进 content 而不是靠 role:调用方对 role 的处理各不相同,
            # 但六个调用点都会渲染 content,信息放在那里才不会漏。
            block = call_record_block(
                _content_text(row.get("content")),
                turn_count=row.get("voice_turn_count"),
                duration_sec=row.get("voice_duration_sec"),
            )
            if not block:
                continue
            record = dict(row)
            record["role"] = VOICE_CALL_RECORD_ROLE
            record["content"] = block
            kept.append(record)
            continue
        row_id = str(row.get("id") or "").strip()
        if row_id in rejected_voice_user_ids:
            continue
        role = str(row.get("role") or "").strip().lower()
        parent_id = str(row.get("reply_to_message_id") or "").strip()
        if role in {"assistant", "agent", "openclaw", "model"} and (
            parent_id in rejected_voice_user_ids
        ):
            continue
        kept.append(row)
    return kept
