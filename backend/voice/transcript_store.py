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
    输入。两处都该看到 TA 给自己伴侣起的名字,而不是一串中性标签。名字取不到时
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


def render_transcript(turns: list[dict], *, user_name: str = "", ai_name: str = "") -> str:
    """Render client turns into the established capture-window line shape.

    Same ``- {label}: {text}`` form the V2 capture handler uses, via
    ``user_naming.transcript_speaker_label`` — the literal role string must
    never reach a model (the "user:" 标签教坏模型 incident). Server-side there
    is no plaintext name source, so labels fall back to 「对方」/「我」 exactly
    like V2's hosted path.
    """
    lines: list[str] = []
    for turn in turns or []:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        role = str(turn.get("role") or "").strip().lower()
        label = user_naming.transcript_speaker_label(
            role, user_name=user_name, ai_name=ai_name
        )
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
