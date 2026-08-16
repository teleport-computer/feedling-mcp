"""Permanent per-call transcript archive: render, persist, read back.

Rows here are written ONCE and never updated. That is not incidental: the TEE
replicator copies this table on a ``created_at`` cursor with no requeue, so an
in-place edit after the row has been copied would leave the TEE shadow
permanently stale. ``chat_message_id`` is therefore supplied at INSERT time
(the card id is deterministic from ``call_id``, so the caller knows it before
either row exists) rather than stamped afterwards.

The full call transcript is archived at hangup and kept forever. Three readers:

- **the user** — Settings lists their calls; the client fetches the envelope and
  decrypts locally (same ``feedling-box-seal-v1`` path as chat messages);
- **Capture** — renders the full text into the memory window in place of the
  small chat card that stands in for the call in the transcript stream;
- **the agent** — the ``voice_transcript_*`` tools pull an old call on demand.

The archive is deliberately NOT a chat row. A chat row is read by both the
prompt tail and the capture window, so a full transcript there would blow the
tail (``worker._bounded_compaction_prefix`` raises on an oversized single row).
The chat stream carries a bounded preview card; the bytes live here.

SQL is local to the voice package, matching ``voice/results.py``; unlike
``db.log_append`` these writes raise, because archiving happens BEFORE the
per-turn rows are deleted — a silent failure would destroy the call.
"""

from __future__ import annotations

import logging

from psycopg.types.json import Jsonb

import db
from core import enclave as core_enclave
from core import envelope as core_envelope
from identity import identity_core
from identity import user_naming

log = logging.getLogger("feedling.voice.transcript_store")

# The chat card's preview. Small on purpose: this string is what the prompt tail
# carries for the whole call, and the tail is budgeted in tokens. Head + tail so
# the card shows what the call was about AND how it ended (decisions/todos
# usually land last) — a plain head-truncation loses the useful half.
PREVIEW_MAX_CHARS = 500
_PREVIEW_HEAD_CHARS = 300
_PREVIEW_TAIL_CHARS = 160


def resolve_speaker_names(store, *, runtime_token: str = "") -> tuple[str, str]:
    """(user_name, ai_name) —— 归档要用真名,不是「对方 / 我」。

    通话逐字记录是**用户会亲眼读的东西**(设置页的通话记录),而且它是 Capture 的
    输入。两处都该看到这个人给自己伴侣起的名字,而不是一串中性标签。名字取不到时
    退回 transcript_speaker_label 的既有兜底(见那里两次事故的注释)。
    """
    user_name = ""
    ai_name = ""
    try:
        body, status = identity_core.get_identity(store)
        card = body.get("identity") if isinstance(body, dict) else None
        if status == 200 and isinstance(card, dict):
            ai_name = str(card.get("agent_name") or "").strip()[:80]
            user_name = str(card.get("user_preferred_name") or "").strip()[:80]
    except Exception as exc:  # noqa: BLE001 — 名字是锦上添花,拿不到就用兜底
        log.warning("[voice.transcript] identity unavailable: %s", str(exc)[:120])
    return user_name, ai_name


# 通话转写的说话人标签:用户侧固定「我」,伴侣侧用它自己的名字(取不到用「TA」,
# 与身份卡的默认值一致)。
#
# 为什么用户侧是第一人称:这份记录首先是**给用户看的**(设置页的通话记录),
# 用户读自己说过的话,理应是「我」。伴侣侧用名字,两者天然区分。
#
# 给模型看的那一份靠**抬头的对照说明**消歧义(见 capture_window_header)。这是
# 有意的取舍:标签会出现几十次而说明只有一次,所以说明必须紧贴转写、写得毫不
# 含糊,并且落卡之后要实测有没有张冠李戴 —— 别只靠推理。
#
# 不复用 transcript_speaker_label:那个函数是给「AI 回看聊天窗口」写的,第一人称
# 锚在 AI 身上(AI=「我」、用户=「对方」),与这里正好相反。
# 标签与 speaker_labels 的**唯一定义**已经移到纯模块 voice.message_filter:
# v2/context.py 只能依赖 stdlib + 那个纯模块(不能 import 本文件,本文件要 db),
# 而它也需要同一套标签去渲染通话记录块。这里只做转出,绝不再抄一份 ——
# 各写一份正是 2026-07-17 字面 `user:` 标签只修了一条 lane 的根因。
from voice.message_filter import (  # noqa: E402
    VOICE_UNKNOWN_AGENT_LABEL,
    VOICE_USER_LABEL,
    speaker_labels,
)

__all_speaker_exports__ = (
    VOICE_USER_LABEL,
    VOICE_UNKNOWN_AGENT_LABEL,
    speaker_labels,
)

def capture_window_header(*, turn_count=None, user_name: str = "",
                          ai_name: str = "") -> str:
    """Capture 窗口里那通电话的抬头。**V2 与 resident 共用这一份** —— 各写一份
    正是标签当年漏掉的原因。

    做两件事:
    1. 说清谁是谁(转写两侧都是真名,不带第一人称);
    2. 换尺子。capture 提示词里那句「宁少勿多、只留一到两件」是为闲聊窗口写的,
       对一通电话是错的量纲 —— 实测 12 件明确值得记的事只留下 2 件
       (2026-08-07 探针)。不改提示词本身(那会影响所有 capture),只在这里把
       「这段是什么」讲明白。
    """
    person, agent = speaker_labels(user_name, ai_name)
    turns_note = f"，共 {turn_count} 轮" if turn_count else ""
    return (
        f"【语音通话逐字记录{turns_note}】\n"
        f"（说话人对照：这份记录里「{person}」是**跟你说话的那个人**，不是你；"
        f"「{agent}」才是你自己。写卡时务必按这个对照归属，别把这个人做的事"
        f"写成你做的。）\n"
        "以下是一通完整电话的逐字记录，不是一段闲聊。\n"
        "电话的信息密度远高于日常对话：这个人会在一通里一口气讲很多件彼此独立的事"
        "——承诺与计划、家人、身体、工作进展、习惯的改变、在意的传统。\n"
        "**上面那条「宁少勿多、只留一到两件」是为闲聊窗口写的，不适用于这里。**"
        "请把这通电话当成一份清单逐件过：这个人明确讲出来的每一件事，只要三个月后"
        "还可能重要、或这个人会希望你记得，就各自成卡。同一件事的多个侧面仍然合成"
        "一张厚卡，但不同的事**不要**为了凑数量少而合并。"
    )


def render_transcript(turns: list[dict], *, user_name: str = "", ai_name: str = "") -> str:
    """把客户端 turns 渲染成 ``- {说话人}: {内容}`` 的逐行文本。

    两侧都用真名(取不到用「本人」/「伴侣」),不用第一人称 —— 见上面常量处的
    说明。原始 role 字面量永远不进文本(``user:`` 教坏模型的那个事故)。
    """
    lines: list[str] = []
    for turn in turns or []:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        role = str(turn.get("role") or "").strip().lower()
        # 再 sanitize 一次(纵深防御):把「用户」/「user」当名字传进来,也不能
        # 变成 "用户: …" 那一行。
        person, agent = speaker_labels(
            user_naming.sanitize_user_name(user_name), ai_name
        )
        label = person if role == "user" else agent
        lines.append(f"- {label}: {text}")
    return "\n".join(lines).strip()


def build_preview(text: str) -> str:
    text = str(text or "").strip()
    if len(text) <= PREVIEW_MAX_CHARS:
        return text
    head = text[:_PREVIEW_HEAD_CHARS].rstrip()
    tail = text[-_PREVIEW_TAIL_CHARS:].lstrip()
    return f"{head}\n…\n{tail}"


def persist(store, call_id: str, text: str, *, turn_count: int,
            duration_sec: int, chat_message_id: str = "") -> dict:
    """Archive one call. Idempotent on ``(user_id, call_id)``; raises on failure.

    Returns the row's metadata so the caller can stamp the chat card without a
    second read.
    """
    text = str(text or "").strip()
    if not text:
        raise ValueError("voice_transcript_empty")
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store, text.encode("utf-8"), item_id=f"vtx_{call_id}"
    )
    if envelope is None:
        raise RuntimeError(f"voice_transcript_envelope_failed:{err}")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO voice_transcripts "
            "(user_id, call_id, chat_message_id, turn_count, duration_sec, "
            " char_count, transcript_envelope) VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id, call_id) DO NOTHING",
            (
                store.user_id,
                str(call_id),
                str(chat_message_id or "")[:160],
                max(0, int(turn_count or 0)),
                max(0, int(duration_sec or 0)),
                len(text),
                Jsonb(envelope),
            ),
        )
    return {
        "call_id": str(call_id),
        "turn_count": max(0, int(turn_count or 0)),
        "duration_sec": max(0, int(duration_sec or 0)),
        "char_count": len(text),
    }


def exists(user_id: str, call_id: str) -> bool:
    """这通电话归档过没有 —— 幂等判据必须问归档表本身。

    不能拿"聊天里已有那张卡"代替:卡的 id 沿用了摘要时代的 uuid5 命名空间,
    所以一条**旧版本写下的 voice_call_summary 行**会让新版本误判"已处理",
    于是跳过归档、照常删掉逐轮行 —— 客户端明明又把全文送来了,却被丢掉,
    而且是永久的(逐轮行也没了)。这个窗口真实存在:旧版本落了摘要行但
    cleanup 502 / 响应丢失,用户升级后客户端重试。
    """
    try:
        with db.get_pool().connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM voice_transcripts WHERE user_id = %s AND call_id = %s",
                (str(user_id), str(call_id)),
            ).fetchone()
        return row is not None
    except Exception as exc:  # noqa: BLE001 — 未知即当作"没归档",宁可重写一次
        log.warning("[voice.transcript] exists check failed user=%s call=%s: %s",
                    str(user_id)[:12], str(call_id)[:24], str(exc)[:120])
        return False


def get_envelope(user_id: str, call_id: str) -> dict | None:
    """One archived call's envelope + metadata, or None."""
    try:
        with db.get_pool().connection() as conn:
            row = conn.execute(
                "SELECT call_id, chat_message_id, turn_count, duration_sec, "
                "char_count, created_at, transcript_envelope "
                "FROM voice_transcripts WHERE user_id = %s AND call_id = %s",
                (str(user_id), str(call_id)),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        log.error("[voice.transcript] get failed user=%s call=%s: %s",
                  str(user_id)[:12], str(call_id)[:24], str(exc)[:160])
        return None
    if row is None:
        return None
    return {
        "call_id": row[0],
        "chat_message_id": row[1],
        "turn_count": int(row[2] or 0),
        "duration_sec": int(row[3] or 0),
        "char_count": int(row[4] or 0),
        "created_at": row[5].isoformat() if row[5] else "",
        "transcript": row[6],
    }


def list_metadata(user_id: str, *, limit: int = 50) -> list[dict]:
    """Newest-first call list WITHOUT the envelope — Settings and the agent's
    ``voice_transcript_list`` both only need "which calls exist"."""
    limit = max(1, min(int(limit or 50), 200))
    try:
        with db.get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT call_id, chat_message_id, turn_count, duration_sec, "
                "char_count, created_at FROM voice_transcripts "
                "WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                (str(user_id), limit),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.error("[voice.transcript] list failed user=%s: %s",
                  str(user_id)[:12], str(exc)[:160])
        return []
    return [
        {
            "call_id": r[0],
            "chat_message_id": r[1],
            "turn_count": int(r[2] or 0),
            "duration_sec": int(r[3] or 0),
            "char_count": int(r[4] or 0),
            "created_at": r[5].isoformat() if r[5] else "",
        }
        for r in rows
    ]


def load_plaintext(user_id: str, call_id: str, *, runtime_token: str = "",
                   api_key: str | None = None) -> str:
    """Decrypt one archived call server-side, through the enclave.

    Raises on failure — every caller (Capture, the agent tools) must treat a
    missing transcript as "do not proceed with the preview instead". Silently
    falling back to the 500-character card would distil a whole call down to
    its first few lines and advance the capture cursor past it, losing the rest
    forever with nobody the wiser.
    """
    row = get_envelope(user_id, call_id)
    if not row or not isinstance(row.get("transcript"), dict):
        raise RuntimeError("voice_transcript_not_found")
    plaintext = core_enclave._decrypt_envelope_via_enclave(
        row["transcript"],
        api_key,
        purpose="voice_transcript_read",
        **({"runtime_token": runtime_token} if runtime_token else {}),
    )
    return plaintext.decode("utf-8")
