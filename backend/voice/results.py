"""Encrypted, short-lived handoff between the model worker and voice SSE."""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import db
from core import runtime_token


RESULT_TTL_SEC = 900.0


def secret() -> bytes:
    dedicated = (os.environ.get("FEEDLING_VOICE_TOKEN_SECRET") or "").strip()
    if dedicated:
        return dedicated.encode("utf-8")
    runtime = (os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET") or "").strip()
    if not runtime:
        raise RuntimeError("voice token secret not configured")
    return hmac.new(
        runtime.encode("utf-8"), b"feedling-voice-secret-v1", hashlib.sha256
    ).digest()


def mint_enclave_token(user_id: str) -> str:
    runtime_secret = (
        os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET") or ""
    ).strip().encode("utf-8")
    if not runtime_secret:
        raise RuntimeError("FEEDLING_RUNTIME_TOKEN_SECRET not set")
    return runtime_token.mint(
        runtime_secret,
        user_id=str(user_id),
        runtime_instance_id="voice-gateway",
        scope=["envelope_decrypt"],
        ttl=180.0,
    )


def _key(call_id: str, turn_id: str) -> bytes:
    label = f"voice-result-v1:{call_id}:{turn_id}".encode("utf-8")
    return hmac.new(secret(), label, hashlib.sha256).digest()


def _aad(user_id: str, call_id: str, turn_id: str) -> bytes:
    return f"{user_id}\n{call_id}\n{turn_id}".encode("utf-8")


def _stream_key(call_id: str, turn_id: str, segment: int) -> bytes:
    label = f"voice-stream-v1:{call_id}:{turn_id}:{segment}".encode("utf-8")
    return hmac.new(secret(), label, hashlib.sha256).digest()


def _stream_aad(user_id: str, call_id: str, turn_id: str, segment: int) -> bytes:
    return f"{user_id}\n{call_id}\n{turn_id}\n{segment}".encode("utf-8")


def store_reply(
    user_id: str,
    *,
    call_id: str,
    turn_id: str,
    message_id: str,
    text: str,
) -> bool:
    user_id = str(user_id).strip()
    call_id = str(call_id).strip()
    turn_id = str(turn_id).strip()
    message_id = str(message_id).strip()
    text = str(text).strip()
    if not user_id or not call_id or not turn_id or not text:
        return False
    if len(call_id) > 96 or len(turn_id) > 128 or len(text) > 12000:
        return False
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key(call_id, turn_id)).encrypt(
        nonce, text.encode("utf-8"), _aad(user_id, call_id, turn_id)
    )
    with db.get_pool().connection() as conn:
        with conn.transaction():
            conn.execute(
                "DELETE FROM voice_turn_results WHERE expires_at <= now()"
            )
            row = conn.execute(
                "INSERT INTO voice_turn_results "
                "(call_id,turn_id,user_id,message_id,nonce,ciphertext,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,to_timestamp(%s)) "
                "ON CONFLICT (call_id,turn_id) DO NOTHING RETURNING call_id",
                (
                    call_id,
                    turn_id,
                    user_id,
                    message_id,
                    nonce,
                    ciphertext,
                    time.time() + RESULT_TTL_SEC,
                ),
            ).fetchone()
    return row is not None


def load_reply(user_id: str, *, call_id: str, turn_id: str) -> dict | None:
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT message_id,nonce,ciphertext FROM voice_turn_results "
            "WHERE user_id=%s AND call_id=%s AND turn_id=%s "
            "AND expires_at > now()",
            (str(user_id), str(call_id), str(turn_id)),
        ).fetchone()
    if row is None:
        return None
    message_id, nonce, ciphertext = row
    plaintext = AESGCM(_key(call_id, turn_id)).decrypt(
        bytes(nonce),
        bytes(ciphertext),
        _aad(str(user_id), str(call_id), str(turn_id)),
    )
    return {"message_id": str(message_id or ""), "text": plaintext.decode("utf-8")}


def store_reply_for_parent(
    user_id: str,
    *,
    parent_message_id: str,
    message_id: str,
    text: str,
) -> bool:
    parent = db.chat_get_strict(str(user_id), str(parent_message_id)) or {}
    call_id = str(parent.get("voice_call_id") or "")
    turn_id = str(parent.get("voice_turn_id") or "")
    if not call_id or not turn_id:
        return False
    return store_reply(
        str(user_id),
        call_id=call_id,
        turn_id=turn_id,
        message_id=message_id,
        text=text,
    )


def store_stream_text(
    user_id: str,
    *,
    call_id: str,
    turn_id: str,
    segment: int,
    text: str,
    is_final: bool = False,
) -> bool:
    user_id = str(user_id).strip()
    call_id = str(call_id).strip()
    turn_id = str(turn_id).strip()
    text = str(text)
    try:
        segment = int(segment)
    except (TypeError, ValueError):
        return False
    if not user_id or not call_id or not turn_id or not text.strip():
        return False
    if (
        segment < 0
        or segment > 31
        or len(call_id) > 96
        or len(turn_id) > 128
        or len(text) > 12000
    ):
        return False
    nonce = os.urandom(12)
    ciphertext = AESGCM(_stream_key(call_id, turn_id, segment)).encrypt(
        nonce,
        text.encode("utf-8"),
        _stream_aad(user_id, call_id, turn_id, segment),
    )
    with db.get_pool().connection() as conn:
        with conn.transaction():
            conn.execute(
                "DELETE FROM voice_turn_streams WHERE expires_at <= now()"
            )
            row = conn.execute(
                "INSERT INTO voice_turn_streams "
                "(call_id,turn_id,segment,user_id,text_len,nonce,ciphertext,is_final,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s)) "
                "ON CONFLICT (call_id,turn_id,segment) DO UPDATE SET "
                "user_id=EXCLUDED.user_id,text_len=EXCLUDED.text_len,"
                "nonce=EXCLUDED.nonce,ciphertext=EXCLUDED.ciphertext,"
                "is_final=voice_turn_streams.is_final OR EXCLUDED.is_final,"
                "expires_at=EXCLUDED.expires_at "
                "WHERE voice_turn_streams.user_id=EXCLUDED.user_id "
                "AND (voice_turn_streams.text_len < EXCLUDED.text_len "
                "OR (voice_turn_streams.text_len = EXCLUDED.text_len "
                "AND NOT voice_turn_streams.is_final AND EXCLUDED.is_final)) "
                "RETURNING segment",
                (
                    call_id,
                    turn_id,
                    segment,
                    user_id,
                    len(text),
                    nonce,
                    ciphertext,
                    bool(is_final),
                    time.time() + RESULT_TTL_SEC,
                ),
            ).fetchone()
    return row is not None


def load_stream_texts(user_id: str, *, call_id: str, turn_id: str) -> list[dict]:
    user_id = str(user_id)
    call_id = str(call_id)
    turn_id = str(turn_id)
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT segment,nonce,ciphertext,is_final FROM voice_turn_streams "
            "WHERE user_id=%s AND call_id=%s AND turn_id=%s "
            "AND expires_at > now() ORDER BY segment ASC",
            (user_id, call_id, turn_id),
        ).fetchall()
    snapshots: list[dict] = []
    for segment, nonce, ciphertext, is_final in rows:
        plaintext = AESGCM(_stream_key(call_id, turn_id, int(segment))).decrypt(
            bytes(nonce),
            bytes(ciphertext),
            _stream_aad(user_id, call_id, turn_id, int(segment)),
        )
        snapshots.append({
            "segment": int(segment),
            "text": plaintext.decode("utf-8"),
            "is_final": bool(is_final),
        })
    return snapshots


def store_stream_text_for_parent(
    user_id: str,
    *,
    parent_message_id: str,
    segment: int,
    text: str,
    is_final: bool = False,
) -> bool:
    parent = db.chat_get_strict(str(user_id), str(parent_message_id)) or {}
    call_id = str(parent.get("voice_call_id") or "")
    turn_id = str(parent.get("voice_turn_id") or "")
    if not call_id or not turn_id:
        return False
    return store_stream_text(
        str(user_id),
        call_id=call_id,
        turn_id=turn_id,
        segment=segment,
        text=text,
        is_final=is_final,
    )
