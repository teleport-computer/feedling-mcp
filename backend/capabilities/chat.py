"""Chat-image capability — enclave-only (no backend route). Mirrors
io_cli.cmd_chat_image: GET {ENCLAVE}/v1/chat/history and pick message by id."""
from __future__ import annotations

import os

import httpx

from screen import screen_read_core   # reuse enclave_forward_headers
from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _safe_json(resp):
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return None


def image_read(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    message_id = params.get("message_id") or params.get("id")
    if not message_id:
        return err(errors.INVALID, "chat image needs message id", retryable=False)
    enclave = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave:
        return err(errors.UNAVAILABLE, "enclave url not configured", retryable=False)
    limit = int(params.get("limit") or 20)
    headers = screen_read_core.enclave_forward_headers(api_key=api_key, runtime_token=runtime_token)
    try:
        resp = httpx.get(f"{enclave}/v1/chat/history",
                         params={"since": 0, "limit": limit},
                         headers=headers, verify=False, timeout=30)
    except Exception as e:  # noqa: BLE001
        return err(errors.UPSTREAM, f"enclave chat history failed: {type(e).__name__}", retryable=True)
    if resp.status_code != 200:
        return err(errors.code_for_status(resp.status_code),
                   errors.message_for_body(_safe_json(resp), "chat history unavailable"),
                   retryable=errors.retryable_for_status(resp.status_code))
    body = _safe_json(resp) or {}
    messages = body.get("messages") if isinstance(body, dict) else None
    msg = next((m for m in (messages or []) if str(m.get("id")) == str(message_id)), None)
    if not msg:
        return err(errors.NOT_FOUND, f"message {message_id} not in recent history", retryable=False)
    image_b64 = msg.get("image_b64")
    if not image_b64:
        return err(errors.NOT_FOUND, "message has no image", retryable=False)
    return ok(data={"message_id": str(message_id),
                    "image_mime": msg.get("image_mime", "image/jpeg"),
                    "image_b64": image_b64})
