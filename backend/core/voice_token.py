"""Short-lived tokens accepted only by the public voice LLM gateway."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class VoiceTokenError(Exception):
    pass


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(secret: bytes, payload: str) -> str:
    return hmac.new(
        secret,
        ("feedling-voice-v1:" + payload).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def mint(
    secret: bytes,
    *,
    user_id: str,
    call_id: str,
    now: float | None = None,
    ttl: float = 600.0,
) -> tuple[str, float]:
    issued = time.time() if now is None else float(now)
    expires = issued + float(ttl)
    claims = {
        "aud": "io_voice_llm",
        "user_id": str(user_id),
        "call_id": str(call_id),
        "iat": issued,
        "exp": expires,
        "v": 1,
    }
    payload = _b64e(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{_signature(secret, payload)}", expires


def verify(secret: bytes, token: str, *, now: float | None = None) -> dict:
    try:
        payload, signature = str(token).split(".", 1)
    except ValueError as exc:
        raise VoiceTokenError("malformed_token") from exc
    if not hmac.compare_digest(_signature(secret, payload), signature):
        raise VoiceTokenError("bad_signature")
    try:
        claims = json.loads(_b64d(payload))
    except Exception as exc:  # noqa: BLE001
        raise VoiceTokenError("bad_payload") from exc
    if claims.get("aud") != "io_voice_llm" or claims.get("v") != 1:
        raise VoiceTokenError("bad_audience")
    clock = time.time() if now is None else float(now)
    if clock >= float(claims.get("exp") or 0):
        raise VoiceTokenError("token_expired")
    if not str(claims.get("user_id") or "") or not str(claims.get("call_id") or ""):
        raise VoiceTokenError("missing_identity")
    return claims
