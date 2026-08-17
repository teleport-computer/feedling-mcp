"""Photo capabilities — facade over backend/perception/perception_read_core.py."""
from __future__ import annotations

import base64
import json

from perception import perception_read_core
from screen import screen_read_core

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _norm(body, status, *, default_msg) -> CapabilityResult:
    if status == 200:
        data = body if isinstance(body, dict) else {"result": body}
        return ok(data=errors.cap_data(data))
    return err(errors.code_for_status(status),
               errors.message_for_body(body, default_msg),
               retryable=errors.retryable_for_status(status))


def recent(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    body, status = perception_read_core.photos_recent(store, params.get("limit"))
    return _norm(body, status, default_msg="photos unavailable")


def _proxy_json_body(response) -> dict | None:
    """Decode the enclave decrypt proxy without mistaking JSON for pixels."""
    if isinstance(response.json_body, dict):
        return response.json_body
    raw = response.raw_body
    if raw is None or "json" not in str(response.media_type or "").lower():
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        decoded = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def read(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    photo_id = params.get("photo_id") or params.get("id")
    if not photo_id:
        return err(errors.INVALID, "photo read needs id", retryable=False)
    body, status = perception_read_core.photo_content(store, photo_id)
    result = _norm(body, status, default_msg="photo unavailable")
    if not result.ok or not params.get("include_image"):
        return result
    frame_id = body.get("frame_id") if isinstance(body, dict) else None
    if not frame_id:
        return result
    img = screen_read_core.frame_decrypt(store, frame_id, include_image="true",
                                         api_key=api_key, runtime_token=runtime_token)
    proxy_body = _proxy_json_body(img)
    if img.status != 200:
        return err(
            errors.code_for_status(img.status),
            errors.message_for_body(proxy_body, "photo pixels unavailable"),
            retryable=errors.retryable_for_status(img.status),
        )

    if proxy_body is not None:
        image_b64 = str(proxy_body.get("image_b64") or "")
        if not image_b64:
            return err(errors.UNAVAILABLE, "photo pixels unavailable", retryable=False)
        result.data = {
            **result.data,
            "image_media_type": str(proxy_body.get("image_mime") or "image/jpeg"),
            "has_image": True,
            "image_b64": image_b64,
        }
        return result

    if "json" in str(img.media_type or "").lower():
        return err(errors.UPSTREAM, "photo decrypt response invalid", retryable=True)
    if img.raw_body is None:
        return err(errors.UNAVAILABLE, "photo pixels unavailable", retryable=False)
    result.data = {
        **result.data,
        "image_media_type": img.media_type,
        "has_image": True,
        "image_b64": base64.b64encode(img.raw_body).decode("ascii"),
    }
    return result
